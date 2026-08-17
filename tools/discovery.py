#!/usr/bin/env python3
"""
Phase 1 – Register Discovery

単一レジスタ読み (FC=3, count=1) を各アドレスに対して行い、
成功/失敗/値を記録して implemented_registers.json を出力します。

依存: minimalmodbus (推奨)。未インストールでも dry-run でJSONを雛形出力可能。
"""
import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from .common import (
    RegisterDef,
    JSON_CATALOG_PATH,
    load_register_defs,
    register_count_for_type,
    write_json,
)


def try_import_minimalmodbus():
    try:
        import minimalmodbus  # type: ignore
        return minimalmodbus
    except Exception:
        return None


def discover(
    port: str,
    slave_id: int,
    baudrate: int,
    timeout: float,
    delay_ms: int,
    regs: List[RegisterDef],
    dry_run: bool = False,
    validate_32bit: bool = True,
    signed_check: bool = False,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    mm = try_import_minimalmodbus()
    if not dry_run and mm is None:
        raise RuntimeError("minimalmodbus is required for a hardware discovery scan")

    instrument = None
    if not dry_run and mm is not None:
        instrument = mm.Instrument(port, slave_id)
        instrument.serial.baudrate = baudrate
        instrument.serial.bytesize = 8
        instrument.serial.parity = mm.serial.PARITY_NONE
        instrument.serial.stopbits = 1
        instrument.serial.timeout = timeout
        instrument.mode = mm.MODE_RTU

    total = len(regs)
    success_cnt = 0
    fail_cnt = 0
    for idx, r in enumerate(regs, start=1):
        rec: Dict[str, Any] = {
            "group": r.group,
            "address": r.address,
            "name": r.name,
            "rw": r.rw,
            "multiplier": r.multiplier,
            "unit": r.unit,
            "data_type": r.data_type,
            "enums": r.enums,
            "description": r.description,
            "min": getattr(r, "min", None),
            "max": getattr(r, "max", None),
            "step": getattr(r, "step", None),
            "success": False,
            "value": None,
            "error": None,
        }
        if dry_run or instrument is None:
            out.append(rec)
            sys.stdout.write(
                f"\r[discovery] {idx}/{total} ({int(idx*100/total)}%) success={success_cnt} fail={fail_cnt}"
            )
            sys.stdout.flush()
            continue

        try:
            val = instrument.read_register(r.address, 0, functioncode=3, signed=False)
            rec["success"] = True
            rec["value"] = val
            success_cnt += 1
        except Exception as e:
            rec["success"] = False
            rec["error"] = str(e)
            fail_cnt += 1
        # Optional diagnostics
        try:
            dt = r.data_type or ""
            regcnt = register_count_for_type(dt)
            # 32-bit validation: ensure both words can be read in one shot
            if validate_32bit and regcnt == 2 and instrument is not None:
                try:
                    words = instrument.read_registers(r.address, 2, functioncode=3)
                    rec["pair_exists"] = True
                    rec["words"] = words
                except Exception as ee:
                    rec["pair_exists"] = False
                    rec["error_32bit"] = str(ee)
            # Signed-check diagnostics
            if signed_check and instrument is not None:
                if isinstance(dt, str) and dt.lower().startswith("int") and regcnt == 1:
                    try:
                        sval = instrument.read_register(r.address, 0, functioncode=3, signed=True)
                        rec["value_signed"] = sval
                    except Exception as se:
                        rec["signed_error"] = str(se)
                elif isinstance(dt, str) and dt.lower().startswith("int") and regcnt == 2:
                    # read two words and compose little-endian int32
                    try:
                        words = rec.get("words")
                        if not words:
                            words = instrument.read_registers(r.address, 2, functioncode=3)
                        lo, hi = int(words[0]) & 0xFFFF, int(words[1]) & 0xFFFF
                        u32 = (hi << 16) | lo
                        i32 = u32 - (1 << 32) if (u32 & 0x80000000) else u32
                        rec["value_u32"] = u32
                        rec["value_i32"] = i32
                    except Exception as se2:
                        rec["signed_error"] = str(se2)
        except Exception:
            pass
        out.append(rec)
        sys.stdout.write(
            f"\r[discovery] {idx}/{total} ({int(idx*100/total)}%) success={success_cnt} fail={fail_cnt}"
        )
        sys.stdout.flush()
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
    sys.stdout.write("\n")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="SRNE inverter register discovery")
    p.add_argument("--port", help="Serial port (e.g. /dev/ttyUSB0 or COM3)")
    p.add_argument("--slave", type=int, default=1, help="Modbus slave id")
    p.add_argument("--baud", type=int, default=9600, help="Baudrate")
    p.add_argument("--timeout", type=float, default=0.5, help="Serial timeout seconds")
    p.add_argument("--delay-ms", type=int, default=50, help="Inter request delay ms")
    p.add_argument("--catalog", type=Path, default=JSON_CATALOG_PATH, help="Path to JSON catalog")
    p.add_argument("--output", type=Path, default=Path("tools/build/implemented_registers.json"))
    p.add_argument("--dry-run", action="store_true", help="Do not access serial; just emit template JSON")
    p.add_argument(
        "--validate-32bit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="For 32-bit types, verify both words can be read (default: enabled)",
    )
    p.add_argument("--signed-check", action="store_true", help="For int types, also read with signed and record diagnostic values")
    args = p.parse_args()

    regs = load_register_defs(args.catalog)
    result = discover(
        port=args.port or "",
        slave_id=args.slave,
        baudrate=args.baud,
        timeout=args.timeout,
        delay_ms=args.delay_ms,
        regs=regs,
        dry_run=args.dry_run or not bool(args.port),
        validate_32bit=args.validate_32bit,
        signed_check=args.signed_check,
    )
    write_json(args.output, result)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
