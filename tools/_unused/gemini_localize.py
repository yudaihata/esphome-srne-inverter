#!/usr/bin/env python3
"""
Gemini を使って ESPHome の表示名(name)を自然な日本語へローカライズします。

要件/注意:
- ネットワーク接続と Google Generative AI SDK が必要です。
- API キーは環境変数 GEMINI_API_KEY から読み込みます（絶対にリポジトリへ保存しない）。
- 既存 YAML は in-place で書き換えます。必ず Git 管理下で実行してください。

使い方:
  # 事前にインストール
  pip install google-generativeai pyyaml

  # 乾式（差分のみ確認）
  GEMINI_API_KEY=xxxxx \
    python3 tools/gemini_localize.py --dry-run

  # 実書き換え
  GEMINI_API_KEY=xxxxx \
    python3 tools/gemini_localize.py --apply

オプション:
  --model   : 既定 'gemini-3.0-flash-preview-1'
  --root    : YAML 探索ルート（既定 esphome/srne）
  --cache   : 既定 tools/build/gemini_label_cache.json
  --apply   : 実際に置換を書き込み
  --dry-run : 置換プレビューのみ
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import google.generativeai as genai  # type: ignore
except Exception:
    genai = None


NAME_RE = re.compile(r"^\s*name:\s*\"(.*?)\"\s*$")


def collect_yaml_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for p in root.rglob("*.yaml"):
        files.append(p)
    return files


def collect_names(files: List[Path]) -> List[str]:
    names: Dict[str, int] = {}
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = NAME_RE.match(line)
            if m:
                nm = m.group(1)
                names[nm] = names.get(nm, 0) + 1
    return list(names.keys())


PROMPT = (
    "以下は ESPHome/Modbus 機器のエンティティ名です。日本語のUIに自然でわかりやすい表記に翻訳してください。\n"
    "厳守事項:\n"
    "- 専門用語は一般的な日本語訳を用い、冗長にしない（例: Frequency→周波数, Voltage→電圧, Current→電流）\n"
    "- 単位記号は維持（V, A, Hz 等は値側に付くため、名前に単位は入れない）\n"
    "- 名詞形で簡潔（文末のです/ます等は不要）\n"
    "- 略語は原則展開（Inv→インバーター, Grid→系統, BMS→BMS）\n"
    "- 'Enable/Disable' の種別は '有効/無効'、スイッチ名は対象+""有効""（例: アラーム有効）\n"
    "- 安全/リセット/工場出荷等の操作は意味が伝わるように（例: 工場出荷状態にリセット）\n"
    "- 同一語は同一表記のまま（用語統一）\n"
    "出力フォーマット: JSON マップ（元名→日本語名）。キーは入力名、値は翻訳後。余計な説明は不要。\n"
)


def translate_batch(model, items: List[str]) -> Dict[str, str]:
    if not items:
        return {}
    # モデルへは JSON 配列で渡す
    content = {
        "input_names": items,
    }
    prompt = PROMPT + json.dumps(items, ensure_ascii=False)
    resp = model.generate_content(prompt)
    text = resp.text or "{}"
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    # フォールバック: 1:1 で返す
    return {it: it for it in items}


def apply_mapping(file_path: Path, mapping: Dict[str, str]) -> Tuple[int, int]:
    src = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out = []
    hits = 0
    for line in src:
        m = NAME_RE.match(line)
        if m:
            orig = m.group(1)
            new = mapping.get(orig)
            if new and new != orig:
                line = line.replace(f'"{orig}"', f'"{new}"')
                hits += 1
        out.append(line)
    if hits:
        file_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return hits, len(src)


def main() -> None:
    ap = argparse.ArgumentParser(description="Localize ESPHome entity names to Japanese via Gemini")
    ap.add_argument("--model", default="gemini-3.0-flash-preview-1")
    ap.add_argument("--root", type=Path, default=Path("esphome/srne"))
    ap.add_argument("--cache", type=Path, default=Path("tools/build/gemini_label_cache.json"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env-file", type=Path, default=Path(".env.local"), help="環境変数を読み込むファイル（KEY=VALUE 形式）")
    args = ap.parse_args()

    if args.dry_run and args.apply:
        print("--dry-run と --apply は同時指定できません。")
        return

    # 先に --env-file を読み込んで環境変数へ取り込む
    if args.env_file and args.env_file.exists():
        for line in args.env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "=" in s:
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    os.environ[k] = v

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY が設定されていません。環境変数で与えてください。")
        return
    if genai is None:
        print("google-generativeai が見つかりません。pip install google-generativeai を実行してください。")
        return
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(args.model)

    files = collect_yaml_files(args.root)
    names = collect_names(files)
    print(f"検出した name 数: {len(names)}（去重済み）")

    # 既存キャッシュ
    mapping: Dict[str, str] = {}
    if args.cache.exists():
        try:
            mapping.update(json.loads(args.cache.read_text(encoding="utf-8")))
        except Exception:
            pass

    # 未翻訳だけバッチ送信
    todo = [n for n in names if n not in mapping]
    print(f"未翻訳: {len(todo)} 件")
    B = 50
    for i in range(0, len(todo), B):
        batch = todo[i : i + B]
        part = translate_batch(model, batch)
        mapping.update(part)

    # キャッシュ保存
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    args.cache.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"キャッシュ更新: {args.cache}")

    # 置換
    total_files = 0
    total_hits = 0
    if args.apply:
        for fp in files:
            hits, _ = apply_mapping(fp, mapping)
            if hits:
                total_files += 1
                total_hits += hits
        print(f"置換完了: {total_files} ファイル、{total_hits} 箇所")
    else:
        print("--dry-run: 置換のプレビューのみ（キャッシュは更新済み）")


if __name__ == "__main__":
    main()
