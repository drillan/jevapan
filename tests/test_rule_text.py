"""ルール文(advisor E)の契約テスト。

locate 質問は「語句を含む」ではなく意味用法を問い、
引用・悪文の説明例・ルール定義の文を著者の違反としない旨を含む。
"""

import pytest

from jevapan.ruleset import load_ruleset, resolve_preset

EXEMPTION = "引用"
AUTHORSHIP = "著者自身の記述"

ALL_PRESETS = ["base", "tech-doc", "blog"]


@pytest.mark.parametrize("name", ALL_PRESETS)
def test_every_locate_has_author_exemption(name: str) -> None:
    rs = load_ruleset(resolve_preset(name))
    for c in rs.categories:
        if c.locate:
            assert EXEMPTION in c.locate, f"{name}.{c.name}"


@pytest.mark.parametrize("name", ALL_PRESETS)
def test_every_locate_requires_own_writing_condition(name: str) -> None:
    """locate は「引用・ルール定義・悪文の説明例ではなく著者自身の記述として
    違反している」を肯定条件として含む(引用等を著者の悪文と同一視しない)。"""
    rs = load_ruleset(resolve_preset(name))
    for c in rs.categories:
        if c.locate:
            assert AUTHORSHIP in c.locate, f"{name}.{c.name}"
            assert "ルール定義" in c.locate, f"{name}.{c.name}"
            assert "説明例" in c.locate, f"{name}.{c.name}"


def test_structure_locate_is_role_conditioned() -> None:
    rs = load_ruleset(resolve_preset("base"))
    s = next(c for c in rs.categories if c.name == "structure")
    assert s.locate
    # 対象は段落先頭文・段落間接続文・見出しに限定(補足文は対象外)
    assert "先頭文" in s.locate and "見出し" in s.locate
    assert "対象外" in s.locate or "違反としない" in s.locate


def test_substance_locate_asks_usage_not_presence() -> None:
    rs = load_ruleset(resolve_preset("base"))
    s = next(c for c in rs.categories if c.name == "substance")
    assert s.locate
    # 「型を含む」ではなく「実質を持たない使われ方」を問う
    assert "含む" not in s.locate
    assert "実質" in s.locate


def test_naturalness_voice_locate_ask_semantics() -> None:
    rs = load_ruleset(resolve_preset("base"))
    for name in ("naturalness", "voice"):
        c = next(c for c in rs.categories if c.name == name)
        assert c.locate
        # 語の出現ではなく不適切な意味用法を問う
        assert "意味" in c.locate or "用法" in c.locate or "違反としない" in c.locate
