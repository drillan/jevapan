from typing import Any

import pytest

from jevapan.ruleset import load_ruleset, merge_rulesets, parse_ruleset


def _rs(cats: list[dict[str, Any]], extends: str | None = None) -> Any:
    return parse_ruleset({"extends": extends, "categories": cats}, "t")


def test_merge_adds_and_overrides() -> None:
    parent = _rs(
        [
            {
                "name": "a",
                "description": "d",
                "levels": ["x", "y"],
                "severity": "warning",
            },
            {"name": "b", "description": "d", "levels": ["x", "y"]},
        ]
    )
    child = _rs(
        [
            {"name": "a", "severity": "error"},  # 部分上書き
            {"name": "c", "description": "d", "levels": ["x", "y"]},  # 追加
        ]
    )
    merged = merge_rulesets(parent, child)
    by_name = {c.name: c for c in merged.categories}
    assert by_name["a"].severity.value == "error"
    assert by_name["a"].description == "d"  # 未指定フィールドは親を継承
    assert "c" in by_name and "b" in by_name


def test_merge_disabled_category() -> None:
    parent = _rs([{"name": "a", "description": "d", "levels": ["x", "y"]}])
    child = _rs([{"name": "a", "enabled": False}])
    assert merge_rulesets(parent, child).categories[0].enabled is False


def test_empty_levels_patch_rejected() -> None:
    """既存カテゴリへの levels:[] patch は完全定義の不変条件を崩すので拒否。"""
    parent = _rs([{"name": "a", "description": "d", "levels": ["x", "y"]}])
    child = _rs([{"name": "a", "levels": []}])
    with pytest.raises(ValueError, match="levels"):
        merge_rulesets(parent, child)


def test_extends_cycle_fails(tmp_path: Any) -> None:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("extends: b\ncategories: []")
    b.write_text("extends: a\ncategories: []")
    with pytest.raises(ValueError, match="cycle"):
        load_ruleset(a)
