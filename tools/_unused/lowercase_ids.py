#!/usr/bin/env python3
import json
import pathlib
from datetime import datetime


def main():
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    json_path = repo_root / "docs" / "srne_hybrid_modbus_v1.96.json"
    if not json_path.exists():
        print(f"ERROR: JSON file not found: {json_path}")
        return 1

    # Read original
    original_text = json_path.read_text(encoding="utf-8")
    data = json.loads(original_text)

    # Prepare backup path
    backups_dir = repo_root / "backups"
    backups_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backups_dir / f"srne_hybrid_modbus_v1.96.json.bak.{ts}"

    # Collect and transform
    changed_groups = 0
    changed_items = 0
    group_ids = []
    item_ids = []

    groups = data.get("groups")
    if isinstance(groups, list):
        for g in groups:
            gid = g.get("id")
            if isinstance(gid, str):
                group_ids.append(gid.lower())
                if gid != gid.lower():
                    g["id"] = gid.lower()
                    changed_groups += 1
            items = g.get("items")
            if isinstance(items, list):
                for it in items:
                    iid = it.get("id")
                    if isinstance(iid, str):
                        item_ids.append(iid.lower())
                        if iid != iid.lower():
                            it["id"] = iid.lower()
                            changed_items += 1

    # Dup checks (post-lowercase)
    def duplicates(seq):
        seen = set()
        dups = set()
        for s in seq:
            if s in seen:
                dups.add(s)
            else:
                seen.add(s)
        return sorted(dups)

    dup_groups = duplicates(group_ids)
    dup_items = duplicates(item_ids)

    # Write backup (original)
    backup_path.write_text(original_text, encoding="utf-8")

    # Save updated
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Lowercased IDs. groups changed: {changed_groups}, items changed: {changed_items}")
    if dup_groups:
        print("WARNING: Duplicate group ids after lowercasing:", ", ".join(dup_groups))
    if dup_items:
        print("WARNING: Duplicate item ids after lowercasing:", ", ".join(dup_items))
    print(f"Backup saved to: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
