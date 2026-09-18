from unittest.mock import AsyncMock

from jevapan.engine import Engine, ScoreResult
from jevapan.mask import analyze_syntax, masked_text
from jevapan.pipeline import lint_text
from jevapan.ruleset import parse_ruleset
from jevapan.segmenter import segment


def test_analyze_fenced_code_backtick() -> None:
    lines = ["本文。", "```python", "x = 1  # comment", "```", "次の文。"]
    assert analyze_syntax(lines) == {1, 2, 3}


def test_analyze_fenced_code_tilde_and_unclosed() -> None:
    lines = ["本文。", "~~~", "code", "~~~", "本文2。", "````", "unclosed"]
    # 言語指定なしの tilde fence と閉じない fence(末尾まで除外)
    assert analyze_syntax(lines) == {1, 2, 3, 5, 6}


def test_analyze_front_matter() -> None:
    lines = ["---", "title: x", "tags: [a]", "---", "", "本文。"]
    assert analyze_syntax(lines) == {0, 1, 2, 3}


def test_analyze_thematic_break_not_front_matter() -> None:
    # 先頭以外の --- は front matter ではない
    lines = ["本文。", "---", "次の文。"]
    assert analyze_syntax(lines) == set()


def test_analyze_front_matter_unclosed_is_not_masked() -> None:
    # 閉じ --- がなければ front matter とみなさない(曖昧なら保留)
    lines = ["---", "title: x", "本文。"]
    assert analyze_syntax(lines) == set()


def test_analyze_table_lines() -> None:
    lines = ["本文。", "| a | b |", "|---|---|", "| c | d |", "次の文。"]
    assert analyze_syntax(lines) == {1, 2, 3}


def test_analyze_no_exclusions() -> None:
    assert analyze_syntax(["a。", "# h", "- item", "b。"]) == set()


def test_masked_text_replaces_runs_with_placeholder() -> None:
    lines = ["本文。", "```", "code", "```", "次の文。"]
    out = masked_text(lines, {1, 2, 3})
    assert "code" not in out
    assert out.count("[excluded]") == 1
    assert "本文。" in out and "次の文。" in out


async def test_segment_forces_cut_around_excluded_code() -> None:
    """除外領域は連結を断つ。prob が全て切断なしでもブロックはまたがない。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value={})  # type: ignore[method-assign]
    text = "段落1。\n\n```\ncode();\n```\n\n段落2。"
    lines = text.splitlines()
    blocks = await segment(eng, text, analyze_syntax(lines))
    assert [(b.start, b.end) for b in blocks] == [(1, 1), (7, 7)]
    assert all("code" not in b.text for b in blocks)


async def test_pipeline_document_score_uses_masked_text() -> None:
    """document scope の採点 state には除外領域の内容が入らない。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value={})  # type: ignore[method-assign]
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
    text = "本文。\n\n```\nSECRET_CODE_TOKEN\n```\n"
    res = await lint_text(eng, text, rs, "f.md")
    assert res.doc_scores
    for c in eng.score_batch.call_args_list:
        state = c.kwargs["state"]
        if isinstance(state, str):
            assert "SECRET_CODE_TOKEN" not in state


async def test_locate_skips_excluded_candidates() -> None:
    """除外行の文は locate 候補にならない。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"s0": 0.9}
    )
    eng.score_batch = AsyncMock(  # type: ignore[method-assign]
        return_value={"c": ScoreResult(0.0, 0.0)}
    )
    rs = parse_ruleset(
        {
            "categories": [
                {
                    "name": "c",
                    "scope": "document",
                    "description": "d",
                    "levels": ["a", "b"],
                    "locate": "x?",
                }
            ]
        },
        "t",
    )
    text = "本文。\n\n```\nコード内の文。\n```\n"
    res = await lint_text(eng, text, rs, "f.md")
    assert all("コード内の文" not in v.text for v in res.violations)
