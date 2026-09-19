"""usage/model のリクエスト単位記録(issue #4)。

score_batch はリクエスト全体の usage を各回答に複製して返すため、
回答単位の合算は重複計上になる。集計は必ず LintResult.calls
(1 API リクエスト=1 CallRecord)から行う。
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from jevapan.engine import USAGE_LOG, CallRecord, Engine, Usage
from jevapan.models import LintResult
from jevapan.pipeline import lint_text
from jevapan.report import render_human, render_json
from jevapan.ruleset import parse_ruleset


def _resp(answers: dict[str, Any], model: str = "", usage: Any = None) -> Any:
    return SimpleNamespace(answers=answers, model=model, usage=usage)


def _doc_ruleset(names: list[str]) -> Any:
    return parse_ruleset(
        {
            "categories": [
                {
                    "name": n,
                    "scope": "document",
                    "description": "d",
                    "levels": ["a", "b"],
                }
                for n in names
            ]
        },
        "t",
    )


def _result_with_calls() -> LintResult:
    return LintResult(
        file="f.md",
        blocks=[],
        block_scores=[],
        doc_scores={},
        violations=[],
        skipped=[],
        summary={"blocks": 0, "violations": 0, "errors": 0},
        calls=[
            CallRecord("score_batch", "m1", Usage(100, 5)),
            CallRecord("noul_batch", "m1", Usage(50, 2)),
        ],
    )


async def test_lint_text_records_request_level_calls() -> None:
    """lint_text は 1 API リクエストごとに stage/model/usage を calls に記録する。"""
    client = AsyncMock()
    client.system_one = AsyncMock(
        return_value=_resp(
            {"c": SimpleNamespace(score=3.0, confidence=0.9)},
            model="jev-1.13",
            usage=SimpleNamespace(input_tokens=100, output_tokens=5),
        )
    )
    eng = Engine(client=client, sem=None)
    res = await lint_text(eng, "短い文。", _doc_ruleset(["c"]), "f.md")
    assert len(res.calls) == 1
    rec = res.calls[0]
    assert rec.stage == "score_batch"
    assert rec.model == "jev-1.13"
    assert rec.usage.input_tokens == 100 and rec.usage.output_tokens == 5


async def test_calls_aggregate_without_double_counting() -> None:
    """score_batch が複数回答を返しても calls は 1リクエスト=1件。
    calls からの合算はリクエスト usage と一致し重複計上しない。"""
    client = AsyncMock()
    client.system_one = AsyncMock(
        return_value=_resp(
            {
                "a": SimpleNamespace(score=3.0, confidence=0.9),
                "b": SimpleNamespace(score=3.0, confidence=0.9),
            },
            model="m",
            usage=SimpleNamespace(input_tokens=200, output_tokens=8),
        )
    )
    eng = Engine(client=client, sem=None)
    res = await lint_text(eng, "文。", _doc_ruleset(["a", "b"]), "f.md")
    assert len(res.calls) == 1
    assert sum(c.usage.input_tokens for c in res.calls) == 200


async def test_calls_are_isolated_per_file_under_gather() -> None:
    """同一 Engine を共有する並列 lint_text でも calls はファイル別に帰属する。"""
    client = AsyncMock()
    client.system_one = AsyncMock(
        return_value=_resp(
            {"c": SimpleNamespace(score=3.0, confidence=0.9)},
            model="m",
            usage=SimpleNamespace(input_tokens=10, output_tokens=1),
        )
    )
    eng = Engine(client=client, sem=None)
    rs = _doc_ruleset(["c"])
    r1, r2 = await asyncio.gather(
        lint_text(eng, "文1。", rs, "f1.md"),
        lint_text(eng, "文2。", rs, "f2.md"),
    )
    assert len(r1.calls) == 1 and len(r2.calls) == 1


async def test_no_prose_document_records_no_calls() -> None:
    """API 呼出しなしの早期 return でも calls は空リストで返る。"""
    eng = Engine(client=AsyncMock(), sem=None)
    res = await lint_text(eng, "```\ncode\n```", _doc_ruleset(["c"]), "code.md")
    assert res.calls == []


async def test_usage_log_reset_after_lint_text() -> None:
    """lint_text 終了後は USAGE_LOG が未設定に戻る(後続呼出しに漏れない)。"""
    client = AsyncMock()
    client.system_one = AsyncMock(
        return_value=_resp(
            {"c": SimpleNamespace(score=3.0, confidence=0.9)},
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
    )
    eng = Engine(client=client, sem=None)
    await lint_text(eng, "文。", _doc_ruleset(["c"]), "f.md")
    assert USAGE_LOG.get() is None


def test_render_json_includes_usage_summary() -> None:
    out = render_json(_result_with_calls())
    assert out["usage"] == {
        "calls": 2,
        "input_tokens": 150,
        "output_tokens": 7,
        "models": ["m1"],
    }


def test_render_human_appends_usage_line() -> None:
    last = render_human(_result_with_calls()).splitlines()[-1]
    assert "usage" in last and "150" in last and "m1" in last
