#!/usr/bin/env python3
import json
import pathlib
import sys
from datetime import datetime


def main():
    # Target JSON path
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    json_path = repo_root / "docs" / "srne_hybrid_modbus_v1.96.json"

    if not json_path.exists():
        print(f"ERROR: JSON file not found: {json_path}")
        return 1

    # Backup
    backups_dir = repo_root / "backups"
    backups_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backups_dir / f"srne_hybrid_modbus_v1.96.json.bak.{ts}"

    # Load (keep original text for backup)
    original_text = json_path.read_text(encoding="utf-8")
    data = json.loads(original_text)

    # Process
    changes = 0
    def visit_items(items):
        nonlocal changes
        for it in items:
            label = it.get("label", {})
            cn = label.get("cn")
            source_key = it.get("source_key")
            if cn == "保留" and isinstance(source_key, str) and source_key:
                if isinstance(label, dict):
                    prev = label.get("en")
                    label["en"] = source_key
                    it["label"] = label
                    changes += 1
                    # print(f"Updated {it.get('id')} en: {prev!r} -> {source_key!r}")

    groups = data.get("groups")
    if isinstance(groups, list):
        for g in groups:
            items = g.get("items")
            if isinstance(items, list):
                visit_items(items)

    if changes == 0:
        print("No items with cn='保留' found or no changes needed.")
        return 0

    # Write backup of the original file
    backup_path.write_text(original_text, encoding="utf-8")

    # Write to original
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Updated label.en for {changes} item(s). Backup: {backup_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
