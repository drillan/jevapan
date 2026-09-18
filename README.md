# jevapan

日本語文書をセマンティックに lint する CLI。TypeSafe の System One モデル Jev を判定エンジンに使い、YAML で定義した規範カテゴリに対して文書を採点し、違反箇所を文レベルで指摘する。修正案の生成は行わない（判定・指摘のみ）。

## インストール

```bash
uv tool install .
# またはリポジトリから
uv tool install git+https://github.com/<owner>/jevapan
```

`jevapan` と短縮形 `jvp` の2コマンドが登録される。

## セットアップ

```bash
export TYPESAFE_API_KEY=...
```

未設定の場合は exit 2 で終了する。

## 使い方

```bash
jvp check docs/guide.md        # ファイル指定
jvp check -                    # stdin(hook 用)
jvp check docs/ -r             # 再帰(*.md, *.txt)
  --ruleset NAME|PATH          # プリセット名 or YAML パス(既定: base)
  --config PATH                # jevapan.yaml 明示(既定: 自動探索)
  --format human|json|auto     # 既定: TTY なら human、パイプなら json
  --concurrency N              # 既定 20
  --fail-under SCORE           # CI ゲート用(任意)
```

同梱プリセット: `base`(全ジャンル共通7カテゴリ) / `tech-doc`(base + rigor, reader_load, restraint) / `blog`(base + restraint, honesty、voice の閾値緩和)。

## jevapan.yaml

カレントディレクトリから上方に遡って `jevapan.yaml` / `.jevapan.yaml` を探索し、選択中のルールセットにマージする。

```yaml
version: 1
extends: tech-doc            # 省略可。プリセット名またはファイルパス
categories:
  - name: substance          # 既存カテゴリの severity を error に上げる
    severity: error
  - name: naturalness        # カテゴリを無効化する
    enabled: false
  - name: my_rule            # 新規カテゴリ(description と levels が必須)
    scope: block             # block(既定) | document | both
    severity: warning        # error | warning | info
    threshold: 1.5           # このスコア未満で flag→文特定へ
    description: "採点基準の説明(質問の instructions になる)"
    levels:                  # 2〜5段。下から違反あり→クリーンの順
      - "悪い状態の説明"
      - "良い状態の説明"
    locate: "この文は〜である"  # 省略可。違反文特定の Noul 指示
```

## 終了コード

- `0`: error severity の違反なし(warning/info のみ or clean)
- `1`: error severity の flag が1件以上
- `2`: 実行エラー(API キー未設定・API エラー・YAML 不正・引数不正等)

## JSON 出力

`--format json` でエージェント消費用の安定スキーマを出力する。

```json
{
  "files": [{
    "file": "docs/guide.md",
    "blocks": [{"lines": [1, 8], "scores": {"structure": {"score": 1.9, "confidence": 0.8}}}],
    "doc_scores": {"consistency": {"score": 2.0, "confidence": 0.9}},
    "violations": [{"lines": [3, 3], "scope": "block", "category": "substance",
                     "severity": "warning", "probability": 0.91, "text": "重要なのは〜である。"}],
    "skipped": [{"category": "consistency", "reason": "state_too_large"}],
    "summary": {"blocks": 12, "violations": 4, "errors": 0}
  }]
}
```

## 開発

```bash
uv sync
uv run pytest                    # ユニットテスト
TYPESAFE_API_KEY=... uv run pytest tests/test_integration.py  # 統合(API 要)
uv run ruff check --fix . && uv run ruff format . && uv run mypy .
```

## 検証課題

- 境界判定の精度とコスト: 行数に比例する質問数。長い文書での上限は要検証
- **初回実測(2026-09-18, samples/bad.md)**: 境界候補は「各非空行と次の非空行」のペア(空行は飛ばす)。空行区切りの段落も質問対象になり、prob >= BOUNDARY_THRESHOLD で切断される。切断点は次の非空行の直前で、空行はどちらのブロック行範囲にも含めない
- `scope: both` のカテゴリ(concision)は同じ文が block/document 両スコープの locate で flag されることがある。同一 (行範囲, オフセット, text, category) は pipeline で1件に集約し、scope は実際の出現集合から求める({block,document}→'both')。probability は大きい方を採用する
- 見出し行(`# まとめ` 等)も文候補として locate の対象になる。structure.locate が見出しを対象とするため候補に残す方針(設計メモ)
- **構文領域の除外(製品仕様)**: fenced code block(```/~~~)、先頭の front matter(`---`〜`---`)、表行(`|` 始まり)は採点対象から除外する。表行はセル内に日本語 prose を含み得るが、行単位の除外を製品仕様として採用(セル単位の prose 抽出は将来検討)。裸の YAML/JSON「らしさ」判定などのヒューリスティックは入れない。除外は境界候補・採点本文・locate 候補に一貫適用し、除外行はブロックをまたがない(強制切断)。文脈(state の context/document フィールド)には残す。行番号・文字オフセットは原文の source map で保持し、行削除・再採番はしない
- `threshold` の既定 1.5、`BOUNDARY_THRESHOLD`/`LOCATE_THRESHOLD` の 0.5 は初回値。実データで調整が要る
- document scope の state 上限(現在は概算 32000 字で skip)
- **既知の制約(state サイズ、フェーズ2で対応予定)**: 境界判定(noul)は各質問に文書全文を state として再送するため、長文ではコスト・サイズが上限(state+最長質問 32k tokens、state+全質問 64k tokens)を超え得る。document locate の state は「先頭16000字+全文の候補列」で、合計が概算 32000 字を超える場合は候補列を切り詰めず `locate_state_too_large` で skipped に記録する。抜本対応は共有ウィンドウ方式でフェーズ2に行う
- Jev の日本語判定精度は対象ドメインでの検証が前提
