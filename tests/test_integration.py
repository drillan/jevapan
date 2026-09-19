"""実 API を叩く統合テスト。

Jev の応答は確率的で、同一文書でも blocks/violations の件数が揺れる
(実測: blocks 18↔19, violations 21↔22, issue #5)。アサートは件数の
厳密一致ではなく、確率に依らず成立する構造的契約と、サンプルが持つ
意味的性質(悪文は検出される・良文はほぼクリーン)に限定する。"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from jevapan.cli import main

pytestmark = pytest.mark.skipif(
    not os.environ.get("TYPESAFE_API_KEY"), reason="requires TYPESAFE_API_KEY"
)

# base ruleset(既定)のカテゴリ。block scope 対象と document scope 対象に分かれる
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


def _assert_score(name: str, s: dict[str, Any]) -> None:
    # SDK の score は水準 0 始まりの期待値(levels=3 → [0, 2])
    assert name in CATEGORIES
    assert 0.0 <= s["score"] <= 2.0
    assert 0.0 <= s["confidence"] <= 1.0


def _assert_result_contract(f: dict[str, Any], lines: list[str]) -> None:
    """確率に依らず成立する出力構造の契約を検証する。"""
    n = len(lines)
    prose = [i for i, ln in enumerate(lines, 1) if ln.strip()]

    # segment は非空行を昇順・非重複のブロックに分割する。件数は揺れるが
    # 被覆の両端(先頭・末尾の非空行)と整列性は契約として固定される
    blocks = f["blocks"]
    assert blocks
    prev_end = 0
    for b in blocks:
        start, end = b["lines"]
        assert 1 <= start <= end <= n
        assert start > prev_end
        prev_end = end
        assert set(b["scores"]) == BLOCK_CATEGORIES
        for name, s in b["scores"].items():
            _assert_score(name, s)
    assert blocks[0]["lines"][0] == prose[0]
    assert blocks[-1]["lines"][1] == prose[-1]

    assert set(f["doc_scores"]) == DOC_CATEGORIES
    for name, s in f["doc_scores"].items():
        _assert_score(name, s)

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

    # skip 理由はいずれも入力サイズで決まり確率的でない。
    # サンプルは上限を大きく下回るので空が契約
    assert f["skipped"] == []

    usage = f["usage"]
    assert usage["calls"] > 0
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0

    # 揺れの実測値を残す(失敗時は captured stderr に出る)
    print(
        f"observed: {f['file']}: blocks={f['summary']['blocks']} "
        f"violations={f['summary']['violations']} calls={usage['calls']}",
        file=sys.stderr,
    )


def _run_check(capsys: Any, path: str) -> dict[str, Any]:
    """jvp check --format json を実行し、構造契約を検証済みの
    ファイル結果を返す。"""
    rc = main(["check", path, "--format", "json"])
    captured = capsys.readouterr()
    # 0/1 は lint の終了コード契約。2(引数・設定・API エラー)は失敗にする
    assert rc in (0, 1), captured.err
    out = json.loads(captured.out)
    files = out["files"]
    assert len(files) == 1
    f: dict[str, Any] = files[0]
    assert f["file"] == path
    _assert_result_contract(f, Path(path).read_text().splitlines())
    return f


def test_bad_doc_flags_violations(capsys: Any) -> None:
    f = _run_check(capsys, "samples/bad.md")
    violations = f["violations"]
    # 件数自体は揺れる(実測 21↔22)が、悪文サンプルで検出ゼロは回帰
    assert violations
    # bad.md は中身のない型(substance)・構造上の問題を仕込んである
    assert {v["category"] for v in violations} & {"substance", "structure"}


def test_good_doc_mostly_clean(capsys: Any) -> None:
    f = _run_check(capsys, "samples/good.md")
    assert len(f["violations"]) <= GOOD_MAX_VIOLATIONS
