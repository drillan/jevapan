# jevapan 設計書

日本語文書をセマンティックに lint する CLI ツール。TypeSafe の System One モデル Jev を判定エンジンに使い、ユーザーが YAML で定義した規範カテゴリに対して文書を採点し、違反箇所を文レベルで指摘する。

- ステータス: 承認待ち
- 日付: 2026-09-18

## 目的と非目的

### 目的

- 日本語の技術文書・ブログ記事などを、規範カテゴリごとに採点し違反箇所を特定する
- エージェントが skill や hook から呼び出して自律的に文章を修正できるよう、機械可読な出力を提供する
- 人間の文章チェックツールとしても使える可読出力を提供する
- `uv tool install jevapan` でインストールでき、設定なしで既定ルールセットが動く

### 非目的

- 修正案の生成。Jev はテキストを生成しない判定モデルであり、修正は呼び出し側（エージェントや人間）が行う
- 機械的ルールの検査。文長・読点数・表記ゆれの網羅列挙など、決定的に検査できるものは textlint 等の既存ツールの領分。表記ゆれは「意味判定として扱える部分」だけを consistency カテゴリで扱う
- 英語など他言語の既定ルール。判定エンジン自体は言語非依存だが、同梱プリセットは日本語向け

## 決定事項の一覧

| 項目 | 決定 |
| --- | --- |
| ツール名 | jevapan（コマンドは `jevapan` と短縮形 `jvp` の2つを登録） |
| 責任範囲 | 判定・指摘のみ。修正は呼び出し側 |
| ルール定義 | YAML テンプレート固定。カテゴリ名・水準・特定指示・severity・threshold・scope のみ書ける |
| ブロック分割 | 方式A: Jev が行境界ごとに「話題・役割が変わるか」を Noul で判定。完全な Markdown パーサは持たないが、fenced code・front matter・表行は決定的に構文解析して採点対象から除外する(mask.py) |
| 既定ルール | base / tech-doc / blog の3プリセットを同梱 |
| ルール層 | Jev のセマンティック判定のみ。決定的ルール層は持たない |
| コスト制御 | キャッシュ機構は拡張点のみ。v1 は毎回全量判定 |
| 継承 | `extends:` でプリセット・ファイルを継承しカテゴリをマージ |
| 一貫性 | `scope: document` で文書横断の判定 |
| 冗長 | `scope: both` でブロック内と節横断の両方を判定 |

## アーキテクチャ

```
src/jevapan/
├── cli.py          … argparse。check サブコマンド
├── config.py       … jevapan.yaml の探索・読み込み・pydantic 検証
├── ruleset.py      … ルールセット解決(プリセット名→パス、extends マージ)
├── mask.py         … 構文領域マスク(fenced code/front matter/表行)の source map
├── segmenter.py    … 行境界 Noul → ブロック列
├── scorer.py       … ブロック×カテゴリの Score を fan-out
├── locator.py      … flag 箇所の文単位 Noul
├── engine.py       … AsyncTypeSafeClient + Semaphore、リトライ
├── report.py       … human/json フォーマット、severity→終了コード
└── rulesets/
    ├── base.yaml
    ├── tech-doc.yaml
    └── blog.yaml
```

## パイプライン(3段)

```
入力(ファイル or stdin)
  → [1] segmenter: 行境界ごとに Noul「ここで話題・役割が変わるか」
        → ブロック列(行番号範囲つき)
  → [2] scorer:
        scope: block のカテゴリ → ブロックごとに1リクエスト、全 block カテゴリの Score を fan-out
        scope: document のカテゴリ → 文書全体を state に1リクエスト
        → {score, confidence}
  → [3] locator: score < threshold のブロック(または文書)×カテゴリだけ
        候補文に Noul「このカテゴリの違反か」→ 違反文リスト(行番号・確率つき)
  → report: severity 集約 → human 表示 / JSON → 終了コード
```

- 全段 `AsyncTypeSafeClient` + `asyncio.Semaphore(20)` で並列。実測で100並列が問題なかった実績あり
- 同一 state への質問は1リクエストに fan-out する
- エンジンは生 `typesafe-sdk`。pydantic-ai は構造化 state を質問に渡せず、境界判定・文特定の「候補を state で与える」形が書けないため不採用
- **既知の制約(score ゲート)**: `score >= threshold` のブロック・文書は locate されないため、全体は良好でも個別違反を含む箇所を見逃し得る(recall は score ゲートに依存)。locate 側の確率閾値はカテゴリ別 `locate_threshold`(既定0.6)で調整可能
- **補助文脈の state 構造**: block 採点と block locate は同じ補助文脈(直近の有効見出し `heading` + 直前の prose 段落 `context`、除外行は含めない)を共有し、評価対象本文(`body`/`block`・`candidates`)と分離して渡す

### 候補の列挙

