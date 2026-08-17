# SRNE Hybrid Inverter - ESPHome generator

SRNEインバーターのModbusレジスタをスキャンし、ESPHome用YAMLを生成するツールです。

## 対応機種

- 実機検証済み: **ASF48100U200-H**
- 想定範囲: SRNE Hybrid Inverter Modbus Protocol v1.96と互換性がある機種

機種名だけで互換性を判定せず、ウィザードがカタログ上の候補レジスタを実機スキャンし、応答した項目だけを生成します。リポジトリ内の生成済みYAMLはASF48100U200-H由来の参照スナップショットであり、他機種ではそのまま使用せず再スキャンしてください。検証範囲と他機種を追加する手順は[`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md)に記載しています。

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
- `esphome/srne/anchors/p00_anchors.yaml` / `p01_anchors.yaml` / `p02_anchors.yaml` / `p09_anchors.yaml` / `p10_anchors.yaml`（確認済みレンジの一括読み取り）
- `esphome/srne/custom/entities_<group>.yaml`（表示/操作するセンサー・設定）
- `esphome/srne/intervals.yaml`（基本更新周期と各グループの `skip_updates`）
- `esphome/secrets.yaml`（Wi‑Fi/API暗号化/OTA用。Gitには含めない）

ウィザードは対象機のスキャン結果から生成YAMLを更新します。生成後は `git diff` で変更内容を確認してください。`tools/build/`のスキャン結果は実機固有データのためGit管理外です。

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

ウィザードの標準生成は単一の`srne_main`を使い、P00/P01/P02/P09の読み取り専用レジスターとP10故障履歴を明示レンジアンカーで一括取得します。P10は2履歴（32ワード）ずつ16レンジで読み取ります。P02の書き込み可能な日時レジスター（524-526）は既存の`number`経路を維持します。

P03の書き込み専用コマンドはボタン操作時だけ送信し、定期読み取りには含めません。P05/P07は書き込みエンティティを維持したまま、連続するRWレジスターの読み取りをESPHome側で各3レンジに結合します。書き込み後は1秒待って対象グループの3レンジを再読込し、連続操作は最後の1回へ集約します。

P00のVersion系5項目は、通常表示を小数2桁のtext sensor、元数値を`_Raw`付きの診断sensorとして生成します。Raw側は`disabled_by_default`で、必要な場合だけHome Assistantから有効化できます。

通信の標準設定は`command_throttle: 100ms`、`max_cmd_retries: 3`、`offline_skip_updates: 10`です。切断時の無制限な再試行を避け、復帰後は通常の更新周期へ戻ります。

据え置き運用でのWi-Fi/API安定性を優先し、ESP32の`power_save_mode`は`NONE`で生成します。

高度な検証用途では、`python -m tools.yaml_generator --packed-groups P00,P01,P02,P09,P10 ...`で対象グループを明示できます。P00/P01/P02/P09はスキャン成功済みの読み取り専用レジスター、P10はスキャン済み故障履歴と派生レシピで定義された16ワード構造を使用します。

## 取得項目のカスタマイズ

- `esphome/srne/custom/entities_<group>.yaml` で不要な項目をコメントアウトできます。

## よくある質問

- 生成が空/少ない → 配線・ポート・タイムアウトを見直し、`wizard.py`を再実行（成功したアドレスだけ採用します）。
- 値の倍率 → JSONの`multiplier`を自動適用します。通常センサーはfilter、packedセンサーはアンカーの配布処理で変換し、RWは逆変換して書き込みます。
- 変更が消える → ウィザードは毎回上書き。編集は生成後に行い、必要ならバックアップを。

## 参考

- 機種互換性と検証手順: `docs/COMPATIBILITY.md`
- 仕様メモ: `docs/SRNE_Inverter_Modbus_Protocol_V1.96_Notes.md`
- レジスタカタログ（参考・実機優先）: `docs/srne_hybrid_modbus_v1.96.json`
