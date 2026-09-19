# jvp check コマンドの使い方

## インストール

```bash
uv tool install .
# またはリポジトリから
uv tool install git+https://github.com/drillan/jevapan
```

`jevapan` と短縮形 `jvp` の 2 コマンドが登録される。

## セットアップ

判定には TypeSafe の API キーが必要である。

```bash
export TYPESAFE_API_KEY=...
```

未設定の場合は終了コード `2` で終了する。

## 基本的なコマンド

```bash
jvp check docs/guide.md        # ファイル指定
jvp check -                    # stdin(hook 用)
jvp check docs/ -r             # 再帰(*.md, *.txt)
```

ディレクトリを `-r` なしで指定するとエラーになる。再帰時は `*.md` と `*.txt` を対象に収集する。

## オプション

| オプション | 既定 | 説明 |
|---|---|---|
| `-r`, `--recursive` | off | ディレクトリを再帰的に探索する |
| `--ruleset NAME\|PATH` | `base` | プリセット名または YAML ファイルのパス |
| `--config PATH` | 自動探索 | `jevapan.yaml` のパスを明示する |
| `--format auto\|human\|json` | `auto` | TTY なら `human`、パイプなら `json` |
| `--concurrency N` | `20` | API 呼び出しの並列数 |
| `--fail-under SCORE` | なし | スコアが SCORE 未満なら失敗(CI ゲート用) |

## 終了コード

- `0`: error severity の違反なし(warning / info のみ、または clean)
- `1`: error severity の flag が 1 件以上
- `2`: 実行エラー(API キー未設定・API エラー・YAML 不正・引数不正など)

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

`skipped` に含まれる項目は採点できなかった箇所であり、stderr にも警告として出力される。
