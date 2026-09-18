from unittest.mock import AsyncMock

from jevapan.engine import Engine
from jevapan.locator import locate_in_block, split_candidates
from jevapan.models import Block
from jevapan.ruleset import Category


def test_split_candidates_sentences_and_bullet_lines() -> None:
    block = Block(1, 4, "一文目。二文目。\n- 箇条書きA\n- 箇条書きB")
    cands = split_candidates(block, block.text.splitlines())
    texts = [t for _, t in cands]
    assert "一文目。" in texts and "二文目。" in texts
    assert "- 箇条書きA" in texts and "- 箇条書きB" in texts


async def test_locate_flags_high_probability_sentences() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    cat = Category(
        name="substance",
        description="d",
        levels=["bad", "good"],
        locate="empty phrase?",
    )
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"s0": 0.9, "s1": 0.2, "s2": 0.1}
    )
    block = Block(1, 3, "a。b。c。")
    out = await locate_in_block(eng, block, cat, "ctx")
    assert len(out) == 1
    assert out[0].text == "a。" and out[0].category == "substance"
