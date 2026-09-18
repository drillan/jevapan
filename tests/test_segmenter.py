from unittest.mock import AsyncMock

from jevapan.engine import Engine
from jevapan.segmenter import find_boundary_candidates, segment


def test_candidates_only_between_nonempty_lines() -> None:
    lines = ["a", "", "b", "c", "", "d"]
    assert find_boundary_candidates(lines) == [2]  # b→c のみ(0始まりindex2)


async def test_segment_groups_lines_by_boundary() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    # 境界: 0-1 なし, 1-2 あり, 2-3 なし → blocks [0-1],[2-3]
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"b0": 0.1, "b1": 0.9, "b2": 0.2}
    )
    blocks = await segment(eng, "a\nb\nc\nd")
    assert [(b.start, b.end) for b in blocks] == [(1, 2), (3, 4)]


async def test_segment_empty_doc_returns_single_block() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value={})  # type: ignore[method-assign]
    blocks = await segment(eng, "")
    assert len(blocks) == 1
