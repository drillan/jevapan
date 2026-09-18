import pytest

from jevapan.ruleset import parse_ruleset


def test_parse_minimal_category() -> None:
    rs = parse_ruleset(
        {"categories": [{"name": "c1", "description": "d", "levels": ["bad", "good"]}]},
        "test",
    )
    assert rs.categories[0].severity.value == "warning"
    assert rs.categories[0].scope.value == "block"
    assert rs.categories[0].threshold == 1.5


def test_levels_length_validated() -> None:
    with pytest.raises(ValueError, match="c1.*levels"):
        parse_ruleset(
            {"categories": [{"name": "c1", "description": "d", "levels": ["only"]}]},
            "test",
        )


def test_missing_description_fails() -> None:
    with pytest.raises(ValueError, match="description"):
        parse_ruleset({"categories": [{"name": "c1", "levels": ["a", "b"]}]}, "test")


def test_scope_both_and_document_accepted() -> None:
    rs = parse_ruleset(
        {
            "categories": [
                {
                    "name": "a",
                    "scope": "both",
                    "description": "d",
                    "levels": ["x", "y"],
                },
                {
                    "name": "b",
                    "scope": "document",
                    "description": "d",
                    "levels": ["x", "y"],
                },
            ]
        },
        "test",
    )
    assert rs.categories[0].scope.value == "both"
    assert rs.categories[1].scope.value == "document"


def test_levels_document_length_validated() -> None:
    with pytest.raises(ValueError, match="levels_document"):
        parse_ruleset(
            {
                "categories": [
                    {
                        "name": "c1",
                        "scope": "document",
                        "description": "d",
                        "levels": ["a", "b"],
                        "levels_document": ["only"],
                    }
                ]
            },
            "test",
        )


def test_levels_document_empty_rejected() -> None:
    with pytest.raises(ValueError, match="levels_document"):
        parse_ruleset(
            {
                "categories": [
                    {
                        "name": "c1",
                        "description": "d",
                        "levels": ["a", "b"],
                        "levels_document": [],
                    }
                ]
            },
            "test",
        )
