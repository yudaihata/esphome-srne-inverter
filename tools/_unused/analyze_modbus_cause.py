#!/usr/bin/env python3
"""
Analyze ESPHome VERY_VERBOSE logs to distinguish likely root cause of
"not enough data for value" errors between:
  - Overload (too many concurrent/rapid requests)
  - Mid-frame truncation (responses shorter than expected, often with large counts)

Usage:
  python tools/analyze_modbus_cause.py --input <logfile> [--output <report.txt>]

What it does:
  - Parses requests (start/count) from fc=0x03 lines or TX hex frames
  - Parses RX hex frames and measures actual response byte length
  - Associates errors with the most recent request
  - Summarizes errors by requested count buckets (1-8 / 9-16 / 17-32)
  - Estimates request rate and correlates error rate with request density
  - Emits a heuristic classification
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from collections import deque, defaultdict


TS = re.compile(r"^\[(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})\]")
REQ_TXT = re.compile(
    r"fc\s*=\s*0x0?3.*?(start|address)\s*[:=]\s*(?P<start>\d+).*?(count|quantity|register_count)\s*[:=]\s*(?P<count>\d+)",
    re.IGNORECASE,
)
TX_HEX = re.compile(r"TX:\s*((?:[0-9A-Fa-f]{2}\s*)+)")
RX_HEX = re.compile(r"RX:\s*((?:[0-9A-Fa-f]{2}\s*)+)")
ERR_NE = re.compile(r"not enough data for value", re.IGNORECASE)


def _ts_to_sec(line: str) -> Optional[float]:
    m = TS.match(line)
    if not m:
        return None
    h, m_, s, ms = map(int, (m.group('h'), m.group('m'), m.group('s'), m.group('ms')))
    return ((h*60 + m_) * 60 + s) + ms/1000.0


def _hex_bytes(blob: str) -> List[int]:
    parts = blob.strip().split()
    out = []
    for p in parts:
        try:
            out.append(int(p, 16))
        except Exception:
            pass
    return out


def parse_log(lines: List[str]) -> Dict[str, any]:
    # Rolling context of last request
    last_req: Dict[str, any] = {}
    # Counters
    errors: List[Dict[str, any]] = []
    reqs: List[Dict[str, any]] = []
    rxs: List[Dict[str, any]] = []

    # For rate estimation
    per_sec: Dict[int, int] = defaultdict(int)

    for line in lines:
        t = _ts_to_sec(line)
        # Detect textual request
        m = REQ_TXT.search(line)
        if m:
            start = int(m.group('start'))
            count = int(m.group('count'))
            last_req = {"t": t, "src": "TXT", "start": start, "count": count, "line": line.rstrip()}
            reqs.append(last_req.copy())
            if t is not None:
                per_sec[int(t)] += 1
            continue
        # TX hex frame
        m = TX_HEX.search(line)
        if m:
            by = _hex_bytes(m.group(1))
            if len(by) >= 6:
                fc = by[1]
                if fc == 0x03:
                    start = (by[2] << 8) | by[3]
                    count = (by[4] << 8) | by[5]
                    last_req = {"t": t, "src": "TX", "start": start, "count": count, "line": line.rstrip()}
                    reqs.append(last_req.copy())
                    if t is not None:
                        per_sec[int(t)] += 1
            continue
        # RX hex frame
        m = RX_HEX.search(line)
        if m:
            by = _hex_bytes(m.group(1))
            rxs.append({"t": t, "len": len(by), "line": line.rstrip(), "req": last_req.copy() if last_req else None})
            continue
        # Error lines
        if ERR_NE.search(line):
            rec = {"t": t, "err": "not_enough_data", "req": last_req.copy() if last_req else None, "line": line.rstrip()}
            errors.append(rec)

    # Post-process: classify errors by req count bucket and RX shortness
    buckets = {"1-8": 0, "9-16": 0, "17-32": 0, ">32": 0, "unknown": 0}
    short_resp = 0
    total_err = len(errors)
    # build mapping from req time to nearest rx
    # naive: find nearest rx after req within 0.5s
    rx_by_req_idx: Dict[int, Dict[str, any]] = {}
    for i, e in enumerate(errors):
        rq = e.get("req") or {}
        cnt = rq.get("count")
        if isinstance(cnt, int):
            if cnt <= 8:
                buckets["1-8"] += 1
            elif cnt <= 16:
                buckets["9-16"] += 1
            elif cnt <= 32:
                buckets["17-32"] += 1
            else:
                buckets[">32"] += 1
        else:
            buckets["unknown"] += 1
        # expected RX len for 0x03 (address, function, bytecount, data(2*cnt), CRC2) -> 3+2*cnt+2
        rt = rq.get("t")
        if isinstance(cnt, int) and rt is not None:
            exp_len = 3 + 2*cnt + 2
            # find first rx after req within 0.5s
            rx = next((r for r in rxs if r.get("t") is not None and r.get("t") >= rt and r.get("t") - rt <= 0.5), None)
            if rx and rx.get("len", 0) < exp_len:
                short_resp += 1

    # Rate correlation
    # compute 1s bins: requests per second
    if per_sec:
        max_rps = max(per_sec.values())
        avg_rps = sum(per_sec.values()) / max(1, len(per_sec))
    else:
        max_rps = 0
        avg_rps = 0.0

    return {
        "errors": errors,
        "reqs": reqs,
        "rxs": rxs,
        "buckets": buckets,
        "short_resp": short_resp,
        "total_err": total_err,
        "per_sec": dict(per_sec),
        "max_rps": max_rps,
        "avg_rps": avg_rps,
    }


def classify(summary: Dict[str, any]) -> str:
    total = summary.get("total_err", 0)
    short = summary.get("short_resp", 0)
    b = summary.get("buckets", {})
    max_rps = summary.get("max_rps", 0)
    # Heuristics:
    # - Many errors with large count (>=17) and many short responses -> truncation-likely
    # - Errors concentrated at high RPS (e.g., > 10 rps) with mixed counts -> overload-likely
    large_cnt_err = b.get("17-32", 0) + b.get(">32", 0)
    if total > 0:
        frac_short = short / total
        frac_large = large_cnt_err / total
    else:
        frac_short = 0.0
        frac_large = 0.0
    if frac_short >= 0.4 and frac_large >= 0.3:
        return "truncation_likely"
    if max_rps >= 10 and frac_large < 0.3:
        return "overload_likely"
    if frac_short >= 0.5:
        return "truncation_possible"
    if max_rps >= 10:
        return "overload_possible"
    return "inconclusive"


def fmt_report(summary: Dict[str, any]) -> List[str]:
    out: List[str] = []
    out.append("==== Modbus Error Analysis ====")
    out.append(f"total_errors: {summary.get('total_err',0)}")
    b = summary.get('buckets', {})
    out.append(f"by_count_bucket: {b}")
    out.append(f"short_responses (rx<len_expected): {summary.get('short_resp',0)}")
    out.append(f"req_rate: max_rps={summary.get('max_rps',0)} avg_rps={summary.get('avg_rps',0):.2f}")
    out.append(f"classification: {classify(summary)}")
    out.append("")
    # Example few errors
    for i, e in enumerate(summary.get('errors', [])[:10], 1):
        rq = e.get('req') or {}
        out.append(f"ERR#{i}: t={e.get('t')} start={rq.get('start')} count={rq.get('count')} src={rq.get('src')}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze ESPHome Modbus logs for root cause of 'not enough data for value'")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()

    if not args.input.exists():
        raise SystemExit(f"Input log not found: {args.input}")
    lines = args.input.read_text(encoding="utf-8", errors="ignore").splitlines()
    summary = parse_log(lines)
    rep = fmt_report(summary)
    text = "\n".join(rep) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
