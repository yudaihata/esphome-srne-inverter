# SRNE Hybrid Inverter - ESPHome generator

SRNEインバーターのModbusレジスタをスキャンし、ESPHome用YAMLを生成するツールです。

## 必要なもの

- Python 3.11+
- ESPHome 2026.2.4（`requirements.txt` で固定）
- USB-RS485アダプタ（A/B配線、必要ならGND共通）

Python依存パッケージは以下で導入します。

```sh
python3 -m venv .venv_esphome
source .venv_esphome/bin/activate
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 使い方

1. インバーターをUSB/RS485でPCへ接続します。
2. `python3 wizard.py` を実行し、USB検出・スキャン・YAML生成を行います。
3. `esphome/secrets.example.yaml` を参考に `esphome/secrets.yaml` を用意します。実ファイルはGitには含めません。
4. `esphome run esphome/srne_inverter.yaml` で検証・ビルド・書き込みを行います。

## 生成されるファイル

- `esphome/srne_inverter.yaml`（ルート設定）
- `esphome/srne/core.yaml`（UART/Modbus/Wi‑Fi/API/OTA/Logger）
- `esphome/srne/custom/entities_<group>.yaml`（表示/操作するセンサー・設定）
- `esphome/srne/intervals.yaml`（基本更新周期と各グループの `skip_updates`）
- `esphome/secrets.yaml`（Wi‑Fi/API暗号化/OTA用。Gitには含めない）

ウィザードは生成YAMLを更新します。生成後は `git diff` で変更内容を確認してください。

## 更新周期

- 既定（秒）
  - P00: 600 / P01: 5 / P02: 5 / P03: 60 / P05: 600 / P07: 600 / P08: 600 / P09: 600 / P10: 600
- 編集場所: `esphome/srne/intervals.yaml`
- `main_update_interval_s` が基本周期、各 `pXX_skip_updates` がグループ別の間引き回数です。
- P02の日時レジスタ（524-526）だけは `p02_slow_skip_updates` で個別に調整します。
- ランタイム変更は不可。変更後は再ビルドしてください。

## 検証

```sh
ruff check .
pytest -q
esphome config esphome/srne_inverter.yaml
```

同じ検証はGitHub Actionsでも実行します。

API暗号化を有効にしたファームウェアへ初めて更新した後、Home Assistantから暗号化キーを求められた場合は、`esphome/secrets.yaml`の`api_encryption_key`を設定してください。

高度な検証用途では、`python -m tools.yaml_generator --packed-groups P02,P05 ...`でpacked読み取りを明示的に生成できます。ウィザードの標準生成は安定性を優先して単一controller構成のままです。

## 取得項目のカスタマイズ

- `esphome/srne/custom/entities_<group>.yaml` で不要な項目をコメントアウトできます。

## よくある質問

- 生成が空/少ない → 配線・ポート・タイムアウトを見直し、`wizard.py`を再実行（成功したアドレスだけ採用します）。
- 値の倍率 → JSONの`multiplier`を自動適用（filters: multiply）。RWは逆変換で書き込みます。
- 変更が消える → ウィザードは毎回上書き。編集は生成後に行い、必要ならバックアップを。

## 参考

- 仕様メモ: `docs/SRNE_Inverter_Modbus_Protocol_V1.96_Notes.md`
- レジスタカタログ（参考・実機優先）: `docs/srne_hybrid_modbus_v1.96.json`
