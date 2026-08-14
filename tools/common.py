import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JSON_CATALOG_PATH = PROJECT_ROOT / "docs" / "srne_hybrid_modbus_v1.96.json"
NOTES_PATH = PROJECT_ROOT / "docs" / "SRNE_Inverter_Modbus_Protocol_V1.96_Notes.md"


def load_json_raw(path: Path = JSON_CATALOG_PATH) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_modbus_endianness(path: Path = JSON_CATALOG_PATH) -> Dict[str, str]:
    """Return modbus endianness info from catalog.

    Notes適合のため、word順序はデフォルトで 'little' を返す。
    registerバイト順はカタログに記載がなければ 'big' を既定にする。
    """
    try:
        raw = load_json_raw(path)
        end = ((raw or {}).get("modbus") or {}).get("endianness") or {}
        register = str(end.get("register", "big"))
        word = str(end.get("word", "little"))
        return {"register": register, "word": word}
    except Exception:
        return {"register": "big", "word": "little"}


def normalize_group(group: str) -> str:
    # Accept leading group tokens like "P00 ..." or "O03 ..."
    m = re.match(r"^((?:P|O)\d{2})\b", group.strip(), re.IGNORECASE)
    return m.group(1).upper() if m else group.strip().split()[0].upper()


@dataclass
class RegisterDef:
    group: str
    address: int
    name: str
    rw: str
    multiplier: float = 1.0
    unit: str = ""
    data_type: Optional[str] = None
    enums: Optional[Dict[str, str]] = None
    description: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "RegisterDef":
        address = d.get("address_dec")
        if address is None:
            hexv = d.get("address_hex")
            if isinstance(hexv, str) and hexv.startswith("0x"):
                address = int(hexv, 16)
        return RegisterDef(
            group=normalize_group(d.get("group", "")),
            address=int(address),
            name=d.get("name", f"Reg_{address}"),
            rw=d.get("rw", "R"),
            multiplier=float(d.get("multiplier", 1.0)),
            unit=d.get("unit", ""),
            data_type=d.get("data_type"),
            enums=d.get("enums"),
            description=d.get("description", ""),
            min=(d.get("min") if d.get("min") is not None else None),
            max=(d.get("max") if d.get("max") is not None else None),
            step=(d.get("step") if d.get("step") is not None else None),
        )

class RW(Enum):
    R = "R"
    W = "W"
    RW = "RW"


class ValueType(Enum):
    U16 = "uint16"
    U32 = "uint32"
    S16 = "int16"
    S32 = "int32"
    F32 = "float"
    STR = "string"


@dataclass
class Label:
    en: str = ""
    cn: str = ""
    ja: str = ""


@dataclass
class Item:
    address_dec: int
    address_hex: str
    label: Label
    rw: str
    scale: float = 1.0
    unit: str = ""
    type: Optional[str] = None
    enums: Optional[Dict[str, str]] = None
    description: str = ""
    source_key: Optional[str] = None
    id: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None


@dataclass
class Group:
    id: str
    label: Label
    base_address_hex: str
    items: List[Item]


@dataclass
class Catalog:
    schema_version: str
    groups: List[Group]

    def iter_items(self) -> Iterable[Item]:
        for g in self.groups:
            for it in g.items:
                yield it


def _derive_group_code(group_obj: Dict[str, Any]) -> str:
    """Derive a Pxx-style group code from group label.cn or base address.

    Falls back to the first token uppercased if Pxx is not detectable.
    """
    # Try label.cn that often includes 'Pxx ...'
    label = group_obj.get("label") or {}
    cn = label.get("cn") or ""
    m = re.search(r"\bP(\d{2})\b", cn)
    if m:
        return f"P{m.group(1)}"
    # Fallback: base address mapping for common groups
    base = (group_obj.get("base_address_hex") or "").strip()
    try:
        base_int = int(base, 16) if base.lower().startswith("0x") else int(base or "0", 16)
    except Exception:
        base_int = -1
    base_map = {
        0x0000: "P00",
        0x000A: "P00",
        0x0100: "P01",
        0x0200: "P02",
        0xDF00: "P03",
        0xE000: "P05",
        0xE200: "P07",
        0xE400: "P08",
        0xF000: "P09",
        0xF800: "P10",
    }
    if base_int in base_map:
        return base_map[base_int]
    # As a last resort, use the first token of en label
    en = (label.get("en") or "").strip().split()
    return (en[0].upper() if en else "")


