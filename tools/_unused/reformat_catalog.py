#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def load_json(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(p: Path, data: Dict[str, Any]) -> None:
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


ITEM_ORDER = [
    "id",
    "source_key",
    "label",
    "address_hex",
    "address_dec",
    "length",
    "rw",
    "type",
    "scale",
    "unit",
    "min",
    "max",
    "step",
    "description",
    "enums",
    "display",
]

GROUP_ORDER = [
    "id",
    "label",
    "base_address_hex",
    "items",
]


def reorder_dict(d: Dict[str, Any], order: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in order:
        if k in d:
            out[k] = d[k]
    for k, v in d.items():
        if k not in out:
            out[k] = v
    return out


def reorder_catalog(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data)
    groups = out.get("groups") or []
    new_groups = []
    for g in groups:
        new_g = dict(g)
        items = new_g.get("items") or []
        new_items = []
        for it in items:
            new_items.append(reorder_dict(it, ITEM_ORDER))
        new_g["items"] = new_items
        new_g = reorder_dict(new_g, GROUP_ORDER)
        new_groups.append(new_g)
    out["groups"] = new_groups
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Reformat catalog JSON with deterministic key order to improve readability")
    ap.add_argument("--input", type=Path, default=Path("docs/srne_hybrid_modbus_v1.96.json"))
    ap.add_argument("--output", type=Path, default=None, help="If set, write to this file; else overwrite input")
    args = ap.parse_args()

    data = load_json(args.input)
    new_data = reorder_catalog(data)

    target = args.output or args.input
    save_json(target, new_data)
    print(f"Rewrote {target} with reordered keys")


if __name__ == "__main__":
    main()
