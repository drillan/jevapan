from unittest.mock import AsyncMock

from jevapan.engine import Engine, NoulResult
from jevapan.locator import locate_in_block, locate_in_document, split_candidates
from jevapan.mask import MASK_PLACEHOLDER
from jevapan.models import Block
from jevapan.ruleset import Category


def test_split_candidates_sentences_and_bullet_lines() -> None:
    block = Block(1, 4, "一文目。二文目。\n- 箇条書きA\n- 箇条書きB")
    cands = split_candidates(block, block.text.splitlines())
    texts = [t for _, _, t in cands]
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
        return_value=NoulResult(probs={"s0": 0.9, "s1": 0.2, "s2": 0.1})
    )
    block = Block(1, 3, "a。b。c。")
    out = await locate_in_block(eng, block, cat, ["a。b。c。"])
    assert len(out) == 1
    assert out[0].text == "a。" and out[0].category == "substance"


async def test_locate_in_block_uses_preceding_context() -> None:
    """後半ブロックの locate で直前段落が state の context に含まれる。"""
    eng = Engine(client=AsyncMock(), sem=None)
    cat = Category(name="c", description="d", levels=["bad", "good"], locate="x?")
    eng.noul_batch = AsyncMock(return_value=NoulResult(probs={"s0": 0.1}))  # type: ignore[method-assign]
    doc_lines = ["# 導入", "直前の段落。", "", "対象の文。"]
    block = Block(4, 4, "対象の文。")
    await locate_in_block(eng, block, cat, doc_lines)
    _, kw = eng.noul_batch.call_args
    assert "直前の段落。" in kw["state"]["context"]
    assert kw["state"]["block"] == "対象の文。"
    assert "a。" not in kw["state"]["context"]


async def test_locate_in_block_masks_excluded_lines_in_state() -> None:
    """除外領域をまたぐブロックでは state["block"] の除外行を
    プレースホルダ化する(scorer と同じ除外集合)。fence コードが
    locate 文脈に混入しない(issue #8)。"""
    eng = Engine(client=AsyncMock(), sem=None)
    cat = Category(name="c", description="d", levels=["a", "b"], locate="x?")
    eng.noul_batch = AsyncMock(return_value=NoulResult(probs={"s0": 0.1, "s1": 0.1}))  # type: ignore[method-assign]
    doc_lines = ["- 項目A", "  ```py", "  code()", "  ```", "- 項目B"]
    excluded = {1, 2, 3}
    block = Block(1, 5, "\n".join(doc_lines))
    await locate_in_block(eng, block, cat, doc_lines, excluded)
    shown = eng.noul_batch.call_args.kwargs["state"]["block"]
    assert "code()" not in shown and "```" not in shown
    assert MASK_PLACEHOLDER in shown
    assert "- 項目A" in shown and "- 項目B" in shown


async def test_locate_questions_explain_excluded_placeholder() -> None:
    """locate の質問に [excluded] 説明を付すのは、state の masked 入力に
    実際にプレースホルダが含まれるときだけ(block/document 両経路)。
    除外領域なしの文書では無関係な説明文で水準シフトしない(issue #16)。"""
    eng = Engine(client=AsyncMock(), sem=None)
    cat = Category(name="c", description="d", levels=["a", "b"], locate="x?")
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value=NoulResult(probs={"s0": 0.1, "s1": 0.1})
    )

    # 除外領域なし → 説明は付かない
    block = Block(1, 1, "対象文。")
    await locate_in_block(eng, block, cat, ["対象文。"])
    q = eng.noul_batch.call_args.kwargs["questions"]["s0"]
    assert "[excluded]" not in q

    eng.noul_batch.reset_mock()
    eng.noul_batch.return_value = NoulResult(probs={"s0": 0.1})
    await locate_in_document(eng, "対象文。", cat, ["対象文。"])
    q = eng.noul_batch.call_args.args[1]["s0"]
    assert "[excluded]" not in q

    # 除外領域をまたぐ入力 → masked state に placeholder があり説明が付く
    eng.noul_batch.reset_mock()
    eng.noul_batch.return_value = NoulResult(probs={"s0": 0.1, "s1": 0.1})
    doc_lines = ["- 項目A", "  ```py", "  code()", "  ```", "- 項目B"]
    excluded = {1, 2, 3}
    block = Block(1, 5, "\n".join(doc_lines))
    await locate_in_block(eng, block, cat, doc_lines, excluded)
    q = eng.noul_batch.call_args.kwargs["questions"]["s0"]
    assert "[excluded]" in q and "評価対象の文章ではない" in q

    eng.noul_batch.reset_mock()
    eng.noul_batch.return_value = NoulResult(probs={"s0": 0.1, "s1": 0.1})
    await locate_in_document(
        eng, "前文。\n[excluded]\n対象文。", cat, doc_lines, excluded
    )
    q = eng.noul_batch.call_args.args[1]["s0"]
    assert "[excluded]" in q and "評価対象の文章ではない" in q


async def test_locate_in_document_does_not_truncate_state() -> None:
    """document locate の state["document"] は黙って切り詰めない(issue #12)。
    上限超過は payload チェックの StateTooLargeError → skipped で明示する。"""
    eng = Engine(client=AsyncMock(), sem=None)
    cat = Category(name="c", description="d", levels=["a", "b"], locate="x?")
    eng.noul_batch = AsyncMock(return_value=NoulResult(probs={"s0": 0.1}))  # type: ignore[method-assign]
    text = "あ" * 17000  # 旧実装の黙った切り詰め境界(16000字)を超える
    await locate_in_document(eng, text, cat, ["対象文。"])
    state = eng.noul_batch.call_args.args[0]
    assert state["document"] == text


async def test_locate_in_block_raises_when_payload_too_large() -> None:
    """block locate の実ペイロード(文脈+本文+候補+質問)が上限超過なら
    StateTooLargeError(切り詰めない)。"""
    import pytest

    from jevapan.locator import StateTooLargeError

    eng = Engine(client=AsyncMock(), sem=None)
    cat = Category(name="c", description="d", levels=["a", "b"], locate="x?")
    block = Block(1, 1, "あ。" * 20000)
    with pytest.raises(StateTooLargeError):
        await locate_in_block(eng, block, cat, [block.text])


async def test_locate_in_block_context_prioritizes_heading_and_paragraph() -> None:
    """locate の補助文脈は直近見出し+直前 prose 段落を優先し、
    長いコード(除外行)で先頭が埋まらない。"""
    eng = Engine(client=AsyncMock(), sem=None)
    cat = Category(name="c", description="d", levels=["bad", "good"], locate="x?")
    eng.noul_batch = AsyncMock(return_value=NoulResult(probs={"s0": 0.1}))  # type: ignore[method-assign]
    doc_lines = [
        "# 実装",
        "直前の段落。",
        "```",
        "code();" * 400,
        "```",
        "対象の文。",
    ]
    excluded = {2, 3, 4}
    block = Block(6, 6, "対象の文。")
    await locate_in_block(eng, block, cat, doc_lines, excluded)
    state = eng.noul_batch.call_args.kwargs["state"]
    assert state["heading"] == "実装"
    assert "直前の段落。" in state["context"]
    assert "code()" not in state["context"]
