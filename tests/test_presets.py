import pytest

from jevapan.ruleset import load_ruleset, resolve_preset


@pytest.mark.parametrize("name", ["base", "tech-doc", "blog"])
def test_preset_loads(name: str) -> None:
    rs = load_ruleset(resolve_preset(name))
    assert len(rs.categories) >= 5
    for c in rs.categories:
        assert c.description and len(c.levels) >= 2


def test_base_has_consistency_document_scope() -> None:
    rs = load_ruleset(resolve_preset("base"))
    cons = next(c for c in rs.categories if c.name == "consistency")
    assert cons.scope.value == "document"


def test_tech_doc_extends_base() -> None:
    rs = load_ruleset(resolve_preset("tech-doc"))
    names = {c.name for c in rs.categories}
    assert {"structure", "rigor", "reader_load", "restraint"} <= names
