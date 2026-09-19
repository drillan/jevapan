# jevapan 開発メモ

## 役割分担

- 実装は herdr の worker エージェントに委譲する(hub が直接コードを書かない)
- hub は設計・レビュー・計測判定・advisor 連携を担当
- advisor( Claude )による着地確認を経てからマージする

## 検証コマンド

- `uv run pytest tests/ -q`
- `uv run ruff check .` / `uv run ruff format .`
- `uv run mypy .`(strict)
- 実 API 計測: `uv run --env-file .env python scripts/eval_thresholds.py samples/bad.md`(ダンプ eval_dump.json は gitignore 済み・ローカル保存)

## 計測の注意

- locate 確率・segment 分割は実行ごとに揺れる。README「回帰判定ルール」に従い、単一閾値の件数増減では回帰と判断しない(基準実行との共通候補で確率差を比較)
- ルールセット(base.yaml 等)の文言変更は全カテゴリのスコア水準を動かしうる(バッチ干渉)。変更後はベースライン取り直し
- ベースライン記録にはルールセット構成・コミット SHA・model・候補数を併記
