# Model Compatibility

This project determines readable registers in two stages rather than selecting a fixed register map from the product name:

1. `docs/srne_hybrid_modbus_v1.96.json` supplies candidate registers.
2. `wizard.py` scans the connected inverter and generates YAML only for registers that respond.

This design can support other models that implement the same SRNE Modbus register layout. A register appearing in the catalog does not, by itself, prove hardware compatibility.

## Verified Hardware

| Model | Catalog | Generation and build | Read-only runtime validation | Write validation |
| --- | --- | --- | --- | --- |
| ASF48100U200-H | v1.96 | Verified | P00/P01/P02/P05/P07/P09/P10 | Not verified; generation and build only |

The generated YAML under `esphome/` is a reference snapshot based on an ASF48100U200-H scan.

## Testing Another Model

1. Do not install the committed generated YAML unchanged. Connect the target inverter through USB/RS485 and run `python3 wizard.py`.
2. Confirm that the scan result was written to `tools/build/implemented_addresses.json`. This device-specific file is excluded from Git.
3. Run `ruff check .`, `pytest -q`, and `esphome config esphome/srne_inverter.yaml`.
4. Start with read-only operation. Check for Modbus exceptions, timeouts, repeated reconnects, and implausible values.
5. Verify the meaning and valid range of every writable register for that model before performing a write test.

## Adding a Verified Model

Record at least the following information before listing a model as verified:

- Exact model name and publishable firmware or protocol versions
- Catalog version and scan date
- P-groups that responded successfully
- ESPHome configuration and build results
- Modbus exceptions, disconnects, or implausible values observed during read-only operation
- Registers and value ranges used for any write validation

Do not commit serial numbers, Wi-Fi credentials, ESPHome API keys, OTA passwords, or raw device-specific scan output.

## 日本語概要

このプロジェクトは機種名で固定レジスタを選ぶのではなく、v1.96カタログの候補を実機スキャンし、応答したレジスタだけを生成します。

- 現在の実機確認済み機種はASF48100U200-Hです。
- `esphome/`以下の生成済みYAMLは、この機種のスキャン結果を基にした参照用です。
- 他機種では必ず`python3 wizard.py`で再スキャンし、最初は読み取りだけで検証してください。
- 書き込み可能レジスタは、対象機種で意味と許容範囲を確認してから試験してください。
- 対応実績を追加する際も、認証情報、シリアル番号、実機固有の生スキャン結果はコミットしません。