- **境界候補**: 各非空・非除外行と次の非空・非除外行のペア(空行は飛ばす)。除外行は連結を断つ。ペアは最大30ずつ連続グループに一意に割当てられ、グループの対象範囲+前後余白を共有 state とした共有ウィンドウで判定する(質問はウィンドウ内 index、ウィンドウ端は強制境界にしない)。core・実ペイロードともに文字数予算を超える場合は分割し、それでも収まらないペアは未検査として skipped に記録する
- **行の3区分**: 行は「採点対象からの除外」「採点するが境界にしない」「通常」の3区分で扱う。除外(fenced code・front matter・表行)は mask.py が決定的に検出し採点対象から外す。「採点するが境界にしない」第3カテゴリは同一リスト木の継続で、開いているリスト項目のコンテキスト(マーカ種別・マーカのインデント・content 列)をスタックで追跡して構文判定する: 最内項目の content 列以上にインデントされた非マーカ継続行(空行を挟んでも同一項目と構文一意に決まる)、最内項目(dedent 後は祖先項目)の content 列以上のマーカ行(ネスト項目・種別不問)、dedent 後の同レベル同種マーカ(兄弟項目)。除外行のインデントが最内項目の content 列未満なら項目を閉じる(トップレベルの fence・表行はリストを閉じる)。空行を挟む loose な項目ペアは抑制せず Jev の意味判定に委ねる(tight 制約はマーカ行のペアにのみ適用)。受容トレードオフとして、同種マーカ連続の途中にある話題転換は構文的に検出しない(質問自体が送られない)
- **文候補(locator)**: ブロック内を「。」と行単位(箇条書き各行)で分割した文列。プロトタイプで判明した箇条書き接着問題を避けるため、行単位も候補に含める
- **スコアの数値範囲**: levels の個数-1 が最大(3段なら 0〜2)。threshold はこの数値と比較する

## ルールセット YAML スキーマ

```yaml
version: 1
extends: base            # 省略可。プリセット名またはファイルパス
categories:
  - name: structure      # 一意。extends 時は同名で上書き、新規名で追加
    enabled: true        # false で無効化(extends した親のカテゴリを消す用途)
    scope: block         # block(既定) | document | both
    severity: warning    # error | warning | info。既定 warning
    threshold: 1.5       # このスコア未満で flag→文特定へ。既定1.5
    description: 採点基準の説明(質問の instructions になる)
    levels:              # 2〜5段。下から違反あり→クリーンの順
      - 話題が混在し、段落の先頭文を読んでも何の話か分からない
      - おおむね一トピックだが接続や流れに弱い箇所がある
      - 一段落一トピックで接続も明示されている
    levels_document:     # scope が document/both のとき文書採点に使う水準。省略時は levels
      - 同じ主張が複数の節・段落で繰り返されている
      - 一部の節で内容が重複している
      - 各節が異なる役割を担い主張の重複がない
    locate: 候補文に問う Noul 質問  # 省略可。省略時はブロック単位の指摘まで。role条件化(先頭文/接続文/見出し等)と「この文は引用・ルール定義・悪文の説明例ではなく著者自身の記述として違反している」の肯定条件を含める(引用等を著者の悪文と同一視しない)
    locate_threshold: 0.6  # locate 確率を flag とする閾値。既定0.6、YAML でカテゴリ別に上書き可
```

### extends のマージ規則

- `extends` はプリセット名または YAML パス。再帰的に解決し循環はエラー
- カテゴリは `name` でキー。子に同名カテゴリがあればそのフィールドで上書き、なければ追加
- カテゴリの削除は `enabled: false` を子で書いて無効化する形を取る

### scope の意味

| scope | 採点単位 | 用途 |
| --- | --- | --- |
| block | ブロックごと | 段落構成・明確さ・語りなど、局所的に判断できる規範 |
| document | 文書全体 | 表記ゆれ・用語・文体の統一など、横断的な性質 |
| both | 両方 | 冗長など、ブロック内と節横断の両方で起きる規範 |

document scope の違反特定は、文書全体をコンテキストに含めた state で候補文を Noul 判定する。「節Aのこの文は節Cと重複」のような横断指摘が可能。

## 既定ルールセット

### base.yaml — 7カテゴリ(全ジャンル共通)

| name | scope | 内容 | 出典 |
| --- | --- | --- | --- |
| structure | block | 段落が一トピックでまとまり見出しが内容を具体的に指すか | k16shikano「段落と論証」「見出し」 |
| clarity | block | 指示語・抽象語の参照が一意に決まり未定義の術語を初出で使わないか | 「読み手の負荷」 |
| concision | both | ブロック内の繰り返し＋節をまたいだ主張の重複がないか | 「冗長の排除」 |
| voice | block | 行為者主語の動作の連なりで対象を指す語が具体的か | 「視点と語り」 |
| naturalness | block | 翻訳調の比喩・擬人化・日本語に存在しない言い回しがないか | 「翻訳調比喩」 |
| substance | block | 「重要なのは」「まとめると」等の中身のない型がないか | 「LLMっぽい表現」 |
| consistency | document | 用語・表記・文体が文書内で統一されているか | JTFガイド・textlint系を意味判定に翻訳 |

### tech-doc.yaml — extends: base、+3カテゴリ

