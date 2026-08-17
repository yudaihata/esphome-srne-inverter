# SRNE Hybrid Inverter ESPHome Generator

This tool scans the Modbus registers exposed by an SRNE inverter and generates an ESPHome YAML configuration for the detected device.

## Compatibility

- Hardware tested: **ASF48100U200-H**
- Intended scope: models compatible with SRNE Hybrid Inverter Modbus Protocol v1.96

Compatibility is not inferred from the model name alone. The wizard scans candidate registers from the catalog and generates entities only for registers that respond on the connected inverter. The generated YAML committed to this repository is a reference snapshot from an ASF48100U200-H. Do not use it unchanged on another model; run a fresh scan first.

See [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) for validation levels and the process for adding another model.

## Requirements

- Python 3.11 or later
- ESPHome 2026.2.4, pinned in `requirements.txt`
- A USB-to-RS485 adapter with A/B connected and a shared ground if required

Install the Python dependencies in a virtual environment:

```sh
python3 -m venv .venv_esphome
source .venv_esphome/bin/activate
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## Usage

1. Connect the inverter to the computer through USB/RS485.
2. Run `python3 wizard.py` to detect the adapter, scan the inverter, and generate YAML.
3. Create `esphome/secrets.yaml` from `esphome/secrets.example.yaml`. The real secrets file is excluded from Git.
4. Run `esphome run esphome/srne_inverter.yaml` to validate, build, and install the firmware.

## Generated Files

- `esphome/srne_inverter.yaml`: root ESPHome configuration
- `esphome/srne/core.yaml`: UART, Modbus, Wi-Fi, API, OTA, and logger configuration
- `esphome/srne/anchors/p00_anchors.yaml`, `p01_anchors.yaml`, `p02_anchors.yaml`, `p09_anchors.yaml`, and `p10_anchors.yaml`: packed readers for verified ranges
- `esphome/srne/custom/entities_<group>.yaml`: sensors and controls exposed by each group
- `esphome/srne/intervals.yaml`: base polling interval and per-group `skip_updates` values
- `esphome/secrets.yaml`: Wi-Fi, API encryption, and OTA credentials; excluded from Git

The wizard replaces generated YAML using the scan result from the connected inverter. Review `git diff` after every generation. Scan output under `tools/build/` is device-specific and excluded from Git.

## Polling Intervals

Default group intervals in seconds:

| Group | P00 | P01 | P02 | P03 | P05 | P07 | P08 | P09 | P10 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Interval | 600 | 5 | 5 | 60 | 600 | 600 | 600 | 600 | 600 |

Edit `esphome/srne/intervals.yaml` to change them. `main_update_interval_s` defines the base interval, and each `pXX_skip_updates` value controls group-level throttling. P02 date/time registers 524-526 use the separate `p02_slow_skip_updates` value. These values are compile-time settings, so rebuild after changing them.

## Modbus Layout

The default generator uses one `srne_main` controller.

- P00, P01, P02, and P09 read-only registers use explicit packed range anchors.
- P10 fault history uses 16 ranges, with two 16-word records per range.
- P02 writable date/time registers 524-526 retain the normal `number` path.
- P03 write-only commands are sent only when a button is operated and are not polled.
- P05 and P07 keep writable entities while coalescing contiguous reads into three ranges per group.
- After a P05 or P07 write, the generator waits one second and rereads all three ranges. Repeated actions are coalesced into the final refresh.

The default communication settings are `command_throttle: 100ms`, `max_cmd_retries: 3`, and `offline_skip_updates: 10`. This prevents unlimited retry pressure while the inverter is offline and returns to normal polling after recovery.

For advanced validation, groups can be selected explicitly:

```sh
python -m tools.yaml_generator --packed-groups P00,P01,P02,P09,P10 ...
```

P00, P01, P02, and P09 use successfully scanned read-only registers. P10 uses scanned fault-history addresses together with the 16-word record structure defined by the derived recipes.

## Home Assistant Representation

Five P00 version fields are exposed as two-decimal text sensors for normal display. Their original numeric values remain available as diagnostic sensors with a `_Raw` suffix and are disabled by default.

ESP32 `power_save_mode` is set to `NONE` to prioritize stable Wi-Fi and Home Assistant API connectivity for stationary installations.

After first installing firmware with API encryption enabled, Home Assistant may request the encryption key. Use `api_encryption_key` from `esphome/secrets.yaml`.

## Validation

```sh
ruff check .
pytest -q
esphome config esphome/srne_inverter.yaml
```

GitHub Actions runs the same checks.

## Customizing Entities

Generated entities are stored in `esphome/srne/custom/entities_<group>.yaml`. The wizard overwrites these files, so persistent changes belong in the catalog, recipes, or generator rather than manual edits to generated YAML.

## Troubleshooting

- Empty or incomplete output: check RS485 wiring, serial port selection, and timeout settings, then rerun `wizard.py`. Only responding addresses are included.
- Incorrect scaling: the generator applies the catalog `multiplier`. Normal sensors use filters, packed sensors scale values during anchor distribution, and writable entities apply the inverse conversion.
- Manual changes disappear: generated files are overwritten on every wizard run. Move persistent behavior into the source catalog, recipes, or generator.

## References

- Model compatibility and validation: `docs/COMPATIBILITY.md`
- Protocol notes: `docs/SRNE_Inverter_Modbus_Protocol_V1.96_Notes.md`
- Register catalog, advisory with device behavior taking precedence: `docs/srne_hybrid_modbus_v1.96.json`

## 日本語概要

このツールは、SRNEインバーターのModbusレジスタを実機スキャンし、応答した項目からESPHome YAMLを生成します。

- 実機確認済み機種はASF48100U200-Hです。
- 他機種では、リポジトリ内の生成済みYAMLをそのまま使わず、`python3 wizard.py`で再スキャンしてください。
- Wi-FiやAPIキーなどの秘密情報と、`tools/build/`以下の実機固有スキャン結果はGit管理外です。
- 更新周期は`esphome/srne/intervals.yaml`で調整し、変更後に再ビルドします。
- 手動で生成済みYAMLを変更すると次回生成時に消えるため、永続的な変更はカタログ、レシピ、または生成ロジックへ入れてください。
- 詳細な対応状況と他機種の検証手順は`docs/COMPATIBILITY.md`を参照してください。
