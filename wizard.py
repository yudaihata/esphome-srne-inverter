#!/usr/bin/env python3
"""Interactive discovery and ESPHome configuration wizard."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.common import (
    JSON_CATALOG_PATH,
    load_register_defs,
    write_json,
    ensure_dir,
    normalize_group,
)
import tools.discovery as disc
import tools.range_builder as rb
from tools.i18n import (
    SUPPORTED_LANGUAGES,
    detect_system_language,
    set_language,
    tr,
)

# yaml_generator から内部関数を再利用
import tools.yaml_generator as yg


BUILD_DIR = Path("tools/build")
ESPHOME_DIR = Path("esphome")
WIZ_STATE_PATH = BUILD_DIR / "wizard_state.json"


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def load_wizard_state(path: Path | None = None) -> Dict[str, Any]:
    path = WIZ_STATE_PATH if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def update_wizard_state(updates: Dict[str, Any], path: Path | None = None) -> None:
    """Merge preferences and scan metadata without dropping unrelated state."""
    path = WIZ_STATE_PATH if path is None else path
    state = load_wizard_state(path)
    state.update(updates)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def select_language(requested: str | None, interactive: bool = True) -> str:
    """Resolve CLI, saved, and system language preferences in that order."""
    state = load_wizard_state()
    saved = state.get("language")
    if requested in SUPPORTED_LANGUAGES:
        return str(requested)
    if requested != "auto" and saved in SUPPORTED_LANGUAGES:
        return str(saved)

    detected = detect_system_language()
    if requested == "auto" or not interactive:
        return detected

    default_choice = "2" if detected == "ja" else "1"
    while True:
        choice = input(tr("language.prompt", default=default_choice)).strip()
        if not choice:
            choice = default_choice
        if choice == "1":
            return "en"
        if choice == "2":
            return "ja"
        print(tr("language.retry"))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover SRNE inverter registers and generate ESPHome YAML."
    )
    parser.add_argument(
        "--lang",
        choices=("auto", *SUPPORTED_LANGUAGES),
        help="wizard language: en, ja, or auto (saved preference when omitted)",
    )
    return parser.parse_args(argv)


def scan_result_is_reusable(
    implemented_path: Path,
    state: Dict[str, Any],
    catalog_path: Path = JSON_CATALOG_PATH,
) -> bool:
    """Only reuse discovery output produced from the current catalog."""
    if not implemented_path.exists() or not state:
        return False
    if state.get("json_catalog_sha256") != file_sha256(catalog_path):
        return False
    if not isinstance(state.get("slave_id"), int) or not isinstance(state.get("baudrate"), int):
        return False
    try:
        records = json.loads(implemented_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(records, list) and bool(records)


def yaml_string(value: str) -> str:
    """JSON strings are valid YAML strings and safely escape user input."""
    return json.dumps(value, ensure_ascii=False)


def ensure_management_secrets(path: Path) -> List[str]:
    """Append missing ESPHome management secrets without replacing Wi-Fi data."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    existing = set(re.findall(r"(?m)^([A-Za-z_][A-Za-z0-9_]*):", content))
    generated = {
        "fallback_ap_password": secrets.token_urlsafe(18),
        "api_encryption_key": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        "ota_password": secrets.token_urlsafe(24),
    }
    missing = [key for key in generated if key not in existing]
    if not missing:
        return []
    suffix = "" if not content or content.endswith("\n") else "\n"
    suffix += "".join(f"{key}: {yaml_string(generated[key])}\n" for key in missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + suffix, encoding="utf-8")
    return missing


