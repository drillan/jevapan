"""実 API を叩く統合テスト。

Jev の応答は確率的で、同一文書でも blocks/violations の件数が揺れる
(実測: blocks 18↔19, violations 21↔22, issue #5)。アサートは件数の
厳密一致ではなく、確率に依らず成立する構造的契約と、サンプルが持つ
意味的性質(悪文は検出される・良文はほぼクリーン)に限定する。"""

import contextlib
import io
import json
import os
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from jevapan.cli import main
from jevapan.mask import analyze_syntax
from jevapan.ruleset import load_effective_ruleset

pytestmark = pytest.mark.skipif(
    not os.environ.get("TYPESAFE_API_KEY"), reason="requires TYPESAFE_API_KEY"
)

BAD = "samples/bad.md"
GOOD = "samples/good.md"

# base ruleset(既定)のカテゴリ形状の契約。preset 変更時はユニット側の
# ミラー(test_presets.py)が先に落ちるので合わせて更新する
BLOCK_CATEGORIES = {
    "structure",
    "clarity",
    "concision",
    "voice",
    "naturalness",
    "substance",
}
DOC_CATEGORIES = {"concision", "consistency"}
CATEGORIES = BLOCK_CATEGORIES | DOC_CATEGORIES
SEVERITIES = {"error", "warning", "info"}
SCOPES = {"block", "document", "both"}

# good.md で許容する違反数の上限。実測では全 locate 閾値で 0 件
# (README「検証課題」)。確率的な誤検出の揺れは吸収しつつ、多発する
# 回帰は検出できるよう小さな上限を置く
GOOD_MAX_VIOLATIONS = 2
# bad.md の違反数下限。実測 21↔22(揺れ ±1)なので確率安全。
# 検出ゼロやごく少数しか出ない検出器の回帰を拾う
BAD_MIN_VIOLATIONS = 5


@cache
def _ruleset() -> Any:
    """main() と同じ経路で effective ruleset を読む(API 不要)。"""
    return load_effective_ruleset(None, Path.cwd())


def _max_score(name: str, scope: str) -> float:
    """カテゴリの score 上限。SDK は水準 0 始まりの期待値を返すので
    len(levels)-1。document 採点は levels_document が優先される。"""
    cat = next(c for c in _ruleset().categories if c.name == name)
    levels = (cat.levels_document or cat.levels) if scope == "document" else cat.levels
    return float(len(levels) - 1)


def _assert_score(name: str, s: dict[str, Any], scope: str) -> None:
    assert name in CATEGORIES
    assert 0.0 <= s["score"] <= _max_score(name, scope)
    assert 0.0 <= s["confidence"] <= 1.0


def _assert_result_contract(f: dict[str, Any], lines: list[str]) -> None:
    """確率に依らず成立する出力構造の契約を検証する。

    ただし scores の完全集合と skipped==[] は「小さいサンプル」の契約:
    ブロック境界は Jev 確率の産物で、ブロックが大きく育つと
    score_state_too_large が発火して scores={}・skipped 記録になりうる
    (間接的に確率依存)。サンプルを拡張する場合はサイズ上限との
    兼ね合いで緩和が要る。"""
    n = len(lines)
    excluded = analyze_syntax(lines)
    # prose 行 = 非空かつ構文除外(front matter・fence・表行)でない行
    prose = [i for i, ln in enumerate(lines, 1) if ln.strip() and i - 1 not in excluded]

    # segment は非除外の非空行を昇順・非重複のブロックに分割する。
    # 件数は揺れるが被覆の両端(先頭・末尾の prose 行)と整列性は固定
    blocks = f["blocks"]
    assert blocks
    prev_end = 0
    for b in blocks:
        start, end = b["lines"]
        assert 1 <= start <= end <= n
        assert start > prev_end
        prev_end = end
        # ブロックが上限内なら全 block カテゴリが採点される(上記 docstring 参照)
        assert set(b["scores"]) == BLOCK_CATEGORIES
        for name, s in b["scores"].items():
            _assert_score(name, s, "block")
    assert blocks[0]["lines"][0] == prose[0]
    assert blocks[-1]["lines"][1] == prose[-1]

    assert set(f["doc_scores"]) == DOC_CATEGORIES
    for name, s in f["doc_scores"].items():
        _assert_score(name, s, "document")

    for v in f["violations"]:
        start, end = v["lines"]
        assert 1 <= start <= end <= n
        assert v["col"] >= 0
        assert v["scope"] in SCOPES
        assert v["category"] in CATEGORIES
        assert v["severity"] in SEVERITIES
        # locate 由来のみ Noul 確率を持つ(Score 由来は score/confidence)
        assert v["probability"] is None or 0.0 <= v["probability"] <= 1.0
        assert isinstance(v["text"], str)

    # summary は詳細リストと常に一致する
    assert f["summary"]["blocks"] == len(blocks)
    assert f["summary"]["violations"] == len(f["violations"])
    assert f["summary"]["errors"] == sum(
        v["severity"] == "error" for v in f["violations"]
    )

    # skip 理由はサイズ上限由来だが、境界(=ブロックサイズ)は確率の産物
    # なので完全な決定論ではない。現サンプルは上限を大きく下回り空が契約
    assert f["skipped"] == []

    usage = f["usage"]
    assert usage["calls"] > 0
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0


def _run_check(path: str) -> dict[str, Any]:
    """jvp check --format json を実行し、構造契約を検証済みの
    ファイル結果を返す。"""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = main(["check", path, "--format", "json"])
    # 0/1 は lint の終了コード契約。2(引数・設定・API エラー)は失敗にする
    assert rc in (0, 1), err_buf.getvalue()
    out = json.loads(out_buf.getvalue())
    files = out["files"]
    assert len(files) == 1
    f: dict[str, Any] = files[0]
    assert f["file"] == path
    _assert_result_contract(f, Path(path).read_text().splitlines())
    # 揺れの実測値を残す(失敗時は captured stderr に出る)
    print(
        f"observed: {path}: blocks={f['summary']['blocks']} "
        f"violations={f['summary']['violations']} calls={f['usage']['calls']}",
        file=sys.stderr,
    )
    return f


@pytest.fixture(scope="module")
def checked() -> dict[str, dict[str, Any]]:
    """bad/good 両サンプルを1回ずつ実行した結果。相対比較は
    同一実行ペアで行うためモジュールスコープで共有する。"""
    return {p: _run_check(p) for p in (BAD, GOOD)}


def test_bad_doc_flags_violations(checked: dict[str, dict[str, Any]]) -> None:
    violations = checked[BAD]["violations"]
    # 件数は揺れる(実測 21↔22)が、検出ゼロやごく少数は検出器の回帰
    assert len(violations) >= BAD_MIN_VIOLATIONS
    # bad.md は中身のない型(substance)・構造上の問題を仕込んである
    assert {v["category"] for v in violations} & {"substance", "structure"}


def test_good_doc_mostly_clean(checked: dict[str, dict[str, Any]]) -> None:
    assert len(checked[GOOD]["violations"]) <= GOOD_MAX_VIOLATIONS


def test_good_doc_is_cleaner_than_bad(checked: dict[str, dict[str, Any]]) -> None:
    # 同一実行ペアの相対契約。両者が同時に悪化するドリフト型の回帰を拾う
    assert len(checked[GOOD]["violations"]) < len(checked[BAD]["violations"])
