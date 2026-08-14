#!/usr/bin/env python3
"""
Backfill min/max/step for P05/P07/P08 from a CSV extracted table.

Inputs:
- Catalog JSON (hierarchical): docs/srne_hybrid_modbus_v1.96.json
- CSV: docs/srne_modbus_raw_extraction.csv

Logic:
- Parse CSV rows that look like setting rows with an address like E000/E2xx/E4xx.
- Convert address_hex to int, and restrict to groups:
    P05: 0xE000-0xE1FF, P07: 0xE200-0xE3FF, P08: 0xE400-0xE5FF
- For matching JSON items (by address_dec) that are write-capable (rw contains 'W'):
    - If JSON already has min/max/step, do not overwrite.
    - Otherwise set min/max from CSV columns.
    - Set step heuristically:
        * If group==P05 and unit=='V': skip step (special 12V-conversion handled downstream).
        * Else if scale in {0.001, 0.01, 0.1, 1}: step = scale
        * Else if scale < 1: step = scale
        * Else: step = 1

Run with --write to apply, otherwise prints a dry-run summary.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CATALOG_DEFAULT = Path("docs/srne_hybrid_modbus_v1.96.json")
CSV_DEFAULT = Path("docs/auto_parsed_register_map.csv")
UNIT_MAP_DEFAULT = Path("docs/Unit and Dimension Description.csv")


def group_for_address(addr: int) -> Optional[str]:
    if 0xE000 <= addr <= 0xE1FF:
        return "P05"
    if 0xE200 <= addr <= 0xE3FF:
        return "P07"
    if 0xE400 <= addr <= 0xE5FF:
        return "P08"
    return None


def parse_num_or_none(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        v = float(s)
        # prefer int if exact
        if int(v) == v:
            return int(v)
        return v
    except Exception:
        return None


def read_csv_limits(csv_path: Path) -> Dict[int, Tuple[Optional[float], Optional[float]]]:
    out: Dict[int, Tuple[Optional[float], Optional[float]]] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        idx_addr = 0
        idx_min = 9
        idx_max = 10
        if header:
            # Try to locate columns by name
            def find_col(name: str) -> Optional[int]:
                for i, v in enumerate(header):
                    if (v or '').strip().lower() == name:
                        return i
                return None
            ia = find_col('address'); imn = find_col('minimum'); imx = find_col('maximum')
            if ia is not None:
                idx_addr = ia
            if imn is not None:
                idx_min = imn
            if imx is not None:
                idx_max = imx
        # Process rows
        for row in reader:
            if not row or len(row) <= max(idx_addr, idx_min, idx_max):
                continue
            addr_hex = (row[idx_addr] or "").strip().upper()
            if not addr_hex:
                continue
            # Expect hex like 'E200' or '0107'
            try:
                addr = int(addr_hex, 16)
            except Exception:
                continue
            g = group_for_address(addr)
            if g is None:
                continue
            min_v = parse_num_or_none(row[idx_min])
            max_v = parse_num_or_none(row[idx_max])
            if min_v is None and max_v is None:
                continue
            out[addr] = (min_v, max_v)
    return out


def load_json(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(p: Path, data: Dict[str, Any]) -> None:
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_index(catalog: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    idx: Dict[int, Dict[str, Any]] = {}
    for g in catalog.get("groups", []):
        for it in (g.get("items") or []):
            try:
                addr = int(it.get("address_dec"))
            except Exception:
                continue
            idx[addr] = it
    return idx


def read_unit_magnifications(csv_path: Path) -> Dict[str, float]:
    """Return unit -> magnification map from 'Unit and Dimension Description.csv'."""
    out: Dict[str, float] = {}
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None) or []
            # Expect columns: Physical Quantity, Unit, Magnification, Description
            # Find 'Unit' and 'Magnification' columns
            def find_col(name: str) -> Optional[int]:
                for i, v in enumerate(header):
                    if (v or '').strip().lower() == name:
                        return i
                return None
            iu = find_col('unit')
            im = find_col('magnification')
            if iu is None or im is None:
                return out
            for row in reader:
                if len(row) <= max(iu, im):
                    continue
                u = (row[iu] or '').strip()
                m = (row[im] or '').strip()
                try:
                    mag = float(m)
                except Exception:
                    continue
                if u:
                    out[u] = mag
    except Exception:
        pass
    return out


def infer_step(it: Dict[str, Any], group: str, unit_mag: Dict[str, float] = None) -> Optional[float]:
    unit = (it.get("unit") or "").strip()
    try:
        scale = float(it.get("scale") or 1.0)
    except Exception:
        scale = 1.0
    # Avoid step for P05 voltage settings due to 12V→rated scaling at YAML stage
    if group == "P05" and unit == "V":
        return None
    if scale in (0.001, 0.01, 0.1, 1.0):
        return scale
    if scale < 1.0:
        return scale
    # If scale is integer >=1, try unit magnification fallback
    if unit_mag:
        mag = unit_mag.get(unit)
        if mag and mag > 1:
            return 1.0 / mag
    return 1.0


def backfill(catalog: Dict[str, Any], limits: Dict[int, Tuple[Optional[float], Optional[float]]], overwrite: bool=False, unit_mag: Dict[str, float] = None) -> List[Tuple[int, Dict[str, Any]]]:
    idx = build_index(catalog)
    changes: List[Tuple[int, Dict[str, Any]]] = []
    for addr, (mn, mx) in limits.items():
        it = idx.get(addr)
        if not it:
            continue
        rw = str(it.get("rw", "")).upper()
        # Only for target groups via address range
        group = group_for_address(addr)
        if not group:
            continue
        # Respect existing manual entries unless overwrite
        if not overwrite and any(k in it for k in ("min", "max", "step")):
            continue
        # Apply: Always backfill min/max for R/W/RW
        if mn is not None:
            it["min"] = mn
        if mx is not None:
            it["max"] = mx
        # Heuristic step only for write-capable (user input)
        if "W" in rw:
            st = infer_step(it, group, unit_mag)
            if st is not None:
                it["step"] = st
        changes.append((addr, it))
    return changes


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill min/max/step for P05/P07/P08 settings from CSV")
    ap.add_argument("--catalog", type=Path, default=CATALOG_DEFAULT)
    ap.add_argument("--csv", type=Path, default=CSV_DEFAULT)
    ap.add_argument("--write", action="store_true", help="Write changes to catalog JSON")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing min/max/step if present")
    ap.add_argument("--unit-map", type=Path, default=UNIT_MAP_DEFAULT, help="Unit magnification csv (for default step)")
    args = ap.parse_args()

    cat = load_json(args.catalog)
    limits = read_csv_limits(args.csv)
    before = json.dumps(cat, ensure_ascii=False)
    unit_mag = read_unit_magnifications(args.unit_map) if args.unit_map and args.unit_map.exists() else {}
    changes = backfill(cat, limits, overwrite=args.overwrite, unit_mag=unit_mag)
    after = json.dumps(cat, ensure_ascii=False)

    print(f"Candidates found: {len(changes)}")
    for addr, it in sorted(changes, key=lambda x: x[0]):
        gid = group_for_address(addr)
        print(f"  - 0x{addr:04X} [{gid}] id={it.get('id')} min={it.get('min')} max={it.get('max')} step={it.get('step')} unit={it.get('unit')} scale={it.get('scale')}")

    if before == after:
        print("No changes (catalog already contains min/max/step or no matches).")
        return

    if args.write:
        save_json(args.catalog, cat)
        print(f"Wrote changes to {args.catalog}")
    else:
        print("Dry-run only. Use --write to apply.")


if __name__ == "__main__":
    main()
