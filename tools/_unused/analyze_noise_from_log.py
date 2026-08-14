#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import statistics
from pathlib import Path
from typing import Dict, List, Tuple


TARGETS = {
    # name: (plausible_low, plausible_high)
    "Grid_Frequency": (45.0, 65.0),  # Hz
    "Inverter_Phase_A_Voltage": (0.0, 300.0),  # V
    "Inverter_Phase_B_Current": (0.0, 200.0),  # A
}


def parse_log(path: Path) -> Dict[str, List[float]]:
    """Parse ESPHome log and extract scaled values from "'Name' >> value unit" lines.

    Returns a mapping: sensor name -> list of float values (scaled).
    """
    data: Dict[str, List[float]] = {}
    # Pattern examples:
    # [12:35:53.132][D][sensor:118]: 'Grid_Frequency' >> 60 Hz
    # [12:35:14.868][D][sensor:118]: 'Inverter_Phase_A_Voltage' >> 105 V
    # [12:35:33.062][D][sensor:118]: 'Inverter_Phase_B_Current' >> 2 A
    pat = re.compile(r"sensor:\d+\]:\s+'([^']+)'\s+>>\s+([0-9.+-]+)")
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = pat.search(line)
            if not m:
                continue
            name = m.group(1)
            try:
                val = float(m.group(2))
            except ValueError:
                continue
            data.setdefault(name, []).append(val)
    return data


def summarize(values: List[float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not values:
        return out
    out["count"] = len(values)
    out["min"] = min(values)
    out["max"] = max(values)
    try:
        out["median"] = statistics.median(values)
    except Exception:
        out["median"] = float("nan")
    try:
        out["mean"] = statistics.fmean(values)
    except Exception:
        out["mean"] = float("nan")
    return out


def analyze(path: Path) -> None:
    data = parse_log(path)
    if not data:
        print("値を抽出できませんでした。'NAME' >> VALUE の形式が含まれているログを指定してください。")
        return
    # CSV 出力
    out_csv = Path("tools/build/noise_from_log.csv")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "value"])
        for name, vals in data.items():
            for v in vals:
                w.writerow([name, v])

    print("=== Log Noise Analysis ===")
    print(f"log: {path}")
    print(f"csv: {out_csv}")
    for name in TARGETS.keys():
        vals = data.get(name, [])
        s = summarize(vals)
        lo, hi = TARGETS[name]
        outliers = [v for v in vals if not (lo <= v <= hi)]
        print(f"- {name}: N={s.get('count',0)} min={s.get('min','-')} max={s.get('max','-')} median={s.get('median','-')} mean={s.get('mean','-')}")
        print(f"  plausible_range=[{lo},{hi}] outliers={len(outliers)}")
        if outliers[:10]:
            print(f"  sample_outliers={outliers[:10]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze ESPHome logs to detect suspicious values for selected sensors")
    ap.add_argument("--log", type=Path, required=True, help="Path to log file (ESPHome output)")
    args = ap.parse_args()
    analyze(args.log)


if __name__ == "__main__":
    main()
