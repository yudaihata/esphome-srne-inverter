import json
import re
import subprocess
import sys
from pathlib import Path

from tools.yaml_generator import (
    apply_group_skip_updates,
    build_packed_read_ranges,
    build_p10_packed_read_ranges,
    coalesce_rw_group_reads,
    gen_sensor_entry,
    generate_anchors_group_yaml,
    generate_core_yaml,
    generate_entities_group_yaml,
    split_ranges_at_boundaries,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_core_uses_bounded_retry_and_offline_backoff():
    output = generate_core_yaml(controllers=[{"id": "srne_main", "update_interval": "5s"}])

    assert "command_throttle: 100ms" in output
    assert "max_cmd_retries: 3" in output
    assert "offline_skip_updates: 10" in output
    assert "power_save_mode: NONE" in output


def test_p10_fault_records_are_packed_two_per_request():
    regs = {
        address: {"address": address, "group": "P10", "rw": "RW", "data_type": "uint16"}
        for address in range(63488, 64000, 16)
    }

    ranges = build_p10_packed_read_ranges(regs)
    output = generate_anchors_group_yaml("P10", ranges, regs_map=regs, packed=True)

    assert len(ranges) == 16
    assert all(item["size"] == 32 for item in ranges)
    assert output.count("register_count: 32") == 16
    assert "id(fr0_off00).publish_state" in output
    assert "id(fr31_off0F).publish_state" in output


def test_p03_write_only_buttons_do_not_create_polling_numbers():
    record = {
        "address": 57090,
        "group": "P03",
        "name": "Control Command",
        "rw": "W",
        "data_type": "uint16",
        "enums": {"1": "Run"},
    }
    ranges = [{"start": 57090, "end": 57090, "size": 1, "addresses": [57090]}]

    output = generate_entities_group_yaml("P03", ranges, {57090: record})

    assert "platform: modbus_controller" not in output
    assert "number.set:" not in output
    assert "ModbusCommandItem::create_write_single_command" in output
    assert "controller, 57090, 1));" in output


def test_rw_group_writes_schedule_real_range_refresh():
    record = {
        "address": 57345,
        "group": "P05",
        "name": "Setting",
        "rw": "RW",
        "data_type": "uint16",
        "multiplier": 1,
    }
    ranges = [{"start": 57345, "end": 57346, "size": 2, "addresses": [57345, 57346]}]

    output = generate_entities_group_yaml("P05", ranges, {57345: record})

    assert "id(p05_refresh_after_write).execute();" in output
    assert "ModbusCommandItem::create_read_command" in output
    assert "57345, 2));" in output
    assert "fast_poll_until_ms" not in output


def test_rw_select_does_not_refresh_on_initial_state():
    record = {
        "address": 57347,
        "group": "P05",
        "name": "Battery Rated Voltage",
        "rw": "RW",
        "data_type": "uint16",
        "enums": {"12": "12V", "48": "48V"},
    }
    ranges = [{"start": 57347, "end": 57347, "size": 1, "addresses": [57347]}]

    output = generate_entities_group_yaml("P05", ranges, {57347: record})

    assert "static bool initialized = false;" in output
    assert "if (!initialized)" in output
    assert output.index("if (!initialized)") < output.index("id(p05_refresh_after_write).execute();")


def test_p00_version_raw_is_diagnostic_and_disabled_by_default():
    output = gen_sensor_entry({
        "group": "P00",
        "address": 20,
        "name": "APP_Version",
        "rw": "R",
        "data_type": "uint16",
        "multiplier": 1,
        "unit": "",
    }, "p00_interval")

    assert "internal: true" not in output
    assert 'name: "APP_Version_Raw"' in output
    assert "disabled_by_default: true" in output
    assert "accuracy_decimals: 2" in output
    assert "- multiply: 0.01" in output
    assert "- round: 2" in output
    assert "entity_category: diagnostic" in output


def test_other_p00_raw_sensor_remains_internal():
    output = gen_sensor_entry({
        "group": "P00",
        "address": 24,
        "name": "Controller_Device_Address",
        "rw": "R",
        "data_type": "uint16",
        "multiplier": 1,
        "unit": "",
    }, "p00_interval")

    assert "internal: true" in output


def test_p02_datetime_uses_slow_cadence():
    source = """sensor:
  - platform: modbus_controller
    register_type: holding
    address: 524
    skip_updates: ${p02_skip_updates}
  - platform: modbus_controller
    register_type: holding
    address: 530
"""

    output = apply_group_skip_updates(source, "P02")

    assert "skip_updates: ${p02_skip_updates}\n    address: 530" in output
    slow_block = output.split("address: 524", 1)[1].split("  - platform:", 1)[0]
    assert "skip_updates: ${p02_slow_skip_updates}" in slow_block
    assert "force_new_range: true" in slow_block


def test_p02_fast_range_restarts_after_datetime():
    source = """sensor:
  - platform: modbus_controller
    register_type: holding
    address: 527
"""

    output = apply_group_skip_updates(source, "P02")

    fast_block = output.split("address: 527", 1)[1]
    assert "force_new_range: true" in fast_block


def test_p05_p07_rw_reads_are_coalesced_without_changing_other_groups():
    source = """number:
  - platform: modbus_controller
    address: 57345
    force_new_range: true
  - platform: modbus_controller
    address: 57346
select:
  - platform: modbus_controller
    address: 57347
    force_new_range: true
"""

    assert "force_new_range: true" not in coalesce_rw_group_reads(source, "P05")
    assert "force_new_range: true" not in coalesce_rw_group_reads(source, "P07")
    assert coalesce_rw_group_reads(source, "P08") == source


def test_packed_p02_range_splits_at_fast_boundary():
    ranges = [{"start": 524, "end": 555, "size": 32, "addresses": list(range(524, 556))}]

    output = split_ranges_at_boundaries(ranges, {527})

    assert [(item["start"], item["end"]) for item in output] == [(524, 526), (527, 555)]


def test_packed_read_ranges_exclude_writable_addresses():
    ranges = [{"start": 524, "end": 529, "size": 6, "addresses": list(range(524, 530))}]
    regs = {
        address: {"address": address, "rw": "RW" if address < 527 else "R"}
        for address in range(524, 530)
    }

    output = build_packed_read_ranges(ranges, regs)

    assert output == [{
        "start": 527,
        "end": 529,
        "size": 3,
        "addresses": [527, 528, 529],
    }]


def test_packed_p00_uses_formatted_version_text_and_diagnostic_raw_sensor():
    ranges = [{"start": 20, "end": 24, "size": 5, "addresses": list(range(20, 25))}]
    regs = {
        address: {
            "address": address,
            "group": "P00",
            "name": "APP_Version" if address == 20 else f"Register_{address}",
            "rw": "R",
            "data_type": "uint16",
            "multiplier": 1,
            "unit": "",
        }
        for address in range(20, 25)
    }

    anchor = generate_anchors_group_yaml("P00", ranges, regs_map=regs, packed=True)
    entities = generate_entities_group_yaml("P00", ranges, dict(regs), packed=True)

    assert "id(sens_p00_20).publish_state((float) raw * 0.01);" in anchor
    version_block = entities.split("id: sens_p00_20", 1)[1].split("  - platform:", 1)[0]
    raw_block = entities.split("id: sens_p00_24", 1)[1].split("  - platform:", 1)[0]
    assert "accuracy_decimals: 2" in version_block
    assert "internal: true" not in version_block
    assert 'name: "APP_Version_Raw"' in version_block
    assert "disabled_by_default: true" in version_block
    assert "entity_category: diagnostic" in version_block
    assert "internal: true" in raw_block
    assert "id: app_version" in entities
    assert 'name: "APP_Version"' in entities
    assert 'snprintf(buf, sizeof(buf), "%.2f", value);' in entities


def test_packed_p09_counter_is_atomic_and_guarded():
    ranges = [{"start": 61492, "end": 61493, "size": 2, "addresses": [61492]}]
    regs = {
        61492: {
            "address": 61492,
            "group": "P09",
            "name": "Total Battery Charge Energy",
            "rw": "R",
            "data_type": "uint32",
            "multiplier": 0.1,
            "unit": "kWh",
        },
    }

    output = generate_anchors_group_yaml("P09", ranges, regs_map=regs, packed=True)

    assert "force_new_range: true" in output
    assert "uint32_t u32 = (hi << 16) | lo;" in output
    assert "static uint32_t last_u32 = 0;" in output
    assert "if (accept) id(sens_p09_61492).publish_state((float) u32 * 0.1);" in output
    assert "millis() < 60000U" not in output


def test_packed_p09_recipe_sources_publish_to_internal_sinks():
    ranges = [{"start": 61504, "end": 61509, "size": 6, "addresses": list(range(61504, 61510))}]
    regs = {
        address: {
            "address": address,
            "group": "P09",
            "name": f"Register_{address}",
            "rw": "R",
            "data_type": "uint16",
        }
        for address in range(61504, 61510)
    }

    output = generate_anchors_group_yaml("P09", ranges, regs_map=regs, packed=True)

    assert "id(p09_uptime_ym).publish_state((float) raw);" in output
    assert "id(p09_eq_time_ms).publish_state((float) raw);" in output
    assert "id(sens_p09_61504)" not in output
    assert "id(sens_p09_61509)" not in output


def test_core_uses_secret_management_credentials():
    output = generate_core_yaml(controllers=[{"id": "srne_main", "update_interval": "5s"}])

    assert "password: !secret fallback_ap_password" in output
    assert "captive_portal:" in output
    assert "key: !secret api_encryption_key" in output
    assert "password: !secret ota_password" in output
    assert "setup-pass" not in output


def test_strict_cli_assigns_per_range_controller(tmp_path):
    implemented = tmp_path / "implemented.json"
    ranges = tmp_path / "ranges.json"
    outdir = tmp_path / "esphome"
    implemented.write_text(json.dumps([
        {"success": True, "group": "P01", "address": 256, "name": "DC Voltage", "rw": "R", "data_type": "uint16"},
        {"success": True, "group": "P02", "address": 512, "name": "Fault Bits", "rw": "R", "data_type": "uint16"},
    ]), encoding="utf-8")
    ranges.write_text(json.dumps({"ranges": {
        "P01": [{"start": 256, "end": 256, "size": 1, "addresses": [256]}],
        "P02": [{"start": 512, "end": 512, "size": 1, "addresses": [512]}],
    }}), encoding="utf-8")

    subprocess.run([
        sys.executable, "-m", "tools.yaml_generator",
        "--implemented", str(implemented),
        "--ranges", str(ranges),
        "--outdir", str(outdir),
        "--custom-overwrite",
        "--split-mode", "strict",
        "--strict-groups", "P02",
    ], cwd=PROJECT_ROOT, check=True, capture_output=True, text=True)

    core = (outdir / "srne" / "core.yaml").read_text(encoding="utf-8")
    p02 = (outdir / "srne" / "custom" / "entities_p02.yaml").read_text(encoding="utf-8")
    assert "id: srne_main" in core
    assert "id: srne_p02_r512_512" in core
    assert "modbus_controller_id: srne_p02_r512_512" in p02


def test_packed_cli_generates_anchor_and_template_entity(tmp_path):
    implemented = tmp_path / "implemented.json"
    ranges = tmp_path / "ranges.json"
    outdir = tmp_path / "esphome"
    implemented.write_text(json.dumps([
        {"success": True, "group": "P02", "address": 530, "name": "Bus Voltage", "rw": "R", "data_type": "uint16", "multiplier": 0.1, "unit": "V"},
    ]), encoding="utf-8")
    ranges.write_text(json.dumps({"ranges": {
        "P02": [{"start": 530, "end": 530, "size": 1, "addresses": [530]}],
    }}), encoding="utf-8")

    subprocess.run([
        sys.executable, "-m", "tools.yaml_generator",
        "--implemented", str(implemented),
        "--ranges", str(ranges),
        "--outdir", str(outdir),
        "--custom-overwrite",
        "--packed-groups", "P02",
    ], cwd=PROJECT_ROOT, check=True, capture_output=True, text=True)

    anchor = (outdir / "srne" / "anchors" / "p02_anchors.yaml").read_text(encoding="utf-8")
    entity = (outdir / "srne" / "custom" / "entities_p02.yaml").read_text(encoding="utf-8")
    core = (outdir / "srne" / "core.yaml").read_text(encoding="utf-8")
    root = (outdir / "srne_inverter.yaml").read_text(encoding="utf-8")
    assert "modbus_controller_id: srne_main" in anchor
    assert "srne_p02_r530_530" not in core
    assert core.count("    address: 1") == 1
    assert "platform: template" in entity
    assert "id: sens_p02_530" in entity
    assert "p02_anchors: !include srne/anchors/p02_anchors.yaml" in root


def _modbus_items(yaml_text):
    items = []
    blocks = re.split(r"(?=^  - platform: modbus_controller$)", yaml_text, flags=re.MULTILINE)
    for block in blocks:
        if not block.startswith("  - platform: modbus_controller"):
            continue
        address = re.search(r"^    address: (\d+)$", block, flags=re.MULTILINE)
        if not address:
            continue
        count = re.search(r"^    register_count: (\d+)$", block, flags=re.MULTILINE)
        items.append({
            "address": int(address.group(1)),
            "count": int(count.group(1)) if count else 1,
            "force": "    force_new_range: true" in block,
        })
    return items


def _simulate_esphome_ranges(items):
    ranges = []
    for item in sorted(items, key=lambda value: (not value["force"], value["address"])):
        if ranges and not item["force"] and item["address"] == ranges[-1][0] + ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], ranges[-1][1] + item["count"])
        else:
            ranges.append((item["address"], item["count"]))
    return ranges


