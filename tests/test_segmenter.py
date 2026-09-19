from typing import Any
from unittest.mock import AsyncMock

from jevapan.engine import Engine, NoulResult
from jevapan.mask import analyze_syntax
from jevapan.segmenter import (
    WINDOW_STATE_CHARS,
    build_windows,
    find_boundary_candidates,
    segment,
)


def test_candidates_pair_with_next_nonempty_line() -> None:
    lines = ["a", "", "b", "c", "", "d"]
    # 各非空行と次の非空行のペア(空行は飛ばす): a→b, b→c, c→d
    assert find_boundary_candidates(lines) == [(0, 2), (2, 3), (3, 5)]


def test_candidates_skip_same_list_item_pairs() -> None:
    """同種リストマーカの連続は同一リストの継続で境界候補にしない
    (構文判断はコードの責務)。リスト→非リストの遷移は候補に残す。"""
    lines = ["- a", "- b", "- c", "text", "- d"]
    # 0→1,1→2 は同一リスト内。2→3(リスト→prose),3→4(prose→リスト)は残す
    assert find_boundary_candidates(lines) == [(2, 3), (3, 4)]


def test_candidates_keep_loose_list_pairs() -> None:
    """空行を挟んだ同種マーカペアは loose list として候補に残す。
    構文抑制は tight 連続のみで、意味判定は Jev に委ねる。"""
    lines = ["- a", "", "- b", "", "para"]
    assert find_boundary_candidates(lines) == [(0, 2), (2, 4)]


def test_candidates_skip_list_item_continuation_lines() -> None:
    """リスト項目の content 列以上にインデントされた非マーカ行は
    同一項目の継続(lazy continuation)としてペアを抑制する。"""
    lines = ["- 項目A", "  続きの行", "- 項目B", "text"]
    # 0→1 は継続行で抑制。1→2(非マーカ→マーカ),2→3(マーカ→prose)は候補
    assert find_boundary_candidates(lines) == [(1, 2), (2, 3)]


def test_candidates_keep_underindented_continuation() -> None:
    """content 列未満の非マーカ行は継続とみなさず候補に残す
    (見出し等は従来通り境界候補)。"""
    assert find_boundary_candidates(["- 項目A", "続きの行"]) == [(0, 1)]
    assert find_boundary_candidates(["- 項目A", "# 見出し"]) == [(0, 1)]


def test_ordered_item_continuation_uses_marker_width() -> None:
    """ordered 項目の content 列はマーカ位置+マーカ長+空白1。
    `1. ` は content 列3、`12. ` は4。"""
    assert find_boundary_candidates(["1. item", "   続き"]) == []
    assert find_boundary_candidates(["1. item", "  続き"]) == [(0, 1)]
    assert find_boundary_candidates(["12. item", "    続き"]) == []


def test_candidates_skip_nested_list_items() -> None:
    """任意の空白インデントのネスト項目も同一リスト木の継続として抑制。
    4文字以上のインデント項目は CommonMark では indented code の可能性が
    あるが、mask.py が indented code を検出しないため項目扱いで割り切る。"""
    lines = ["- a", "  - b", "    - c", "- d"]
    assert find_boundary_candidates(lines) == []


def test_thematic_break_is_not_list_marker() -> None:
    """thematic break はリスト項目より優先。`- - -` や `---` が
    項目継続に飲み込まれず、前後のペアは候補に残る。"""
    for tb in ("- - -", "---", "* * *", "___"):
        lines = ["- a", tb, "- b"]
        assert find_boundary_candidates(lines) == [(0, 1), (1, 2)]


def test_candidates_keep_loose_continuation_line() -> None:
    """空行を挟んだ継続行は loose な継続として候補に残す
    (tight 連続のみ構文抑制する L7 の分離に倣う)。"""
    lines = ["- a", "", "  cont"]
    assert find_boundary_candidates(lines) == [(0, 2)]


def test_marker_edge_cases() -> None:
    """例外的入力の挙動を固定。行末のみの `-` は空項目としてマーカ、
    タブは空白とみなさない(先頭タブ・マーカ直後タブともに非項目)。"""
    # `-` 単体は空の bullet 項目。content 列2以上の継続行は抑制
    assert find_boundary_candidates(["-", "  x"]) == []
    # 先頭タブは項目にしない
    assert find_boundary_candidates(["- a", "\t- b"]) == [(0, 1)]
    # マーカ直後がタブでも項目にしない
    assert find_boundary_candidates(["- a", "-\tb"]) == [(0, 1)]


def test_topic_change_inside_list_is_not_structurally_detected() -> None:
    """受容トレードオフ: 同種マーカ連続の途中にある話題転換は
    構文的に検出しない(質問自体が送られない)。構文で確実に
    同一ブロックと分かるものだけを抑制する設計の帰結として固定。"""
    lines = ["- りんごについて", "- 全く別の話題", "- また別件"]
    assert find_boundary_candidates(lines) == []