def load_catalog(path: Path = JSON_CATALOG_PATH) -> Catalog:
    """Load hierarchical catalog and validate minimal schema (no legacy support)."""
    raw = load_json_raw(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("groups"), list):
        raise ValueError("Unsupported catalog schema: expected { groups: [...] }")
    schema_version = str(raw.get("schema_version", ""))
    groups: List[Group] = []
    for g in raw["groups"]:
        glab = g.get("label") or {}
        group = Group(
            id=str(g.get("id", "")),
            label=Label(
                en=str(glab.get("en", "")),
                cn=str(glab.get("cn", "")),
                ja=str(glab.get("ja", "")),
            ),
            base_address_hex=str(g.get("base_address_hex", "")),
            items=[],
        )
        for it in (g.get("items") or []):
            ilab = it.get("label") or {}
            item = Item(
                address_dec=int(it.get("address_dec")),
                address_hex=str(it.get("address_hex")),
                label=Label(
                    en=str(ilab.get("en", "")),
                    cn=str(ilab.get("cn", "")),
                    ja=str(ilab.get("ja", "")),
                ),
                rw=str(it.get("rw", "R")),
                scale=float(it.get("scale", 1.0)),
                unit=str(it.get("unit", "")),
                type=it.get("type"),
                enums=it.get("enums"),
                description=str(it.get("description", "")),
                source_key=it.get("source_key"),
                id=it.get("id"),
                min=it.get("min"),
                max=it.get("max"),
                step=it.get("step"),
            )
            group.items.append(item)
        groups.append(group)
    return Catalog(schema_version=schema_version, groups=groups)


def load_register_defs(path: Path = JSON_CATALOG_PATH) -> List[RegisterDef]:
    """Flatten items from the hierarchical catalog into RegisterDef list."""
    cat = load_catalog(path)
    regs: List[RegisterDef] = []
    for g in cat.groups:
        group_code = _derive_group_code({
            "label": {"cn": g.label.cn, "en": g.label.en},
            "base_address_hex": g.base_address_hex,
        })
        for it in g.items:
            flat: Dict[str, Any] = {
                "group": group_code,
                "address_dec": it.address_dec,
                "address_hex": it.address_hex,
                "name": it.label.en or it.source_key or it.id or f"Reg_{it.address_dec}",
                "rw": it.rw,
                "multiplier": it.scale,
                "unit": it.unit,
                "data_type": it.type,
                "enums": it.enums,
                "description": it.description,
                "min": getattr(it, "min", None) if hasattr(it, "min") else None,
                "max": getattr(it, "max", None) if hasattr(it, "max") else None,
                "step": getattr(it, "step", None) if hasattr(it, "step") else None,
            }
            try:
                regs.append(RegisterDef.from_json(flat))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid register definition in group {group_code or g.id!r} "
                    f"at address {it.address_dec!r}: {exc}"
                ) from exc
    return regs


def tier_for_group(group: str) -> str:
    g = normalize_group(group)
    # デフォルトのグループ別更新間隔（ベースは秒表記。分散はjitter_ms側で付与）
    mapping = {
        "P00": "600s",   # RARE (Product Info)
        "P01": "5s",     # FAST (DC Data)
        "P02": "5s",     # FAST（Inverter Data）
        "P03": "60s",    # NORMAL (Device Control)
        "P05": "600s",   # RARE (Battery Settings)
        "P07": "600s",   # RARE (User Settings)
        "P08": "600s",   # RARE (Grid Settings)
        "P09": "600s",   # RARE (Power Stats Historical)
        "P10": "600s",   # RARE (Fault Record)
    }
    return mapping.get(g, "60s")


def entity_info_category(group: str, rw: str) -> Optional[str]:
    if rw and rw.upper().startswith(("RW", "W")):
        return "config"
    if normalize_group(group) == "P00":
        return "diagnostic"
    return None


def esphome_value_type(data_type: Optional[str]) -> str:
    if not data_type:
        return "U_WORD"
    dt = data_type.upper()
    if dt in ("U16", "UINT16", "WORD"):
        return "U_WORD"
    if dt in ("S16", "INT16"):
        return "S_WORD"
    if dt in ("U32", "UINT32", "DWORD"):
        return "U_DWORD"
    if dt in ("S32", "INT32"):
        return "S_DWORD"
    if dt in ("FLOAT", "F32", "IEEE754"):
        return "FP32"
    return "U_WORD"


def register_count_for_type(data_type: Optional[str]) -> int:
    """Return Modbus register count required for the given data type.

    - 16-bit (uint16/int16/word) -> 1
    - 32-bit (uint32/int32/float/fp32/dword) -> 2
    - string系は可変長なので 0（呼び出し側でaddress/lengthを参照）
    - 不明な型は安全側で 1
    """
    if not data_type:
        return 1
    dt = str(data_type).lower()
    if dt in ("uint16", "int16", "word", "u16", "s16"):
        return 1
    if dt in ("uint32", "int32", "float", "fp32", "dword", "u32", "s32"):
        return 2
    if "string" in dt:
        return 0
    return 1


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
