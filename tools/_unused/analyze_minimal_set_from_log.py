#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import statistics
from pathlib import Path
from typing import Dict, List, Tuple


# 監視対象（最小16項目）と妥当レンジ（プラウジブル範囲）
TARGETS: Dict[str, Tuple[float, float]] = {
    # P02
    "Device_State": (0, 20),
    "Grid_Frequency": (45, 65),  # Hz
    "Grid_Phase_A_Voltage": (0, 300),  # V（広め）
    "Grid_Phase_B_Voltage": (0, 300),
    "Load_Phase_A_Rate": (0, 100),  # %
    "Load_Phase_B_Rate": (0, 100),
    "Load_Phase_A_Active_Power": (0, 10000),  # W（広め）
    "Load_Phase_B_Active_Power": (0, 10000),
    "Load_Phase_C_Active_Power": (0, 10000),
    "Heatsink_Temperature_A": (-20, 120),  # °C
    "Heatsink_Temperature_B": (-20, 120),
    "Heatsink_Temperature_C": (-20, 120),
    "Load_Total_Active_Power": (0, 20000),
    # P01 (PV1 最小)
    "PV1_Voltage": (0, 300),
    "PV1_Current": (0, 200),
    "PV1_Power": (0, 10000),
}


def parse_log_values(path: Path) -> Dict[str, List[float]]:
    # 'NAME' >> VALUE 形式を抽出
    pat = re.compile(r"sensor:\d+\]:\s+'([^']+)'\s+>>\s+([0-9.+-]+)")
    data: Dict[str, List[float]] = {k: [] for k in TARGETS.keys()}
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = pat.search(line)
            if not m:
                continue
            name, sval = m.group(1), m.group(2)
            if name not in data:
                continue
            try:
                v = float(sval)
            except ValueError:
                continue
            data[name].append(v)
    return data


def summarize(vals: List[float]) -> Dict[str, float]:
    if not vals:
        return {"count": 0}
    out = {
        "count": len(vals),
        "min": min(vals),
        "max": max(vals),
        "median": statistics.median(vals) if len(vals) else float("nan"),
        "mean": statistics.fmean(vals) if len(vals) else float("nan"),
    }
    return out


def analyze(path: Path) -> None:
    data = parse_log_values(path)
    out_csv = Path("tools/build/minimal_set_log_values.csv")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "value"])
        for name, vals in data.items():
            for v in vals:
                w.writerow([name, v])

    print("=== Minimal Set Log Analysis ===")
    print(f"log: {path}")
    print(f"csv: {out_csv}")
    any_outlier = False
    for name, rng in TARGETS.items():
        lo, hi = rng
        vals = data.get(name, [])
        s = summarize(vals)
        outs = [v for v in vals if not (lo <= v <= hi)]
        if outs:
            any_outlier = True
        print(f"- {name}: N={s.get('count',0)} min={s.get('min','-')} max={s.get('max','-')} median={s.get('median','-')} mean={s.get('mean','-')}")
        print(f"  plausible=[{lo},{hi}] outliers={len(outs)}" + (f" sample={outs[:5]}" if outs else ""))

    if not any_outlier:
        print("→ すべて妥当レンジ内。値の混入は観測されませんでした。")
    else:
        print("→ 妥当レンジ外の値が検出されました。通信層の取りこぼし/混線、あるいは初期化直後の乱れが疑われます。")


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze minimal P02/P01 set values in ESPHome logs for outliers")
    ap.add_argument("--log", type=Path, required=True, help="Path to ESPHome log file")
    args = ap.parse_args()
    analyze(args.log)


if __name__ == "__main__":
    main()
