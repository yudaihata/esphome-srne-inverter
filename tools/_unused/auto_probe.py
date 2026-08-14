#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ESPHOME_DIR = ROOT / "esphome"
CORE_YAML = ESPHOME_DIR / "srne" / "core.yaml"
SRNE_YAML = ESPHOME_DIR / "srne_inverter.yaml"
INTERVALS_USER = ESPHOME_DIR / "srne" / "intervals_user.yaml"
BUILD_DIR = ROOT / "tools" / "build"


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> int:
    print("$", " ".join(cmd))
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, timeout=timeout)
        return p.returncode
    except subprocess.TimeoutExpired:
        print("[timeout]", file=sys.stderr)
        return 124


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def write_text(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")


def backup_dir(dst_root: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = dst_root / f"auto_probe_{ts}"
    dst.mkdir(parents=True, exist_ok=True)
    return dst


def set_logger_verbose(core_yaml: Path, enable: bool) -> None:
    s = read_text(core_yaml)
    # ensure logger section exists
    if "\nlogger:" not in s:
        s += "\nlogger:\n  level: INFO\n"
    if enable:
        s = re.sub(r"logger:\s*\n(?:.*\n)*?level:\s*\w+",
                   "logger:\n  level: VERY_VERBOSE", s, count=1)
        # add modbus/uart logs mapping if absent
        if "modbus: VERY_VERBOSE" not in s:
            s = s.replace(
                "level: VERY_VERBOSE",
                "level: VERY_VERBOSE\n  logs:\n    modbus: VERY_VERBOSE\n    modbus_controller: VERY_VERBOSE\n    uart: VERY_VERBOSE",
            )
    else:
        s = re.sub(r"logger:\s*\n(?:.*\n)*?level:\s*\w+",
                   "logger:\n  level: INFO", s, count=1)
        s = re.sub(r"\n\s*logs:\s*\n\s*modbus:.*?(\n\s*modbus_controller:.*)?", "", s)
    write_text(core_yaml, s)


def tweak_throttle_and_wait(core_yaml: Path, throttle_ms: int | None, send_wait_ms: int | None) -> None:
    s = read_text(core_yaml)
    if throttle_ms is not None:
        s = re.sub(r"command_throttle:\s*\d+ms", f"command_throttle: {throttle_ms}ms", s)
    if send_wait_ms is not None:
        if "send_wait_time:" in s:
            s = re.sub(r"send_wait_time:\s*\d+ms", f"send_wait_time: {send_wait_ms}ms", s)
        else:
            s = s.replace("modbus:\n  id: modbus1\n  uart_id: uart_bus",
                          f"modbus:\n  id: modbus1\n  uart_id: uart_bus\n  send_wait_time: {send_wait_ms}ms")
    write_text(core_yaml, s)


def set_p02_intervals(intervals_user: Path, fast_blocks: list[int], slow_sec: int) -> None:
    s = read_text(intervals_user)
    # set P02 blkN to slow except fast_blocks
    for bi in range(0, 10):
        pat = rf"(p02_blk{bi}_interval_s:\s*')\d+(')"
        new = f"\\g<1>{5 if bi in fast_blocks else slow_sec}\\g<2>"
        s2, n = re.subn(pat, new, s)
        if n:
            s = s2
    write_text(intervals_user, s)


def regenerate_with_chunk(max_chunk: int) -> None:
    # Rebuild ranges with given chunk and regenerate YAML in strict mode
    ranges_tmp = BUILD_DIR / f"device_specific_ranges_chunk{max_chunk}.json"
    run([sys.executable, str(ROOT / "tools" / "range_builder.py"), "--input", str(BUILD_DIR / "implemented_registers.json"), "--output", str(ranges_tmp), "--max-chunk", str(max_chunk)])
    run([sys.executable, str(ROOT / "tools" / "yaml_generator.py"), "--implemented", str(BUILD_DIR / "implemented_registers.json"), "--ranges", str(ranges_tmp), "--outdir", str(ESPHOME_DIR), "--split-mode", "strict", "--custom-overwrite"])


def esphome_upload_and_logs(yaml: Path, device: str, duration_sec: int, out_log: Path) -> int:
    # Build and upload via OTA
    rc = run(["esphome", "upload", str(yaml), "--device", device])
    if rc != 0:
        return rc
    # Collect logs for duration without relying on system 'timeout'
    with out_log.open("wb") as f:
        proc = subprocess.Popen(["esphome", "logs", str(yaml), "--device", device], stdout=f, stderr=subprocess.STDOUT)
        try:
            time.sleep(max(5, duration_sec))
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except KeyboardInterrupt:
            try:
                proc.terminate()
            except Exception:
                pass
    return 0


def analyze_log(logfile: Path, out_report: Path) -> None:
    run([sys.executable, str(ROOT / "tools" / "analyze_modbus_cause.py"), "--input", str(logfile), "--output", str(out_report)])


def main() -> None:
    ap = argparse.ArgumentParser(description="Auto probe ESPHome Modbus stability causes")
    ap.add_argument("--device", required=True, help="ESPHome device host/IP for OTA")
    ap.add_argument("--duration", type=int, default=180, help="Log capture duration per scenario (seconds)")
    ap.add_argument("--scenarios", type=str, default="A,B,C", help="Comma list of scenarios: A=lowload, B=waits, C=chunk16, P02S=per-block scan")
    args = ap.parse_args()

    backup = backup_dir(ROOT / "backups")
    print("Backup dir:", backup)
    # Backup files we will modify
    shutil.copy2(CORE_YAML, backup / "core.yaml.bak")
    shutil.copy2(INTERVALS_USER, backup / "intervals_user.yaml.bak")

    try:
        # Always enable VERY_VERBOSE during probing
        set_logger_verbose(CORE_YAML, True)
        scenarios = [s.strip().upper() for s in args.scenarios.split(',') if s.strip()]
        results = []
        for sc in scenarios:
            print(f"\n=== Scenario {sc} ===")
            # restore throttle/wait defaults baseline for each scenario
            tweak_throttle_and_wait(CORE_YAML, throttle_ms=120, send_wait_ms=80)
            # restore user intervals backup as base
            shutil.copy2(backup / "intervals_user.yaml.bak", INTERVALS_USER)
            # restore YAML structure (skip regeneration unless scenario C)
            if sc == 'A':
                # Low-load: keep P02 blk0/1/4/5 at 5s, others 60s
                set_p02_intervals(INTERVALS_USER, fast_blocks=[0, 1, 4, 5], slow_sec=60)
            elif sc == 'B':
                # Increase waits
                tweak_throttle_and_wait(CORE_YAML, throttle_ms=200, send_wait_ms=150)
            elif sc == 'C':
                # Re-generate with max_chunk=16
                regenerate_with_chunk(16)
            elif sc in ('P02S','P02SCAN'):
                # Per-block scan for P02: each block fast 5s, others 600s
                # Determine number of P02 blocks by counting update_interval substitutions present
                iu = read_text(INTERVALS_USER)
                import re
                blk_indices = sorted({int(m.group(1)) for m in re.finditer(r"p02_blk(\d+)_interval_s:", iu)})
                scan_results = []
                for bi in blk_indices:
                    print(f"-- P02 block {bi} scan --")
                    # reset to backup then set only this block fast
                    shutil.copy2(backup / "intervals_user.yaml.bak", INTERVALS_USER)
                    set_p02_intervals(INTERVALS_USER, fast_blocks=[bi], slow_sec=600)
                    logf = backup / f"logs_p02_b{bi}.txt"
                    repf = backup / f"report_p02_b{bi}.txt"
                    rc = esphome_upload_and_logs(SRNE_YAML, args.device, args.duration, logf)
                    if rc != 0:
                        print(f"P02 block {bi}: upload/log failed rc={rc}")
                        continue
                    # Simple error count
                    txt = read_text(logf)
                    err_cnt = len([1 for ln in txt.splitlines() if 'not enough data for value' in ln])
                    # Also run analyzer for completeness
                    analyze_log(logf, repf)
                    scan_results.append((bi, err_cnt, logf, repf))
                # Print summary ranking
                scan_results.sort(key=lambda x: x[1], reverse=True)
                print("\nP02 per-block scan summary (block, errors):")
                for bi, ec, lf, rf in scan_results:
                    print(f"  blk{bi}: {ec}  log={lf.name} report={rf.name}")
                # Append to overall results and continue next scenario
                results.extend([(f"P02_b{bi}", lf, rf) for bi, ec, lf, rf in scan_results])
                continue
            else:
                print("Unknown scenario, skipping")
                continue
            # Upload and capture logs
            logf = backup / f"logs_{sc.lower()}.txt"
            repf = backup / f"report_{sc.lower()}.txt"
            rc = esphome_upload_and_logs(SRNE_YAML, args.device, args.duration, logf)
            if rc != 0:
                print(f"Scenario {sc}: upload/log failed rc={rc}")
                continue
            analyze_log(logf, repf)
            results.append((sc, logf, repf))

        print("\n=== Summary ===")
        for sc, logf, repf in results:
            print(f"Scenario {sc}: log={logf} report={repf}")
            try:
                print((repf).read_text(encoding='utf-8'))
            except Exception:
                pass
    finally:
        # Restore logger level to INFO by default
        try:
            set_logger_verbose(CORE_YAML, False)
        except Exception:
            pass


if __name__ == "__main__":
    main()
