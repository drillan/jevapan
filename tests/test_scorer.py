from unittest.mock import AsyncMock

from jevapan.engine import Engine, ScoreResult
from jevapan.models import Block
from jevapan.ruleset import Category, Scope
from jevapan.scorer import score_blocks, score_document


def _cat(
    name: str,
    scope: str = "block",
    levels: list[str] | None = None,
    levels_document: list[str] | None = None,
) -> Category:
    return Category(
        name=name,
        scope=Scope(scope),
        description=f"desc-{name}",
        levels=levels or ["bad", "good"],
        levels_document=levels_document,
    )


async def test_block_categories_scored_per_block() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "structure": ScoreResult(1.8, 0.7),
            "clarity": ScoreResult(0.9, 0.6),
        }
    )
    blocks = [Block(1, 3, "t1"), Block(4, 5, "t2")]
    out = await score_blocks(eng, blocks, [_cat("structure"), _cat("clarity")])
    assert len(out) == 2
    assert out[0].scores["structure"].score == 1.8
    assert eng.score_batch.call_count == 2  # ブロックごとに1リクエスト


async def test_document_scope_uses_document_levels() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"consistency": ScoreResult(0.4, 0.9)}
    )
    cats = [
        _cat(
            "consistency",
            scope="document",
            levels_document=["doc-bad", "doc-good"],
        )
    ]
    out = await score_document(eng, "full text", cats)
    _, kw = eng.score_batch.call_args
    assert kw["questions"]["consistency"][1] == ["doc-bad", "doc-good"]
    assert out["consistency"].scope == "document"


async def test_scope_both_in_both_passes() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"concision": ScoreResult(1.0, 0.5)}
    )
    cats = [_cat("concision", scope="both")]
    blocks = [Block(1, 2, "t")]
    b_out = await score_blocks(eng, blocks, cats)
    d_out = await score_document(eng, "t", cats)
    assert "concision" in b_out[0].scores and "concision" in d_out


async def test_block_scores_use_nearest_preceding_heading() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"structure": ScoreResult(1.5, 0.9)}
    )
    doc_lines = ["# 導入", "", "前文。", "# 実装", "", "対象文。"]
    blocks = [Block(3, 3, "前文。"), Block(6, 6, "対象文。")]
    await score_blocks(eng, blocks, [_cat("structure")], doc_lines=doc_lines)
    states = [c.kwargs["state"] for c in eng.score_batch.call_args_list]
    assert states[0]["heading"] == "導入"
    assert states[1]["heading"] == "実装"
