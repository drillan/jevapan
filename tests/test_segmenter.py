from unittest.mock import AsyncMock

from jevapan.engine import Engine
from jevapan.segmenter import find_boundary_candidates, segment


def test_candidates_pair_with_next_nonempty_line() -> None:
    lines = ["a", "", "b", "c", "", "d"]
    # 各非空行と次の非空行のペア(空行は飛ばす): a→b, b→c, c→d
    assert find_boundary_candidates(lines) == [(0, 2), (2, 3), (3, 5)]


async def test_segment_groups_lines_by_boundary() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    # 境界: 0-1 なし, 1-2 あり, 2-3 なし → blocks [0-1],[2-3]
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"b0": 0.1, "b1": 0.9, "b2": 0.2}
    )
    blocks = await segment(eng, "a\nb\nc\nd")
    assert [(b.start, b.end) for b in blocks] == [(1, 2), (3, 4)]


async def test_segment_splits_blank_separated_paragraphs() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"b0": 0.9, "b2": 0.1}
    )
    blocks = await segment(eng, "a\n\nb\nc")
    # a→b で切断、b→c は切断しない → blocks [1],[3-4]
    assert [(b.start, b.end) for b in blocks] == [(1, 1), (3, 4)]
    assert blocks[0].text == "a" and blocks[1].text == "b\nc"


async def test_segment_empty_doc_returns_single_block() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value={})  # type: ignore[method-assign]
    blocks = await segment(eng, "")
    assert len(blocks) == 1
