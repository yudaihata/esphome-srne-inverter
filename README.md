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
python3 -m pip install -r requirements.txt
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
- `esphome/secrets.yaml`（Wi‑Fi SSID/PW。必要に応じて編集）

ウィザードは生成YAMLを更新します。生成後は `git diff` で変更内容を確認してください。

## 更新周期

- 既定（秒）
  - P00: 600 / P01: 5 / P02: 5 / P03: 60 / P05: 600 / P07: 600 / P08: 600 / P09: 600 / P10: 600
- 編集場所: `esphome/srne/intervals.yaml`
- `main_update_interval_s` が基本周期、各 `pXX_skip_updates` がグループ別の間引き回数です。
- ランタイム変更は不可。変更後は再ビルドしてください。

## 取得項目のカスタマイズ

- `esphome/srne/custom/entities_<group>.yaml` で不要な項目をコメントアウトできます。

## よくある質問

- 生成が空/少ない → 配線・ポート・タイムアウトを見直し、`wizard.py`を再実行（成功したアドレスだけ採用します）。
- 値の倍率 → JSONの`multiplier`を自動適用（filters: multiply）。RWは逆変換で書き込みます。
- 変更が消える → ウィザードは毎回上書き。編集は生成後に行い、必要ならバックアップを。

## 参考

- 仕様メモ: `docs/SRNE_Inverter_Modbus_Protocol_V1.96_Notes.md`
- レジスタカタログ（参考・実機優先）: `docs/srne_hybrid_modbus_v1.96.json`
