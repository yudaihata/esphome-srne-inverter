#!/usr/bin/env python3
"""
初心者向けステップバイステップ・ウィザード

このスクリプトは以下を対話式に案内します:
  1) 依存/環境チェック、シリアルポート選択 or ドライラン
  2) フェーズ1: レジスタ探索 (implemented_registers.json)
  3) フェーズ2: 範囲構築 (device_specific_ranges.json)
  4) フェーズ3: YAML生成 (esphome/*)

途中で明確な次のアクションとファイル出力パスを表示します。
"""
from __future__ import annotations

import base64
import getpass
import secrets
import sys
import json
from pathlib import Path
import hashlib
import re
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


def load_wizard_state(path: Path = WIZ_STATE_PATH) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


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
    # UI 統一: はい=Enter を基本に表示
    if default_yes:
        prompt_str = f"{text} [Enter=はい / n=いいえ]: "
    else:
        prompt_str = f"{text} [y=はい / Enter=いいえ]: "
    s = input(prompt_str).strip().lower()
    if not s:
        return default_yes
    return s in ("y", "yes")


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
    print_header("ステップ0: 環境チェック")
    print("Python:", sys.version.split(" ")[0])
    has_mm = check_minimalmodbus()
    print("minimalmodbus:", "OK" if has_mm else "未インストール")
    if not has_mm:
        print("- 注意: 実機探索には minimalmodbus が必要です。")
        print("  インストール: pip install minimalmodbus")
        print("  インストール後に再実行してください。")
        sys.exit(1)

    has_pyserial = check_pyserial()
    print("pyserial:", "OK" if has_pyserial else "未インストール")
    if not has_pyserial:
        print("- 注意: USBシリアル自動検出には pyserial が必要です。")
        print("  インストール: pip install pyserial")
        print("  インストール後に再実行してください。")
        sys.exit(1)

    # スキャン結果の再利用確認はステップ1で行う
    return {}


def step_discovery(env: Dict[str, Any]) -> Path:
    print_header("ステップ1: レジスタ探索 (discovery)")
    # 既存スキャン結果があれば、最初に再利用可否を確認して即終了できるようにする
    implemented_default = BUILD_DIR / "implemented_registers.json"
    prev = load_wizard_state()
    if scan_result_is_reusable(implemented_default, prev):
        print(f"既存のスキャン結果が見つかりました: {implemented_default}")
        if yes_no("前回のスキャン結果を利用しますか?", True):
            return implemented_default
    elif implemented_default.exists():
        print("既存のスキャン結果は、現在のカタログと一致しないため再利用しません。")

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
            sel = input(f"前回のポート {prev_port} を使用します。Enter=続行, n=選び直し, q=終了: ").strip().lower()
            if sel in ("",):
                port = prev_port
                break
            if sel == "q":
                print("終了しました。")
                sys.exit(0)
            # 選び直し
            prev_port = ""
            continue

        if not devices:
            sel = input("USB未検出。Enter=再スキャン, q=終了: ").strip().lower()
            if sel == "q":
                print("終了しました。")
                sys.exit(0)
            # Enter またはその他で再スキャン
            continue
        if len(devices) == 1:
            dev = devices[0]
            sel = input(f"検出: {dev.get('device','')}  Enter=続行, r=再スキャン, q=終了: ").strip().lower()
            if sel in ("",):
                port = dev.get("device", "")
                break
            if sel == "q":
                print("終了しました。")
                sys.exit(0)
            # r などは再スキャン
            continue
        # 複数検出
        print("現在接続されているUSBシリアル:")
        for i, info in enumerate(devices, 1):
            print(f"  {i}) {info.get('device','')}  -  {info.get('description','')}")
        sel = input(f"番号を選択 (1-{len(devices)}) / Enter=再スキャン / q=終了: ").strip().lower()
        if sel == "q":
            print("終了しました。")
            sys.exit(0)
        if sel == "":
            continue
        if sel.isdigit():
            idx = int(sel)
            if 1 <= idx <= len(devices):
                port = devices[idx - 1].get("device", "")
                if port:
                    break
        print("無効な選択です。")

    print(f"使用ポート: {port}")

    catalog_hash = file_sha256(JSON_CATALOG_PATH)

    out_path = BUILD_DIR / "implemented_registers.json"
    ensure_dir(BUILD_DIR)

    # 標準設定で進めるかを先に確認（既定: はい）
    if yes_no("標準設定で進めますか?", True):
        # 既定値（安全側）を使用（前回値があれば再利用）
        slave = int(prev.get("slave_id", 1))
        baud = int(prev.get("baudrate", 9600))
        timeout = float(prev.get("timeout", 0.5))
        delay_ms = int(prev.get("delay_ms", 50))
    else:
        # 詳細設定（必要なときだけ表示）
        slave = int(prompt("スレーブID", str(prev.get("slave_id", 1))))
        baud = int(prompt("ボーレート", str(prev.get("baudrate", 9600))))
        timeout = float(prompt("タイムアウト秒", str(prev.get("timeout", 0.5))))
        delay_ms = int(prompt("リクエスト間ウェイト(ms)", str(prev.get("delay_ms", 50))))

    regs = load_register_defs(JSON_CATALOG_PATH)
    print(f"探索対象レジスタ件数: {len(regs)} 件")
    # 詳細設定に分岐しない場面なので、否定は「いいえ」ではなくキャンセルの表現に統一
    resp = input("スキャンを開始しますか? [Enter=はい / c=キャンセル]: ").strip().lower()
    if resp == "c":
        print("キャンセルしました。")
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
    print(f"探索完了: success={succ} / total={len(result)} → {out_path}")

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
    try:
        WIZ_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        WIZ_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return out_path


