#!/usr/bin/env python3
"""
Phase 3 – ESPHome YAML Generation

device_specific_ranges.json と implemented_registers.json を入力に、
以下の構成でYAMLを生成します。

esphome/
  srne_inverter.yaml
  srne/
    core.yaml
    anchors/
      p00_anchors.yaml
      p01_anchors.yaml
      p02_anchors.yaml
      p03_anchors.yaml
    custom/
      entities_p00.yaml
      entities_p01.yaml
      entities_p02.yaml
      entities_p03.yaml

標準生成は単一のmodbus_controllerを使い、P00/P01/P02/P09の読み取り専用範囲を
明示的なpackedアンカーで管理します。strictは互換性維持用の実験モードです。

strictモードは --strict-groups でグループを個別指定でき、ハイブリッド運用が可能。
"""
import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import (
    entity_info_category,
    esphome_value_type,
    get_modbus_endianness,
    register_count_for_type,
    normalize_group,
    tier_for_group,
    load_register_defs,
    JSON_CATALOG_PATH,
)
DERIVED_RECIPES_PATH = Path("docs/derived_recipes.json")
SINGLE_CONTROLLER_ID = "srne_main"
STANDARD_PACKED_GROUPS = {"P00", "P01", "P02", "P09", "P10"}
STANDARD_COALESCED_RW_GROUPS = {"P05", "P07"}
P00_VERSION_ADDRS = {20, 21, 22, 23, 28}
P02_SLOW_ADDRS = {524, 525, 526}
P09_TOTAL_COUNTER_GUARD_ADDRS = {
    61490, 61492, 61494, 61496, 61498, 61510, 61512,
}

_DERIVED_RECIPES_CACHE: Optional[Dict[str, Any]] = None
_GENERATION_HINTS_CACHE: Optional[Dict[str, Any]] = None


def effective_multiplier(group: str, address: int, multiplier: Any) -> float:
    """Return protocol display scaling, including documented P00 versions."""
    try:
        value = float(multiplier or 1.0)
    except (TypeError, ValueError):
        value = 1.0
    if normalize_group(group) == "P00" and address in P00_VERSION_ADDRS:
        return 0.01
    return value


def infer_accuracy_decimals(unit_val: str, value_type_val: str, mult_val: float) -> Optional[int]:
    u = (unit_val or "").strip()
    if u == "Hz":
        return 2
    if u in ("V", "A", "°C", "°F"):
        return 1
    if u in ("W", "kW", "VA", "%", "Wh", "kWh"):
        return 0
    if abs(mult_val - 0.01) < 1e-12:
        return 2
    if abs(mult_val - 0.1) < 1e-12:
        return 1
    if value_type_val in ("U_WORD", "S_WORD", "U_DWORD", "S_DWORD") and abs(mult_val - 1.0) < 1e-12:
        return 0
    return None

def load_derived_recipes() -> Dict[str, Any]:
    global _DERIVED_RECIPES_CACHE
    if _DERIVED_RECIPES_CACHE is not None:
        return _DERIVED_RECIPES_CACHE
    try:
        _DERIVED_RECIPES_CACHE = load_json(DERIVED_RECIPES_PATH)
    except Exception:
        _DERIVED_RECIPES_CACHE = {"recipes": []}
    return _DERIVED_RECIPES_CACHE


def load_generation_hints() -> Dict[str, Any]:
    global _GENERATION_HINTS_CACHE
    if _GENERATION_HINTS_CACHE is not None:
        return _GENERATION_HINTS_CACHE
    try:
        catalog = load_json(JSON_CATALOG_PATH)
        hints = catalog.get("generation_hints") or {}
        if isinstance(hints, dict):
            _GENERATION_HINTS_CACHE = hints
        else:
            _GENERATION_HINTS_CACHE = {}
    except Exception:
        _GENERATION_HINTS_CACHE = {}
    return _GENERATION_HINTS_CACHE


def build_catalog_meta_map(path: Path) -> Dict[tuple, Dict[str, Any]]:
    """Build ((group, address) -> metadata) map from catalog JSON.

    Supports both legacy {groups:[{items:...}]} and current
    {sections:[{groups:[{registers:...}]}]} layouts.
    """
    raw = load_json(path)
    out: Dict[tuple, Dict[str, Any]] = {}

    def _addr_of(item: Dict[str, Any]) -> Optional[int]:
        try:
            if item.get("address_dec") is not None:
                return int(item.get("address_dec"))
            ah = item.get("address_hex")
            if isinstance(ah, str) and ah:
                return int(ah, 16) if ah.lower().startswith("0x") else int(ah)
        except Exception:
            return None
        return None

    def _name_of(item: Dict[str, Any]) -> str:
        try:
            lab = item.get("label") or {}
            if isinstance(lab, dict) and str(lab.get("en") or "").strip():
                return str(lab.get("en")).strip()
            nm = item.get("name")
            if isinstance(nm, dict) and str(nm.get("en") or "").strip():
                return str(nm.get("en")).strip()
            if isinstance(nm, str) and nm.strip():
                return nm.strip()
            for k in ("id", "source_key"):
                v = item.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        except Exception:
            pass
        return ""

    def _mult_of(item: Dict[str, Any]) -> float:
        try:
            if item.get("scale") is not None:
                return float(item.get("scale"))
            if item.get("multiplier") is not None:
                return float(item.get("multiplier"))
        except Exception:
            pass
        return 1.0

    def _group_from_addr(addr: int) -> str:
        a = int(addr)
        if 0x0000 <= a < 0x0100:
            return "P00"
        if 0x0100 <= a < 0x0200:
            return "P01"
        if 0x0200 <= a < 0x0300:
            return "P02"
        if 0xDF00 <= a < 0xE000:
            return "P03"
        if 0xE000 <= a < 0xE200:
            return "P05"
        if 0xE200 <= a < 0xE400:
            return "P07"
        if 0xE400 <= a < 0xE800:
            return "P08"
        if 0xF000 <= a < 0xF800:
            return "P09"
        if 0xF800 <= a < 0x10000:
            return "P10"
        return ""

    def _add(group_raw: str, item: Dict[str, Any]) -> None:
        a = _addr_of(item)
        if a is None:
            return
        g = normalize_group(group_raw or "")
        if not re.match(r"^(?:P|O)\d{2}$", g):
            g = _group_from_addr(int(a))
        if not g:
            return
        out[(g, int(a))] = {
            "multiplier": _mult_of(item),
            "unit": (item.get("unit") or ""),
            "name": _name_of(item),
            "enums": item.get("enums"),
        }

    # Current schema: sections -> groups -> registers
    try:
        for sec in (raw.get("sections") or []):
            for g in (sec.get("groups") or []):
                graw = g.get("id") or (g.get("label") or {}).get("en") or (g.get("label") or {}).get("cn") or ""
                for r in (g.get("registers") or []):
                    _add(graw, r)
    except Exception:
        pass

    # Legacy schema: groups -> items/registers
    try:
        for g in (raw.get("groups") or []):
            graw = g.get("id") or (g.get("label") or {}).get("en") or (g.get("label") or {}).get("cn") or ""
            for it in (g.get("items") or g.get("registers") or []):
                _add(graw, it)
    except Exception:
        pass

    return out


def resolve_split_hint_addresses(group_norm: str, split_item_ids: List[Any], regs_map: Dict[int, Dict[str, Any]]) -> set:
    addrs: set = set()
    if not split_item_ids or not regs_map:
        return addrs
    try:
        target_ids = set(str(x).strip() for x in split_item_ids if str(x).strip())
        if not target_ids:
            return addrs
        for a, reg in regs_map.items():
            try:
                if normalize_group(reg.get("group", "")) != group_norm:
                    continue
                rid = str(reg.get("id") or "").strip()
                if rid and rid in target_ids:
                    addrs.add(int(a))
            except Exception:
                continue
    except Exception:
        return set()
    return addrs


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def sensor_name(name: str) -> str:
    # センサーのエンティティ名: 空白はアンダースコア、スラッシュは分数スラッシュに置換
    return (name or "").replace(" ", "_").replace("/", "\u2044").replace("\u2215", "\u2044")

def sanitize_display_name(name: str) -> str:
    # 表示名系（button/select等）: スラッシュはURL区切りと衝突するので分数スラッシュに置換
    return (name or "").replace("/", "\u2044")

def sensor_entity_name(name: str) -> str:
    # 実際のセンサーエンティティ名（id相当）: 空白はアンダースコア、スラッシュは分数スラッシュ
    return (name or "").replace(" ", "_").replace("/", "\u2044")


def generate_core_yaml(
    uart_tx: str = "GPIO17",
    uart_rx: str = "GPIO16",
    baud_rate: int = 9600,
    slave_addr: int = 1,
    groups: List[str] = None,
    board: str = "esp32dev",
    platform: str = "esp32",
    controllers: List[dict] = None,
    log_level: str = "INFO",
    # シンプル構成に合わせて既定値を軽量化
    send_wait_time_ms: int = 100,
    command_throttle_ms: int = 100,
    max_command_retries: int = 3,
    offline_skip_updates: int = 10,
    flow_control_pin: Optional[str] = None,
) -> str:
    lines: List[str] = [
            "esphome:",
            "  name: srne-inverter",
            "  friendly_name: SRNE-Inverter",
            "  comment: 'ESPHome SRNE Inverter - auto-generated'",
            "",
            "# プラットフォーム/ボード設定（必要に応じて変更してください）",
            "esp32:",
            f"  board: {board}",
            "  framework:",
            "    type: arduino",
            "",
            "# UART/RS485/Modbus設定は環境に合わせて編集してください",
            "uart:",
            "  id: uart_bus",
            f"  tx_pin: {uart_tx}   # 例: ESP32",
            f"  rx_pin: {uart_rx}   # 例: ESP32",
            f"  baud_rate: {baud_rate}",
            "  parity: NONE",
            "  stop_bits: 1",
            # 既定は最小限の構成
            "",
            "modbus:",
            "  id: modbus1",
            "  uart_id: uart_bus",
            f"  send_wait_time: {send_wait_time_ms}ms",
            "",
            "modbus_controller:",
    "",
            "# ネットワーク/管理設定（必要に応じて編集してください）",
            "wifi:",
            "  ssid: !secret wifi_ssid",
            "  password: !secret wifi_password",
            "  power_save_mode: NONE",
            "  ap:",
            "    ssid: 'srne-inverter-setup'",
            "    password: !secret fallback_ap_password",
            "",
            "captive_portal:",
            "",
            "api:",
            "  encryption:",
            "    key: !secret api_encryption_key",
            "",
            "ota:",
            "  - platform: esphome",
            "    password: !secret ota_password",
            "",
            "# 時刻同期（HA優先 / SNTPフォールバック）: DateTime 同期ボタン用",
            "time:",
            "  - platform: homeassistant",
            "    id: ha_time",
            "  - platform: sntp",
            "    id: sntp_time",
            "",
            "logger:",
            f"  level: {log_level}",
            "",
            "# System entities (diagnostic / connectivity)",
            "sensor:",
            "  - platform: uptime",
            "    name: \"ESP Uptime\"",
            "    entity_category: diagnostic",
            "  - platform: wifi_signal",
            "    id: wifi_rssi_db",
            "    name: \"WiFi RSSI\"",
            "    update_interval: 60s",
            "    entity_category: diagnostic",
            "    unit_of_measurement: dBm",
            "  - platform: copy",
            "    source_id: wifi_rssi_db",
            "    name: \"WiFi Signal\"",
            "    unit_of_measurement: \"%\"",
            "    entity_category: diagnostic",
            "    filters:",
            "      - lambda: |-",
            "          if (x <= -100.0f) return 0.0f;",
            "          if (x >= -50.0f) return 100.0f;",
            "          return (x + 100.0f) * 2.0f;",
            "",
            "text_sensor:",
            "  - platform: wifi_info",
            "    ip_address:",
            "      name: \"WiFi IP Address\"",
            "      entity_category: diagnostic",
            "    ssid:",
            "      name: \"WiFi SSID\"",
            "      entity_category: diagnostic",
            "    bssid:",
            "      name: \"WiFi BSSID\"",
            "      entity_category: diagnostic",
            "    mac_address:",
            "      name: \"WiFi MAC Address\"",
            "      entity_category: diagnostic",
            "  - platform: version",
            "    name: \"ESPHome Version\"",
            "    entity_category: diagnostic",
            "",
            "button:",
            "  - platform: restart",
            "    name: \"ESP Restart\"",
            "    entity_category: diagnostic",
            "",
            "switch:",
            "  - platform: restart",
            "    name: \"ESP Restart Switch\"",
            "    entity_category: diagnostic",
            "",
    ]
    # flow_control_pin を modbus セクションへ挿入
    if flow_control_pin:
        try:
            sw = f"  send_wait_time: {send_wait_time_ms}ms"
            ins_idx = lines.index(sw) + 1
            lines.insert(ins_idx, f"  flow_control_pin: {flow_control_pin}")
        except ValueError:
            pass

    # 補助: 詳細ログを明示したい時だけ、modbus系の詳細ログを有効化
    if str(log_level).upper() == "VERY_VERBOSE":
        # Safely insert module-specific logs under the logger block
        try:
            logger_idx = lines.index("logger:")
            # Expect structure: logger:, level: ..., ""
            insert_idx = logger_idx + 2 if logger_idx + 2 <= len(lines) else len(lines)
            extra = [
                "  logs:",
                "    modbus: VERY_VERBOSE",
                "    modbus_controller: VERY_VERBOSE",
            ]
            # Insert before the trailing empty line of logger section if present
            if insert_idx < len(lines) and lines[insert_idx] == "":
                lines[insert_idx:insert_idx] = extra + [""]
            else:
                lines[insert_idx:insert_idx] = extra
        except ValueError:
            # Fallback: append at the end under a logger block if not found (should not happen)
            lines += [
                "logger:",
                "  level: VERY_VERBOSE",
                "  logs:",
                "    modbus: VERY_VERBOSE",
                "    modbus_controller: VERY_VERBOSE",
                "",
            ]
    # ESP32 のみ温度センサーを追加
    if (platform or "esp32").lower() == "esp32":
        try:
            sensor_idx = lines.index("sensor:")
            insert_idx = sensor_idx + 1
            while insert_idx < len(lines) and lines[insert_idx].startswith("  - platform:"):
                # 次のエンティティブロック先頭まで進める
                insert_idx += 1
                while insert_idx < len(lines) and (lines[insert_idx].startswith("    ") or lines[insert_idx] == ""):
                    insert_idx += 1
            lines[insert_idx:insert_idx] = [
                "  - platform: internal_temperature",
                "    name: \"ESP32 Temperature\"",
                "    entity_category: diagnostic",
                "    update_interval: 60s",
                "",
            ]
        except ValueError:
            pass
    # Insert controllers after 'modbus_controller:' line (just before empty line we added)
    idx = lines.index("modbus_controller:") + 1
    ctl_lines: List[str] = []
    if controllers is not None:
        for c in controllers:
            cid = c.get("id")
            upd = c.get("update_interval") or "5s"
            ctl_lines += [
                f"  - id: {cid}",
                f"    address: {slave_addr}",
                "    modbus_id: modbus1",
                f"    command_throttle: {command_throttle_ms}ms",
                f"    max_cmd_retries: {max_command_retries}",
                f"    offline_skip_updates: {offline_skip_updates}",
                f"    update_interval: {upd}",
            ]
    elif groups:
        for g in groups:
            ctl_lines += [
                f"  - id: srne_{g.lower()}",
                f"    address: {slave_addr}",
                "    modbus_id: modbus1",
                f"    command_throttle: {command_throttle_ms}ms",
                f"    max_cmd_retries: {max_command_retries}",
                f"    offline_skip_updates: {offline_skip_updates}",
                f"    update_interval: ${{{interval_var_name(g)}}}",
            ]
    else:
        ctl_lines += [
            "  - id: srne",
            f"    address: {slave_addr}",
            "    modbus_id: modbus1",
            f"    command_throttle: {command_throttle_ms}ms",
            f"    max_cmd_retries: {max_command_retries}",
            f"    offline_skip_updates: {offline_skip_updates}",
            "    update_interval: 5s",
        ]
    lines[idx:idx] = ctl_lines
    out = "\n".join(lines)
    if (platform or "esp32").lower() == "esp8266":
        # Replace esp32 header with esp8266 header
        needle = "esp32:\n  board: " + board + "\n  framework:\n    type: arduino\n\n"
        repl = "esp8266:\n  board: " + board + "\n\n"
        out = out.replace(needle, repl)
        # Disable serial logger on UART0 for ESP-01S (free UART for RS485)
        out = out.replace("\nlogger:\n\n", "\nlogger:\n  baud_rate: 0\n\n")
    return out

