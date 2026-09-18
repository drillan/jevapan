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
