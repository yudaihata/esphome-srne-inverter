#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

CATALOG = Path('docs/srne_hybrid_modbus_v1.96.json')
OUT = Path('esphome/standard_p02_all.yaml')


def value_type(t: str) -> str:
    t = (t or '').lower()
    if t in ('uint16', 'u16', 'word'): return 'U_WORD'
    if t in ('int16', 's16'): return 'S_WORD'
    if t in ('uint32', 'u32', 'dword'): return 'U_DWORD'
    if t in ('int32', 's32'): return 'S_DWORD'
    if t in ('float', 'f32', 'ieee754'): return 'FP32'
    return 'U_WORD'


def main() -> None:
    raw = json.loads(CATALOG.read_text(encoding='utf-8'))
    items = []
    for g in raw['groups']:
        if g.get('id') == 'inverter_data_area':
            for it in g['items']:
                name = (it.get('label') or {}).get('en', '')
                t = it.get('type') or ''
                addr = it.get('address_dec')
                scale = it.get('scale', 1.0)
                unit = it.get('unit') or ''
                items.append((int(addr), name, t, float(scale), unit))
    items.sort()

    y = []
    y += [
        "esphome:",
        "  name: srne-p02-all",
        "  friendly_name: SRNE-P02-All",
        "",
        "esp32:",
        "  board: esp32-s3-devkitc-1",
        "  framework:",
        "    type: arduino",
        "",
        "logger:",
        "  level: VERY_VERBOSE",
        "  logs:",
        "    modbus: VERY_VERBOSE",
        "    modbus_controller: VERY_VERBOSE",
        "",
        "wifi:",
        "  ssid: !secret wifi_ssid",
        "  password: !secret wifi_password",
        "",
        "api:",
        "",
        "ota:",
        "  platform: esphome",
        "",
        "uart:",
        "  id: uart_bus",
        "  tx_pin: GPIO5",
        "  rx_pin: GPIO7",
        "  baud_rate: 9600",
        "  parity: NONE",
        "  stop_bits: 1",
        "",
        "modbus:",
        "  id: modbus1",
        "  uart_id: uart_bus",
        "  send_wait_time: 400ms",
        "  flow_control_pin: GPIO6",
        "",
        "modbus_controller:",
        "  - id: srne_p02_all",
        "    address: 1",
        "    modbus_id: modbus1",
        "    command_throttle: 800ms",
        "    update_interval: 5s",
        "",
        "sensor:",
    ]

    for addr, name, t, scale, unit in items:
        vt = value_type(t)
        safe_name = name.replace('\\\\','/').replace('"','\"')
        y += [
            "  - platform: modbus_controller",
            "    modbus_controller_id: srne_p02_all",
            "    register_type: holding",
            f"    address: {addr}",
            f"    value_type: {vt}",
            f"    name: \"{safe_name}\"",
        ]
        if unit:
            y.append(f"    unit_of_measurement: \"{unit}\"")
        if abs(scale - 1.0) > 1e-12:
            y += [
                "    filters:",
                f"      - multiply: {scale}",
            ]
        y.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(y) + "\n", encoding='utf-8')
    print(f"Wrote {OUT}")


if __name__ == '__main__':
    main()
