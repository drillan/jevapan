from unittest.mock import AsyncMock

from jevapan.engine import Engine, NoulResult, ScoreResult
from jevapan.mask import analyze_syntax, masked_slice, masked_text
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


def test_fence_close_requires_only_trailing_whitespace() -> None:
    """閉じ fence の後に非空白があれば閉じとみなさない(CommonMark)。
    偽終端でコードを prose に戻し、本文を誤除外してはいけない。"""
    lines = ["```", "```not_a_closer", "code。", "```", "actual prose。"]
    assert analyze_syntax(lines) == {0, 1, 2, 3}


def test_fence_close_allows_trailing_whitespace() -> None:
    lines = ["```", "code", "```   ", "本文。"]
    assert analyze_syntax(lines) == {0, 1, 2}


def test_fence_open_backtick_info_must_not_contain_backtick() -> None:
    """backtick fence の info string に backtick を含む行は開きでない
    (CommonMark)。tilde fence の info は backtick を含んでよい。"""
    lines = ["``` ``", "本文。", "~~~ ` info", "code", "~~~", "本文2。"]
    assert analyze_syntax(lines) == {2, 3, 4}


def test_fence_indented_up_to_3_spaces() -> None:
    lines = ["   ```", "code", "   ```", "本文。"]
    assert analyze_syntax(lines) == {0, 1, 2}


def test_fence_tab_indented_is_not_fence() -> None:
    """行頭タブは CommonMark では4桁インデント相当→ indented code であり
    fence にならない。"""
    lines = ["\t```", "code。", "\t```"]
    assert analyze_syntax(lines) == set()


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


def test_analyze_syntax_records_kinds() -> None:
    """analyze_syntax は除外行→種別("code block"/"table"/"front matter")
    のマッピングを kinds 属性で保持する。集合としての振る舞いは不変。"""
    lines = [
        "---",
        "title: x",
        "---",
        "本文。",
        "| a | b |",
        "```",
        "code",
        "```",
        "次の文。",
    ]
    excluded = analyze_syntax(lines)
    assert excluded == {0, 1, 2, 4, 5, 6, 7}
    assert excluded.kinds == {
        0: "front matter",
        1: "front matter",
        2: "front matter",
        4: "table",
        5: "code block",
        6: "code block",
        7: "code block",
    }


def test_masked_text_typed_placeholder() -> None:
    """種別情報つきの除外集合では、プレースホルダが自己説明型の
    `[excluded: <種別>]` になる(質問文への別途説明を不要にする)。"""
    lines = ["本文。", "```", "code", "```", "| a |", "次の文。"]
    out = masked_text(lines, analyze_syntax(lines))
    assert "code" not in out.splitlines() and "| a |" not in out.splitlines()
    assert "[excluded: code block]" in out
    assert "[excluded: table]" in out
    assert "本文。" in out and "次の文。" in out


def test_masked_text_splits_runs_across_kinds() -> None:
    """連続する除外領域が種別をまたぐ場合は、種別ごとに別々の
    プレースホルダを出す。"""
    lines = ["本文。", "| a |", "```", "code", "```"]
    out = masked_text(lines, analyze_syntax(lines))
    assert "[excluded: table]" in out and "[excluded: code block]" in out
    assert out.count("[excluded") == 2


def test_masked_text_front_matter_placeholder() -> None:
    lines = ["---", "title: x", "---", "本文。"]
    out = masked_text(lines, analyze_syntax(lines))
    assert out.startswith("[excluded: front matter]\n")


def test_masked_slice_propagates_kinds() -> None:
    """masked_slice も種別情報を維持し、`[excluded: <種別>]` を生成する。"""
    doc_lines = ["- 項目A", "  ```py", "  code()", "  ```", "- 項目B"]
    out = masked_slice(doc_lines, 1, analyze_syntax(doc_lines))
    assert "[excluded: code block]" in out
    assert "code()" not in out


async def test_segment_forces_cut_around_excluded_code() -> None:
    """除外領域は連結を断つ。prob が全て切断なしでもブロックはまたがない。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value=NoulResult(probs={}))  # type: ignore[method-assign]
    text = "段落1。\n\n```\ncode();\n```\n\n段落2。"
    lines = text.splitlines()
    blocks = await segment(eng, text, analyze_syntax(lines))
    assert [(b.start, b.end) for b in blocks] == [(1, 1), (7, 7)]
    assert all("code" not in b.text for b in blocks)


async def test_pipeline_document_score_uses_masked_text() -> None:
    """document scope の採点 state には除外領域の内容が入らない。"""
    eng = Engine(client=AsyncMock(), sem=None)
    eng.noul_batch = AsyncMock(return_value=NoulResult(probs={}))  # type: ignore[method-assign]
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
        return_value=NoulResult(probs={"s0": 0.9})
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
