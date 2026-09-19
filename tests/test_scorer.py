from typing import Any
from unittest.mock import AsyncMock

from jevapan.engine import Engine, ScoreResult
from jevapan.mask import MASK_PLACEHOLDER
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


async def test_score_blocks_skips_oversized_payload() -> None:
    """実ペイロード(見出し/文脈/本文/質問)が上限超過のブロックは採点せず
    skipped に記録する(既定値で握らない)。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"structure": ScoreResult(1.8, 0.7)}
    )
    big = Block(1, 1, "あ" * 40000)
    skipped: list[dict[str, Any]] = []
    out = await score_blocks(eng, [big], [_cat("structure")], skipped=skipped)
    eng.score_batch.assert_not_awaited()
    assert out[0].scores == {}
    assert skipped == [
        {
            "category": "structure",
            "reason": "score_state_too_large",
            "lines": [1, 1],
        }
    ]


async def test_score_blocks_passes_heading_and_preceding_prose() -> None:
    """採点 state は locator と同じ補助文脈を持つ: 直近の有効見出しと
    直前の prose 段落(除外行=コードは文脈を埋めない)。本文は body。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"structure": ScoreResult(1.5, 0.9)}
    )
    doc_lines = [
        "# 設計",
        "x = 1  # コード内コメント",
        "直前の段落で語を定義する。",
        "```",
        "code();" * 300,
        "```",
        "対象ブロックの文。",
    ]
    excluded = {1, 3, 4, 5}
    blocks = [Block(7, 7, "対象ブロックの文。")]
    await score_blocks(
        eng, blocks, [_cat("structure")], doc_lines=doc_lines, excluded=excluded
    )
    state = eng.score_batch.call_args.kwargs["state"]
    assert state["heading"] == "設計"
    assert "直前の段落で語を定義する。" in state["context"]
    # 除外行(コード)は補助文脈に入れない
    assert "code()" not in state["context"]
    assert "x = 1" not in state["context"]
    assert state["body"] == "対象ブロックの文。"


async def test_score_blocks_masks_excluded_lines_in_body() -> None:
    """除外行を内包するブロック(項目内 fence をまたぐリスト等)は、
    scorer に渡す body で除外行をプレースホルダ化する。
    コードを prose として採点する回帰を防ぐ(issue #8)。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"structure": ScoreResult(1.5, 0.9)}
    )
    doc_lines = ["- 項目A", "  ```py", "  code()", "  ```", "- 項目B"]
    excluded = {1, 2, 3}
    blocks = [Block(1, 5, "\n".join(doc_lines))]
    await score_blocks(
        eng, blocks, [_cat("structure")], doc_lines=doc_lines, excluded=excluded
    )
    body = eng.score_batch.call_args.kwargs["state"]["body"]
    assert "code()" not in body and "```" not in body
    assert MASK_PLACEHOLDER in body
    assert "- 項目A" in body and "- 項目B" in body


async def test_score_instructions_include_authorship_scope() -> None:
    """Score の採点基準(instructions)にも locate と同じ「著者自身の記述を
    対象とする」条件を含め、採点と特定の対象契約を一致させる。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"structure": ScoreResult(1.5, 0.9)}
    )
    blocks = [Block(1, 1, "対象文。")]
    await score_blocks(eng, blocks, [_cat("structure")])
    instr = eng.score_batch.call_args.kwargs["questions"]["structure"][0]
    assert "著者自身の記述" in instr
    assert "引用" in instr and "ルール定義" in instr


async def test_document_score_instructions_include_authorship_scope() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"consistency": ScoreResult(1.5, 0.9)}
    )
    await score_document(eng, "doc", [_cat("consistency", scope="document")])
    instr = eng.score_batch.call_args.kwargs["questions"]["consistency"][0]
    assert "著者自身の記述" in instr


async def test_score_instructions_explain_excluded_placeholder() -> None:
    """採点基準に [excluded] がコードブロック・表・front matter の
    置換目印で評価対象の文章ではない旨を含める
    (block/document 両経路の masked 入力と一致させる)。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"structure": ScoreResult(1.5, 0.9)}
    )
    blocks = [Block(1, 1, "対象文。")]
    await score_blocks(eng, blocks, [_cat("structure")])
    instr = eng.score_batch.call_args.kwargs["questions"]["structure"][0]
    assert "[excluded]" in instr and "評価対象の文章ではない" in instr

    eng.score_batch.reset_mock()
    await score_document(eng, "doc", [_cat("structure", scope="document")])
    instr = eng.score_batch.call_args.kwargs["questions"]["structure"][0]
    assert "[excluded]" in instr and "評価対象の文章ではない" in instr