def _fault_code_switch_lines(indent: str = "") -> List[str]:
    # Shared fault-code labels used by P02/P10 derived text sensors.
    return [
        f"{indent}const char *code_text = \"Unknown\";",
        f"{indent}switch (code) {{",
        f"{indent}  case 1: code_text = \"Battery Undervoltage Alarm\"; break;",
        f"{indent}  case 2: code_text = \"Battery Discharge Overcurrent Protection (Software)\"; break;",
        f"{indent}  case 3: code_text = \"Battery Disconnection Fault\"; break;",
        f"{indent}  case 4: code_text = \"Battery End of Discharge Protection\"; break;",
        f"{indent}  case 5: code_text = \"Battery Overcurrent Protection (Hardware)\"; break;",
        f"{indent}  case 6: code_text = \"Battery Charge Overvoltage Protection\"; break;",
        f"{indent}  case 7: code_text = \"DC Link Overvoltage Protection (Hardware)\"; break;",
        f"{indent}  case 8: code_text = \"DC Link Overvoltage Protection (Software)\"; break;",
        f"{indent}  case 9: code_text = \"PV Input Overvoltage Protection\"; break;",
        f"{indent}  case 10: code_text = \"PV Boost Stage Overcurrent Protection (Software)\"; break;",
        f"{indent}  case 11: code_text = \"PV Boost Stage Overcurrent Protection (Hardware)\"; break;",
        f"{indent}  case 12: code_text = \"SPI Communication Fault\"; break;",
        f"{indent}  case 13: code_text = \"Bypass Output Overload Protection\"; break;",
        f"{indent}  case 14: code_text = \"Inverter Output Overload Protection\"; break;",
        f"{indent}  case 15: code_text = \"Inverter Output Overcurrent Protection (Hardware)\"; break;",
        f"{indent}  case 16: code_text = \"Slave DSP PWM Shutdown Request Fault\"; break;",
        f"{indent}  case 17: code_text = \"Inverter Output Short Circuit Protection\"; break;",
        f"{indent}  case 18: code_text = \"DC Link Soft Start Failure\"; break;",
        f"{indent}  case 19: code_text = \"MPPT Heatsink Overtemperature Protection\"; break;",
        f"{indent}  case 20: code_text = \"Inverter Heatsink Overtemperature Protection\"; break;",
        f"{indent}  case 21: code_text = \"Cooling Fan Failure Fault\"; break;",
        f"{indent}  case 22: code_text = \"EEPROM Memory Fault\"; break;",
        f"{indent}  case 23: code_text = \"Model Configuration Fault\"; break;",
        f"{indent}  case 24: code_text = \"DC Link Voltage Imbalance Fault\"; break;",
        f"{indent}  case 25: code_text = \"DC Link Short Circuit Protection\"; break;",
        f"{indent}  case 26: code_text = \"AC Output Relay Short Circuit Fault\"; break;",
        f"{indent}  case 28: code_text = \"AC Input Phase Sequence Error\"; break;",
        f"{indent}  case 29: code_text = \"DC Link Undervoltage Protection\"; break;",
        f"{indent}  case 30: code_text = \"Battery Capacity Low Alarm (10%)\"; break;",
        f"{indent}  case 31: code_text = \"Battery Capacity Critical Alarm (5%)\"; break;",
        f"{indent}  case 32: code_text = \"Battery Capacity Shutdown Protection\"; break;",
        f"{indent}  case 34: code_text = \"Parallel System CAN Communication Fault\"; break;",
        f"{indent}  case 35: code_text = \"Parallel System Address Configuration Fault\"; break;",
        f"{indent}  case 37: code_text = \"Parallel System Current Sharing Fault\"; break;",
        f"{indent}  case 38: code_text = \"Parallel System Battery Voltage Mismatch Fault\"; break;",
        f"{indent}  case 39: code_text = \"Parallel System AC Source Inconsistency Fault\"; break;",
        f"{indent}  case 40: code_text = \"Parallel System Hardware Synchronization Fault\"; break;",
        f"{indent}  case 41: code_text = \"Inverter DC Link Voltage Fault\"; break;",
        f"{indent}  case 42: code_text = \"Parallel System Firmware Version Mismatch Fault\"; break;",
        f"{indent}  case 43: code_text = \"Parallel System Connection Fault\"; break;",
        f"{indent}  case 44: code_text = \"Serial Number Configuration Fault\"; break;",
        f"{indent}  case 45: code_text = \"Split Phase Mode Configuration Fault\"; break;",
        f"{indent}  case 56: code_text = \"PV Insulation Resistance Fault\"; break;",
        f"{indent}  case 57: code_text = \"Residual Current Protection Fault\"; break;",
        f"{indent}  case 58: code_text = \"BMS Communication Fault\"; break;",
        f"{indent}  case 60: code_text = \"BMS Undervoltage Temperature Alarm\"; break;",
        f"{indent}  case 61: code_text = \"BMS Overtemperature Alarm\"; break;",
        f"{indent}  case 62: code_text = \"BMS Overcurrent Alarm\"; break;",
        f"{indent}  case 63: code_text = \"BMS Undervoltage Alarm\"; break;",
        f"{indent}  case 64: code_text = \"BMS Overvoltage Alarm\"; break;",
        f"{indent}  default: break;",
        f"{indent}}}",
    ]


def interval_var_name(group: str) -> str:
    return f"{group.lower()}_update_interval"

def interval_var_name_p03_writeonly() -> str:
    return "p03_writeonly_update_interval"

def interval_var_name_main() -> str:
    return "main_update_interval_s"

def skip_updates_var_name(group: str) -> str:
    return f"{group.lower()}_skip_updates"


def estimate_group_force_new_range_count(group: str, ranges: List[Dict[str, Any]], regs_map: Optional[Dict[int, Dict[str, Any]]] = None) -> int:
    """Estimate effective force_new_range boundary count used in generation for a group."""
    gnorm = normalize_group(group)
    boundary_addrs: set = set()
    try:
        for rng in (ranges or []):
            boundary_addrs.add(int(rng.get("start")))
    except Exception:
        boundary_addrs = set()

    try:
        if gnorm == "P10" and boundary_addrs:
            hints = load_generation_hints()
            p10_hints = (hints.get("groups") or {}).get("P10") or {}
            prefer_single_range = bool(p10_hints.get("prefer_single_range", False))
            split_hint_ids = p10_hints.get("split_hints_by_item_id") or []
            split_hint_addrs = resolve_split_hint_addresses("P10", split_hint_ids, regs_map or {})
            if prefer_single_range:
                min_addr = min(boundary_addrs)
                boundary_addrs = {min_addr} | split_hint_addrs
            else:
                boundary_addrs = set(boundary_addrs) | split_hint_addrs
    except Exception:
        pass

    return max(1, len(boundary_addrs))


