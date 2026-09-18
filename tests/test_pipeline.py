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
