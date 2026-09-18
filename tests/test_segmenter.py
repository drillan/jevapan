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


async def test_segment_empty_doc_returns_no_blocks() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value={})  # type: ignore[method-assign]
    assert await segment(eng, "") == []


async def test_segment_short_doc_uses_single_window() -> None:
    """短い文書は1ウィンドウ。質問はウィンドウ内 index で参照する。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"b0": 0.1, "b1": 0.9, "b2": 0.2}
    )
    blocks = await segment(eng, "a\nb\nc\nd")
    assert eng.noul_batch.call_count == 1
    state, questions = eng.noul_batch.call_args.args
    assert state == {"lines": ["a", "b", "c", "d"]}
    assert "lines[1]" in questions["b1"] and "lines[2]" in questions["b1"]
    assert [(b.start, b.end) for b in blocks] == [(1, 2), (3, 4)]


async def test_segment_long_doc_uses_shared_windows() -> None:
    """長い文書は複数ウィンドウ。全ペアが一意に担当され、state は部分列。"""
    eng = Engine(client=AsyncMock(), sem=None)

    async def fake(state: object, questions: dict[str, str]) -> dict[str, float]:
        return {k: 0.0 for k in questions}

    eng.noul_batch = AsyncMock(side_effect=fake)  # type: ignore[method-assign]
    lines = [f"文{i}。" for i in range(80)]  # 79ペア → 複数ウィンドウ
    await segment(eng, "\n".join(lines))
    calls = eng.noul_batch.call_args_list
    assert len(calls) >= 2
    seen: list[str] = []
    for c in calls:
        state, questions = c.args
        # ウィンドウは文書全体ではなく部分範囲+余白
        assert len(state["lines"]) < len(lines)
        assert len(questions) <= 50
        seen += list(questions)
    # 全ペアがちょうど1回ずつ判定される(余白内の重複判定なし)
    assert sorted(seen) == sorted(f"b{i}" for i in range(79))


async def test_segment_window_blocks_align_by_original_lines() -> None:
    """複数ウィンドウでも切断点は元の行番号で正しく張り合う。"""
    eng = Engine(client=AsyncMock(), sem=None)
    # 50行 → 49ペア。ペア (30,31) のみ切断される
    lines = [f"文{i}。" for i in range(50)]

    def flagged(state: object, questions: dict[str, str]) -> dict[str, float]:
        return {k: 0.9 if k == "b30" else 0.0 for k in questions}

    eng.noul_batch = AsyncMock(side_effect=flagged)  # type: ignore[method-assign]
    blocks = await segment(eng, "\n".join(lines))
    assert [(b.start, b.end) for b in blocks] == [(1, 31), (32, 50)]
