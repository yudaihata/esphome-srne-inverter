import json
import re
import sys
import unicodedata
from pathlib import Path


def slugify(text: str) -> str:
    # Normalize and keep ASCII, then snake_case
    s = unicodedata.normalize("NFKD", text)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"['`’]", "", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower()


def main(path_str: str) -> None:
    path = Path(path_str)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # Update group IDs based on label.en
    for g in data.get("groups", []):
        label = g.get("label", {}).get("en")
        if label:
            g["id"] = slugify(label)

    # Update item IDs based on label.en, ensuring uniqueness by suffixing address if duplicated
    used_ids = set()
    for g in data.get("groups", []):
        for item in g.get("items", []):
            label_en = item.get("label", {}).get("en")
            if not label_en:
                continue
            base = slugify(label_en)
            new_id = base
            if new_id in used_ids:
                # Prefer stable suffix using address if available
                addr_hex = str(item.get("address_hex") or item.get("address_dec") or "")
                if isinstance(addr_hex, str) and addr_hex.lower().startswith("0x"):
                    addr_suffix = addr_hex[2:].lower()
                else:
                    addr_suffix = str(addr_hex)
                if addr_suffix:
                    new_id = f"{base}_{addr_suffix}"
                # If still collides or no address, de-duplicate numerically
                counter = 1
                temp = new_id
                while temp in used_ids:
                    temp = f"{new_id}_{counter}"
                    counter += 1
                new_id = temp
            used_ids.add(new_id)
            item["id"] = new_id

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python tools/update_ids_from_label_en.py <path-to-json>")
        sys.exit(1)
    main(sys.argv[1])
