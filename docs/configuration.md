# jevapan.yaml による設定

## jevapan.yaml の探索

カレントディレクトリから上方に遡って `jevapan.yaml` / `.jevapan.yaml` を探索し、選択中のルールセットにマージする。`--config PATH` で明示も可能である。

## 設定例

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
    threshold: 1.5           # このスコア未満で flag → 文特定へ
    description: "採点基準の説明(質問の instructions になる)"
    levels:                  # 2〜5 段。下から違反あり → クリーンの順
      - "悪い状態の説明"
      - "良い状態の説明"
    locate: "この文は〜である"  # 省略可。違反文特定の指示
    locate_threshold: 0.6     # locate 確率の flag 閾値。既定 0.6、カテゴリ別に上書き可
```

## フィールド

| フィールド | 既定 | 説明 |
|---|---|---|
| `name` | (必須) | カテゴリ名。既存名なら上書き、新規なら追加 |
| `scope` | `block` | 採点単位。`document` は文書全体、`both` は両方 |
| `severity` | `warning` | `error` は終了コード 1 の対象になる |
| `threshold` | `1.5` | flag に引っかかるスコアの上限 |
| `description` | (新規時必須) | 採点基準。API への instructions になる |
| `levels` | (新規時必須) | 2〜5 段の評価基準。悪い状態から良い状態の順 |
| `enabled` | `true` | `false` でカテゴリを無効化 |
| `locate` | なし | 違反文を特定するための指示文 |
| `locate_threshold` | `0.6` | locate 確率がこの値以上の文を違反として採用 |

## 調整の指針

- `threshold` を下げると flag が減り、見落としは増える
- `locate_threshold` を上げると指摘数が減り精度が上がる。初回値の 0.6 は `scripts/eval_thresholds.py` の実測に基づく
- score ゲートを通過したブロックだけが locate されるため、全体が良好な文書内の個別違反は捕捉されない場合がある
