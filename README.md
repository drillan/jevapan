# jevapan

日本語文書をセマンティックに lint する CLI。TypeSafe の System One モデル Jev を判定エンジンに使い、YAML で定義した規範カテゴリに対して文書を採点し、違反箇所を文レベルで指摘する。修正案の生成は行わない（判定・指摘のみ）。

## インストール

```bash
uv tool install .
# またはリポジトリから
uv tool install git+https://github.com/drillan/jevapan
```

`jevapan` と短縮形 `jvp` の2コマンドが登録される。

## セットアップ

```bash
export TYPESAFE_API_KEY=...
```

実行時に cwd の `.env` も自動で読み込む(KEY=VALUE・`export` 接頭辞・
引用符・コメントを受理する簡易パーサ。既存の環境変数は上書きしない)。
未設定の場合は exit 2 で終了する。

## 使い方

```bash
jvp check docs/guide.md        # ファイル指定
jvp check -                    # stdin(hook 用)
jvp check docs/ -r             # 再帰(*.md, *.txt)
  --exclude GLOB               # 走査除外パターン(複数指定可)
  --no-default-excludes        # 既定の除外セットを無効化
  --ruleset NAME|PATH          # プリセット名 or YAML パス(既定: base)
  --config PATH                # jevapan.yaml 明示(既定: 自動探索)
  --format human|json|auto     # 既定: TTY なら human、パイプなら json
  --concurrency N              # 既定 20
  --fail-under SCORE           # CI ゲート用(任意)
```

### 走査時の除外

`-r` のディレクトリ走査では、生成物・VCS・仮想環境などのディレクトリを既定で除外する(除外した件数は stderr に警告として出る)。明示指定したファイル・ディレクトリ自身には除外を適用しない。

既定除外: `.git` `.hg` `.svn` `_build` `build` `dist` `node_modules` `.venv` `venv` `.tox` `.nox` `.mypy_cache` `.pytest_cache` `.ruff_cache` `__pycache__` `*.egg-info` `.worktrees` `.claude` `.devin` `.agents` `.cursor`

`--exclude GLOB` は走査ルートまたは cwd からの相対パス・名前にマッチする(`*` は `/` をまたぐ。複数指定可)。例: `--exclude 'docs/superpowers' --exclude '*.md.txt'`。`--no-default-excludes` で既定セットのみ無効化できる(`--exclude` は引き続き有効)。

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
    locate_threshold: 0.6     # locate 確率の flag 閾値。既定0.6、カテゴリ別に上書き可
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
- `threshold` の既定 1.5、`BOUNDARY_THRESHOLD` の 0.5 は初回値。実データで調整が要る
- **locate_threshold 実測(2026-09-19, jev-1.13.0, 著者条件適用後のルール文)**: `scripts/eval_thresholds.py` で samples/bad.md(176候補)の「採用質問件数/統合後違反件数」は >=0.5:48/41、>=0.6:26/24、>=0.7:8/8、>=0.8:2/2。samples/good.md(26候補)は全閾値で 0 件で分離は良好。concision が最も閾値敏感。既定 0.6 は妥当な初回値と判断。カテゴリ別に `locate_threshold` を YAML で上書き可。なお候補は「score ゲートを通過し locate が実行されたもの」に限定され、ゲートで落ちた候補は含まれない。「採用質問件数」は API 回答件数、「統合後違反件数」は block/document 同一候補を (行範囲,col,text,category) で統合した数(以前の 159候補/38件という記録は前者)
- **既知の制約(score ゲートの recall)**: `score >= threshold` のブロック・文書は locate されないため、全体は良好でも個別違反を含む箇所を見逃し得る。閾値検討用に `scripts/eval_thresholds.py` が locate の生確率をダンプし、0.5/0.6/0.7/0.8 での採用質問件数と統合後違反件数を比較出力する(生確率・候補本文・原文位置・model・usage・入力hash は JSON に保存): `TYPESAFE_API_KEY=... uv run python scripts/eval_thresholds.py samples/bad.md`
- document scope の state 上限(現在は概算 32000 字で skip)
- **境界判定の共有ウィンドウ**: 境界ペアは最大30ペアずつの連続グループに一意に割当てられ、各グループの対象範囲+前後余白(最大20行)を1つの state として共有する。core(担当ペアが張る行範囲)自体も ~16000字の文字数予算で分割し、単一ペアでも収まらないペアは「未検査」として `skipped{stage:"segment", reason:"window_state_too_large", lines}` に記録する(黙って「境界なし」扱いにしない)。質問はウィンドウ内 index で参照し、元行番号との対応はコードが管理する。余白内のペアは重複判定せず、ウィンドウ端を強制境界にしない。グループ単位のリクエストは semaphore 内で asyncio.gather により並列実行される
- **既知の制約(state サイズ)**: 全 API 呼出しの実ペイロード(state+questions の JSON 概算文字数)に予算を適用する。日本語は概ね 1 文字 ≈ 1 token で、state+質問+回答がモデルの入力制約に収まる必要があるため、採点/locate は 32000 字、共有ウィンドウは 16000 字を保守的な頭金として設計。超過時は切り詰めず skipped で明示する(`state_too_large`/`window_state_too_large`/`score_state_too_large`/`locate_state_too_large`/`no_evaluable_prose`)。locate の候補 state はカテゴリごとに再送される(同一ブロックの flag 済みカテゴリ×候補をまとめる余地あり)
- Jev の日本語判定精度は対象ドメインでの検証が前提
- **境界判定の実測(2026-09-19, jev-1.13.0, 166リクエスト)**: 共有ウィンドウ方式の境界 Noul は、リクエスト単位でほぼ一定の確率を返し、ペア間で判別していない。plan.md の1窓(30問)は全問 0.30–0.38(SD 0.019)で、明白な境界と明白な継続が同値だった。窓ごとの定数は 0.33/0.50/0.52/0.56/0.67 と異なり、ブロック数の実行間変動(実測 62–66切断)は、その窓の定数が閾値 0.5 の近傍に落ちたかどうかで決まる(定数が 0.33/0.67 の窓は flip 0%)。エージェント作成のラベル93件に対する AUC は 0.435(95%CI [0.30, 0.57])で偶然と区別できず、構文だけの規則「次行が見出し→境界」が正解率 74.2%・適合率 0.88 とこれを上回る。構文が沈黙する51件に限っても AUC 0.423 [0.25, 0.59] で、Jev は構文が決められない箇所でも情報を足していない。確率は構文特徴とも無相関(最大 +0.048)。質問文5種・バッチ3水準・state 4水準・文書2種の16条件すべてで AUC が偶然を上回らなかった。Noul の criteria で裁定規則を明文化しても改善せず、plan.md の定型区間では有意に反転した(AUC 0.177、95%CI [−0.02, 0.38])。Choice 型に定式化を変えると定数化と実行間の揺れは構造的に解消した(argmax が反復間で完全一致、flip 0%)が、正解選択は 2/9 で偶然水準だった(期待値 1.53、P(≥2)=0.457、有意には 4/9 が必要)。この結論は jev-1.13.0・単一著者コーパス・エージェント作成ラベル 97件という条件下のものである。生データはタグ eval/issue-2-stage1 のブランチに保存