def print_header(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def prompt(text: str, default: Optional[str] = None) -> str:
    if default is None:
        return input(f"{text}: ").strip()
    else:
        s = input(f"{text} [{default}]: ").strip()
        return s or default


def yes_no(text: str, default_yes: bool = True) -> bool:
    if default_yes:
        prompt_str = tr("common.yes_no_default_yes", text=text)
    else:
        prompt_str = tr("common.yes_no_default_no", text=text)
    s = input(prompt_str).strip().lower()
    if not s:
        return default_yes
    return s in ("y", "yes", "はい")


def try_list_serial_ports_all():
    try:
        import serial.tools.list_ports  # type: ignore
        return list(serial.tools.list_ports.comports())
    except Exception:
        return []


def is_usb_port(port_info) -> bool:
    dev = getattr(port_info, "device", "") or ""
    desc = getattr(port_info, "description", "") or ""
    hwid = getattr(port_info, "hwid", "") or ""
    # Linux
    if dev.startswith("/dev/ttyUSB") or dev.startswith("/dev/ttyACM"):
        return True
    # macOS
    if "/dev/tty.usb" in dev or "/dev/cu.usb" in dev:
        return True
    # Windows: デバイス名だけではUSB判定できない場合があるため、説明/ハードウェアIDを参照
    if "USB" in desc.upper() or "VID:PID" in hwid.upper():
        return True
    return False


def list_usb_serial_ports() -> List[Dict[str, str]]:
    ports = try_list_serial_ports_all()
    usb_ports = [p for p in ports if is_usb_port(p)]
    out: List[Dict[str, str]] = []
    for p in usb_ports:
        out.append({
            "device": getattr(p, "device", ""),
            "description": getattr(p, "description", ""),
        })
    return out


def check_minimalmodbus() -> bool:
    try:
        import minimalmodbus  # type: ignore
        return True
    except Exception:
        return False


def check_pyserial() -> bool:
    try:
        import serial.tools.list_ports  # type: ignore
        return True
    except Exception:
        return False


def step_environment() -> Dict[str, Any]:
    print_header(tr("environment.title"))
    print("Python:", sys.version.split(" ")[0])
    has_mm = check_minimalmodbus()
    print("minimalmodbus:", "OK" if has_mm else tr("common.not_installed"))
    if not has_mm:
        print(
            tr(
                "environment.missing_dependency",
                package="minimalmodbus",
                purpose=tr("environment.purpose_modbus"),
            )
        )
        print(tr("environment.install", package="minimalmodbus"))
        print(tr("environment.rerun"))
        sys.exit(1)

    has_pyserial = check_pyserial()
    print("pyserial:", "OK" if has_pyserial else tr("common.not_installed"))
    if not has_pyserial:
        print(
            tr(
                "environment.missing_dependency",
                package="pyserial",
                purpose=tr("environment.purpose_serial"),
            )
        )
        print(tr("environment.install", package="pyserial"))
        print(tr("environment.rerun"))
        sys.exit(1)

    # スキャン結果の再利用確認はステップ1で行う
    return {}


def step_discovery(env: Dict[str, Any]) -> Path:
    print_header(tr("discovery.title"))
    # 既存スキャン結果があれば、最初に再利用可否を確認して即終了できるようにする
    implemented_default = BUILD_DIR / "implemented_registers.json"
    prev = load_wizard_state()
    if scan_result_is_reusable(implemented_default, prev):
        print(tr("discovery.previous_found", path=implemented_default))
        if yes_no(tr("discovery.reuse_previous"), True):
            return implemented_default
    elif implemented_default.exists():
        print(tr("discovery.previous_mismatch"))

    # 以降は新規スキャンのためのポート選択
    # ポート選択（ここで実施）
    # 前回ポートの再利用候補
    prev_port = ""
    prev_port = str(prev.get("port") or "")

    port: str = ""
    while True:
        devices = list_usb_serial_ports()
        # 前回ポートが生きていれば優先採用（確認のみ）
        if prev_port and any(d.get("device") == prev_port for d in devices):
            sel = input(tr("discovery.use_port", port=prev_port)).strip().lower()
            if sel in ("",):
                port = prev_port
                break
            if sel == "q":
                print(tr("common.finished"))
                sys.exit(0)
            # 選び直し
            prev_port = ""
            continue

        if not devices:
            sel = input(tr("discovery.usb_not_found")).strip().lower()
            if sel == "q":
                print(tr("common.finished"))
                sys.exit(0)
            # Enter またはその他で再スキャン
            continue
        if len(devices) == 1:
            dev = devices[0]
            sel = input(tr("discovery.detected_port", port=dev.get("device", ""))).strip().lower()
            if sel in ("",):
                port = dev.get("device", "")
                break
            if sel == "q":
                print(tr("common.finished"))
                sys.exit(0)
            # r などは再スキャン
            continue
        # 複数検出
        print(tr("discovery.connected_ports"))
        for i, info in enumerate(devices, 1):
            print(f"  {i}) {info.get('device','')}  -  {info.get('description','')}")
        sel = input(tr("discovery.select_port", count=len(devices))).strip().lower()
        if sel == "q":
            print(tr("common.finished"))
            sys.exit(0)
        if sel == "":
            continue
        if sel.isdigit():
            idx = int(sel)
            if 1 <= idx <= len(devices):
                port = devices[idx - 1].get("device", "")
                if port:
                    break
        print(tr("common.invalid_selection"))

    print(tr("discovery.using_port", port=port))

    catalog_hash = file_sha256(JSON_CATALOG_PATH)

    out_path = BUILD_DIR / "implemented_registers.json"
    ensure_dir(BUILD_DIR)

    # 標準設定で進めるかを先に確認（既定: はい）
    if yes_no(tr("discovery.standard_settings"), True):
        # 既定値（安全側）を使用（前回値があれば再利用）
        slave = int(prev.get("slave_id", 1))
        baud = int(prev.get("baudrate", 9600))
        timeout = float(prev.get("timeout", 0.5))
        delay_ms = int(prev.get("delay_ms", 50))
    else:
        # 詳細設定（必要なときだけ表示）
        slave = int(prompt(tr("discovery.slave_id"), str(prev.get("slave_id", 1))))
        baud = int(prompt(tr("discovery.baud_rate"), str(prev.get("baudrate", 9600))))
        timeout = float(prompt(tr("discovery.timeout"), str(prev.get("timeout", 0.5))))
        delay_ms = int(prompt(tr("discovery.request_delay"), str(prev.get("delay_ms", 50))))

    regs = load_register_defs(JSON_CATALOG_PATH)
    print(tr("discovery.register_count", count=len(regs)))
    # 詳細設定に分岐しない場面なので、否定は「いいえ」ではなくキャンセルの表現に統一
    resp = input(tr("discovery.start")).strip().lower()
    if resp == "c":
        print(tr("discovery.cancelled"))
        sys.exit(0)

    result = disc.discover(
        port=port,
        slave_id=slave,
        baudrate=baud,
        timeout=timeout,
        delay_ms=delay_ms,
        regs=regs,
        dry_run=False,
        validate_32bit=True,
    )
    write_json(out_path, result)
    succ = sum(1 for r in result if r.get("success"))
    print(tr("discovery.complete", success=succ, total=len(result), path=out_path))

    # 状態を保存（次回の再利用判定用）
    state = {
        "port": port,
        "slave_id": slave,
        "baudrate": baud,
        "timeout": timeout,
        "delay_ms": delay_ms,
        "json_catalog_sha256": catalog_hash,
        "implemented_path": str(out_path),
    }
    update_wizard_state(state)
    return out_path


def step_ranges(implemented_path: Path) -> Path:
    print_header(tr("ranges.title"))
    with implemented_path.open("r", encoding="utf-8") as f:
        records = json.load(f)
    # 連続まとめ読みは 32 語以内に分割（機器上限に合わせた標準設定）
    built = rb.build_ranges(records, max_chunk=32)
    out_path = BUILD_DIR / "device_specific_ranges.json"
    write_json(out_path, built)

    # 簡易サマリ
    ranges = built.get("ranges", {})
    total_blocks = sum(len(v) for v in ranges.values())
    print(
        tr(
            "ranges.complete",
            groups=len(ranges),
            blocks=total_blocks,
            path=out_path,
        )
    )
    for g in sorted(ranges.keys()):
        blks = ranges.get(g, [])
        print(tr("ranges.block_summary", group=g, count=len(blks)))
    return out_path


def step_yaml(implemented_path: Path, ranges_path: Path) -> None:
    print_header(tr("yaml.title"))
    impl = yg.load_json(implemented_path)
    ranged = yg.load_json(ranges_path)

    ranges_by_group = ranged.get("ranges", {})
    all_groups = sorted(ranges_by_group.keys())

    # 実装レジスタを group/address で引けるように
    regs_by_group_addr: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for rec in impl:
        if not rec.get("success"):
            continue
        g = normalize_group(rec.get("group", ""))
        regs_by_group_addr.setdefault(g, {})[int(rec["address"])] = rec

    # カタログの倍率/単位/名称/enums をフォールバックで補完
    try:
        cat_regs = load_register_defs(JSON_CATALOG_PATH)
        cat_map: Dict[tuple, Dict[str, Any]] = {}
        for r in cat_regs:
            key = (normalize_group(r.group), int(r.address))
            cat_map[key] = {
                "multiplier": float(getattr(r, "multiplier", 1.0) or 1.0),
                "unit": getattr(r, "unit", "") or "",
                "name": getattr(r, "name", "") or "",
                "enums": getattr(r, "enums", None),
            }
        for g, amap in regs_by_group_addr.items():
            for a, rec in amap.items():
                meta = cat_map.get((normalize_group(g), int(a)))
                if not meta:
                    continue
                # 補完: 倍率/単位
                try:
                    m = float(rec.get("multiplier") if rec.get("multiplier") is not None else 1.0)
                except Exception:
                    m = 1.0
                if abs(m - 1.0) < 1e-12 and abs(meta.get("multiplier", 1.0) - 1.0) > 1e-12:
                    rec["multiplier"] = meta.get("multiplier", 1.0)
                u = (rec.get("unit") or "").strip()
                mu = (meta.get("unit") or "").strip()
                if (not u) and mu:
                    rec["unit"] = meta.get("unit")
                # 補完: 名称/enums は常にカタログを優先
                if (meta.get("name") or "").strip():
                    rec["name"] = meta.get("name")
                rec["enums"] = meta.get("enums")
    except Exception:
        pass

    srne_dir = ESPHOME_DIR / "srne"
    custom_dir = srne_dir / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)

    # ターゲットボード と UART/GPIO, Wi‑Fi 設定（ESP32系のみ）
    print(tr("yaml.select_board"))
    presets = [
        {"label": "ESP32-S3 DevKitC-1", "board": "esp32-s3-devkitc-1"},
        {"label": "ESP32 DevKit", "board": "esp32dev"},
        {"label": tr("yaml.board_manual"), "board": None},
    ]
    for i, p in enumerate(presets, 1):
        print(f"  {i}) {p['label']}")
    while True:
        sel = input(f"{tr('yaml.board_number')}: ").strip()
        if sel.isdigit():
            sel_idx = int(sel)
            if 1 <= sel_idx <= len(presets):
                break
        print(tr("yaml.select_board_invalid", count=len(presets)))
    chosen = presets[sel_idx - 1]
    if chosen["board"] is None:
        # 手動入力
        board_name = prompt(tr("yaml.board_name"), "esp32dev")
    else:
        board_name = chosen["board"]
    # UART ピンは全員手動入力（Enterでボード既定値を採用）。
    def _ask_gpio(label: str, default_gpio: str) -> str:
        while True:
            s = input(f"{label} [{default_gpio}]: ").strip()
            if not s:
                return default_gpio
            su = s.upper()
            if su.startswith("GPIO") and su[4:].isdigit():
                return su
            if s.isdigit():
                return f"GPIO{s}"
            print(tr("yaml.gpio_invalid"))

    if board_name == "esp32-s3-devkitc-1":
        # シンプル構成の既定: TX=GPIO5, RX=GPIO7
        uart_tx = _ask_gpio(tr("yaml.pin_tx"), "GPIO5")
        uart_rx = _ask_gpio(tr("yaml.pin_rx"), "GPIO7")
    elif board_name == "esp32dev":
        uart_tx = _ask_gpio(tr("yaml.pin_tx"), "GPIO17")
        uart_rx = _ask_gpio(tr("yaml.pin_rx"), "GPIO16")
    else:
        uart_tx = _ask_gpio(tr("yaml.pin_tx"), "GPIO17")
        uart_rx = _ask_gpio(tr("yaml.pin_rx"), "GPIO16")
    # Modbus基本設定はプロンプトせず、前回値があれば再利用、なければ既定値を使用
    slave_addr_cfg = 1
    baud_cfg = 9600
    try:
        if WIZ_STATE_PATH.exists():
            _prev = json.loads(WIZ_STATE_PATH.read_text(encoding="utf-8"))
            slave_addr_cfg = int(_prev.get("slave_id", slave_addr_cfg))
            baud_cfg = int(_prev.get("baudrate", baud_cfg))
    except Exception:
        pass

    # RS485の方向制御ピン(flow_control_pin)の設定（任意）
    def _ask_gpio_optional(label: str, default_gpio: str = "") -> str:
        while True:
            hint = tr("yaml.gpio_optional_hint", default=default_gpio) if default_gpio else ""
            s = input(tr("yaml.gpio_optional", label=label, hint=hint)).strip()
            if not s:
                return default_gpio
            if s.lower() in ("none", "null", "off", "-"):
                return ""
            su = s.upper()
            if su.startswith("GPIO") and su[4:].isdigit():
                return su
            if s.isdigit():
                return f"GPIO{s}"
            print(tr("yaml.gpio_invalid"))

    # 直接入力方式: Enterなら未設定(None)として生成
    flow_control_pin = _ask_gpio_optional(tr("yaml.direction_pin"), "")

    # Wi‑Fiシークレットの登録（既存があれば再利用 or 直接上書きの二択に集約）
    secrets_path = ESPHOME_DIR / "secrets.yaml"
    if secrets_path.exists():
        print(tr("yaml.wifi_existing", path=secrets_path))
        reuse = yes_no(tr("yaml.wifi_reuse"), True)
        if not reuse:
            ssid = prompt(tr("yaml.wifi_ssid"))
            pw = getpass.getpass(tr("yaml.wifi_password"))
            secrets_path.parent.mkdir(parents=True, exist_ok=True)
            secrets_content = f"wifi_ssid: {yaml_string(ssid)}\nwifi_password: {yaml_string(pw)}\n"
            secrets_path.write_text(secrets_content, encoding="utf-8")
            print(tr("yaml.wifi_updated", path=secrets_path))
    else:
        if yes_no(tr("yaml.wifi_register"), True):
            ssid = prompt(tr("yaml.wifi_ssid"))
            pw = getpass.getpass(tr("yaml.wifi_password"))
            secrets_path.parent.mkdir(parents=True, exist_ok=True)
            secrets_content = f"wifi_ssid: {yaml_string(ssid)}\nwifi_password: {yaml_string(pw)}\n"
            secrets_path.write_text(secrets_content, encoding="utf-8")
            print(tr("yaml.wifi_written", path=secrets_path))
        else:
            print(tr("yaml.wifi_missing"))
            sys.exit(1)

    added_secrets = ensure_management_secrets(secrets_path)
    if added_secrets:
        print(tr("yaml.management_secrets_created"))

    # 単一controller構成（全グループ共通）
    controllers: List[Dict[str, Any]] = [{
        "id": yg.SINGLE_CONTROLLER_ID,
        "update_interval": f"${{{yg.interval_var_name_main()}}}000ms",
    }]
    controller_for_addr_by_group: Dict[str, Dict[int, str]] = {}
    for g in all_groups:
        gup = str(g).upper()
        for rng in ranges_by_group.get(g, []):
            for addr in (rng.get("addresses") or []):
                controller_for_addr_by_group.setdefault(gup, {})[int(addr)] = yg.SINGLE_CONTROLLER_ID

    packed_groups = {
        yg.normalize_group(group)
        for group in all_groups
        if yg.normalize_group(group) in yg.STANDARD_PACKED_GROUPS
    }
    anchor_ranges_by_group: Dict[str, List[Dict[str, Any]]] = {}
    for group in all_groups:
        gnorm = yg.normalize_group(group)
        if gnorm not in packed_groups:
            continue
        group_ranges = ranges_by_group.get(group, [])
        if gnorm == "P02":
            group_ranges = yg.split_ranges_at_boundaries(group_ranges, {527})
        if gnorm == "P10":
            anchor_ranges_by_group[group] = yg.build_p10_packed_read_ranges(
                regs_by_group_addr.get(group, {}),
            )
        else:
            anchor_ranges_by_group[group] = yg.build_packed_read_ranges(
                group_ranges,
                regs_by_group_addr.get(group, {}),
            )

    # core.yaml（UART/MODBUS/Wi‑Fi/API/OTA/Logger）を書き出し（上記の設定で生成）
    core_text = yg.generate_core_yaml(
        uart_tx=uart_tx,
        uart_rx=uart_rx,
        baud_rate=baud_cfg,
        slave_addr=slave_addr_cfg,
        controllers=controllers,
        groups=all_groups,
        board=board_name,
        # 標準生成は INFO レベル
        log_level="INFO",
        # 速度重視設定
        send_wait_time_ms=100,
        command_throttle_ms=100,
        max_command_retries=3,
        offline_skip_updates=10,
        flow_control_pin=(flow_control_pin or None),
    )
    (srne_dir / "core.yaml").write_text(core_text, encoding="utf-8")

    files = {}
    anchor_files: Dict[str, Path] = {}
    anchors_dir = srne_dir / "anchors"
    for g in all_groups:
        # YAMLに制御文字が混入しないようにヌル文字を除去/エスケープ
        def _sanitize_yaml(s: str) -> str:
            try:
                return s.replace("\u0000", "\\0")
            except Exception:
                return s

        is_packed = yg.normalize_group(g) in packed_groups
        if is_packed:
            anchors_dir.mkdir(parents=True, exist_ok=True)
            anchor_content = yg.generate_anchors_group_yaml(
                g,
                anchor_ranges_by_group.get(g, []),
                strict=False,
                regs_map=regs_by_group_addr.get(g, {}),
                packed=True,
            )
            anchor_path = anchors_dir / f"{g.lower()}_anchors.yaml"
            anchor_path.write_text(_sanitize_yaml(anchor_content), encoding="utf-8")
            anchor_files[g] = anchor_path

        # Entities (生成物なので、ウィザード実行時は常に再生成)
        e_fname = custom_dir / f"entities_{g.lower()}.yaml"
        entities_content = yg.generate_entities_group_yaml(
            g,
            ranges_by_group.get(g, []),
            regs_by_group_addr.get(g, {}),
            controller_for_addr=controller_for_addr_by_group.get(g.upper()),
            packed=is_packed,
        )
        e_fname.write_text(_sanitize_yaml(entities_content), encoding="utf-8")
        files[g] = e_fname

    # グループ単位の更新間隔ファイルを生成
    def _ms(v: str) -> int:
        s = v.strip().lower()
        if s.endswith("ms"):
            return int(s[:-2])
        if s.endswith("s"):
            return int(float(s[:-1]) * 1000)
        return int(s)

    # 既定グループ間隔は derived_recipes.json で上書き可能（現在はskip_updates中心運用）
    try:
        _recipes = yg.load_derived_recipes()
    except Exception:
        _recipes = {}
    user_lines = [
        "# User-editable intervals (seconds only)",
        "# Edit only this file: set the main interval and per-group skip_updates.",
        "main_update_interval_s: '5'",
    ]
    group_loads: Dict[str, int] = {}
    group_force_counts: Dict[str, int] = {}
    for g in all_groups:
        gnorm = yg.normalize_group(g)
        load = 0
        for rng in (ranges_by_group.get(g, []) or []):
            try:
                load += len(rng.get("addresses") or [])
            except Exception:
                pass
        group_loads[gnorm] = max(1, load)
        group_force_counts[gnorm] = yg.estimate_group_force_new_range_count(
            gnorm,
            ranges_by_group.get(g, []) or [],
            regs_by_group_addr.get(g, {}),
        )
    skip_defaults = yg.suggest_group_skip_defaults(all_groups, group_loads, group_force_counts)
    # 読みやすさのため、グループ順（P00, P01, ...）でユーザー設定を並べる
    for g in all_groups:
        gup = yg.normalize_group(g)
        user_lines.append(f"{g.lower()}_skip_updates: '{int(skip_defaults.get(gup, 119))}'")
        if gup == "P02":
            user_lines.append("p02_slow_skip_updates: '11'")

    (srne_dir / "intervals.yaml").write_text("\n".join(user_lines) + "\n", encoding="utf-8")
    lines = [
        "# Auto-generated by wizard.py",
        "substitutions:",
        "  <<: !include srne/intervals.yaml",
        "",
        "packages:",
        f"  core: !include srne/core.yaml",
    ]
    for g in all_groups:
        if g in anchor_files:
            lines.append(f"  {g.lower()}_anchors: !include srne/anchors/{g.lower()}_anchors.yaml")
        lines.append(f"  {g.lower()}_entities: !include srne/custom/entities_{g.lower()}.yaml")
    lines.append("")
    root_yaml = "\n".join(lines)
    (ESPHOME_DIR / "srne_inverter.yaml").write_text(root_yaml, encoding="utf-8")

    print(tr("yaml.files_ready"))
    print(f"- {ESPHOME_DIR / 'srne_inverter.yaml'}")
    print(f"- {srne_dir / 'core.yaml'}")
    for a in anchor_files.values():
        print(f"- {a}")
    for g, e in files.items():
        print(f"- {e}")

    print(f"\n{tr('yaml.next_steps')}")
    print(tr("yaml.next_optional"))
    print(tr("yaml.next_build"))


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    language = select_language(args.lang, interactive=sys.stdin.isatty())
    set_language(language)
    update_wizard_state({"language": language})

    print_header(tr("main.title"))
    print(tr("main.intro"))

    # ステップ0: 環境チェック
    env = step_environment()
    # スキャン実施フロー（ステップ1、ここで既存結果の再利用確認も実施）
    print(tr("main.overwrite"))
    implemented_path = step_discovery(env)
    ranges_path = step_ranges(implemented_path)
    step_yaml(implemented_path, ranges_path)
    print(f"\n{tr('main.complete')}")


if __name__ == "__main__":
    main()
