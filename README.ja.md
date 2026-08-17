# ESPHome SRNE Inverter

[English](README.md) | 日本語

このツールは、SRNEインバーターが公開するModbusレジスタをスキャンし、検出した機器に対応するESPHome YAML設定を生成します。

## 対応機種

- 実機確認済み: **ASF48100U200-H**
- 想定範囲: SRNE Hybrid Inverter Modbus Protocol v1.96と互換性がある機種

機種名だけでは互換性を判定しません。ウィザードはカタログ上の候補レジスタを実機スキャンし、接続したインバーターから応答したレジスタだけをエンティティとして生成します。リポジトリ内の生成済みYAMLはASF48100U200-Hから生成した参照用スナップショットです。他機種へそのまま使用せず、必ず新しくスキャンしてください。

検証レベルと他機種を追加する手順は[`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md)を参照してください。

## 必要なもの

- Python 3.11以降
- `requirements.txt`で固定されたESPHome 2026.2.4
- USB-RS485アダプター。A/Bを接続し、必要な場合はGNDも共有

Python依存パッケージを仮想環境へインストールします。

```sh
python3 -m venv .venv_esphome
source .venv_esphome/bin/activate
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 使い方

1. インバーターをUSB/RS485経由でPCへ接続します。
2. `python3 wizard.py`を実行し、アダプター検出、実機スキャン、YAML生成を行います。
3. `esphome/secrets.example.yaml`を基に`esphome/secrets.yaml`を作成します。実際のSecretsファイルはGit管理外です。
4. `esphome run esphome/srne_inverter.yaml`で設定検証、ビルド、書き込みを行います。

## 生成ファイル

- `esphome/srne_inverter.yaml`: ESPHomeのルート設定
- `esphome/srne/core.yaml`: UART、Modbus、Wi-Fi、API、OTA、Logger設定
- `esphome/srne/anchors/p00_anchors.yaml`、`p01_anchors.yaml`、`p02_anchors.yaml`、`p09_anchors.yaml`、`p10_anchors.yaml`: 確認済みレンジの一括読み取り
- `esphome/srne/custom/entities_<group>.yaml`: 各グループで公開するセンサーと操作項目
- `esphome/srne/intervals.yaml`: 基本読み取り周期とグループ別`skip_updates`
- `esphome/secrets.yaml`: Wi-Fi、API暗号化、OTAの認証情報。Git管理外

ウィザードは接続したインバーターのスキャン結果を使って生成済みYAMLを置き換えます。生成後は毎回`git diff`を確認してください。`tools/build/`以下のスキャン結果は実機固有データのためGit管理外です。

## 読み取り周期

グループ別の標準周期は次のとおりです。単位は秒です。

| グループ | P00 | P01 | P02 | P03 | P05 | P07 | P08 | P09 | P10 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 周期 | 600 | 5 | 5 | 60 | 600 | 600 | 600 | 600 | 600 |

変更する場合は`esphome/srne/intervals.yaml`を編集します。`main_update_interval_s`が基本周期、各`pXX_skip_updates`がグループ単位の間引きを定義します。P02の日時レジスタ524-526は、個別の`p02_slow_skip_updates`を使用します。これらはコンパイル時設定なので、変更後は再ビルドしてください。

## Modbus構成

標準生成では1つの`srne_main`コントローラーを使用します。

- P00、P01、P02、P09の読み取り専用レジスタは、明示的な一括レンジアンカーを使用します。
- P10の故障履歴は16レンジで読み取り、1レンジにつき16ワードの履歴を2件取得します。
- P02の書き込み可能な日時レジスタ524-526は通常の`number`経路を維持します。
- P03の書き込み専用コマンドはボタン操作時だけ送信し、定期読み取りしません。
- P05とP07は書き込みエンティティを維持しつつ、連続する読み取りを各グループ3レンジへ結合します。
- P05またはP07への書き込み後は1秒待機し、3レンジすべてを再読み取りします。連続操作時の再読み取りは最後の1回へ集約します。

通信の標準設定は`command_throttle: 100ms`、`max_cmd_retries: 3`、`offline_skip_updates: 10`です。インバーターがオフラインの間に無制限な再試行が発生することを防ぎ、復帰後は通常の読み取りへ戻ります。

高度な検証では、対象グループを明示できます。

```sh
python -m tools.yaml_generator --packed-groups P00,P01,P02,P09,P10 ...
```

P00、P01、P02、P09はスキャンに成功した読み取り専用レジスタを使用します。P10はスキャン済みの故障履歴アドレスと、派生レシピで定義した16ワード構造を使用します。

## Home Assistantでの表示

P00のVersion系5項目は、通常表示用に小数2桁のtext sensorとして公開します。元の数値は`_Raw`接尾辞を持つ診断sensorとして残し、標準では無効にします。

据え置き運用時のWi-FiとHome Assistant APIの安定性を優先し、ESP32の`power_save_mode`は`NONE`に設定します。

API暗号化を有効にしたファームウェアを初めて書き込んだ後、Home Assistantから暗号化キーを求められる場合があります。`esphome/secrets.yaml`の`api_encryption_key`を使用してください。

## 検証

```sh
ruff check .
pytest -q
esphome config esphome/srne_inverter.yaml
```

GitHub Actionsでも同じ検証を実行します。

## エンティティのカスタマイズ

生成済みエンティティは`esphome/srne/custom/entities_<group>.yaml`に保存されます。ウィザードはこれらを上書きするため、永続的な変更は生成済みYAMLへ直接書かず、カタログ、レシピ、または生成ロジックへ追加してください。

## トラブルシューティング

- 生成結果が空または少ない場合: RS485配線、シリアルポート、タイムアウト設定を確認して`wizard.py`を再実行してください。応答したアドレスだけが採用されます。
- 倍率が正しくない場合: 生成処理はカタログの`multiplier`を適用します。通常sensorはfilter、一括読み取りsensorはアンカーから値を配布するときに変換し、書き込みエンティティは逆変換します。
- 手動変更が消える場合: 生成済みファイルはウィザード実行時に上書きされます。永続的な処理はカタログ、レシピ、または生成ロジックへ移してください。

## 参考資料

- 機種互換性と検証手順: `docs/COMPATIBILITY.md`
- プロトコルメモ: `docs/SRNE_Inverter_Modbus_Protocol_V1.96_Notes.md`
- レジスタカタログ。参考情報であり実機動作を優先: `docs/srne_hybrid_modbus_v1.96.json`
- 第三者の権利とプロトコル文書の取り扱い: `NOTICE.md`

ベンダー提供のプロトコルPDFは、このリポジトリでは再配布しません。正規の経路から仕様書を入手し、すべてのレジスタ動作を接続した実機で確認してください。