def step_ranges(implemented_path: Path) -> Path:
    print_header("ステップ2: 範囲構築 (range_builder)")
    with implemented_path.open("r", encoding="utf-8") as f:
        records = json.load(f)
    # 連続まとめ読みは 32 語以内に分割（機器上限に合わせた標準設定）
    built = rb.build_ranges(records, max_chunk=32)
    out_path = BUILD_DIR / "device_specific_ranges.json"
    write_json(out_path, built)

    # 簡易サマリ
    ranges = built.get("ranges", {})
    total_blocks = sum(len(v) for v in ranges.values())
    print(f"連続区間の構築完了: グループ={len(ranges)} / ブロック={total_blocks} → {out_path}")
    for g in sorted(ranges.keys()):
        blks = ranges.get(g, [])
        print(f"  {g}: {len(blks)} ブロック")
    return out_path


def step_yaml(implemented_path: Path, ranges_path: Path) -> None:
    print_header("ステップ3: YAML生成 (yaml_generator)")
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
    print("ターゲットボードを選択してください（番号を入力）")
    presets = [
        {"label": "ESP32-S3 DevKitC-1", "board": "esp32-s3-devkitc-1"},
        {"label": "ESP32 DevKit", "board": "esp32dev"},
        {"label": "スキップ（手動入力）", "board": None},
    ]
    for i, p in enumerate(presets, 1):
        print(f"  {i}) {p['label']}")
    while True:
        sel = input("番号: ").strip()
        if sel.isdigit():
            sel_idx = int(sel)
            if 1 <= sel_idx <= len(presets):
                break
        print(f"無効な入力です。1〜{len(presets)} の番号を入力してください。")
    chosen = presets[sel_idx - 1]
    if chosen["board"] is None:
        # 手動入力
        board_name = prompt("ESP32ボード名 (esp32: board)", "esp32dev")
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
            print("無効な入力です。例: 8 または GPIO8 を入力してください。")

    if board_name == "esp32-s3-devkitc-1":
        # シンプル構成の既定: TX=GPIO5, RX=GPIO7
        uart_tx = _ask_gpio("UART TXピン", "GPIO5")
        uart_rx = _ask_gpio("UART RXピン", "GPIO7")
    elif board_name == "esp32dev":
        uart_tx = _ask_gpio("UART TXピン", "GPIO17")
        uart_rx = _ask_gpio("UART RXピン", "GPIO16")
    else:
        uart_tx = _ask_gpio("UART TXピン", "GPIO17")
        uart_rx = _ask_gpio("UART RXピン", "GPIO16")
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
            hint = f"（既定={default_gpio}）" if default_gpio else ""
            s = input(f"{label} {hint} [未設定=Enter]: ").strip()
            if not s:
                return default_gpio
            if s.lower() in ("none", "null", "off", "-"):
                return ""
            su = s.upper()
            if su.startswith("GPIO") and su[4:].isdigit():
                return su
            if s.isdigit():
                return f"GPIO{s}"
            print("無効な入力です。例: 22 または GPIO22 を入力してください。")

    # 直接入力方式: Enterなら未設定(None)として生成
    flow_control_pin = _ask_gpio_optional("UART 方向制御ピン", "")

    # Wi‑Fiシークレットの登録（既存があれば再利用 or 直接上書きの二択に集約）
    secrets_path = ESPHOME_DIR / "secrets.yaml"
    if secrets_path.exists():
        print(f"既存のWi‑Fi設定が見つかりました: {secrets_path}")
        reuse = yes_no("既存のWi‑Fi設定を再利用しますか?", True)
        if not reuse:
            ssid = prompt("Wi‑Fi SSID")
            pw = getpass.getpass("Wi‑Fi パスワード: ")
            secrets_path.parent.mkdir(parents=True, exist_ok=True)
            secrets_content = f"wifi_ssid: {yaml_string(ssid)}\nwifi_password: {yaml_string(pw)}\n"
            secrets_path.write_text(secrets_content, encoding="utf-8")
            print(f"Wi‑Fi設定を更新しました: {secrets_path}")
    else:
        if yes_no("Wi‑Fi設定（SSID/パスワード）を登録しますか?", True):
            ssid = prompt("Wi‑Fi SSID")
            pw = getpass.getpass("Wi‑Fi パスワード: ")
            secrets_path.parent.mkdir(parents=True, exist_ok=True)
            secrets_content = f"wifi_ssid: {yaml_string(ssid)}\nwifi_password: {yaml_string(pw)}\n"
            secrets_path.write_text(secrets_content, encoding="utf-8")
            print(f"Wi‑Fi設定を書き込みました: {secrets_path}")
        else:
            print("Wi‑Fi設定がないためYAMLを生成できません。")
            sys.exit(1)

    added_secrets = ensure_management_secrets(secrets_path)
    if added_secrets:
        print("API暗号化・OTA・フォールバックAP用の認証情報を生成しました。")

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
        "# ここだけ編集してください。main更新秒と各グループのskip_updatesを指定します。",
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

    print("生成完了。以下のファイルをESPHomeで利用できます:")
    print(f"- {ESPHOME_DIR / 'srne_inverter.yaml'}")
    print(f"- {srne_dir / 'core.yaml'}")
    for a in anchor_files.values():
        print(f"- {a}")
    for g, e in files.items():
        print(f"- {e}")

    print("\n次のステップ:")
    print("- 任意: 不要なエンティティをコメントアウト (custom/entities_*.yaml)")
    print("- ビルド/フラッシュ: ESPHomeで srne_inverter.yaml を実行")


def main() -> None:
    print_header("SRNE Modbus 自動生成ウィザードへようこそ")
    print("このウィザードは次の3段階を自動で案内します: \n"
          "  1) 実装レジスタ探索 → 2) 連続区間構築 → 3) YAML生成")

    # ステップ0: 環境チェック
    env = step_environment()
    # スキャン実施フロー（ステップ1、ここで既存結果の再利用確認も実施）
    print("既存の成果物がある場合でも、全て上書きして再生成します。")
    implemented_path = step_discovery(env)
    ranges_path = step_ranges(implemented_path)
    step_yaml(implemented_path, ranges_path)
    print("\nすべて完了しました。良きSRNEライフを！")


if __name__ == "__main__":
    main()
