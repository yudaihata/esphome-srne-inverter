#!/usr/bin/env python3
"""
Verify whether suspicious values are due to physical-layer noise by direct Modbus polling.

This script repeatedly reads specified Modbus holding registers using minimalmodbus
and analyzes outliers vs. plausible ranges. If outliers occur even in this
single-threaded, spaced polling, the cause is likely physical (line noise,
auto-DE timing, termination/bias). If values are clean here but noisy via ESPHome,
then scheduling/driver on the device is the likely cause.

Usage examples:
  python3 tools/verify_physical_noise.py --port /dev/ttyUSB0 --slave 1 --baud 9600 \
      --addresses 533 534 558 --samples 300 --interval-ms 300

Defaults (if wizard has been run): port/slave/baud will be loaded from
tools/build/wizard_state.json unless explicitly specified.

Outputs: writes CSV to tools/build/noise_probe.csv and prints a summary.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

BUILD_DIR = Path("tools/build")
WIZ_STATE_PATH = BUILD_DIR / "wizard_state.json"


def try_import_minimalmodbus():
    try:
        import minimalmodbus  # type: ignore
        return minimalmodbus
    except Exception as e:
        print("[ERROR] minimalmodbus が見つかりません。pip install minimalmodbus を実行してください。", file=sys.stderr)
        print(str(e), file=sys.stderr)
        sys.exit(1)


def load_prev_state() -> Dict[str, Any]:
    if not WIZ_STATE_PATH.exists():
        return {}
    try:
        return json.loads(WIZ_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def plausible_range_for_address(addr: int) -> Tuple[float, float]:
    """Return a conservative plausible range for known addresses.

    These ranges are used only for quick outlier tagging, not for validation.
    - 533 (Grid Frequency, scale 0.01): 45..65 Hz
    - 534 (Inverter Phase A Voltage, scale 0.1): 0..300 V
    - 558 (Inverter Phase B Current, scale 0.1): 0..200 A
    Others: no constraint (return -inf..+inf)
    """
    if addr == 533:
        return (45.0, 65.0)
    if addr == 534:
        return (0.0, 300.0)
    if addr == 558:
        return (0.0, 200.0)
    return (-1e300, 1e300)


def scale_for_address(addr: int) -> float:
    # Known scales from catalog v1.96 (fallback: 1.0)
    return {533: 0.01, 534: 0.1, 558: 0.1}.get(addr, 1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe Modbus registers to detect physical-layer noise/outliers")
    ap.add_argument("--port", help="Serial port (e.g. /dev/ttyUSB0, /dev/cu.usbserial)")
    ap.add_argument("--slave", type=int, help="Modbus slave id")
    ap.add_argument("--baud", type=int, help="Baudrate", choices=[2400, 4800, 9600, 19200, 38400, 57600, 115200])
    ap.add_argument("--addresses", type=int, nargs="+", default=[533, 534, 558], help="List of holding register addresses to probe")
    ap.add_argument("--samples", type=int, default=300, help="Number of total samples across addresses")
    ap.add_argument("--interval-ms", type=int, default=300, help="Delay between individual reads (ms)")
    ap.add_argument("--triples", action="store_true", help="For each sample, read the same address 3x back-to-back to check consistency")
    args = ap.parse_args()

    prev = load_prev_state()
    port = args.port or str(prev.get("port") or "")
    slave = int(args.slave if args.slave is not None else prev.get("slave_id", 1))
    baud = int(args.baud if args.baud is not None else prev.get("baudrate", 9600))

    if not port:
        print("[ERROR] シリアルポートが未指定です。--port を指定するか、ウィザードを一度実行して wizard_state.json を生成してください。", file=sys.stderr)
        sys.exit(2)

    mm = try_import_minimalmodbus()
    inst = mm.Instrument(port, slave)
    inst.serial.baudrate = baud
    inst.serial.bytesize = 8
    inst.serial.parity = mm.serial.PARITY_NONE
    inst.serial.stopbits = 1
    inst.serial.timeout = 0.8
    inst.mode = mm.MODE_RTU

    # Prepare randomized schedule across addresses
    schedule: List[int] = []
    while len(schedule) < args.samples:
        schedule += random.sample(args.addresses, k=len(args.addresses))
    schedule = schedule[: args.samples]

    out_csv = BUILD_DIR / "noise_probe.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    f = out_csv.open("w", newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(["ts", "addr", "raw_u16", "scaled", "ok", "err", "r2", "r3"])

    total = 0
    errs = 0
    outliers = 0
    inconsistent = 0
    t0 = time.time()
    try:
        for addr in schedule:
            time.sleep(max(0, args.interval_ms) / 1000.0)
            total += 1
            raw = None
            err = ""
            r2 = r3 = None
            try:
                v1 = inst.read_register(addr, 0, functioncode=3, signed=False)
                raw = int(v1) & 0xFFFF
                if args.triples:
                    v2 = inst.read_register(addr, 0, functioncode=3, signed=False)
                    v3 = inst.read_register(addr, 0, functioncode=3, signed=False)
                    r2 = int(v2) & 0xFFFF
                    r3 = int(v3) & 0xFFFF
            except Exception as e:
                errs += 1
                err = str(e)

            ts = time.time() - t0
            if raw is None:
                w.writerow([f"{ts:.3f}", addr, "", "", "", err, r2, r3])
                continue

            scale = scale_for_address(addr)
            scaled = raw * scale
            lo, hi = plausible_range_for_address(addr)
            ok = (lo <= scaled <= hi)
            if not ok:
                outliers += 1

            # Consistency check for triples: mark as inconsistent if any mismatch
            if r2 is not None:
                if (r2 != raw) or (r3 is not None and r3 != raw):
                    inconsistent += 1

            w.writerow([f"{ts:.3f}", addr, raw, f"{scaled:.3f}", int(ok), err, r2, r3])
    finally:
        f.close()

    print("=== Probe Summary ===")
    print(f"Port={port} Slave={slave} Baud={baud}")
    print(f"Addresses={args.addresses} Samples={total} Interval={args.interval_ms}ms Triples={args.triples}")
    print(f"Errors={errs}  Outliers={outliers}  InconsistentTriples={inconsistent}")
    print(f"CSV: {out_csv}")

    if errs > 0:
        print("- 読取エラーが発生しています。配線・終端・バイアス・速度の見直しを検討してください。")
    if outliers > 0:
        print("- 範囲外値（外れ値）が観測されました。物理層ノイズや自動DEタイミングの影響が疑われます。")
    if args.triples and inconsistent > 0:
        print("- 同一レジスタの3連続読みで不一致が見られました。通信層に起因する不安定の可能性が高いです。")


if __name__ == "__main__":
    main()
