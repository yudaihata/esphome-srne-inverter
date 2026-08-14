#!/usr/bin/env python3
"""
Generate derived_recipes entries for P10 Fault Records.

Usage:
  python tools/gen_fault_record_recipes.py --start 0xF800 --count 4

Prints JSON snippet to stdout that you can paste into docs/derived_recipes.json.
"""
from __future__ import annotations
import argparse
import json

CODE_MAP = {
    1:  "Battery Undervoltage Alarm",
    2:  "Battery Discharge Overcurrent Protection (Software)",
    3:  "Battery Disconnection Fault",
    4:  "Battery End of Discharge Protection",
    5:  "Battery Overcurrent Protection (Hardware)",
    6:  "Battery Charge Overvoltage Protection",
    7:  "DC Link Overvoltage Protection (Hardware)",
    8:  "DC Link Overvoltage Protection (Software)",
    9:  "PV Input Overvoltage Protection",
    10: "PV Boost Stage Overcurrent Protection (Software)",
    11: "PV Boost Stage Overcurrent Protection (Hardware)",
    12: "SPI Communication Fault",
    13: "Bypass Output Overload Protection",
    14: "Inverter Output Overload Protection",
    15: "Inverter Output Overcurrent Protection (Hardware)",
    16: "Slave DSP PWM Shutdown Request Fault",
    17: "Inverter Output Short Circuit Protection",
    18: "DC Link Soft Start Failure",
    19: "MPPT Heatsink Overtemperature Protection",
    20: "Inverter Heatsink Overtemperature Protection",
    21: "Cooling Fan Failure Fault",
    22: "EEPROM Memory Fault",
    23: "Model Configuration Fault",
    24: "DC Link Voltage Imbalance Fault",
    25: "DC Link Short Circuit Protection",
    26: "AC Output Relay Short Circuit Fault",
    28: "AC Input Phase Sequence Error",
    29: "DC Link Undervoltage Protection",
    30: "Battery Capacity Low Alarm (10%)",
    31: "Battery Capacity Critical Alarm (5%)",
    32: "Battery Capacity Shutdown Protection",
    34: "Parallel System CAN Communication Fault",
    35: "Parallel System Address Configuration Fault",
    37: "Parallel System Current Sharing Fault",
    38: "Parallel System Battery Voltage Mismatch Fault",
    39: "Parallel System AC Source Inconsistency Fault",
    40: "Parallel System Hardware Synchronization Fault",
    41: "Inverter DC Link Voltage Fault",
    42: "Parallel System Firmware Version Mismatch Fault",
    43: "Parallel System Connection Fault",
    44: "Serial Number Configuration Fault",
    45: "Split Phase Mode Configuration Fault",
    56: "PV Insulation Resistance Fault",
    57: "Residual Current Protection Fault",
    58: "BMS Communication Fault",
    60: "BMS Undervoltage Temperature Alarm",
    61: "BMS Overtemperature Alarm",
    62: "BMS Overcurrent Alarm",
    63: "BMS Undervoltage Alarm",
    64: "BMS Overvoltage Alarm",
}


def code_switch_inline(var: str = "code") -> str:
    lines = [f"std::string code_text; switch({var}){{"]
    for k in sorted(CODE_MAP.keys()):
        lines.append(f"  case {k}: code_text=\\\"{CODE_MAP[k]}\\\"; break;")
    lines.append("  default: { char t[16]; snprintf(t,sizeof(t),\\\"0x%02X\\\", " + var + "); code_text=std::string(t);} }")
    return " ".join(lines)


def make_recipe(index: int, base: int) -> dict:
    rec = {
        "id": f"p10_fault_record_{index}",
        "group": "P10",
        "requires": [base],
        "numeric": [],
        "template_sensor": {
            "id": f"fault_record_{index}_text",
            "name": f"Fault Record {index}",
            "update_interval": "600s",
        },
        "skip_base_addresses": list(range(base, base + 16)),
    }
    # numeric 16 words
    for off in range(16):
        a = base + off
        rec["numeric"].append({
            "address": a,
            "id": f"fr{index}_off{off:02X}",
            "name": f"FaultRec{index}_{off:02X}",
            "value_type": "U_WORD",
        })
    # Lambda
    code = f"uint16_t code = (uint16_t) id(fr{index}_off00).state;\\nif (code == 0) {{ return {{}}; }}\\n"
    code += code_switch_inline("code") + "\\n"
    code += (
        f"uint16_t ym = (uint16_t) id(fr{index}_off01).state; "
        f"uint16_t dh = (uint16_t) id(fr{index}_off02).state; "
        f"uint16_t ms = (uint16_t) id(fr{index}_off03).state; "
        "int year = 2000 + ((ym >> 8) & 0xFF); int month = (ym & 0xFF); "
        "int day = ((dh >> 8) & 0xFF); int hour = (dh & 0xFF); "
        "int minute = ((ms >> 8) & 0xFF); int second = (ms & 0xFF); "
        "char buf[320]; int n = snprintf(buf, sizeof(buf), \"code=%u(%s), time=%04d-%02d-%02d %02d:%02d:%02d, data=\", code, code_text.c_str(), year, month, day, hour, minute, second); "
    )
    # Append 12 data words
    data_ids = ", ".join([f"(uint16_t) id(fr{index}_off{off:02X}).state" for off in range(4, 16)])
    code += (
        "snprintf(buf+n, sizeof(buf)-n, \"0x%04X,0x%04X,0x%04X,0x%04X,0x%04X,0x%04X,0x%04X,0x%04X,0x%04X,0x%04X,0x%04X,0x%04X\","
        + data_ids + "); return std::string(buf);"
    )
    rec["template_sensor"]["lambda"] = code
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="0xF800")
    ap.add_argument("--count", type=int, default=4)
    args = ap.parse_args()
    base0 = int(str(args.start), 0)
    out = []
    for i in range(args.count):
        out.append(make_recipe(i, base0 + 16 * i))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
