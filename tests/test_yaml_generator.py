import json
import subprocess
import sys
from pathlib import Path

from tools.yaml_generator import (
    apply_group_skip_updates,
    gen_sensor_entry,
    generate_core_yaml,
    split_ranges_at_boundaries,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_p00_version_is_public_rounded_numeric_sensor():
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
    assert "accuracy_decimals: 2" in output
    assert "- multiply: 0.01" in output
    assert "- round: 2" in output
    assert "entity_category: diagnostic" not in output


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

    assert "address: 524\n    skip_updates: ${p02_slow_skip_updates}" in output
    assert "skip_updates: ${p02_skip_updates}\n    address: 530" in output
    slow_block = output.split("address: 524", 1)[1].split("  - platform:", 1)[0]
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


def test_packed_p02_range_splits_at_fast_boundary():
    ranges = [{"start": 524, "end": 555, "size": 32, "addresses": list(range(524, 556))}]

    output = split_ranges_at_boundaries(ranges, {527})

    assert [(item["start"], item["end"]) for item in output] == [(524, 526), (527, 555)]


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
    root = (outdir / "srne_inverter.yaml").read_text(encoding="utf-8")
    assert "modbus_controller_id: srne_p02_r530_530" in anchor
    assert "platform: template" in entity
    assert "id: sens_p02_530" in entity
    assert "p02_anchors: !include srne/anchors/p02_anchors.yaml" in root