def suggest_group_skip_defaults(
    groups: List[str],
    group_loads: Optional[Dict[str, int]] = None,
    group_force_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """Suggest balanced default skip_updates per group.

    Rules:
    - P01, P02: 0 (high-frequency read)
    - P03: 65535 (write-only)
    - Others: around 119, widened by per-group effective load and de-collided.
      effective load = address load + (force_new_range_count * 8)
    """
    fixed = {
        "P01": 0,
        "P02": 0,
        "P03": 65535,
    }
    out: Dict[str, int] = {}
    gset = sorted(set(normalize_group(g) for g in (groups or []) if normalize_group(g)))
    for g in gset:
        if g in fixed:
            out[g] = int(fixed[g])

    # Dynamic defaults for non-fixed groups.
    # Base: 119 (about 10 min at 5s main interval), then widen by load ratio.
    dyn_groups = [g for g in gset if g not in out]
    if dyn_groups:
        loads: Dict[str, int] = {}
        for g in dyn_groups:
            lv = 0
            fv = 0
            try:
                if group_loads and g in group_loads:
                    lv = int(group_loads[g])
            except Exception:
                lv = 0
            try:
                if group_force_counts and g in group_force_counts:
                    fv = int(group_force_counts[g])
            except Exception:
                fv = 0
            loads[g] = max(1, lv + max(0, fv) * 8)

        load_vals = sorted(loads.values())
        median_load = load_vals[len(load_vals) // 2] if load_vals else 1
        median_load = max(1, median_load)

        proposed: Dict[str, int] = {}
        for g in dyn_groups:
            ratio = loads[g] / float(median_load)
            # ratio=1.0 -> 119, heavier -> larger skip, lighter -> smaller skip
            cand = int(round(119 + (ratio - 1.0) * 24.0))
            cand = max(95, min(255, cand))
            # keep even spacing tendency
            if cand % 2 == 0:
                cand += 1
            proposed[g] = cand

        # De-collide: keep at least 2 between dynamic groups
        used = set(out.values())
        for g in sorted(dyn_groups, key=lambda x: (proposed[x], x)):
            v = proposed[g]
            while v in used:
                v += 2
            out[g] = v
            used.add(v)
    return out

def apply_group_skip_updates(
    yaml_text: str,
    group: str,
    force_new_range_addresses: Optional[set[int]] = None,
) -> str:
    """Apply the appropriate update cadence to every Modbus entity block."""
    gnorm = normalize_group(group)
    if not gnorm:
        return yaml_text
    lines = yaml_text.splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("  - platform: modbus_controller"):
            start = i
            i += 1
            while i < len(lines):
                l = lines[i]
                if l.startswith("  - platform:"):
                    break
                if l and not l.startswith("  "):
                    break
                i += 1
            block = lines[start:i]
            address = None
            for b in block:
                match = re.match(r"\s*address:\s*(\d+)\s*$", b)
                if match:
                    address = int(match.group(1))
                    break
            var_name = skip_updates_var_name(gnorm)
            if gnorm == "P02" and address in P02_SLOW_ADDRS:
                var_name = "p02_slow_skip_updates"
            skip_line = f"    skip_updates: ${{{var_name}}}"
            skip_indexes = [bi for bi, b in enumerate(block) if b.strip().startswith("skip_updates:")]
            if skip_indexes:
                block[skip_indexes[0]] = skip_line
            else:
                insert_at = 1
                for bi, b in enumerate(block):
                    if b.strip().startswith("register_type:"):
                        insert_at = bi + 1
                        break
                else:
                    for bi, b in enumerate(block):
                        if b.strip().startswith("modbus_controller_id:"):
                            insert_at = bi + 1
                            break
                block.insert(insert_at, skip_line)
            force_addrs = {524, 527} if force_new_range_addresses is None else force_new_range_addresses
            if gnorm == "P02" and address in force_addrs:
                if not any(b.strip().startswith("force_new_range:") for b in block):
                    address_index = next(
                        (bi for bi, b in enumerate(block) if b.strip().startswith("address:")),
                        len(block) - 1,
                    )
                    block.insert(address_index + 1, "    force_new_range: true")
            out.extend(block)
            continue
        out.append(line)
        i += 1
    return ("\n".join(out) + "\n") if yaml_text.endswith("\n") else "\n".join(out)


def coalesce_rw_group_reads(yaml_text: str, group: str) -> str:
    """Let ESPHome merge contiguous P05/P07 RW entities into catalog ranges."""
    if normalize_group(group) not in STANDARD_COALESCED_RW_GROUPS:
        return yaml_text
    lines = [line for line in yaml_text.splitlines() if line.strip() != "force_new_range: true"]
    return ("\n".join(lines) + "\n") if yaml_text.endswith("\n") else "\n".join(lines)


def split_ranges_at_boundaries(
    ranges: List[Dict[str, Any]], boundaries: set[int]
) -> List[Dict[str, Any]]:
    """Split packed ranges where different polling cadences must not be merged."""
    result: List[Dict[str, Any]] = []
    for source in ranges:
        start = int(source["start"])
        end = int(source["end"])
        cuts = sorted(boundary for boundary in boundaries if start < boundary <= end)
        segment_starts = [start, *cuts]
        segment_ends = [cut - 1 for cut in cuts] + [end]
        addresses = [int(address) for address in source.get("addresses", [])]
        for segment_start, segment_end in zip(segment_starts, segment_ends):
            segment_addresses = [
                address for address in addresses if segment_start <= address <= segment_end
            ]
            if not segment_addresses:
                continue
            result.append({
                "start": segment_start,
                "end": segment_end,
                "size": segment_end - segment_start + 1,
                "addresses": segment_addresses,
            })
    return result


def build_packed_read_ranges(
    ranges: List[Dict[str, Any]],
    regs_map: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep only contiguous, confirmed read-only addresses for packed anchors."""
    result: List[Dict[str, Any]] = []
    for source in ranges:
        segment: List[int] = []

        def flush() -> None:
            if not segment:
                return
            result.append({
                "start": segment[0],
                "end": segment[-1],
                "size": segment[-1] - segment[0] + 1,
                "addresses": list(segment),
            })
            segment.clear()

        for raw_address in source.get("addresses", []):
            address = int(raw_address)
            record = regs_map.get(address)
            rw = str((record or {}).get("rw", "R")).upper()
            is_read_only = record is not None and not rw.startswith(("RW", "W"))
            if not is_read_only:
                flush()
                continue
            if segment and address != segment[-1] + 1:
                flush()
            segment.append(address)
        flush()
    return result


def build_p10_packed_read_ranges(
    regs_map: Dict[int, Dict[str, Any]],
    max_chunk: int = 32,
) -> List[Dict[str, Any]]:
    """Build contiguous P10 fault-history reads from recipe-defined words."""
    available = set(int(address) for address in regs_map)
    addresses: set[int] = set()
    for recipe in load_derived_recipes().get("recipes", []):
        if normalize_group(recipe.get("group", "")) != "P10":
            continue
        requires = {int(address) for address in recipe.get("requires", []) or []}
        if requires and not requires.issubset(available):
            continue
        for numeric in recipe.get("numeric", []) or []:
            addresses.add(int(numeric["address"]))

    ordered = sorted(addresses)
    result: List[Dict[str, Any]] = []
    for offset in range(0, len(ordered), max_chunk):
        chunk = ordered[offset : offset + max_chunk]
        if not chunk:
            continue
        if chunk[-1] - chunk[0] + 1 != len(chunk):
            raise ValueError("P10 packed recipe addresses must be contiguous")
        result.append({
            "start": chunk[0],
            "end": chunk[-1],
            "size": len(chunk),
            "addresses": chunk,
        })
    return result


def apply_event_driven_templates(yaml_text: str, group: str) -> str:
    """Switch selected template text sensors from interval polling to on_value updates."""
    gnorm = normalize_group(group)
    if gnorm not in {"P00", "P05", "P09"}:
        return yaml_text

    interval_var = f"${{{interval_var_name(gnorm)}}}"
    lines = yaml_text.splitlines()
    remove_line_idx: set[int] = set()
    dep_to_templates: Dict[str, set[str]] = {}

    def _is_top_level_key(l: str) -> bool:
        return (not l.startswith(" ")) and bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*:\s*$", l))

    in_text_sensor = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "text_sensor:":
            in_text_sensor = True
            i += 1
            continue
        if in_text_sensor and _is_top_level_key(line):
            in_text_sensor = False
        if not in_text_sensor:
            i += 1
            continue

        if line.startswith("  - platform: template"):
            j = i + 1
            while j < len(lines):
                if lines[j].startswith("  - platform: "):
                    break
                if _is_top_level_key(lines[j]):
                    break
                j += 1
            block = lines[i:j]
            tid = None
            upd_idx = None
            block_text = "\n".join(block)
            for bi, bl in enumerate(block):
                s = bl.strip()
                if s.startswith("id: "):
                    tid = s.split(":", 1)[1].strip()
                if s == f"update_interval: {interval_var}":
                    upd_idx = i + bi
            if tid:
                if upd_idx is not None:
                    remove_line_idx.add(upd_idx)
                deps = set(re.findall(r"id\(([^)]+)\)\.state", block_text))
                for dep in deps:
                    dep_to_templates.setdefault(dep, set()).add(tid)
            i = j
            continue
        i += 1

    out = [ln for idx, ln in enumerate(lines) if idx not in remove_line_idx]

    # Add on_value hooks to source sensors that feed template text sensors.
    i = 0
    in_sensor = False
    while i < len(out):
        line = out[i]
        if line.strip() == "sensor:":
            in_sensor = True
            i += 1
            continue
        if in_sensor and _is_top_level_key(line):
            in_sensor = False
        if not in_sensor:
            i += 1
            continue

        if line.startswith("  - platform: "):
            j = i + 1
            while j < len(out):
                if out[j].startswith("  - platform: "):
                    break
                if _is_top_level_key(out[j]):
                    break
                j += 1
            block = out[i:j]
            sid = None
            has_on_value = False
            for bl in block:
                s = bl.strip()
                if s.startswith("id: "):
                    sid = s.split(":", 1)[1].strip()
                if s == "on_value:":
                    has_on_value = True
            if sid and sid in dep_to_templates and not has_on_value:
                inject = ["    on_value:", "      then:"]
                for tid in sorted(dep_to_templates.get(sid, set())):
                    inject.append(f"        - component.update: {tid}")
                out[j:j] = inject
                i = j + len(inject)
                continue
            i = j
            continue
        i += 1

    return ("\n".join(out) + "\n") if yaml_text.endswith("\n") else "\n".join(out)

def gen_anchor_sensor(range_idx: int, start: int, size: int, interval_var: str, gid: str) -> str:
    return "\n".join(
        [
            f"  # Anchor for {gid} range {range_idx} (0x{start:04X} size={size})",
            "  - platform: modbus_controller",
            f"    modbus_controller_id: srne_{gid.lower()}",
            f"    name: \"{gid} Anchor {range_idx}\"",
            "    internal: true",
            "    register_type: holding",
            "    force_new_range: true",
            f"    address: {start}",
            f"    register_count: {size}",
        ]
    )


def gen_sensor_entry(
    reg: Dict[str, Any],
    interval_var: str,
    name_registry: Dict[str, set] = None,
    controller_for_addr: Dict[int, str] = None,
    boundary_addrs: set = None,
) -> str:
    def yaml_escape(s: str) -> str:
        return s.replace('\\', r'\\').replace('"', r'\"')

    def c_escape(s: str) -> str:
        return s.replace('\\', r'\\').replace('"', r'\"')

    rw = (reg.get("rw") or "R").upper()
    enums = reg.get("enums")
    component = "sensor"
    if enums and rw.startswith("R") and not rw.startswith(("RW", "W")):
        component = "text_sensor"
    elif enums and (rw.startswith("RW") or rw.startswith("W")):
        # enums の 2値を switch 化するのは 0=disabled,1=enabled の場合だけ
        def _enabled_disabled_enums(e: Dict[str, str]) -> bool:
            v0 = None
            v1 = None
            for k, v in (e or {}).items():
                try:
                    iv = int(k, 10)
                except Exception:
                    try:
                        iv = int(k, 16)
                    except Exception:
                        return False
                if iv == 0:
                    v0 = str(v).strip().lower()
                elif iv == 1:
                    v1 = str(v).strip().lower()
                else:
                    return False
            return (v0 == "disabled") and (v1 == "enabled")
        if _enabled_disabled_enums(enums):
            component = "switch"
        else:
            component = "select"
    elif rw.startswith("RW") or rw.startswith("W"):
        component = "number"
    else:
        component = "sensor"

    # 型と読取レジスタ数、エンディアンの補助情報
    dt = reg.get("data_type")
    value_type = esphome_value_type(dt)
    reg_count = register_count_for_type(dt)
    endian = get_modbus_endianness()
    name = sensor_entity_name(reg.get("name") or f"Reg_{reg['address']}")
    # "Reserved" 等の汎用名は重複しやすいため、アドレスをサフィックス付与して一意化
    def _needs_suffix(n: str) -> bool:
        nl = (n or "").strip().lower()
        return nl in ("reserved", "unknown", "unused", "undefined", "n/a", "na", "none", "null") or nl.startswith("reserved_")
    if _needs_suffix(name):
        name = f"{name}_{int(reg['address'])}"
    unit = reg.get("unit") or ""
    # (Derived recipes are handled in generate_entities_group_yaml to avoid duplicate top-level keys)
    try:
        addr_ver = int(reg.get("address"))
    except Exception:
        addr_ver = None
    g_norm = normalize_group(reg.get("group", ""))
    multiplier = effective_multiplier(g_norm, addr_ver, reg.get("multiplier"))
    apply_total_counter_guard = (
        normalize_group(reg.get("group", "")) == "P09"
        and addr_ver in P09_TOTAL_COUNTER_GUARD_ADDRS
    )
    cat = entity_info_category(reg.get("group", ""), rw)
    # P00 Version raw values remain available for diagnostics; recipes provide formatted text.
    if g_norm == "P00" and addr_ver in P00_VERSION_ADDRS:
        cat = "diagnostic"
    # P00 の text_sensor で常用表示にしたい項目はカテゴリなし
    if normalize_group(reg.get("group", "")) == "P00" and addr_ver in (11,):
        cat = None

    lines: List[str] = []
    lines.append(f"  - platform: modbus_controller")
    # strictモードではアドレスに割り当てられたコントローラIDを優先
    try:
        addr_int = int(reg["address"])
    except Exception:
        addr_int = None
    if controller_for_addr and addr_int is not None and addr_int in controller_for_addr:
        lines.append(f"    modbus_controller_id: {controller_for_addr[addr_int]}")
    else:
        lines.append(f"    modbus_controller_id: {SINGLE_CONTROLLER_ID}")
    # 'select' では register_type は無効のため出力しない
    if component != "select":
        lines.append(f"    register_type: holding")
    lines.append(f"    address: {int(reg['address'])}")
    # 境界アドレスのみ force_new_range を付与（内部は自動マージでまとめ読み）
    if component != "select":
        try:
            aint = int(reg.get("address"))
        except Exception:
            aint = None
        if boundary_addrs and aint in boundary_addrs:
            lines.append(f"    force_new_range: true")
    # 以前の個別force指定は、全体でforce_new_rangeを付与する方針に統一したため削除
    # 32bit系センサーは、modbus_controllerの単発2語読取で不足応答が出る機器があるため
    # 低リスクな回避策として 2 語をそれぞれ 1 語センサーで読み、テンプレートで合成する
    if component == "sensor" and reg_count and reg_count > 1 and value_type in ("U_DWORD","S_DWORD","FP32"):
        # バッキング内部センサー（lo/hi）
        try:
            addr_i = int(reg.get("address"))
        except Exception:
            addr_i = None
        if addr_i is not None:
            lo_id = f"sens_{g_norm.lower()}_{addr_i}_lo"
            hi_id = f"sens_{g_norm.lower()}_{addr_i}_hi"
            back: List[str] = []
            back += [
                f"  - platform: modbus_controller",
                f"    id: {lo_id}",
                f"    modbus_controller_id: {controller_for_addr[addr_i] if (controller_for_addr and addr_i in controller_for_addr) else SINGLE_CONTROLLER_ID}",
                f"    internal: true",
                f"    register_type: holding",
                f"    address: {addr_i}",
                f"    value_type: U_WORD",
                f"    force_new_range: true",
            ]
            back += [
                f"  - platform: modbus_controller",
                f"    id: {hi_id}",
                f"    modbus_controller_id: {controller_for_addr[addr_i] if (controller_for_addr and addr_i in controller_for_addr) else SINGLE_CONTROLLER_ID}",
                f"    internal: true",
                f"    register_type: holding",
                f"    address: {addr_i+1}",
                f"    value_type: U_WORD",
                f"    force_new_range: true",
            ]
            # 仕上げのテンプレートセンサー
            fin: List[str] = []
            fin.append(f"  - platform: template")
            fin.append(f"    id: sens_{g_norm.lower()}_{addr_i}")
            # 出力名（重複回避）
            try:
                disp_name = sanitize_display_name(name)
                if name_registry is not None:
                    seen = name_registry.setdefault('sensor', set())
                    if disp_name in seen:
                        disp_name = f"{disp_name}_{addr_i}"
                    seen.add(disp_name)
            except Exception:
                disp_name = sanitize_display_name(name)
            fin.append(f"    name: \"{yaml_escape(disp_name)}\"")
            if g_norm == "P00" and addr_i not in P00_VERSION_ADDRS:
                fin.append("    internal: true")
            if unit:
                fin.append(f"    unit_of_measurement: \"{yaml_escape(unit)}\"")
            acc = infer_accuracy_decimals(unit, value_type, multiplier)
            if acc is not None:
                fin.append(f"    accuracy_decimals: {acc}")
            if cat:
                fin.append(f"    entity_category: {cat}")
            # device_class/state_classの付与
            try:
                u = (unit or "").strip()
                dev_cls = None
                st_cls = None
                if u in ("W", "kW"):
                    dev_cls = "power"; st_cls = "measurement"
                elif u in ("Wh", "kWh"):
                    dev_cls = "energy"; st_cls = "total_increasing"
                elif u == "V":
                    dev_cls = "voltage"; st_cls = "measurement"
                elif u == "A":
                    dev_cls = "current"; st_cls = "measurement"
                elif u == "Hz":
                    dev_cls = "frequency"; st_cls = "measurement"
                elif u in ("°C", "°F"):
                    dev_cls = "temperature"; st_cls = "measurement"
                elif u == "VA":
                    dev_cls = "apparent_power"; st_cls = "measurement"
                if dev_cls:
                    fin.append(f"    device_class: {dev_cls}")
                if st_cls:
                    fin.append(f"    state_class: {st_cls}")
            except Exception:
                pass
            # 合成ロジック
            fin.append("    lambda: |-")
            if value_type == "FP32":
                fin += [
                    f"      uint32_t lo = (uint32_t) id({lo_id}).state;",
                    f"      uint32_t hi = (uint32_t) id({hi_id}).state;",
                    f"      uint32_t u32 = (hi << 16) | (lo & 0xFFFF);",
                    f"      float f; memcpy(&f, &u32, sizeof(float));",
                    f"      return f * {multiplier};" if abs(multiplier-1.0)>1e-12 else "      return f;",
                ]
            elif value_type == "S_DWORD":
                fin += [
                    f"      uint32_t lo = (uint32_t) id({lo_id}).state;",
                    f"      uint32_t hi = (uint32_t) id({hi_id}).state;",
                    f"      uint32_t u32 = (hi << 16) | (lo & 0xFFFF);",
                    f"      int32_t i32 = (u32 & 0x80000000) ? (int32_t)(u32 - 0x100000000ULL) : (int32_t)u32;",
                    f"      return (float)i32 * {multiplier};" if abs(multiplier-1.0)>1e-12 else "      return (float)i32;",
                ]
            else:  # U_DWORD
                fin += [
                    f"      uint32_t lo = (uint32_t) id({lo_id}).state;",
                    f"      uint32_t hi = (uint32_t) id({hi_id}).state;",
                    f"      uint32_t u32 = (hi << 16) | (lo & 0xFFFF);",
                ]
                if apply_total_counter_guard:
                    fin += [
                        "      // Guard against reboot-time spikes for long-term total counters.",
                        "      static bool init_done = false;",
                        "      static uint32_t last_u32 = 0;",
                        "      if (millis() < 60000U) { return {}; }",
                        "      if (!init_done) {",
                        "        init_done = true;",
                        "        last_u32 = u32;",
                        "        return {};",
                        "      }",
                        "      if (u32 < last_u32) { return {}; }",
                        "      uint32_t delta = u32 - last_u32;",
                        "      if (delta > 50000U) { return {}; }",
                        "      last_u32 = u32;",
                    ]
                fin.append(
                    f"      return (float)u32 * {multiplier};" if abs(multiplier-1.0)>1e-12 else "      return (float)u32;"
                )
            # 予約名のフィルタリングなどは従来通り
            return "\n".join(back + fin)

    # 32bit等の複数レジスタを要する型は register_count を明示（上の合成ルートに該当しないケース）
    if component != "select" and reg_count and reg_count > 1:
        lines.append(f"    register_count: {reg_count}")
    # 値型を明示（U_WORD/U_DWORD/S_WORD/S_DWORD/FP32）
    # text_sensor/select は value_type を受け付けないため除外
    if component in ("sensor", "number"):
        lines.append(f"    value_type: {value_type}")
    # エンディアン（word順）は実装側の参考としてコメントで付加
    lines.append(f"    # endianness: register={endian.get('register','big')}, word={endian.get('word','little')}")
    # No per-sensor update_interval; updates are driven by anchor interval automation

    # 名前の一意化（同一platform内で重複させない）
    eff_name = name
    if name_registry is not None:
        plat_key = component
        seen = name_registry.setdefault(plat_key, set())
        if eff_name in seen:
            eff_name = f"{eff_name}_{int(reg['address'])}"
        seen.add(eff_name)
    eff_name = sanitize_display_name(eff_name)
    if g_norm == "P00" and addr_ver in P00_VERSION_ADDRS:
        eff_name = f"{eff_name}_Raw"

    # coalesceを許可（個別force指定は付けない）
    if component == "sensor":
        # Assign a stable id based on group/address for referencing in template sensors
        try:
            addr_i = int(reg.get("address"))
        except Exception:
            addr_i = None
        if addr_i is not None:
            lines.append(f"    id: sens_{g_norm.lower()}_{addr_i}")
            # P00の生値センサーは表示用text_sensorへ渡すためHAには公開しない
            if g_norm == "P00" and addr_i not in P00_VERSION_ADDRS:
                lines.append("    internal: true")
        lines.append(f"    name: \"{yaml_escape(eff_name)}\"")
        if g_norm == "P00" and addr_i in P00_VERSION_ADDRS:
            lines.append("    disabled_by_default: true")
        if unit:
            lines.append(f"    unit_of_measurement: \"{yaml_escape(unit)}\"")
        acc = infer_accuracy_decimals(unit, value_type, multiplier)
        if acc is not None:
            lines.append(f"    accuracy_decimals: {acc}")
        # まず倍率を適用（multiplyフィルタのみ適用）
        if multiplier != 1.0:
            lines.append("    filters:")
            lines.append(f"      - multiply: {multiplier}")
            if g_norm == "P00" and addr_i in P00_VERSION_ADDRS:
                lines.append("      - round: 2")
        # フィルタ（外れ値/median）はユーザー要望により現時点では適用しない
        # Runtime/SOC系のスムージング・レート制限は適用しない
        # 外れ値・スパイク対策は適用しない（ユーザー要望により削除）
        # device_class/state_class の自動付与（HAの統計/ダッシュボード互換）
        # 基本は測定値: measurement。総量系（kWh/Wh）は total_increasing。
        try:
            u = (unit or "").strip()
            dev_cls = None
            st_cls = None
            if u in ("W", "kW"):
                dev_cls = "power"; st_cls = "measurement"
            elif u in ("Wh", "kWh"):
                dev_cls = "energy"; st_cls = "total_increasing"
            elif u == "V":
                dev_cls = "voltage"; st_cls = "measurement"
            elif u == "A":
                dev_cls = "current"; st_cls = "measurement"
            elif u == "Hz":
                dev_cls = "frequency"; st_cls = "measurement"
            elif u in ("°C", "°F"):
                dev_cls = "temperature"; st_cls = "measurement"
            elif u == "VA":
                dev_cls = "apparent_power"; st_cls = "measurement"
            # % や独自単位は無指定（統計要件を避ける）
            if dev_cls:
                lines.append(f"    device_class: {dev_cls}")
            if st_cls:
                lines.append(f"    state_class: {st_cls}")
        except Exception:
            pass
    elif component == "text_sensor":
        lines.append(f"    name: \"{yaml_escape(eff_name)}\"")
        lines.append("    lambda: |-")
        lines.append("      int v = 0;")
        lines.append("      if (!x.empty()) v = atoi(x.c_str());")
        enums: Dict[str, str] = reg.get("enums") or {}
        lines.append("      switch (v) {")
        for k, lbl in enums.items():
            try:
                iv = int(k, 10)
            except Exception:
                try:
                    iv = int(k, 16)
                except Exception:
                    continue
            lines.append(f"        case {iv}: return std::string(\"{c_escape(lbl)}\");")
        lines.append("        default: return std::string(\"unknown\");")
        lines.append("      }")
    elif component == "number":
        lines.append(f"    name: \"{yaml_escape(eff_name)}\"")
        if unit:
            lines.append(f"    unit_of_measurement: \"{yaml_escape(unit)}\"")
        if cat:
            lines.append(f"    entity_category: {cat}")
        # JSONの任意フィールド min/max/step を尊重（存在すれば出力）
        try:
            _min = reg.get("min")
            _max = reg.get("max")
            _step = reg.get("step")
        except Exception:
            _min = _max = _step = None
        g_norm_local = normalize_group(reg.get("group", ""))
        p05_voltage_mode = (g_norm_local == "P05") and ((unit or "").strip() == "V")
        base_min = _min
        base_max = _max
        if p05_voltage_mode:
            # UI入力は細かく受け付けるためstepは0.1V固定。
            _step = 0.1
            # 動的電圧系（12/24/36/48）をすべて許容するため、表示範囲は広めに確保。
            try:
                if _min is not None:
                    _min = float(_min) * 1.0
            except Exception:
                pass
            try:
                if _max is not None:
                    _max = float(_max) * 4.0
            except Exception:
                pass
        def _fmt_num(v):
            try:
                if isinstance(v, bool) or v is None:
                    return None
                iv = int(v)
                if float(v) == float(iv):
                    return str(iv)
                return str(float(v))
            except Exception:
                return None
        # Emit bounds only if valid (max > min). If equal/invalid, omit to avoid ESPHome validation error.
        fv_min = _fmt_num(_min) if _min is not None else None
        fv_max = _fmt_num(_max) if _max is not None else None
        valid_bounds = True
        try:
            if fv_min is not None and fv_max is not None:
                valid_bounds = float(fv_max) > float(fv_min)
        except Exception:
            valid_bounds = False
        if valid_bounds:
            if fv_min is not None:
                lines.append(f"    min_value: {fv_min}")
            if fv_max is not None:
                lines.append(f"    max_value: {fv_max}")
        if _step is not None:
            fv = _fmt_num(_step)
            if fv is not None:
                lines.append(f"    step: {fv}")
        # 特例: P05 の設定電圧（Unit=V）は Battery_Rated_Voltage(57347) に応じて動的換算
        if p05_voltage_mode:
            inv_scale = (1.0 / multiplier) if multiplier not in (0, 0.0) else 1.0
            min_expr = "NAN"
            max_expr = "NAN"
            try:
                if base_min is not None:
                    min_expr = str(float(base_min))
            except Exception:
                min_expr = "NAN"
            try:
                if base_max is not None:
                    max_expr = str(float(base_max))
            except Exception:
                max_expr = "NAN"
            lines.append("    lambda: |-")
            lines.append("      if (id(sel_p05_57347).current_option().empty()) return NAN;")
            lines.append("      int rv = atoi(id(sel_p05_57347).current_option().c_str());")
            lines.append("      if (!(rv == 12 || rv == 24 || rv == 36 || rv == 48)) return NAN;")
            lines.append("      float ratio = ((float) rv) / 12.0f;")
            lines.append(f"      return x * {multiplier} * ratio;")
            lines.append("    write_lambda: |-")
            lines.append("      if (id(sel_p05_57347).current_option().empty()) {")
            lines.append("        ESP_LOGW(\"p05_voltage\", \"Battery_Rated_Voltage unavailable, block write\");")
            lines.append("        return {};")
            lines.append("      }")
            lines.append("      int rv = atoi(id(sel_p05_57347).current_option().c_str());")
            lines.append("      if (!(rv == 12 || rv == 24 || rv == 36 || rv == 48)) {")
            lines.append("        ESP_LOGW(\"p05_voltage\", \"Battery_Rated_Voltage invalid (%d), block write\", rv);")
            lines.append("        return {};")
            lines.append("      }")
            lines.append("      float ratio = ((float) rv) / 12.0f;")
            lines.append(f"      float min_v = {min_expr} * ratio;")
            lines.append(f"      float max_v = {max_expr} * ratio;")
            lines.append("      if (!isnan(min_v) && x < min_v) {")
            lines.append("        ESP_LOGW(\"p05_voltage\", \"Input %.3fV below min %.3fV, block write\", x, min_v);")
            lines.append("        return {};")
            lines.append("      }")
            lines.append("      if (!isnan(max_v) && x > max_v) {")
            lines.append("        ESP_LOGW(\"p05_voltage\", \"Input %.3fV above max %.3fV, block write\", x, max_v);")
            lines.append("        return {};")
            lines.append("      }")
            lines.append("      id(p05_refresh_after_write).execute();")
            lines.append(f"      return (int) floorf(x / ratio * {inv_scale});")
        else:
            # 一般の数値: 倍率のみ適用
            if multiplier != 1.0:
                lines.append("    lambda: |-")
                lines.append(f"      return x * {multiplier};")
            lines.append("    write_lambda: |-")
            if g_norm_local == "P07":
                lines.append("      id(p07_refresh_after_write).execute();")
            if g_norm_local == "P05":
                lines.append("      id(p05_refresh_after_write).execute();")
            inv = 1.0 / multiplier if multiplier not in (0, 0.0) else 1.0
            lines.append(f"      return (int) round(x * {inv});")
    elif component == "select":
        try:
            lines.append(f"    id: sel_{normalize_group(reg.get('group', '')).lower()}_{int(reg['address'])}")
        except Exception:
            pass
        lines.append(f"    name: \"{yaml_escape(eff_name)}\"")
        # 結合読取は抑止（選択肢更新の確実性重視）
        lines.append(f"    force_new_range: true")
        if cat:
            lines.append(f"    entity_category: {cat}")
        # P05/P07 の select 変更時にも fast-poll を起動
        sg = normalize_group(reg.get("group", ""))
        if sg in ("P05", "P07"):
            lines.append("    optimistic: true")
            lines.append("    on_value:")
            lines.append("      - lambda: |-")
            lines.append("          static std::string prev = \"\";")
            lines.append("          static bool initialized = false;")
            lines.append("          if (!initialized) {")
            lines.append("            initialized = true;")
            lines.append("            prev = x;")
            lines.append("            return;")
            lines.append("          }")
            lines.append("          if (prev == x) return;")
            lines.append("          prev = x;")
            lines.append(f"          id({sg.lower()}_refresh_after_write).execute();")
        lines.append("    optionsmap:")
        # select.modbus_controller は optionsmap が必須（enumベース）
        enums: Dict[str, str] = reg.get("enums") or {}
        for k, lbl in enums.items():
            # 値は数値コードに変換、キーはラベル文字列
            try:
                iv = int(k, 10)
            except Exception:
                try:
                    iv = int(k, 16)
                except Exception:
                    continue
            lines.append(f"      \"{yaml_escape(lbl)}\": {iv}")
    elif component == "switch":
        # Switch as template switch; use lambda to write register (no built-in action exists)
        out: List[str] = []
        out.append(f"  - platform: template")
        out.append(f"    name: \"{yaml_escape(name)}\"")
        if cat:
            out.append(f"    entity_category: {cat}")
        out.append(f"    optimistic: true")
        out.append(f"    restore_mode: DISABLED")
        out.append(f"    turn_on_action:")
        if g_norm == "P07":
            out.append(f"      - lambda: |-")
            out.append(f"          id(p07_refresh_after_write).execute();")
        out.append(f"      - lambda: |-")
        g_norm = normalize_group(reg.get("group", ""))
        try:
            _addr_i = int(reg.get("address"))
        except Exception:
            _addr_i = None
        _w_mc = SINGLE_CONTROLLER_ID
        if controller_for_addr and _addr_i is not None and _addr_i in controller_for_addr:
            try:
                _w_mc = str(controller_for_addr[_addr_i])
            except Exception:
                _w_mc = SINGLE_CONTROLLER_ID
        out.append(f"          auto controller = id({_w_mc});")
        out.append("          controller->queue_command(")
        out.append("              esphome::modbus_controller::ModbusCommandItem::create_write_single_command(")
        out.append(f"                  controller, {int(reg['address'])}, 1));")
        out.append(f"    turn_off_action:")
        if g_norm == "P07":
            out.append(f"      - lambda: |-")
            out.append(f"          id(p07_refresh_after_write).execute();")
        out.append(f"      - lambda: |-")
        out.append(f"          auto controller = id({_w_mc});")
        out.append("          controller->queue_command(")
        out.append("              esphome::modbus_controller::ModbusCommandItem::create_write_single_command(")
        out.append(f"                  controller, {int(reg['address'])}, 0));")
        return "\n".join(out)

    if cat and component in ("sensor", "text_sensor"):
        lines.append(f"    entity_category: {cat}")

    return "\n".join(lines)


def generate_anchors_group_yaml(group: str, ranges: List[Dict[str, Any]], strict: bool = False, regs_map: Dict[int, Dict[str, Any]] = None, packed: bool = False, disable: bool = False) -> str:
    # disable=True の場合はアンカーを生成せず、モドバスコントローラの結合読取に任せる
    if disable:
        return "# Anchors disabled; rely on ModbusController's coalesced block reads.\n"
    interval_var = interval_var_name(group)
    lines: List[str] = ["sensor:"]
    gnorm = normalize_group(group)
    packed_raw_enum_addrs: set[int] = set()
    packed_skip_base_addrs: set[int] = set()
    packed_numeric_sink_ids: Dict[int, List[str]] = {}
    packed_numeric_meta: Dict[int, Dict[str, Any]] = {}
    if packed:
        try:
            for recipe in load_derived_recipes().get("recipes", []):
                if normalize_group(recipe.get("group", "")) != gnorm:
                    continue
                for address in recipe.get("skip_base_addresses", []) or []:
                    packed_skip_base_addrs.add(int(address))
                for numeric in recipe.get("numeric", []) or []:
                    address = int(numeric["address"])
                    sink_id = str(numeric.get("id") or "").strip()
                    if sink_id:
                        packed_numeric_sink_ids.setdefault(address, []).append(sink_id)
                    packed_numeric_meta[address] = {
                        "rw": "R",
                        "data_type": numeric.get("value_type", "U_WORD"),
                        "multiplier": numeric.get("multiplier", 1.0),
                    }
                text_from_numeric = recipe.get("text_from_numeric") or {}
                if text_from_numeric.get("address") is not None:
                    packed_raw_enum_addrs.add(int(text_from_numeric["address"]))
        except Exception:
            packed_raw_enum_addrs = set()
            packed_skip_base_addrs = set()
            packed_numeric_sink_ids = {}
            packed_numeric_meta = {}
    # strict=false/packed=false: 従来のグループ単位アンカー
    if not strict and not packed:
        for i, rng in enumerate(ranges):
            start = int(rng["start"])
            size = int(rng["size"])
            # 末尾が32bitの場合、+1したいが32超は避ける
            try:
                addrs = rng.get("addresses") or []
                if addrs:
                    last = int(addrs[-1])
                    if regs_map:
                        rec = regs_map.get(last)
                        if rec:
                            vt = esphome_value_type(rec.get("data_type"))
                            if vt in ("U_DWORD", "S_DWORD", "FP32"):
                                ext = (last - start + 1) + 1
                                size = ext if ext <= 32 else size
            except Exception:
                pass
            lines.append(gen_anchor_sensor(i + 1, start, size, interval_var, group))
        return "\n".join(lines) + "\n"

    # Packed anchors use explicit ranges on the shared controller. Strict mode
    # retains the legacy per-range controller topology for compatibility.
    for i, rng in enumerate(ranges):
        start = int(rng["start"])
        end = int(rng["end"])
        size = int(rng["size"])
        # 末尾32bitなら+1（ただし32超は回避）
        try:
            addrs = rng.get("addresses") or []
            if addrs:
                last = int(addrs[-1])
                if regs_map:
                    rec = regs_map.get(last)
                    if rec:
                        vt = esphome_value_type(rec.get("data_type"))
                        if vt in ("U_DWORD", "S_DWORD", "FP32"):
                            ext = (last - start + 1) + 1
                            if ext <= 32:
                                size = ext
        except Exception:
            pass
        mc_id = SINGLE_CONTROLLER_ID if packed else f"srne_{gnorm.lower()}_r{start}_{end}"
        # アンカー行を直接構築（ブロック専用コントローラに紐付け）
        lines += [
            f"  # Anchor for {gnorm} range {i+1} (0x{start:04X}-0x{end:04X}, size={size})",
            "  - platform: modbus_controller",
            f"    modbus_controller_id: {mc_id}",
            f"    name: \"{gnorm} Anchor {i+1}\"",
            "    internal: true",
            "    register_type: holding",
            "    force_new_range: true",
            f"    address: {start}",
            f"    register_count: {size}",
        ]
        if packed:
            # ブロック一括読取結果をテンプレートセンサーへ配布
            def _cpp_escape(s: str) -> str:
                return s.replace('\\\\', r'\\\\').replace('"', r'\\\"')
            addrs = rng.get("addresses") or []
            lines.append("    lambda: |-")
            lines.append("      // Distribute 16-bit words to per-register template sensors")
            for a in addrs:
                try:
                    aa = int(a)
                except Exception:
                    continue
                rec = packed_numeric_meta.get(aa) or (regs_map or {}).get(aa)
                if not rec:
                    continue
                rw = str(rec.get("rw", "R")).upper()
                if rw.startswith("RW") or rw.startswith("W"):
                    # 書込み系はここでは配布しない
                    continue
                vt = esphome_value_type(rec.get("data_type"))
                mult = effective_multiplier(gnorm, aa, rec.get("multiplier"))
                idx = aa - start
                byte_idx = idx * 2
                sink_ids = [f"sens_{gnorm.lower()}_{aa}"]
                if aa in packed_numeric_sink_ids:
                    sink_ids = packed_numeric_sink_ids.get(aa, [])

                def _publish(expression: str, condition: str = "") -> None:
                    for sink_id in sink_ids:
                        prefix = f"{condition} " if condition else ""
                        lines.append(f"        {prefix}id({sink_id}).publish_state({expression});")

                enums = rec.get("enums") or None
                if enums:
                    lines.append(f"      if ((size_t){byte_idx + 1} < data.size()) {{")
                    lines.append(f"        uint16_t v = ((uint16_t)data[{byte_idx}] << 8) | data[{byte_idx + 1}];")
                    if aa in packed_raw_enum_addrs:
                        lines.append(f"        id(sens_{gnorm.lower()}_{aa}).publish_state((float) v);")
                        lines.append("      }")
                        continue
                    lines.append("        switch (v) {")
                    for k, lbl in (enums or {}).items():
                        try:
                            iv = int(k, 10)
                        except Exception:
                            try:
                                iv = int(k, 16)
                            except Exception:
                                continue
                        lines.append(f"          case {iv}: id(txt_{gnorm.lower()}_{aa}).publish_state(\"{_cpp_escape(str(lbl))}\"); break;")
                    lines.append("          default: id(txt_" + gnorm.lower() + f"_{aa}).publish_state(\"unknown\"); break;")
                    lines.append("        }")
                    lines.append("      }")
                    continue
                # 数値センサー
                if vt in ("U_WORD", "S_WORD"):
                    lines.append(f"      if ((size_t){byte_idx + 1} < data.size()) {{")
                    if vt == "S_WORD":
                        lines.append(f"        int16_t raw = (int16_t)(((uint16_t)data[{byte_idx}] << 8) | data[{byte_idx + 1}]);")
                        if abs(mult - 1.0) < 1e-12:
                            _publish("(float) raw")
                        else:
                            _publish(f"(float) raw * {mult}")
                    else:
                        lines.append(f"        uint16_t raw = ((uint16_t)data[{byte_idx}] << 8) | data[{byte_idx + 1}];")
                        if gnorm == "P09" and aa in P09_TOTAL_COUNTER_GUARD_ADDRS:
                            lines += [
                                "        // Packed reads are atomic; publish the first complete value, then reject regressions/spikes.",
                                "        static bool init_done = false;",
                                "        static uint32_t last_raw = 0;",
                                "        bool accept = false;",
                                "        if (!init_done) {",
                                "          init_done = true;",
                                "          last_raw = raw;",
                                "          accept = true;",
                                "        } else if (raw >= last_raw && ((uint32_t)raw - last_raw) <= 50000U) {",
                                "          last_raw = raw;",
                                "          accept = true;",
                                "        }",
                            ]
                            value = "(float) raw" if abs(mult - 1.0) < 1e-12 else f"(float) raw * {mult}"
                            _publish(value, "if (accept)")
                        elif abs(mult - 1.0) < 1e-12:
                            _publish("(float) raw")
                        else:
                            _publish(f"(float) raw * {mult}")
                    lines.append("      }")
                elif vt in ("U_DWORD", "S_DWORD", "FP32"):
                    lines.append(f"      if ((size_t){byte_idx + 3} < data.size()) {{")
                    lines.append(f"        uint32_t lo = ((uint32_t)data[{byte_idx}] << 8) | data[{byte_idx + 1}];")
                    lines.append(f"        uint32_t hi = ((uint32_t)data[{byte_idx + 2}] << 8) | data[{byte_idx + 3}];")
                    lines.append(f"        uint32_t u32 = (hi << 16) | lo;")
                    if vt == "FP32":
                        lines.append("        float fval = 0.0f;")
                        lines.append("        memcpy(&fval, &u32, sizeof(float));")
                        if abs(mult - 1.0) < 1e-12:
                            _publish("fval")
                        else:
                            _publish(f"fval * {mult}")
                    elif vt == "S_DWORD":
                        lines.append("        int32_t i32 = (u32 & 0x80000000) ? (int32_t)(u32 - 0x100000000ULL) : (int32_t)u32;")
                        if abs(mult - 1.0) < 1e-12:
                            _publish("(float) i32")
                        else:
                            _publish(f"(float) i32 * {mult}")
                    else:
                        if gnorm == "P09" and aa in P09_TOTAL_COUNTER_GUARD_ADDRS:
                            lines += [
                                "        // Packed reads are atomic; publish the first complete value, then reject regressions/spikes.",
                                "        static bool init_done = false;",
                                "        static uint32_t last_u32 = 0;",
                                "        bool accept = false;",
                                "        if (!init_done) {",
                                "          init_done = true;",
                                "          last_u32 = u32;",
                                "          accept = true;",
                                "        } else if (u32 >= last_u32 && (u32 - last_u32) <= 50000U) {",
                                "          last_u32 = u32;",
                                "          accept = true;",
                                "        }",
                            ]
                            value = "(float) u32" if abs(mult - 1.0) < 1e-12 else f"(float) u32 * {mult}"
                            _publish(value, "if (accept)")
                        elif abs(mult - 1.0) < 1e-12:
                            _publish("(float) u32")
                        else:
                            _publish(f"(float) u32 * {mult}")
                    lines.append("      }")
            lines.append("      return 0;")
    rendered = "\n".join(lines) + "\n"
    rendered = apply_event_driven_templates(rendered, group)
    return apply_group_skip_updates(
        rendered,
        group,
    )


def generate_entities_group_yaml(
    group: str,
    ranges: List[Dict[str, Any]],
    regs_map: Dict[int, Dict[str, Any]],
    controller_for_addr: Dict[int, str] = None,
    packed: bool = False,
) -> str:
    interval_var = interval_var_name(group)
    lines: List[str] = []
    group_controller_ids: List[str] = [SINGLE_CONTROLLER_ID]
    try:
        if controller_for_addr:
            ids = sorted(set(str(v) for v in controller_for_addr.values() if v))
            if ids:
                group_controller_ids = ids
    except Exception:
        pass
    polled_addrs: set = set()
    try:
        for rng in ranges:
            for a in (rng.get("addresses") or []):
                polled_addrs.add(int(a))
    except Exception:
        polled_addrs = set()
    packed_read_addrs = {
        address
        for address in polled_addrs
        if address in regs_map
        and not str(regs_map[address].get("rw", "R")).upper().startswith(("RW", "W"))
    }
    if packed and normalize_group(group) == "P10":
        available = set(regs_map)
        for recipe in load_derived_recipes().get("recipes", []):
            if normalize_group(recipe.get("group", "")) != "P10":
                continue
            requires = {int(address) for address in recipe.get("requires", []) or []}
            if requires and not requires.issubset(available):
                continue
            packed_read_addrs.update(
                int(numeric["address"])
                for numeric in recipe.get("numeric", []) or []
            )
    group_regs = [regs_map[a] for rng in ranges for a in rng["addresses"] if a in regs_map]
    skipped_base_addrs: set = set()
    custom_internal_number_addrs: set = set()

    g_norm = normalize_group(group)
    group_primary_controller = group_controller_ids[0] if group_controller_ids else SINGLE_CONTROLLER_ID


    # P03 (Device Control) ボタン化はレシピで有効時のみ実行
    if g_norm == "P03":
        try:
            recipes = load_derived_recipes().get("recipes", [])
            enabled = any(normalize_group(r.get("group",""))=="P03" and r.get("auto_write_only_buttons") for r in recipes)
        except Exception:
            enabled = False
        if enabled:
            w_only = [r for r in group_regs if str(r.get("rw","R")).upper().startswith("W")]
            if w_only:
                lines.append("button:")
                btn_registry: Dict[str, set] = {}
                for r in w_only:
                    lines.append(gen_button_for_wreg(
                        r,
                        btn_registry,
                        direct_write=True,
                        controller_id=group_primary_controller,
                    ))
                # Guard against unintended writes during early boot.
                lines.append("")
                lines.append("globals:")
                lines.append("  - id: p03_write_actions_enabled")
                lines.append("    type: bool")
                lines.append("    restore_value: no")
                lines.append("    initial_value: 'false'")
                lines.append("")
                lines.append("interval:")
                lines.append("  - interval: 1s")
                lines.append("    then:")
                lines.append("      - lambda: |-")
                lines.append("          if (!id(p03_write_actions_enabled) && millis() > 3000U) id(p03_write_actions_enabled) = true;")
                rendered = "\n".join(lines) + "\n"
                rendered = apply_event_driven_templates(rendered, group)
                return apply_group_skip_updates(rendered, group)

    def _is_enabled_disabled_enums(e: Dict[str, str]) -> bool:
        v0 = None
        v1 = None
        for k, v in (e or {}).items():
            try:
                iv = int(k, 10)
            except Exception:
                try:
                    iv = int(k, 16)
                except Exception:
                    return False
            if iv == 0:
                v0 = str(v).strip().lower()
            elif iv == 1:
                v1 = str(v).strip().lower()
            else:
                return False
        return (v0 == "disabled") and (v1 == "enabled")

    def _is_bool_like_rw_no_enums(r: Dict[str, Any]) -> bool:
        # ルール（強化版）:
        # - 対象: P05/P07/P08 のみ
        # - RW かつ enums なし
        # - 倍率=1、unitが数値型（空 or %d）
        # - かつ min/max が (0,1) であること（CSV反映済みの上下限を厳密条件とする）
        group = normalize_group(r.get("group", ""))
        if group not in ("P05", "P07", "P08"):
            return False
        rwv = str(r.get("rw", "R")).upper()
        if not rwv.startswith("RW"):
            return False
        if r.get("enums"):
            return False
        try:
            mult = float(r.get("multiplier") or 1.0)
        except Exception:
            mult = 1.0
        unit = (r.get("unit") or "").strip()
        # 厳格: min/max で 0/1 の定義がある場合のみtrue
        mn = r.get("min")
        mx = r.get("max")
        if not (mn in (0, 0.0) and mx in (1, 1.0)):
            return False
        if abs(mult - 1.0) > 1e-9:
            return False
        if unit not in ("", "%d"):
            return False
        return True

    buckets = {
        "sensor": [r for r in group_regs if not r.get("enums") and not str(r.get("rw","R")).upper().startswith(("RW","W"))],
        "text_sensor": [r for r in group_regs if r.get("enums") and not str(r.get("rw","R")).upper().startswith(("RW","W"))],
        "number": [r for r in group_regs if ((not r.get("enums")) and str(r.get("rw","R")).upper().startswith(("RW","W")) and not _is_bool_like_rw_no_enums(r))],
        "select": [r for r in group_regs if (r.get("enums") and str(r.get("rw","R")).upper().startswith(("RW","W")) and not _is_enabled_disabled_enums(r.get("enums") or {}))],
        "switch": [r for r in group_regs if ((r.get("enums") and str(r.get("rw","R")).upper().startswith("RW") and _is_enabled_disabled_enums(r.get("enums") or {})) or _is_bool_like_rw_no_enums(r))],
    }
    # Apply skip_base_addresses before default sensor generation to avoid duplicate public sensors.
    pre_skip_base_addrs: set = set()
    try:
        recipes_pre = load_derived_recipes().get("recipes", [])
        addrs_pre = set(regs_map.keys() if regs_map else [])
        for rec_pre in recipes_pre:
            if normalize_group(rec_pre.get("group", "")) != g_norm:
                continue
            req_pre = rec_pre.get("requires") or []
            try:
                req_set_pre = set(int(a) for a in req_pre)
            except Exception:
                req_set_pre = set()
            if req_set_pre and not req_set_pre.issubset(addrs_pre):
                continue
            for a in rec_pre.get("skip_base_addresses", []) or []:
                try:
                    pre_skip_base_addrs.add(int(a))
                except Exception:
                    pass
    except Exception:
        pre_skip_base_addrs = set()

    # Build sensor entries including extra sync sensors for switches
    sensor_entries: List[str] = []
    text_entries: List[str] = []
    binary_entries: List[str] = []
    button_entries: List[str] = []
    # 数値系エンティティ（number）
    number_entries: List[str] = []
    # ESPHome の text コンポーネント用（例: 日時設定の one-shot 入力）
    text_components: List[str] = []
    name_registry: Dict[str, set] = {}
    # Prepare boundary address set (range starts)
    boundary_addrs: set = set()
    try:
        for rng in ranges:
            boundary_addrs.add(int(rng.get("start")))
    except Exception:
        boundary_addrs = set()

    # P10 optimization:
    # Prefer catalog-driven boundary hints when available.
    # Fallback to legacy thinning when hints are not defined.
    try:
        if normalize_group(group) == "P10" and boundary_addrs:
            hints = load_generation_hints()
            p10_hints = (hints.get("groups") or {}).get("P10") or {}
            prefer_single_range = bool(p10_hints.get("prefer_single_range", False))
            split_hint_ids = p10_hints.get("split_hints_by_item_id") or []
            split_hint_addrs = resolve_split_hint_addresses("P10", split_hint_ids, regs_map)

            if prefer_single_range:
                min_addr = min(boundary_addrs)
                boundary_addrs = {min_addr} | split_hint_addrs
            else:
                boundary_addrs = set(boundary_addrs) | split_hint_addrs
    except Exception:
        pass

    for r in buckets["sensor"]:
        try:
            addr_skip = int(r.get("address"))
        except Exception:
            addr_skip = None
        if addr_skip is not None and addr_skip in pre_skip_base_addrs:
            continue
        if packed:
            try:
                addr = int(r["address"])
            except Exception:
                continue
            g_norm_local = normalize_group(r.get("group", ""))
            nm_raw = r.get("name") or f"Reg_{addr}"
            # 予約語・汎用名はアドレスサフィックスで一意化
            def _needs_suffix(n: str) -> bool:
                nl = (n or "").strip().lower()
                return nl in ("reserved", "unknown", "unused", "undefined", "n/a", "na", "none", "null") or nl.startswith("reserved_")
            nm = sensor_entity_name(nm_raw)
            if _needs_suffix(nm):
                nm = f"{nm}_{addr}"
            # グループ内重複も回避
            seen = name_registry.setdefault("sensor", set())
            if nm in seen:
                nm = f"{nm}_{addr}"
            seen.add(nm)
            if g_norm_local == "P00" and addr in P00_VERSION_ADDRS:
                nm = f"{nm}_Raw"
            unit = r.get("unit") or ""
            mult_local = effective_multiplier(g_norm_local, addr, r.get("multiplier"))
            vt_local = esphome_value_type(r.get("data_type"))
            ent: List[str] = [
                f"  - platform: template",
                f"    id: sens_{g_norm_local.lower()}_{addr}",
                f"    name: \"{sanitize_display_name(nm)}\"",
            ]
            if g_norm_local == "P00" and addr not in P00_VERSION_ADDRS:
                ent.append("    internal: true")
            if g_norm_local == "P00" and addr in P00_VERSION_ADDRS:
                ent.append("    disabled_by_default: true")
            if unit:
                ent.append(f"    unit_of_measurement: \"{unit}\"")
            acc_local = infer_accuracy_decimals(unit, vt_local, mult_local)
            if acc_local is not None:
                ent.append(f"    accuracy_decimals: {acc_local}")
            # device_class/state_class（簡易）
            try:
                u = (unit or "").strip()
                dev_cls = None
                st_cls = None
                if u in ("W", "kW"):
                    dev_cls = "power"; st_cls = "measurement"
                elif u in ("Wh", "kWh"):
                    dev_cls = "energy"; st_cls = "total_increasing"
                elif u == "V":
                    dev_cls = "voltage"; st_cls = "measurement"
                elif u == "A":
                    dev_cls = "current"; st_cls = "measurement"
                elif u == "Hz":
                    dev_cls = "frequency"; st_cls = "measurement"
                elif u in ("°C", "°F"):
                    dev_cls = "temperature"; st_cls = "measurement"
                elif u == "VA":
                    dev_cls = "apparent_power"; st_cls = "measurement"
                if dev_cls:
                    ent.append(f"    device_class: {dev_cls}")
                if st_cls:
                    ent.append(f"    state_class: {st_cls}")
            except Exception:
                pass
            cat_local = entity_info_category(r.get("group", ""), r.get("rw", "R"))
            if g_norm_local == "P00" and addr in P00_VERSION_ADDRS:
                cat_local = "diagnostic"
            if cat_local:
                ent.append(f"    entity_category: {cat_local}")
            sensor_entries += ent + [""]
        else:
            sensor_entries.append(gen_sensor_entry(r, interval_var, name_registry, controller_for_addr, boundary_addrs))


    # (removed legacy text input for DateTime; rely on sync button only)

    # Add sync sensors for switches to reflect external changes back to switches
    for r in buckets.get("switch", []):
        g_norm_r = normalize_group(r.get("group", ""))
        addr = int(r["address"])
        # Skip reserved-like items (一致してスイッチ自体も生成しないため、同期センサーも不要)
        name_r = r.get("name") or f"Reg_{addr}"
        disp_r = sanitize_display_name(name_r)
        nl_r = (disp_r or '').strip().lower()
        if nl_r in ("reserved", "unknown", "unused", "undefined", "n/a", "na", "none", "null") or nl_r.startswith("reserved_"):
            continue
        sens_id = f"sens_{g_norm_r.lower()}_{addr}"
        sw_id = f"sw_{g_norm_r.lower()}_{addr}"
        # Determine controller id (respect strict per-address mapping if provided)
        mc_id = SINGLE_CONTROLLER_ID
        if controller_for_addr and addr in (controller_for_addr or {}):
            try:
                mc_id = controller_for_addr[addr]
            except Exception:
                mc_id = SINGLE_CONTROLLER_ID
        # Sensor to read raw 0/1 and publish to switch state
        sensor_entries += [
            f"  - platform: modbus_controller",
            f"    id: {sens_id}",
            f"    modbus_controller_id: {mc_id}",
            f"    register_type: holding",
            f"    internal: true",
            f"    force_new_range: true",
            f"    address: {addr}",
            f"    value_type: U_WORD",
            f"    on_value:",
            f"      then:",
            f"        - lambda: |-",
            f"            id({g_norm_r.lower()}_write_actions_enabled) = true;",
            f"        - if:",
            f"            condition:",
            f"              lambda: 'return x > 0.5;'",
            f"            then:",
            f"              - switch.template.publish:",
            f"                  id: {sw_id}",
            f"                  state: ON",
            f"            else:",
            f"              - switch.template.publish:",
            f"                  id: {sw_id}",
            f"                  state: OFF",
        ]

    # Derived sensors via docs/derived_recipes.json (before emitting 'sensor:' section)
    try:
        recipes = load_derived_recipes().get("recipes", [])
        gnorm = normalize_group(group)
        addrs = set(regs_map.keys() if regs_map else [])
        for rec in recipes:
            if normalize_group(rec.get("group", "")) != gnorm:
                continue
            req = rec.get("requires") or []
            try:
                req_set = set(int(a) for a in req)
            except Exception:
                req_set = set()
            if req_set and not req_set.issubset(addrs):
                continue
            # Optionally skip base addresses to avoid duplication in default generation
            try:
                for a in rec.get("skip_base_addresses", []) or []:
                    try:
                        skipped_base_addrs.add(int(a))
                        regs_map.pop(int(a), None)
                    except Exception:
                        pass
            except Exception:
                pass
            tmpl = rec.get("template_sensor")
            if tmpl:
                lines_ts: List[str] = []
                dev_cls = str(tmpl.get("device_class") or "")
                rec_id = str(rec.get("id") or "").strip().lower()
                forced_name = None
                if gnorm == "P10" and rec_id.startswith("p10_fault_record_"):
                    try:
                        rec_no = int(rec_id.rsplit("_", 1)[-1], 10)
                        forced_name = f"Fault Record {rec_no:02d}"
                    except Exception:
                        forced_name = None
                # template_sensor を text_sensor 扱いにするか sensor 扱いにするかを判定
                is_numeric_template = bool(
                    (tmpl.get("unit") not in (None, ""))
                    or (tmpl.get("state_class") not in (None, ""))
                    or isinstance(tmpl.get("accuracy_decimals"), int)
                    or (dev_cls not in ("", "date", "timestamp"))
                )
                lines_ts.append("  - platform: template")
                if tmpl.get("id"):
                    lines_ts.append(f"    id: {tmpl.get('id')}")
                ts_name = forced_name or tmpl.get("name")
                if ts_name:
                    lines_ts.append(f"    name: \"{sanitize_display_name(ts_name)}\"")
                if tmpl.get("entity_category"):
                    lines_ts.append(f"    entity_category: {tmpl.get('entity_category')}")
                if is_numeric_template and tmpl.get("unit"):
                    lines_ts.append(f"    unit_of_measurement: \"{tmpl.get('unit')}\"")
                if tmpl.get("device_class"):
                    lines_ts.append(f"    device_class: {tmpl.get('device_class')}")
                if is_numeric_template and tmpl.get("state_class"):
                    lines_ts.append(f"    state_class: {tmpl.get('state_class')}")
                if is_numeric_template and isinstance(tmpl.get("accuracy_decimals"), int):
                    lines_ts.append(f"    accuracy_decimals: {int(tmpl.get('accuracy_decimals'))}")
                if isinstance(tmpl.get("internal"), bool):
                    lines_ts.append(f"    internal: {'true' if tmpl.get('internal') else 'false'}")
                if tmpl.get("internal_subst"):
                    # boolが確定できる文字列のみ反映（置換文字列は型不一致を起こしやすいため無視）
                    iv = str(tmpl.get("internal_subst") or "").strip().lower()
                    if iv in ("true", "false"):
                        lines_ts.append(f"    internal: {iv}")
                if tmpl.get("update_interval"):
                    lines_ts.append(f"    update_interval: {tmpl.get('update_interval')}")
                lam = tmpl.get("lambda") or ""
                # P10 fault record text is intentionally compact to keep HA cards readable.
                if gnorm == "P10" and rec_id.startswith("p10_fault_record_"):
                    nums = rec.get("numeric") or []
                    code_id = None
                    ym_id = None
                    dh_id = None
                    ms_id = None
                    for it in nums:
                        iid = str(it.get("id") or "")
                        li = iid.lower()
                        if li.endswith("_off00"):
                            code_id = iid
                        elif li.endswith("_off01"):
                            ym_id = iid
                        elif li.endswith("_off02"):
                            dh_id = iid
                        elif li.endswith("_off03"):
                            ms_id = iid
                    if code_id and ym_id and dh_id and ms_id:
                        lam_lines = [
                            f"uint16_t code = (uint16_t) id({code_id}).state;",
                            f"uint16_t ym = (uint16_t) id({ym_id}).state;",
                            f"uint16_t dh = (uint16_t) id({dh_id}).state;",
                            f"uint16_t ms = (uint16_t) id({ms_id}).state;",
                            "if (code == 0 || code == 0xFFFF) { return std::string(\"Not Set\"); }",
                            "if ((ym == 0xFFFF && dh == 0xFFFF && ms == 0xFFFF) || (ym == 0 && dh == 0 && ms == 0)) { return std::string(\"Not Set\"); }",
                        ]
                        lam_lines.extend(_fault_code_switch_lines(""))
                        lam_lines += [
                            "int year = 2000 + ((ym >> 8) & 0xFF);",
                            "int month = (ym & 0xFF);",
                            "int day = ((dh >> 8) & 0xFF);",
                            "int hour = (dh & 0xFF);",
                            "int minute = ((ms >> 8) & 0xFF);",
                            "int second = (ms & 0xFF);",
                            "if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) { return std::string(\"Not Set\"); }",
                            "char buf[160];",
                            "snprintf(buf, sizeof(buf), \"%04d-%02d-%02d %02d:%02d:%02d | C%u | %s\", year, month, day, hour, minute, second, code, code_text);",
                            "return std::string(buf);",
                        ]
                        lam = "\n".join(lam_lines)
                if lam:
                    # Sanitize potential NUL and other control chars in lambda payload to keep YAML valid
                    lam_s = str(lam).replace("\x00", "\\0")
                    lines_ts.append("    lambda: |-")
                    for ln in lam_s.split("\n"):
                        lines_ts.append(f"      {ln}")
                if is_numeric_template:
                    sensor_entries += lines_ts
                else:
                    # Template returning string should be emitted under text_sensor
                    text_entries += lines_ts

            # Generic numeric: emit internal readers via standard sensor path
            # (except system_datetime handled below)
            nums_generic = rec.get("numeric") or []
            if nums_generic and (str(rec.get("id") or "").strip().lower() != "system_datetime"):
                for it in nums_generic:
                    try:
                        addr_i = int(it.get("address"))
                    except Exception:
                        continue
                    mc_id = SINGLE_CONTROLLER_ID
                    if controller_for_addr and addr_i in (controller_for_addr or {}):
                        try:
                            mc_id = controller_for_addr[addr_i]
                        except Exception:
                            mc_id = SINGLE_CONTROLLER_ID
                    if packed and addr_i in packed_read_addrs:
                        ent = [
                            "  - platform: template",
                            f"    id: {it.get('id')}",
                            "    internal: true",
                        ]
                    else:
                        ent = [
                            "  - platform: modbus_controller",
                            f"    id: {it.get('id')}",
                            f"    modbus_controller_id: {mc_id}",
                            "    register_type: holding",
                            "    internal: true",
                            f"    address: {addr_i}",
                            f"    value_type: {it.get('value_type','U_WORD')}",
                        ]
                        # 標準ルートと同様に、レンジ境界だけ強制分割
                        try:
                            if boundary_addrs and addr_i in boundary_addrs:
                                ent.append("    force_new_range: true")
                        except Exception:
                            pass
                    sensor_entries += ent

            # text_from_numeric: make internal raw numeric and a template text mapped from enums
            tfn = rec.get("text_from_numeric")
            if tfn:
                try:
                    addr_i = int(tfn.get("address"))
                except Exception:
                    addr_i = None
                if addr_i is not None:
                    mc_id = SINGLE_CONTROLLER_ID
                    if controller_for_addr and addr_i in (controller_for_addr or {}):
                        try:
                            mc_id = controller_for_addr[addr_i]
                        except Exception:
                            mc_id = SINGLE_CONTROLLER_ID
                    rid = tfn.get("id_raw", f"sens_{gnorm.lower()}_{addr_i}")
                    tid = tfn.get("id_text", f"txt_{gnorm.lower()}_{addr_i}")
                    nm = sanitize_display_name(tfn.get("name") or f"Reg_{addr_i}")
                    # Internal numeric reader, or a packed value sink fed by the anchor.
                    if packed and addr_i in packed_read_addrs:
                        sensor_entries += [
                            "  - platform: template",
                            f"    id: {rid}",
                            "    internal: true",
                        ]
                    else:
                        sensor_entries += [
                            "  - platform: modbus_controller",
                            f"    id: {rid}",
                            f"    modbus_controller_id: {mc_id}",
                            "    register_type: holding",
                            "    internal: true",
                            f"    address: {addr_i}",
                            "    value_type: U_WORD",
                        ]
                    # template text mapped via enums (from regs_map or catalog fallback)
                    text_entries += [
                        f"  - platform: template",
                        f"    id: {tid}",
                        f"    name: \"{nm}\"",
                        f"    lambda: |-",
                        f"      uint16_t v = (uint16_t) id({rid}).state;",
                    ]
                    enums_src = None
                    try:
                        rec_src = (regs_map or {}).get(addr_i)
                        if rec_src and rec_src.get("enums"):
                            enums_src = rec_src.get("enums")
                        if not enums_src:
                            cat_regs = load_register_defs(JSON_CATALOG_PATH)
                            for rcat in cat_regs:
                                if normalize_group(getattr(rcat, 'group','')) == gnorm and int(getattr(rcat, 'address',-1)) == addr_i and getattr(rcat,'enums',None):
                                    enums_src = getattr(rcat,'enums')
                                    break
                    except Exception:
                        enums_src = None
                    if isinstance(enums_src, dict) and enums_src:
                        text_entries.append("      switch (v) {")
                        for k, lbl in (enums_src or {}).items():
                            iv = None
                            try:
                                iv = int(k, 10)
                            except Exception:
                                try:
                                    iv = int(k, 16)
                                except Exception:
                                    iv = None
                            if iv is None:
                                continue
                            text_entries.append(f"        case {iv}: return std::string(\"{sanitize_display_name(str(lbl))}\");")
                        text_entries += [
                            "        default: {",
                            "          char buf[16]; snprintf(buf, sizeof(buf), \"0x%02X\", v & 0xFF);",
                            "          return std::string(buf);",
                            "        }",
                            "      }",
                        ]
                    else:
                        text_entries += [
                            "      {",
                            "        char buf[16]; snprintf(buf, sizeof(buf), \"0x%02X\", v & 0xFF);",
                            "        return std::string(buf);",
                            "      }",
                        ]
            # Special: system_datetime recipe — emit internal numbers + formatted text + text input + optional sync button
            if (rec.get("id") or "").strip().lower() == "system_datetime":
                nums = rec.get("numeric") or []
                # Build a map id->address
                id_to_addr: Dict[str, int] = {}
                for it in nums:
                    try:
                        a = int(it.get("address"))
                        i = str(it.get("id"))
                        id_to_addr[i] = a
                    except Exception:
                        continue
                ym_id = None; dh_id = None; ms_id = None
                for k in id_to_addr.keys():
                    lk = (k or "").lower()
                    if lk.endswith("_ym"): ym_id = k
                    elif lk.endswith("_dh"): dh_id = k
                    elif lk.endswith("_ms"): ms_id = k
                # Emit internal number entries
                for it in nums:
                    try:
                        addr_i = int(it.get("address"))
                    except Exception:
                        continue
                    mc_id = SINGLE_CONTROLLER_ID
                    if controller_for_addr and addr_i in (controller_for_addr or {}):
                        try:
                            mc_id = controller_for_addr[addr_i]
                        except Exception:
                            mc_id = SINGLE_CONTROLLER_ID
                    number_entries += [
                        f"  - platform: modbus_controller",
                        f"    modbus_controller_id: {mc_id}",
                        f"    register_type: holding",
                        f"    address: {addr_i}",
                        f"    value_type: {it.get('value_type','U_WORD')}",
                        f"    id: {it.get('id')}",
                        f"    internal: true",
                        f"    name: \"{sanitize_display_name(it.get('name') or f'Reg_{addr_i}')}\"",
                        f"    min_value: 0",
                        f"    max_value: 65535",
                        f"    write_lambda: |-",
                        f"      return (int) x;",
                    ]
                # Emit formatted text_sensor
                ts = rec.get("text_sensor") or {}
                if ym_id and dh_id and ms_id:
                    year_base = int(ts.get("year_base", 2000))
                    nm = sanitize_display_name(ts.get("name") or "System DateTime")
                    text_entries += [
                        f"  - platform: template",
                        f"    id: {ts.get('id','sys_datetime_text')}",
                        f"    name: \"{nm}\"",
                        "    lambda: |-",
                        f"      if (isnan(id({ym_id}).state) || isnan(id({dh_id}).state) || isnan(id({ms_id}).state)) {{",
                        "        return {};",
                        "      }",
                        f"      uint16_t ym = (uint16_t) id({ym_id}).state;",
                        f"      uint16_t dh = (uint16_t) id({dh_id}).state;",
                        f"      uint16_t ms = (uint16_t) id({ms_id}).state;",
                        f"      int year = {year_base} + ((ym >> 8) & 0xFF);",
                        "      int month = (ym & 0xFF);",
                        "      int day = ((dh >> 8) & 0xFF);",
                        "      int hour = (dh & 0xFF);",
                        "      int minute = ((ms >> 8) & 0xFF);",
                        "      int second = (ms & 0xFF);",
                        "      char buf[20];",
                        "      snprintf(buf, sizeof(buf), \"%04d-%02d-%02d %02d:%02d:%02d\", year, month, day, hour, minute, second);",
                        "      return {buf};",
                    ]
                    # (Removed) Free text input for setting datetime; using sync button instead
                    # Optional sync button (now) using SNTP/HA time component with simple recipe overrides
                    if rec.get("sync_button"):
                        btn_name = rec.get("button_name") or "Sync System DateTime (now)"
                        write_cfg = rec.get("write") or {}
                        write_mode = str(write_cfg.get("mode", "fc10")).lower()
                        delay_ms_cfg = int(write_cfg.get("delay_ms", 400))
                        # Button header
                        button_entries += [
                            "  - platform: template",
                            "    id: sync_sys_datetime_now",
                            f"    name: \"{sanitize_display_name(btn_name)}\"",
                            "    icon: mdi:clock-check",
                            "    entity_category: config",
                            "    on_press:",
                            "      then:",
                            "        - lambda: |-",
                            f"            auto t = id(ha_time).now();",
                            f"            if (!t.is_valid()) t = id(sntp_time).now();",
                            f"            if (!t.is_valid()) return;",
                            "            ESP_LOGI(\"datetime\", \"Sync now -> %04d-%02d-%02d %02d:%02d:%02d\", t.year, t.month, t.day_of_month, t.hour, t.minute, t.second);",
                        ]
                        # FC=10 mode: use official API signature from docs (0x020C..0x020E, count=3)
                        if write_mode == "fc10":
                            button_entries += [
                                "        - lambda: |-",
                                f"            auto t2 = id(ha_time).now();",
                                f"            if (!t2.is_valid()) t2 = id(sntp_time).now();",
                                f"            if (!t2.is_valid()) return;",
                                f"            uint16_t ym = ((uint16_t)((t2.year - {year_base}) & 0xFF) << 8) | (uint16_t)(t2.month & 0xFF);",
                                "            uint16_t dh = ((uint16_t)(t2.day_of_month & 0xFF) << 8) | (uint16_t)(t2.hour & 0xFF);",
                                "            uint16_t ms = ((uint16_t)(t2.minute & 0xFF) << 8) | (uint16_t)(t2.second & 0xFF);",
                                "            std::vector<uint16_t> words; words.reserve(3);",
                                "            words.push_back(ym); words.push_back(dh); words.push_back(ms);",
                                f"            auto controller = id({group_primary_controller});",
                                "            if (controller == nullptr) return;",
                                "            esphome::modbus_controller::ModbusCommandItem set_rtc_command =",
                                f"                esphome::modbus_controller::ModbusCommandItem::create_write_multiple_command(controller, 0x020C, 3, words);",
                                "            controller->queue_command(set_rtc_command);",
                                "            // Reflect to internal numbers for immediate UI coherence",
                                f"            id({ym_id}).publish_state((float) ym);",
                                f"            id({dh_id}).publish_state((float) dh);",
                                f"            id({ms_id}).publish_state((float) ms);",
                            ]
                            button_entries += [
                                f"        - delay: {delay_ms_cfg}ms",
                                f"        - component.update: {ts.get('id','sys_datetime_text')}",
                            ]
                        else:
                            # Sequential mode: YM -> DH -> MS 固定順で number.set、各ステップ間に delay
                            button_entries += [
                                "        - number.set:",
                                f"            id: {ym_id}",
                                "            value: !lambda |-",
                                f"              auto t4 = id(ha_time).now();",
                                f"              if (!t4.is_valid()) t4 = id(sntp_time).now();",
                                f"              if (!t4.is_valid()) return 0;",
                                f"              uint16_t ymw = ((uint16_t)((t4.year - {year_base}) & 0xFF) << 8) | (uint16_t)(t4.month & 0xFF);",
                                f"              return (int) ymw;",
                                f"        - delay: {max(100, delay_ms_cfg)}ms",
                                "        - number.set:",
                                f"            id: {dh_id}",
                                "            value: !lambda |-",
                                "              auto t3 = id(ha_time).now();",
                                "              if (!t3.is_valid()) t3 = id(sntp_time).now();",
                                "              if (!t3.is_valid()) return 0;",
                                "              uint16_t dhw = ((uint16_t)(t3.day_of_month & 0xFF) << 8) | (uint16_t)(t3.hour & 0xFF);",
                                "              return (int) dhw;",
                                f"        - delay: {max(100, delay_ms_cfg)}ms",
                                "        - number.set:",
                                f"            id: {ms_id}",
                                "            value: !lambda |-",
                                "              auto t2 = id(ha_time).now();",
                                "              if (!t2.is_valid()) t2 = id(sntp_time).now();",
                                "              if (!t2.is_valid()) return 0;",
                                "              uint16_t msw = ((uint16_t)(t2.minute & 0xFF) << 8) | (uint16_t)(t2.second & 0xFF);",
                                "              return (int) msw;",
                                f"        - component.update: {ts.get('id','sys_datetime_text')}",
                            ]
    except Exception:
        pass

    # P02 Active Fault Codes (0x0204..0x0207): simple mapping via internal raw + template text
    try:
        if normalize_group(group) == "P02":
            p02_recipes = []
            try:
                p02_recipes = [r for r in load_derived_recipes().get("recipes", []) if normalize_group(r.get("group", "")) == "P02"]
            except Exception:
                p02_recipes = []

            fault_text_cfg = None
            try:
                fault_text_cfg = next((r.get("active_fault_code_text") for r in p02_recipes if r.get("active_fault_code_text")), None)
            except Exception:
                fault_text_cfg = None

            fault_addrs = [0x204, 0x205, 0x206, 0x207]
            try:
                if isinstance(fault_text_cfg, dict) and fault_text_cfg.get("addresses"):
                    fault_addrs = [int(a) for a in (fault_text_cfg.get("addresses") or [])]
            except Exception:
                fault_addrs = [0x204, 0x205, 0x206, 0x207]
            # enums は常にカタログ優先で参照（存在しない場合のみ regs_map をフォールバック）
            enums_src = None
            try:
                from .common import JSON_CATALOG_PATH as _CAT
                cat_regs = load_register_defs(_CAT)
                for r in cat_regs:
                    if normalize_group(getattr(r, 'group', '')) == 'P02' and int(getattr(r, 'address', -1)) in fault_addrs:
                        if getattr(r, 'enums', None):
                            enums_src = getattr(r, 'enums')
                            break
                if not enums_src:
                    for fa in fault_addrs:
                        rec = (regs_map or {}).get(fa)
                        if rec and rec.get("enums"):
                            enums_src = rec.get("enums")
                            break
            except Exception:
                pass
            # レシピで fault code の対応表が定義されている場合はそれを優先
            try:
                if isinstance(fault_text_cfg, dict) and isinstance(fault_text_cfg.get("map"), dict) and fault_text_cfg.get("map"):
                    enums_src = fault_text_cfg.get("map")
            except Exception:
                pass

            names_map = {}
            try:
                if isinstance(fault_text_cfg, dict) and isinstance(fault_text_cfg.get("names"), dict):
                    names_map = fault_text_cfg.get("names") or {}
            except Exception:
                names_map = {}
            for fa in fault_addrs:
                mc_id = SINGLE_CONTROLLER_ID
                if controller_for_addr and fa in (controller_for_addr or {}):
                    try:
                        mc_id = controller_for_addr[fa]
                    except Exception:
                        mc_id = SINGLE_CONTROLLER_ID
                # public template text
                try:
                    base_name = (regs_map.get(fa, {}) or {}).get("name") if regs_map else None
                except Exception:
                    base_name = None
                disp_name_cfg = None
                try:
                    disp_name_cfg = names_map.get(str(fa))
                    if disp_name_cfg is None:
                        disp_name_cfg = names_map.get(fa)
                except Exception:
                    disp_name_cfg = None
                disp = sanitize_display_name(disp_name_cfg or base_name or f"Active_Fault_{fa-0x204+1}")
                text_entries += [
                    f"  - platform: template",
                    f"    id: txt_p02_{fa}",
                    f"    name: \"{disp}\"",
                    f"    entity_category: diagnostic",
                    f"    lambda: |-",
                    f"      uint16_t v = (uint16_t) id(sens_p02_{fa}).state;",
                ]
                if isinstance(enums_src, dict) and enums_src:
                    text_entries.append("      switch (v) {")
                    for k, lbl in (enums_src or {}).items():
                        iv = None
                        try:
                            iv = int(k, 10)
                        except Exception:
                            try:
                                iv = int(k, 16)
                            except Exception:
                                iv = None
                        if iv is None:
                            continue
                        text_entries.append(f"        case {iv}: return std::string(\"{sanitize_display_name(str(lbl))}\");")
                    text_entries += [
                        "        default: {",
                        "          char buf[16]; snprintf(buf, sizeof(buf), \"0x%02X\", v & 0xFF);",
                        "          return std::string(buf);",
                        "        }",
                        "      }",
                    ]
                else:
                    text_entries += [
                        "      {",
                        "        char buf[16]; snprintf(buf, sizeof(buf), \"0x%02X\", v & 0xFF);",
                        "        return std::string(buf);",
                        "      }",
                    ]
    except Exception:
        pass

    # P05 slot start/end pairs: expose text input "HH:MM-HH:MM" + apply button + formatted display text.
    try:
        if normalize_group(group) == "P05":
            time_pairs = [
                (57382, 57383, "Slot 1 Charging Time Range"),
                (57384, 57385, "Slot 2 Charging Time Range"),
                (57386, 57387, "Slot 3 Charging Time Range"),
                (57389, 57390, "Slot 1 Discharge Time Range"),
                (57391, 57392, "Slot 2 Discharge Time Range"),
                (57393, 57394, "Slot 3 Discharge Time Range"),
            ]
            for a_start, a_end, label in time_pairs:
                if a_start not in regs_map or a_end not in regs_map:
                    continue
                mc_id = SINGLE_CONTROLLER_ID
                try:
                    if controller_for_addr and a_start in (controller_for_addr or {}):
                        mc_id = controller_for_addr[a_start]
                except Exception:
                    mc_id = SINGLE_CONTROLLER_ID

                # Suppress default public number generation for raw packed values.
                skipped_base_addrs.add(a_start)
                skipped_base_addrs.add(a_end)
                custom_internal_number_addrs.add(a_start)
                custom_internal_number_addrs.add(a_end)

                id_s = f"raw_p05_{a_start}"
                id_e = f"raw_p05_{a_end}"
                in_id = f"txtin_p05_{a_start}_{a_end}"
                out_id = f"txt_p05_range_{a_start}_{a_end}"

                sensor_entries += [
                    "  - platform: modbus_controller",
                    f"    modbus_controller_id: {mc_id}",
                    "    register_type: holding",
                    f"    address: {a_start}",
                    "    value_type: U_WORD",
                    f"    id: {id_s}",
                    "    internal: true",
                    f"    force_new_range: true",
                    "  - platform: modbus_controller",
                    f"    modbus_controller_id: {mc_id}",
                    "    register_type: holding",
                    f"    address: {a_end}",
                    "    value_type: U_WORD",
                    f"    id: {id_e}",
                    "    internal: true",
                ]

                text_entries += [
                    "  - platform: template",
                    f"    id: {out_id}",
                    f"    name: \"{sanitize_display_name(label)}\"",
                    "    entity_category: config",
                    f"    update_interval: ${{{interval_var}}}",
                    "    lambda: |-",
                    f"      if (isnan(id({id_s}).state) || isnan(id({id_e}).state)) return {{}};",
                    f"      int s = (int) llroundf(id({id_s}).state);",
                    f"      int e = (int) llroundf(id({id_e}).state);",
                    "      int sh = (s >> 8) & 0xFF; int sm = s & 0xFF;",
                    "      int eh = (e >> 8) & 0xFF; int em = e & 0xFF;",
                    "      if (sh < 0 || sh > 23 || eh < 0 || eh > 23 || sm < 0 || sm > 59 || em < 0 || em > 59) return {};",
                    "      char buf[24];",
                    "      snprintf(buf, sizeof(buf), \"%02d:%02d-%02d:%02d\", sh, sm, eh, em);",
                    "      return std::string(buf);",
                ]

                text_components += [
                    "  - platform: template",
                    f"    id: {in_id}",
                    f"    name: \"{sanitize_display_name(label + ' Input')}\"",
                    "    entity_category: config",
                    "    optimistic: true",
                    "    mode: text",
                    "    min_length: 11",
                    "    max_length: 11",
                    "    initial_value: \"00:00-00:00\"",
                    "    set_action:",
                    "      - lambda: |-",
                    "          if (!id(p05_write_actions_enabled)) {",
                    "            ESP_LOGW(\"p05_range\", \"Write action blocked during startup guard window\");",
                    "            return;",
                    "          }",
                    "          std::string s = x;",
                    "          if (s.size() != 11 || s[2] != ':' || s[5] != '-' || s[8] != ':') {",
                    "            ESP_LOGW(\"p05_range\", \"Invalid format, use HH:MM-HH:MM: %s\", s.c_str());",
                    "            return;",
                    "          }",
                    "          auto d = [](char c)->bool { return c >= '0' && c <= '9'; };",
                    "          if (!d(s[0]) || !d(s[1]) || !d(s[3]) || !d(s[4]) || !d(s[6]) || !d(s[7]) || !d(s[9]) || !d(s[10])) {",
                    "            ESP_LOGW(\"p05_range\", \"Invalid digits: %s\", s.c_str());",
                    "            return;",
                    "          }",
                    "          int sh = (s[0]-'0')*10 + (s[1]-'0');",
                    "          int sm = (s[3]-'0')*10 + (s[4]-'0');",
                    "          int eh = (s[6]-'0')*10 + (s[7]-'0');",
                    "          int em = (s[9]-'0')*10 + (s[10]-'0');",
                    "          if (sh < 0 || sh > 23 || eh < 0 || eh > 23 || sm < 0 || sm > 59 || em < 0 || em > 59) {",
                    "            ESP_LOGW(\"p05_range\", \"Out of range HH/MM: %s\", s.c_str());",
                    "            return;",
                    "          }",
                    "          uint16_t w_start = ((uint16_t)(sh & 0xFF) << 8) | (uint16_t)(sm & 0xFF);",
                    "          uint16_t w_end = ((uint16_t)(eh & 0xFF) << 8) | (uint16_t)(em & 0xFF);",
                    "          std::vector<uint16_t> words; words.reserve(2);",
                    "          words.push_back(w_start);",
                    "          words.push_back(w_end);",
                    f"          auto controller = id({mc_id});",
                    "          if (controller == nullptr) return;",
                    f"          auto cmd = esphome::modbus_controller::ModbusCommandItem::create_write_multiple_command(controller, {a_start}, 2, words);",
                    "          controller->queue_command(cmd);",
                    "          id(p05_refresh_after_write).execute();",
                    f"          id({id_s}).publish_state((float) w_start);",
                    f"          id({id_e}).publish_state((float) w_end);",
                    f"          id({out_id}).update();",
                ]
    except Exception:
        pass

    if sensor_entries:
        lines.append("")
        lines.append("sensor:")
        lines.extend(sensor_entries)
    if buckets["text_sensor"]:
        # Build text sensor entries (collect first; header added once)
        for r in buckets["text_sensor"]:
            # 0x0204..0x0207 は常に専用実装で出すため、一般ルートはスキップ
            try:
                addr_ts = int(r.get("address"))
            except Exception:
                addr_ts = None
            if addr_ts is not None and addr_ts in skipped_base_addrs:
                continue
            g_norm_ts = normalize_group(r.get("group", ""))
            if g_norm_ts == "P02" and addr_ts in (0x204, 0x205, 0x206, 0x207):
                continue
            if packed:
                try:
                    addr = int(r["address"])
                except Exception:
                    continue
                g_norm_local = normalize_group(r.get("group", ""))
                nm_raw = r.get("name") or f"Reg_{addr}"
                def _needs_suffix(n: str) -> bool:
                    nl = (n or "").strip().lower()
                    return nl in ("reserved", "unknown", "unused", "undefined", "n/a", "na", "none", "null") or nl.startswith("reserved_")
                nm = sanitize_display_name(nm_raw)
                if _needs_suffix(nm):
                    nm = f"{nm}_{addr}"
                text_entries += [
                    f"  - platform: template",
                    f"    id: txt_{g_norm_local.lower()}_{addr}",
                    f"    name: \"{nm}\"",
                ]
            else:
                # default
                text_entries.append(gen_sensor_entry(r, interval_var, name_registry, controller_for_addr))
        # do not emit here; appended once at the end

    # P00 remaining raw numeric sensors: expose integer text sensors with the original entity names.
    try:
        if normalize_group(group) == "P00":
            skip_addrs = {20, 21, 22, 23, 28, 29, 30, 31}
            diag_text_addrs = {10, 12, 24, 33, 73}
            for r in buckets.get("sensor", []):
                try:
                    addr = int(r.get("address"))
                except Exception:
                    continue
                if addr in skip_addrs:
                    continue
                nm = sensor_entity_name(r.get("name") or f"Reg_{addr}")
                nml = (nm or "").strip().lower()
                if nml in ("reserved", "unknown", "unused", "undefined", "n/a", "na", "none", "null") or nml.startswith("reserved_"):
                    nm = f"{nm}_{addr}"
                nm = sanitize_display_name(nm)
                text_entries += [
                    "  - platform: template",
                    f"    id: txt_p00_raw_{addr}",
                    f"    name: \"{nm}\"",
                    *(["    entity_category: diagnostic"] if addr in diag_text_addrs else []),
                    f"    update_interval: ${{{interval_var}}}",
                    "    lambda: |-",
                    f"      if (isnan(id(sens_p00_{addr}).state)) return std::string(\"Unknown\");",
                    f"      long long raw = llroundf(id(sens_p00_{addr}).state);",
                    "      if (raw > 999999999LL || raw < -999999999LL) return std::string(\"Unknown\");",
                    "      char buf[24];",
                    "      snprintf(buf, sizeof(buf), \"%lld\", raw);",
                    "      return std::string(buf);",
                ]
    except Exception:
        pass

    # (moved to derived_recipes.json: p02_active_fault_bits)

    if text_entries:
        lines.append("")
        lines.append("text_sensor:")
        lines.extend(text_entries)
    if text_components:
        lines.append("")
        lines.append("text:")
        lines.extend(text_components)
    if binary_entries:
        lines.append("")
        lines.append("binary_sensor:")
        lines.extend(binary_entries)

    if button_entries:
        lines.append("")
        lines.append("button:")
        lines.extend(button_entries)

    # P02 Active Fault Bits generation moved under derived_recipes.json (p02_active_fault_bits)
    try:
        if normalize_group(group) == "P02":
            recipes = load_derived_recipes().get("recipes", [])
            r = next((x for x in recipes if normalize_group(x.get("group",""))=="P02" and x.get("active_fault_bits")), None)
            if r:
                base = 0x0200
                present = [a for a in range(base, base+4) if a in regs_map]
                if present:
                    afb = r.get("active_fault_bits") or {}
                    upd = afb.get("update_interval", "30s")
                    for idx, addr in enumerate(range(base, base+4)):
                        mc_id = SINGLE_CONTROLLER_ID
                        if controller_for_addr and addr in (controller_for_addr or {}):
                            try:
                                mc_id = controller_for_addr[addr]
                            except Exception:
                                mc_id = SINGLE_CONTROLLER_ID
                        sensor_entries += [
                            f"  - platform: modbus_controller",
                            f"    id: raw_fault_word_p02_{idx}",
                            f"    modbus_controller_id: {mc_id}",
                            f"    register_type: holding",
                            f"    internal: true",
                            f"    address: {addr}",
                            f"    value_type: U_WORD",
                            f"    force_new_range: true",
                        ]
                    if afb.get("emit_hex64_text", True):
                        text_entries += [
                            f"  - platform: template",
                            f"    id: txt_p02_faults_64",
                            f"    name: \"Active_Faults_64bit\"",
                            f"    entity_category: diagnostic",
                            f"    update_interval: {upd}",
                            f"    lambda: |-",
                            f"      uint64_t v = 0ULL;",
                            f"      v |= ((uint64_t)((uint32_t) id(raw_fault_word_p02_0).state & 0xFFFF)) << 0;",
                            f"      v |= ((uint64_t)((uint32_t) id(raw_fault_word_p02_1).state & 0xFFFF)) << 16;",
                            f"      v |= ((uint64_t)((uint32_t) id(raw_fault_word_p02_2).state & 0xFFFF)) << 32;",
                            f"      v |= ((uint64_t)((uint32_t) id(raw_fault_word_p02_3).state & 0xFFFF)) << 48;",
                            f"      char buf[32];",
                            f"      snprintf(buf, sizeof(buf), \"0x%016llX\", (unsigned long long) v);",
                            f"      return std::string(buf);",
                        ]
                    if afb.get("emit_bits_binary", True):
                        for b in range(64):
                            word = b // 16
                            bit = b % 16
                            binary_entries += [
                                f"  - platform: template",
                                f"    name: \"Fault_Bit_{b}\"",
                                f"    entity_category: diagnostic",
                                f"    lambda: |-",
                                f"      uint16_t w = (uint16_t) id(raw_fault_word_p02_{word}).state;",
                                f"      return (bool) ((w >> {bit}) & 0x1);",
                                f"    update_interval: {upd}",
                            ]
    except Exception:
        pass
    # (moved up) Special handling for P02 Active Fault slots inserted before section emission

    # Collect number entries, including hidden internal numbers for switch backing
    # Backing numbers for switches (internal)
    for r in buckets.get("switch", []):
        g_norm_r = normalize_group(r.get("group", ""))
        addr = int(r["address"])
        # Create internal number with id
        def yaml_escape(s: str) -> str:
            return s.replace('\\', r'\\').replace('"', r'\"')
        name = r.get("name") or f"Reg_{addr}"
        value_type = esphome_value_type(r.get("data_type"))
        try:
            multiplier = float(r.get("multiplier") or 1.0)
        except Exception:
            multiplier = 1.0
        num_id = f"num_{g_norm_r.lower()}_{addr}"
        number_entries += [
            f"  - platform: modbus_controller",
            f"    id: {num_id}",
            f"    modbus_controller_id: {controller_for_addr.get(addr, SINGLE_CONTROLLER_ID) if controller_for_addr else SINGLE_CONTROLLER_ID}",
            f"    register_type: holding",
            f"    internal: true",

            f"    address: {addr}",
            f"    value_type: {value_type}",
        ]
        if multiplier != 1.0:
            number_entries += [
                "    lambda: |-",
                f"      return x * {multiplier};",
            ]
        # write_lambda for back conversion
        inv = 1.0 / multiplier if multiplier not in (0, 0.0) else 1.0
        number_entries += [
            "    write_lambda: |-",
            f"      return (int) round(x * {inv});",
        ]

    # Regular numbers
    for r in buckets["number"]:
        try:
            if normalize_group(group) == "P02" and int(r.get("address")) in (524, 525, 526):
                # Avoid duplicate emission of datetime split numbers
                continue
            if int(r.get("address")) in skipped_base_addrs or int(r.get("address")) in custom_internal_number_addrs:
                continue
        except Exception:
            pass
        number_entries.append(gen_sensor_entry(r, interval_var, name_registry, controller_for_addr))
    if number_entries:
        lines.append("")
        lines.append("number:")
        lines.extend(number_entries)
    if buckets["select"]:
        lines.append("")
        lines.append("select:")
        for r in buckets["select"]:
            lines.append(gen_sensor_entry(r, interval_var, name_registry, controller_for_addr))

    if buckets.get("switch"):
        lines.append("")
        lines.append("switch:")
        for r in buckets["switch"]:
            # Emit template switch that delegates to hidden number via number.set
            g_norm_r = normalize_group(r.get("group", ""))
            addr = int(r["address"])
            def yaml_escape(s: str) -> str:
                return s.replace('\\', r'\\').replace('"', r'\"')
            name = r.get("name") or f"Reg_{addr}"
            # Sanitize and ensure uniqueness; avoid generic names like "Reserved"
            disp = sanitize_display_name(name)
            nl = (disp or '').strip().lower()
            # reserved系はスイッチとして不適切なため、数字サフィックスではなくスキップ
            if nl in ("reserved", "unknown", "unused", "undefined", "n/a", "na", "none", "null") or nl.startswith("reserved_"):
                # reservedはスイッチ生成を行わない
                continue
            if name_registry is not None:
                seen = name_registry.setdefault('switch', set())
                if disp in seen:
                    disp = f"{disp}_{addr}"
                    # それでも衝突する場合は末尾に再度アドレスを付与して一意化
                    while disp in seen:
                        disp = f"{disp}_{addr}"
                seen.add(disp)
            cat = entity_info_category(r.get("group",""), r.get("rw","RW"))
            num_id = f"num_{g_norm_r.lower()}_{addr}"
            switch_guard_id = f"{g_norm_r.lower()}_write_actions_enabled"
            sw_lines = [
                "  - platform: template",
                f"    id: sw_{g_norm_r.lower()}_{addr}",
                f"    name: \"{yaml_escape(disp)}\"",
                "    optimistic: true",
                "    restore_mode: DISABLED",
            ]
            if cat:
                sw_lines.append(f"    entity_category: {cat}")
            sw_lines += [
                "    turn_on_action:",
                "      - if:",
                "          condition:",
                f"            lambda: 'return id({switch_guard_id});'",
                "          then:",
                *(["            - lambda: |-", f"                id({g_norm_r.lower()}_refresh_after_write).execute();"] if g_norm_r in ("P05", "P07") else []),
                "            - number.set:",
                f"                id: {num_id}",
                "                value: 1",
                "    turn_off_action:",
                "      - if:",
                "          condition:",
                f"            lambda: 'return id({switch_guard_id});'",
                "          then:",
                *(["            - lambda: |-", f"                id({g_norm_r.lower()}_refresh_after_write).execute();"] if g_norm_r in ("P05", "P07") else []),
                "            - number.set:",
                f"                id: {num_id}",
                "                value: 0",
            ]
            lines.append("\n".join(sw_lines))

    if normalize_group(group) in ("P05", "P07"):
        fast_g = normalize_group(group).lower()
        lines.append("")
        lines.append("script:")
        lines.append(f"  - id: {fast_g}_refresh_after_write")
        lines.append("    mode: restart")
        lines.append("    then:")
        lines.append("      - delay: 1s")
        lines.append("      - lambda: |-")
        lines.append(f"          auto controller = id({group_primary_controller});")
        lines.append("          if (controller == nullptr) return;")
        for refresh_range in ranges:
            start = int(refresh_range["start"])
            size = int(refresh_range["size"])
            lines.append("          controller->queue_command(")
            lines.append("              esphome::modbus_controller::ModbusCommandItem::create_read_command(")
            lines.append("                  controller, esphome::modbus_controller::ModbusRegisterType::HOLDING,")
            lines.append(f"                  {start}, {size}));")
        if buckets.get("switch"):
            lines.append("")
            lines.append("globals:")
            lines.append(f"  - id: {fast_g}_write_actions_enabled")
            lines.append("    type: bool")
            lines.append("    restore_value: no")
            lines.append("    initial_value: 'false'")
        if buckets.get("switch"):
            lines.append("")
            lines.append("interval:")
            lines.append("  - interval: 1s")
            lines.append("    then:")
            lines.append("      - lambda: |-")
            lines.append(f"          if (!id({fast_g}_write_actions_enabled) && millis() > 3000U) id({fast_g}_write_actions_enabled) = true;")
    elif buckets.get("switch"):
        gnorm = normalize_group(group).lower()
        lines.append("")
        lines.append("globals:")
        lines.append(f"  - id: {gnorm}_write_actions_enabled")
        lines.append("    type: bool")
        lines.append("    restore_value: no")
        lines.append("    initial_value: 'false'")
        lines.append("")
        lines.append("interval:")
        lines.append("  - interval: 1s")
        lines.append("    then:")
        lines.append("      - lambda: |-")
        lines.append(f"          if (!id({gnorm}_write_actions_enabled) && millis() > 3000U) id({gnorm}_write_actions_enabled) = true;")

    rendered = "\n".join(lines) + "\n"
    rendered = apply_event_driven_templates(rendered, group)
    rendered = apply_group_skip_updates(
        rendered,
        group,
        force_new_range_addresses=set() if packed else None,
    )
    return rendered if packed else coalesce_rw_group_reads(rendered, group)

def gen_button_for_wreg(
    reg: Dict[str, Any],
    name_registry: Dict[str, set] = None,
    direct_write: bool = False,
    controller_id: str = SINGLE_CONTROLLER_ID,
) -> str:
    # 書き込み専用レジスタを template ボタンとして生成し、押下で内部numberに値を書き込む
    def yaml_escape(s: str) -> str:
        return s.replace('\\', r'\\').replace('"', r'\"')
    def parse_enum_key(k: Any) -> Optional[int]:
        try:
            return int(str(k), 10)
        except Exception:
            try:
                return int(str(k), 16)
            except Exception:
                return None
    def uniq_button_name(raw: str, addr_local: int) -> str:
        disp = sanitize_display_name(raw)
        nl = (disp or "").strip().lower()
        if nl in ("reserved", "unknown", "unused", "undefined", "n/a", "na", "none", "null") or nl.startswith("reserved_"):
            disp = f"{disp}_{addr_local}"
        if name_registry is not None:
            seen = name_registry.setdefault("button", set())
            if disp in seen:
                disp = f"{disp}_{addr_local}"
            seen.add(disp)
        return disp

    name = reg.get("name") or f"Reg_{reg.get('address')}"
    addr = int(reg.get("address"))
    g_norm = normalize_group(reg.get("group",""))
    base_lines: List[str] = []
    internal_line = ["    internal: true"] if g_norm == "P03" else []
    guard_lines = [
        "        - if:",
        "            condition:",
        f"              lambda: 'return id({g_norm.lower()}_write_actions_enabled);'",
        "            then:",
    ]
    def write_action(value: int) -> List[str]:
        if direct_write:
            return [
                "              - lambda: |-",
                f"                  auto controller = id({controller_id});",
                "                  controller->queue_command(",
                "                      esphome::modbus_controller::ModbusCommandItem::create_write_single_command(",
                f"                          controller, {addr}, {value}));",
            ]
        return [
            "              - number.set:",
            f"                  id: num_{g_norm.lower()}_{addr}",
            f"                  value: {value}",
        ]
    # バッキングnumberはP03ブロック側で生成済み。ここではテンプレートボタンのみ出力。

    enums = reg.get("enums") or {}
    enum_items: List[tuple] = []
    if isinstance(enums, dict) and enums:
        for k, v in enums.items():
            iv = parse_enum_key(k)
            if iv is None:
                continue
            enum_items.append((iv, str(v)))
        # Enums-driven emission for P03 write-only commands:
        # - 0/1: emit two explicit buttons
        # - single enum: emit one button with that command value
        # - multi enum: emit one button per command value
        if enum_items:
            keys = {iv for iv, _ in enum_items}
            base = sanitize_display_name(name)
            if keys == {0, 1}:
                # Prefer action first (1), then neutral/disable (0)
                ordered = sorted(enum_items, key=lambda x: (0 if x[0] == 1 else 1, x[0]))
                for iv, lbl in ordered:
                    suffix = sanitize_display_name(lbl).strip() or str(iv)
                    disp = uniq_button_name(f"{base} {suffix}", addr)
                    base_lines += [
                        "  - platform: template",
                        f"    name: \"{yaml_escape(disp)}\"",
                        "    entity_category: config",
                        *internal_line,
                        "    on_press:",
                        "      then:",
                        *guard_lines,
                        *write_action(iv),
                    ]
                return "\n".join(base_lines)
            if len(enum_items) == 1:
                iv, _lbl = enum_items[0]
                disp = uniq_button_name(sanitize_display_name(name), addr)
                base_lines += [
                    "  - platform: template",
                    f"    name: \"{yaml_escape(disp)}\"",
                    "    entity_category: config",
                    *internal_line,
                    "    on_press:",
                    "      then:",
                    *guard_lines,
                    *write_action(iv),
                ]
                return "\n".join(base_lines)
            for iv, lbl in sorted(enum_items, key=lambda x: x[0]):
                suffix = sanitize_display_name(lbl).strip() or str(iv)
                # 0xDF02 (57090): expose each command as enum-label-only button names
                if addr == 57090:
                    disp = uniq_button_name(suffix, addr)
                else:
                    disp = uniq_button_name(f"{name} {suffix}", addr)
                base_lines += [
                    "  - platform: template",
                    f"    name: \"{yaml_escape(disp)}\"",
                    "    entity_category: config",
                    *internal_line,
                    "    on_press:",
                    "      then:",
                    *guard_lines,
                    *write_action(iv),
                ]
            return "\n".join(base_lines)

    # その他は単発実行ボタンを1つ出す（値=1）
    # 汎用名はアドレスサフィックス、スラッシュは置換、重複も回避
    disp = uniq_button_name(sanitize_display_name(name), addr)

    base_lines += [
        "  - platform: template",
        f"    name: \"{yaml_escape(disp)}\"",
        "    entity_category: config",
        *internal_line,
        "    on_press:",
        "      then:",
        *guard_lines,
        *write_action(1),
    ]
    return "\n".join(base_lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate ESPHome YAML (anchors + entities)")
    ap.add_argument("--implemented", type=Path, default=Path("tools/build/implemented_registers.json"))
    ap.add_argument("--ranges", type=Path, default=Path("tools/build/device_specific_ranges.json"))
    ap.add_argument("--outdir", type=Path, default=Path("esphome"))
    ap.add_argument("--custom-overwrite", action="store_true", help="Overwrite custom entities files if they already exist")
    ap.add_argument("--split-mode", choices=["group", "strict"], default="group", help="Group-level single controller or per-block strict controllers")
    ap.add_argument("--strict-groups", type=str, default="", help="Comma-separated list of groups (e.g., P02,P09,P10) to apply strict split; effective only in strict mode")
    ap.add_argument("--packed-groups", type=str, default="", help="Comma-separated groups to read through packed range anchors (advanced)")
    args = ap.parse_args()

    impl = load_json(args.implemented)
    ranged = load_json(args.ranges)
    ranges_by_group: Dict[str, List[Dict[str, Any]]] = ranged.get("ranges", {})

    regs_by_group_addr: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for rec in impl:
        if not rec.get("success"):
            continue
        g = normalize_group(rec.get("group", ""))
        regs_by_group_addr.setdefault(g, {})[int(rec["address"])] = rec

    # Fallback: augment multipliers/units/names/enums from catalog
    try:
        cat_map: Dict[tuple, dict] = build_catalog_meta_map(JSON_CATALOG_PATH)
        if not cat_map:
            # Last resort for legacy environments
            cat_regs = load_register_defs(JSON_CATALOG_PATH)
            for r in cat_regs:
                cat_map[(normalize_group(r.group), int(r.address))] = {
                    "multiplier": float(getattr(r, "multiplier", 1.0) or 1.0),
                    "unit": getattr(r, "unit", "") or "",
                    "name": getattr(r, "name", "") or "",
                    "enums": getattr(r, "enums", None),
                }
        for g, addr_map in regs_by_group_addr.items():
            for a, rec in addr_map.items():
                key = (normalize_group(g), int(a))
                meta = cat_map.get(key)
                if not meta:
                    continue
                try:
                    m = float(rec.get("multiplier") if rec.get("multiplier") is not None else 1.0)
                except Exception:
                    m = 1.0
                if abs(m - 1.0) < 1e-12 and abs(meta.get("multiplier", 1.0) - 1.0) > 1e-12:
                    rec["multiplier"] = meta.get("multiplier", 1.0)
                # Optionally, fill unit if empty
                if not (rec.get("unit") or "").strip() and (meta.get("unit") or "").strip():
                    rec["unit"] = meta.get("unit")
                # Prefer catalog label for name to reflect latest wording
                if (meta.get("name") or "").strip():
                    rec["name"] = meta.get("name")
                # Align enums to catalog (clear if catalog has none)
                rec["enums"] = meta.get("enums")
    except Exception:
        pass

    out_root = args.outdir
    srne_dir = out_root / "srne"
    anchors_dir = srne_dir / "anchors"
    custom_dir = srne_dir / "custom"
    # Create the anchors directory lazily when packed groups are requested.
    custom_dir.mkdir(parents=True, exist_ok=True)

    # Write core.yaml. Packed groups share srne_main; strict mode remains legacy per-range.

    files = {}
    all_groups = sorted(ranges_by_group.keys())

    strict_mode = args.split_mode == "strict"
    requested_strict = {
        normalize_group(group)
        for group in args.strict_groups.split(",")
        if group.strip()
    }
    strict_set = requested_strict or {normalize_group(group) for group in all_groups}
    packed_groups = {
        normalize_group(group)
        for group in args.packed_groups.split(",")
        if group.strip()
    }
    anchor_ranges_by_group: Dict[str, List[Dict[str, Any]]] = {}
    for group, group_ranges in ranges_by_group.items():
        gnorm = normalize_group(group)
        if gnorm not in packed_groups:
            continue
        split_ranges = group_ranges
        if gnorm == "P02":
            split_ranges = split_ranges_at_boundaries(group_ranges, {527})
        if gnorm == "P10":
            anchor_ranges_by_group[group] = build_p10_packed_read_ranges(
                regs_by_group_addr.get(gnorm, {}),
            )
        else:
            anchor_ranges_by_group[group] = build_packed_read_ranges(
                split_ranges,
                regs_by_group_addr.get(gnorm, {}),
            )

    # Strict groups get one controller per generated range. Other groups share one controller.
    controllers: List[dict] = []
    controller_for_addr_by_group: Dict[str, Dict[int, str]] = {}
    has_shared_controller = any(
        not (
            (strict_mode and normalize_group(group) in strict_set)
        )
        for group in all_groups
    )
    if has_shared_controller:
        controllers.append({
            "id": SINGLE_CONTROLLER_ID,
            "update_interval": f"${{{interval_var_name_main()}}}000ms",
        })
    for g in all_groups:
        gnorm = normalize_group(g)
        for rng in ranges_by_group.get(g, []):
            controller_id = SINGLE_CONTROLLER_ID
            if strict_mode and gnorm in strict_set:
                start = int(rng["start"])
                end = int(rng["end"])
                controller_id = f"srne_{gnorm.lower()}_r{start}_{end}"
                controllers.append({
                    "id": controller_id,
                    "update_interval": f"${{{interval_var_name_main()}}}000ms",
                })
            for addr in rng.get("addresses", []):
                controller_for_addr_by_group.setdefault(gnorm, {})[int(addr)] = controller_id

    # 既定のシンプル設定で core.yaml を生成
    core_yaml = generate_core_yaml(
        controllers=controllers,
        # pins/baud/flow_control は wizard 側で選択された値を反映できるよう、
        # 後段で上書きされることを想定した既定値を使用
    )
    (srne_dir / "core.yaml").write_text(core_yaml, encoding="utf-8")
    anchors_written = False
    for g in all_groups:
        g_up = g.upper()
        is_packed = g_up in packed_groups
        is_strict = strict_mode and g_up in strict_set
        # Non-packed entities are read by their assigned controller; adding anchors would double-read.
        anchors_content = generate_anchors_group_yaml(
            g,
            anchor_ranges_by_group.get(g, ranges_by_group.get(g, [])),
            strict=is_strict,
            regs_map=regs_by_group_addr.get(g, {}),
            packed=is_packed,
            disable=(not is_packed),
        )
        # 無効時はファイルを生成しない
        a_fname = None
        if anchors_content and not anchors_content.strip().startswith("# Anchors disabled"):
            if not anchors_written:
                anchors_dir.mkdir(parents=True, exist_ok=True)
                anchors_written = True
            a_fname = anchors_dir / f"{g.lower()}_anchors.yaml"
            a_fname.write_text(anchors_content, encoding="utf-8")

        entities_content = generate_entities_group_yaml(
            g,
            ranges_by_group.get(g, []),
            regs_by_group_addr.get(g, {}),
            controller_for_addr=controller_for_addr_by_group.get(g.upper()),
            packed=(g_up in packed_groups),
        )
        e_fname = custom_dir / f"entities_{g.lower()}.yaml"
        if e_fname.exists() and not args.custom_overwrite:
            pass
        else:
            e_fname.write_text(entities_content, encoding="utf-8")
        files[g] = (a_fname, e_fname)

    # Build intervals_user/system files for substitutions
    user_lines = [
        "# User-editable intervals (seconds only)",
        "# ここだけ編集してください。main更新秒と各グループのskip_updatesを指定します。",
        "main_update_interval_s: '5'",
    ]
    group_loads: Dict[str, int] = {}
    group_force_counts: Dict[str, int] = {}
    for g in all_groups:
        gnorm = normalize_group(g)
        load = 0
        for rng in (ranges_by_group.get(g, []) or []):
            try:
                load += len(rng.get("addresses") or [])
            except Exception:
                pass
        group_loads[gnorm] = max(1, load)
        group_force_counts[gnorm] = estimate_group_force_new_range_count(
            gnorm,
            ranges_by_group.get(g, []) or [],
            regs_by_group_addr.get(g, {}) if isinstance(regs_by_group_addr, dict) else {},
        )
    skip_defaults = suggest_group_skip_defaults(all_groups, group_loads, group_force_counts)
    for g in all_groups:
        gnorm = normalize_group(g)
        default_skip = int(skip_defaults.get(gnorm, 119))
        user_lines.append(f"{skip_updates_var_name(g)}: '{default_skip}'")
        if gnorm == "P02":
            user_lines.append("p02_slow_skip_updates: '11'")
    (srne_dir / "intervals.yaml").write_text("\n".join(user_lines) + "\n", encoding="utf-8")

    # Build root YAML: substitutions merge includes + packages
    lines = [
        "# Auto-generated by yaml_generator.py",
        "# 必要に応じてUARTピン等を調整してください",
        "substitutions:",
        "  <<: !include srne/intervals.yaml",
        "",
        "packages:",
        "  core: !include srne/core.yaml",
    ]
    for g in all_groups:
        anchor_path, _ = files[g]
        if anchor_path is not None:
            lines.append(f"  {g.lower()}_anchors: !include srne/anchors/{g.lower()}_anchors.yaml")
        lines.append(f"  {g.lower()}_entities: !include srne/custom/entities_{g.lower()}.yaml")
    lines.append("")
    root_yaml = "\n".join(lines)
    (out_root / "srne_inverter.yaml").write_text(root_yaml, encoding="utf-8")

    print("Generated YAML files:")
    print(f"  - {out_root / 'srne_inverter.yaml'}")
    for g, pair in files.items():
        a, e = pair
        if a is not None:
            print(f"  - {a}")
        print(f"  - {e}")


if __name__ == "__main__":
    main()