def test_single_controller_packed_ranges_match_runtime_boundaries():
    p00_ranges = [
        {"start": 11, "end": 11, "size": 1, "addresses": [11]},
        {"start": 20, "end": 23, "size": 4, "addresses": list(range(20, 24))},
        {"start": 26, "end": 28, "size": 3, "addresses": list(range(26, 29))},
        {"start": 30, "end": 33, "size": 4, "addresses": list(range(30, 34))},
        {"start": 53, "end": 53, "size": 1, "addresses": [53]},
    ]
    p01_ranges = [
        {"start": 256, "end": 259, "size": 4, "addresses": list(range(256, 260))},
        {"start": 263, "end": 267, "size": 5, "addresses": list(range(263, 268))},
        {"start": 270, "end": 273, "size": 4, "addresses": list(range(270, 274))},
    ]
    p02_ranges = [
        {"start": 512, "end": 512, "size": 1, "addresses": [512]},
        {"start": 516, "end": 519, "size": 4, "addresses": list(range(516, 520))},
        {"start": 527, "end": 555, "size": 29, "addresses": list(range(527, 556))},
        {"start": 556, "end": 569, "size": 14, "addresses": list(range(556, 570))},
    ]
    p09_ranges = [
        {"start": 61440, "end": 61471, "size": 32, "addresses": list(range(61440, 61472))},
        {"start": 61472, "end": 61491, "size": 20, "addresses": list(range(61472, 61491))},
        {"start": 61492, "end": 61493, "size": 2, "addresses": [61492]},
        {"start": 61494, "end": 61495, "size": 2, "addresses": [61494]},
        {"start": 61496, "end": 61497, "size": 2, "addresses": [61496]},
        {"start": 61498, "end": 61499, "size": 2, "addresses": [61498]},
        {"start": 61500, "end": 61511, "size": 12, "addresses": list(range(61500, 61511))},
        {"start": 61512, "end": 61513, "size": 2, "addresses": [61512]},
        {"start": 61514, "end": 61516, "size": 3, "addresses": list(range(61514, 61517))},
    ]
    p00_regs = {
        address: {"address": address, "group": "P00", "rw": "R", "data_type": "uint16"}
        for item in p00_ranges for address in item["addresses"]
    }
    p01_regs = {
        address: {"address": address, "group": "P01", "rw": "R", "data_type": "uint16"}
        for item in p01_ranges for address in item["addresses"]
    }
    p02_regs = {
        address: {"address": address, "group": "P02", "rw": "R", "data_type": "uint16"}
        for item in p02_ranges for address in item["addresses"]
    }
    p09_regs = {
        address: {
            "address": address,
            "group": "P09",
            "rw": "R",
            "data_type": "uint32" if address in {61490, 61492, 61494, 61496, 61498, 61510, 61512} else "uint16",
        }
        for item in p09_ranges for address in item["addresses"]
    }

    p00_yaml = generate_anchors_group_yaml("P00", p00_ranges, regs_map=p00_regs, packed=True)
    p01_yaml = generate_anchors_group_yaml("P01", p01_ranges, regs_map=p01_regs, packed=True)
    p02_yaml = generate_anchors_group_yaml("P02", p02_ranges, regs_map=p02_regs, packed=True)
    p09_yaml = generate_anchors_group_yaml("P09", p09_ranges, regs_map=p09_regs, packed=True)

    assert _simulate_esphome_ranges(_modbus_items(p00_yaml)) == [
        (11, 1), (20, 4), (26, 3), (30, 4), (53, 1),
    ]
    assert _simulate_esphome_ranges(_modbus_items(p01_yaml)) == [(256, 4), (263, 5), (270, 4)]
    assert _simulate_esphome_ranges(_modbus_items(p02_yaml)) == [
        (512, 1),
        (516, 4),
        (527, 29),
        (556, 14),
    ]
    assert _simulate_esphome_ranges(_modbus_items(p09_yaml)) == [
        (61440, 32), (61472, 20), (61492, 2), (61494, 2), (61496, 2),
        (61498, 2), (61500, 12), (61512, 2), (61514, 3),
    ]
