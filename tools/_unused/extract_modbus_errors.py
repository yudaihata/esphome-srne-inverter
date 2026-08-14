#!/usr/bin/env python3
"""
ESPHomeログからModbus関連のエラー（特に "not enough data for value"）を抽出し、
直前の読取リクエスト（start/count 等）と突き合わせた要約を出力します。

使い方:
  python tools/extract_modbus_errors.py [--input <logfile>] [--output <outfile>]

既定:
  --input  = esphome_run_fault_log.txt （リポジトリ直下）
  --output = errorlog.txt （リポジトリ直下）

注意:
  - リクエスト行は logger を DEBUG/VERY_VERBOSE にすると詳細が出やすいです。
  - いくつかの既知パターン（fc=0x03, start=..., count=... 等）を正規表現で検出します。
  - 最後に見つかったリクエストとエラーを近似対応付けします（厳密なスレッド/ID対応ではありません）。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from collections import deque


REQ_PATTERNS = [
    # fc=0x03 start=NNN count=MM
    re.compile(r"(?P<ts>^\[\d{2}:\d{2}:\d{2}\.\d{3}\])?.*fc\s*=\s*0x0?3.*?(start|address)\s*[:=]\s*(?P<start>\d+).*?(count|quantity|register_count)\s*[:=]\s*(?P<count>\d+)", re.IGNORECASE),
    # Read Holding Registers start/address, count/quantity
    re.compile(r"(?P<ts>^\[\d{2}:\d{2}:\d{2}\.\d{3}\])?.*Read\s+Holding\s+Registers.*?(start|address)\s*[:=]\s*(?P<start>\d+).*?(count|quantity)\s*[:=]\s*(?P<count>\d+)", re.IGNORECASE),
]

ERR_PATTERN = re.compile(r"(?P<ts>^\[\d{2}:\d{2}:\d{2}\.\d{3}\]).*not enough data for value", re.IGNORECASE)


def parse_log(lines: List[str]) -> Tuple[List[str], int]:
    """Parse lines and return report lines and error count."""
    last_requests: Deque[Dict[str, str]] = deque(maxlen=50)
    report: List[str] = []
    err_count = 0

    # Keep a small context window to include around errors
    context_window: Deque[str] = deque(maxlen=8)

    for i, line in enumerate(lines):
        s = line.rstrip("\n")
        context_window.append(s)

        # Detect request lines
        matched_req = None
        for rp in REQ_PATTERNS:
            m = rp.search(s)
            if m:
                matched_req = {
                    "ts": (m.group("ts") or "").strip("[] ") if m.groupdict().get("ts") else "",
                    "start": m.group("start"),
                    "count": m.group("count"),
                    "line": s,
                    "idx": str(i),
                }
                break
        if matched_req:
            last_requests.append(matched_req)
            continue

        # Detect error lines
        me = ERR_PATTERN.search(s)
        if me:
            err_count += 1
            ts = (me.group("ts") or "").strip("[] ") if me.groupdict().get("ts") else ""
            report.append("==== ERROR {} ====".format(err_count))
            report.append(f"time: {ts}  line_no: {i}")
            report.append(s)
            # Attach closest last request (most recent)
            if last_requests:
                lr = last_requests[-1]
                report.append("-- closest request --")
                report.append(f"time: {lr.get('ts','')}  line_no: {lr.get('idx','')}")
                report.append(f"start={lr.get('start')}  count={lr.get('count')}")
                report.append(lr.get("line", ""))
            # Add a few lines of previous context
            if context_window:
                report.append("-- recent context --")
                for ctx in list(context_window)[-5:]:
                    report.append(ctx)
            report.append("")

    # Summary
    report.append("==== SUMMARY ====")
    report.append(f"error_count: {err_count}")
    report.append(f"last_request_seen: {last_requests[-1]['line'] if last_requests else 'N/A'}")

    return report, err_count


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract Modbus errors and map to recent requests from ESPHome logs")
    ap.add_argument("--input", type=Path, default=Path("esphome_run_fault_log.txt"))
    ap.add_argument("--output", type=Path, default=Path("errorlog.txt"))
    args = ap.parse_args()

    if not args.input.exists():
        raise SystemExit(f"Input log not found: {args.input}")

    lines = args.input.read_text(encoding="utf-8", errors="ignore").splitlines()
    report, cnt = parse_log(lines)
    args.output.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} (errors={cnt})")


if __name__ == "__main__":
    main()