| name | scope | 内容 |
| --- | --- | --- |
| rigor | block | 断定と推量の使い分け、因果に機構があるか、単一原因への還元がないか |
| reader_load | block | 後で参照しない固有名・概念の初出順・装飾的精度 |
| restraint | block | 決め台詞・修辞疑問・太字や感嘆符の多用がないか |

### blog.yaml — extends: base、+2カテゴリ

| name | scope | 内容 |
| --- | --- | --- |
| restraint | block | 同上。ブログでも過剰な演出は減点 |
| honesty | block | 確認していないことを確認した風に書かない、作為的な例への弁明 |

blog.yaml は voice の threshold を緩めに設定（語り口の自由度が高いため）。

全カテゴリの既定 severity は warning。error に上げるかはユーザーの jevapan.yaml で決める運用。

## CLI

```
jvp check <file...>          # ファイル指定
jvp check -                  # stdin(hook 用)
jvp check docs/ -r           # 再帰
  --ruleset NAME|PATH        # プリセット名 or YAML パス(既定: base)
  --config PATH              # jevapan.yaml 明示(既定: 自動探索)
  --format human|json        # 既定: TTY なら human、パイプなら json
  --concurrency N            # 既定 20
  --fail-under SCORE         # CI ゲート用(任意)
```

設定ファイル `jevapan.yaml` / `.jevapan.yaml` をカレントディレクトリから上方に遡って探索。textlint の慣例に倣う。

`[project.scripts]` に `jevapan` と `jvp` の2エントリを登録。

## 出力

### human

節×カテゴリのスコア表＋違反文の一覧(行番号・カテゴリ・確率・テキスト)。

### json — エージェント消費用の安定スキーマ

```json
{
  "file": "docs/guide.md",
  "blocks": [{"lines": [1, 8], "scores": {"structure": {"score": 1.9, "confidence": 0.8}}}],
  "violations": [{"lines": [3, 3], "scope": "block", "category": "substance",
                   "severity": "warning", "probability": 0.91,
                   "score": null, "confidence": null,
                   "text": "重要なのは〜である。"}],
  "skipped": [{"category": "consistency", "reason": "state_too_large"}],
  "summary": {"blocks": 12, "violations": 4, "errors": 0}
}
```

違反レコードの結果種別契約:

- **locate 由来**: `probability` に Noul の yes 確率、`score`/`confidence` は null
- **locate 未指定 flag(Score 由来)**: `probability` は null、`score`/`confidence` に Score の値。Score confidence は水準分布の集中度であり違反確率ではないため `probability` に流用しない
- human 出力の `P=` 表示は Noul 由来のみ(Score 由来は `score=` 表示)

`skipped` エントリは `{category, reason}`(カテゴリ単位の未検査)または `{stage, reason, lines}`(境界ペア等カテゴリを持たない未検査)。reason は `state_too_large`/`window_state_too_large`/`score_state_too_large`/`locate_state_too_large`/`no_evaluable_prose`。未検査は切り詰め・既定値での継続をせず必ずここに記録する

### 終了コード

- 0: error severity の違反なし(warning/info のみ or clean)
- 1: error severity の flag が1件以上
- 2: 実行エラー(API エラー・YAML 不正・引数不正等)

## エラー処理

| 状況 | 挙動 |
| --- | --- |
| API キー未設定 | 起動時に終了、exit 2、stderr に設定方法を表示 |
| API エラー | RetryPolicy で上限付きリトライ、枯渇で exit 2。部分的結果で成功を装わない |
| document scope が state 上限超過 | そのカテゴリをスキップ、`skipped` に理由を記録 |
| YAML スキーマ違反 | pydantic で起動時検証、どのカテゴリのどのフィールドが不正かを出して exit 2 |
| extends の循環 | exit 2、循環したパスを表示 |
| 空文書・評価対象 prose 0(コードのみ等) | 採点・locate を実行せず blocks=[]、document scope カテゴリは `skipped{reason:"no_evaluable_prose"}` |

## テスト方針

- ユニット: YAML マージ規則、境界判定→ブロック化の合成、severity→終了コード、scope による採点単位の振り分け。Jev 呼び出しはモック
- 統合(API 要): 違反を埋め込んだ日本語文書とクリーン文書の `samples/` で、想定カテゴリに flag が立つかのスモーク。`--require-api` フラグで任意実行
- 実検証: プロトタイプ(jev-practice の doc_lint.py)との結果比較を初回マイルストーンに入れる

## 拡張点(v1 では実装しない)

- ブロック単位キャッシュ(変更ブロックのみ再判定)
- scope 別パラメータの拡張(locate_document など文書スコープ固有の定義)
- severity 別の集計や失敗条件の高度化(カテゴリ別ゲート等)
- 修正案生成(--fix)。Jev 専用方針のため別モデル統合が前提

## 未解決・検証課題

- 境界判定の精度とコスト: 行数に比例する質問数。長い文書で上限を要検証
- 閾値 1.5 は仮置き。実データで調整が要る
- document scope の state 上限(具体値は API 仕様で要確認)
- Jev の日本語判定精度は対象ドメインでの検証が前提(公式方針)