def test_candidates_keep_different_marker_transition() -> None:
    """マーカ種別が変わる点は別リストの開始として候補に残す。"""
    lines = ["- a", "* b", "1. c", "2. d", "1) e", "2) f"]
    # -→*(別マーカ), *→1.(bullet→ordered), 1.→2.(同種skip),
    # 2.→1)(区切り変更), 1)→2)(同種skip)
    assert find_boundary_candidates(lines) == [(0, 1), (1, 2), (3, 4)]


async def test_segment_does_not_ask_about_list_continuation() -> None:
    """同一リスト内のペアは質問自体を送らない。"""
    eng = Engine(client=AsyncMock(), sem=None)
    asked: list[str] = []

    async def fake(state: object, questions: dict[str, str]) -> NoulResult:
        asked.extend(questions)
        return NoulResult(probs={k: 0.9 for k in questions})

    eng.noul_batch = AsyncMock(side_effect=fake)  # type: ignore[method-assign]
    blocks = await segment(eng, "- a\n- b\n- c\ntail")
    # 質問されるのは list→tail (b2) のみ。b0,b1 は問われない
    assert asked == ["b2"]
    assert [(b.start, b.end) for b in blocks] == [(1, 3), (4, 4)]


async def test_segment_groups_lines_by_boundary() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    # 境界: 0-1 なし, 1-2 あり, 2-3 なし → blocks [0-1],[2-3]
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value=NoulResult(probs={"b0": 0.1, "b1": 0.9, "b2": 0.2})
    )
    blocks = await segment(eng, "a\nb\nc\nd")
    assert [(b.start, b.end) for b in blocks] == [(1, 2), (3, 4)]


async def test_segment_splits_blank_separated_paragraphs() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value=NoulResult(probs={"b0": 0.9, "b2": 0.1})
    )
    blocks = await segment(eng, "a\n\nb\nc")
    # a→b で切断、b→c は切断しない → blocks [1],[3-4]
    assert [(b.start, b.end) for b in blocks] == [(1, 1), (3, 4)]
    assert blocks[0].text == "a" and blocks[1].text == "b\nc"


async def test_segment_empty_doc_returns_no_blocks() -> None:
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value=NoulResult(probs={}))  # type: ignore[method-assign]
    assert await segment(eng, "") == []


async def test_segment_short_doc_uses_single_window() -> None:
    """短い文書は1ウィンドウ。質問はウィンドウ内 index で参照する。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value=NoulResult(probs={"b0": 0.1, "b1": 0.9, "b2": 0.2})
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

    async def fake(state: object, questions: dict[str, str]) -> NoulResult:
        return NoulResult(probs={k: 0.0 for k in questions})

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

    def flagged(state: object, questions: dict[str, str]) -> NoulResult:
        return NoulResult(probs={k: 0.9 if k == "b30" else 0.0 for k in questions})

    eng.noul_batch = AsyncMock(side_effect=flagged)  # type: ignore[method-assign]
    blocks = await segment(eng, "\n".join(lines))
    assert [(b.start, b.end) for b in blocks] == [(1, 31), (32, 50)]


def test_build_windows_splits_core_over_budget() -> None:
    """core が予算超過ならグループを分割し、除外領域をまたいだ巨大な
    原文範囲をひとまとめにしない。"""
    lines = ["a", "b", "```", "X" * 50000, "```", "c", "d"]
    excluded = analyze_syntax(lines)
    candidates = find_boundary_candidates(lines, excluded)
    windows, uninspected = build_windows(lines, candidates)
    assert uninspected == []
    # 全ペアが一意に担当され、各ウィンドウは予算内
    assert [p for w in windows for p in w.pairs] == candidates
    for w in windows:
        total = sum(len(lines[k]) + 1 for k in range(w.start, w.end + 1))
        assert total <= WINDOW_STATE_CHARS
    # 5万字の除外行はどのウィンドウにも含まれない
    assert all(w.start > 3 or w.end < 3 for w in windows)


def test_build_windows_marks_oversized_single_pair_uninspected() -> None:
    """単一ペアでも core が予算に収まらない場合は未検査として返す
    (既定値で握らない)。"""
    lines = ["あ" * 20000, "い" * 20000]
    windows, uninspected = build_windows(lines, [(0, 1)])
    assert windows == []
    assert uninspected == [(0, 1)]


async def test_segment_records_uninspected_pairs_in_skipped() -> None:
    """未検査ペアは skipped に記録され、切断されない(未検査=境界なし
    ではなく未評価として明示)。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value=NoulResult(probs={}))  # type: ignore[method-assign]
    skipped: list[dict[str, Any]] = []
    text = "あ" * 20000 + "\n" + "い" * 20000
    blocks = await segment(eng, text, skipped=skipped)
    assert skipped == [
        {"stage": "segment", "reason": "window_state_too_large", "lines": [1, 2]}
    ]
    eng.noul_batch.assert_not_awaited()
    assert [(b.start, b.end) for b in blocks] == [(1, 2)]
