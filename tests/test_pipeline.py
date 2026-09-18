from unittest.mock import AsyncMock

from jevapan.engine import Engine, ScoreResult
from jevapan.pipeline import lint_text
from jevapan.ruleset import parse_ruleset


async def test_pipeline_skips_document_scope_over_limit() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value={"b0": 0.0})  # type: ignore[method-assign]
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"c": ScoreResult(1.9, 0.9)}
    )
    rs = parse_ruleset(
        {
            "categories": [
                {
                    "name": "c",
                    "scope": "document",
                    "description": "d",
                    "levels": ["a", "b"],
                }
            ]
        },
        "t",
    )
    res = await lint_text(eng, "x" * 40000, rs, "big.md")
    assert res.skipped and res.skipped[0]["reason"] == "state_too_large"
    # 上限チェックは全 API 呼出し前に行われ、document 採点(全文 state)は呼ばれない
    assert all(
        not isinstance(c.kwargs["state"], str) for c in eng.score_batch.call_args_list
    )


async def test_document_locate_state_overflow_is_skipped() -> None:
    """document locate の state(先頭16000字+候補列)が上限超過 → skipped で明示。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"s0": 0.9}
    )
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"c": ScoreResult(0.0, 0.0)}
    )
    rs = parse_ruleset(
        {
            "categories": [
                {
                    "name": "c",
                    "scope": "document",
                    "description": "d",
                    "levels": ["a", "b"],
                    "locate": "x?",
                }
            ]
        },
        "t",
    )
    # len(text) <= STATE_DOC_LIMIT だが locate state (16000 + 候補 ~30000) は超過
    res = await lint_text(eng, "あ。" * 15000, rs, "f.md")
    assert res.skipped == [{"category": "c", "reason": "locate_state_too_large"}]
    assert res.violations == []


async def test_pipeline_merges_duplicate_violations() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    # block scope と document scope の locate が同じ文を指す → 1件に集約
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"s0": 0.9, "s1": 0.1}, {"s0": 0.8, "s1": 0.2}]
    )
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"concision": ScoreResult(0.5, 0.9)}
    )
    rs = parse_ruleset(
        {
            "categories": [
                {
                    "name": "concision",
                    "scope": "both",
                    "description": "d",
                    "levels": ["a", "b"],
                    "locate": "dup?",
                }
            ]
        },
        "t",
    )
    res = await lint_text(eng, "x。y。", rs, "f.md")
    assert len(res.violations) == 1
    v = res.violations[0]
    assert v.scope == "both" and v.probability == 0.9 and v.text == "x。"


async def test_pipeline_does_not_merge_distinct_sentences() -> None:
    """同一行の別文は別 violation として残り、block 同士で 'both' に化けない。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"s0": 0.8, "s1": 0.9}
    )
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"c": ScoreResult(0.5, 0.9)}
    )
    rs = parse_ruleset(
        {
            "categories": [
                {
                    "name": "c",
                    "scope": "block",
                    "description": "d",
                    "levels": ["a", "b"],
                    "locate": "x?",
                }
            ]
        },
        "t",
    )
    res = await lint_text(eng, "一文目。二文目。", rs, "f.md")
    assert len(res.violations) == 2
    assert {v.text for v in res.violations} == {"一文目。", "二文目。"}
    assert all(v.scope == "block" for v in res.violations)
    # オフセットで区別できる
    assert {v.col for v in res.violations} == {0, 4}
