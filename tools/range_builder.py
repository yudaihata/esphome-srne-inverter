#!/usr/bin/env python3
"""
Phase 2 – Range Construction

implemented_registers.json から成功したアドレスのみを抽出し、
グループ別に連続区間へマージ、最大32レジスタ毎に分割した
device_specific_ranges.json を出力します。
"""
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from .common import normalize_group, write_json, register_count_for_type


def load_implemented(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_reserved_like_record(rec: Dict[str, Any]) -> bool:
    """Return True when the record looks like a reserved/undefined placeholder.

    We exclude these from polling to reduce inverter load and avoid emitting
    entities that cannot be interpreted from the protocol document.
    """
    name = str(rec.get("name") or "").strip().lower()
    desc = str(rec.get("description") or "").strip().lower()
    generic = {"reserved", "unknown", "unused", "undefined", "n/a", "na", "none", "null"}
    if name in generic:
        return True
    if any(k in name for k in ("reserved", "revserved", "reversed")):
        return True
    if any(k in desc for k in ("reserved", "revserved", "reversed")):
        return True
    return False


def build_ranges(records: List[Dict[str, Any]], max_chunk: int = 32) -> Dict[str, Any]:
    # group -> sorted list of addresses
    groups: Dict[str, List[int]] = {}
    by_addr: Dict[tuple, Dict[str, Any]] = {}
    for rec in records:
        if not rec.get("success"):
            continue
        # Skip reserved-like points to reduce polling load.
        if is_reserved_like_record(rec):
            continue
        # Exclude 32-bit registers whose pair (N+1) could not be confirmed
        try:
            regcnt = register_count_for_type(rec.get("data_type"))
            if regcnt == 2 and not bool(rec.get("pair_exists")):
                continue
        except Exception:
            pass
        g = normalize_group(str(rec.get("group", "")))
        addr = int(rec["address"])
        groups.setdefault(g, []).append(addr)
        by_addr[(g, addr)] = rec

    for g in groups:
        groups[g] = sorted(set(groups[g]))

    ranges_out: Dict[str, Any] = {}
    for g, addrs in groups.items():
        chunks: List[Dict[str, Any]] = []
        if not addrs:
            ranges_out[g] = []
            continue

        # まず「アドレスの連続ブロック」を作る（32bit上位語や未定義で分断）
        blocks: List[List[int]] = []
        cur: List[int] = [addrs[0]]
        for a in addrs[1:]:
            if a == cur[-1] + 1:
                cur.append(a)
            else:
                blocks.append(cur)
                cur = [a]
        blocks.append(cur)

        # 各連続ブロックを、実際のModbusリクエスト幅（register_countを考慮）で32以下に分割
        for block in blocks:
            if not block:
                continue
            # 境界の再分割
            i = 0
            while i < len(block):
                start_addr = block[i]
                span_end = start_addr + max(1, register_count_for_type(by_addr[(g, start_addr)].get("data_type"))) - 1
                part_addrs: List[int] = [start_addr]
                j = i + 1
                while j < len(block):
                    a = block[j]
                    regcnt = max(1, register_count_for_type(by_addr[(g, a)].get("data_type")))
                    cand_end = max(span_end, a + regcnt - 1)
                    span = cand_end - start_addr + 1
                    if span > max_chunk:
                        break
                    # 取り込む
                    part_addrs.append(a)
                    span_end = cand_end
                    j += 1
                # 出力チャンク
                chunks.append({
                    "start": start_addr,
                    "end": span_end,
                    "size": span_end - start_addr + 1,
                    "addresses": part_addrs,
                })
                i = j

        ranges_out[g] = chunks
    return {
        "max_chunk": max_chunk,
        "ranges": ranges_out,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build device-specific contiguous ranges from discovery output")
    ap.add_argument("--input", type=Path, default=Path("tools/build/implemented_registers.json"))
    ap.add_argument("--output", type=Path, default=Path("tools/build/device_specific_ranges.json"))
    ap.add_argument("--max-chunk", type=int, default=32)
    args = ap.parse_args()

    records = load_implemented(args.input)
    built = build_ranges(records, max_chunk=args.max_chunk)
    write_json(args.output, built)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
