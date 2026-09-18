from jevapan.models import Block, Violation
from jevapan.pipeline import LintResult
from jevapan.report import exit_code, render_json


def _result(violations: list[Violation]) -> LintResult:
    return LintResult(
        file="f.md",
        blocks=[Block(1, 3, "t")],
        block_scores=[],
        doc_scores={},
        violations=violations,
        skipped=[],
        summary={},
    )


def test_exit_code_error_violation() -> None:
    v = Violation(1, 1, "block", "substance", "error", 0.9, "x")
    assert exit_code(_result([v])) == 1


def test_exit_code_warning_only() -> None:
    v = Violation(1, 1, "block", "substance", "warning", 0.9, "x")
    assert exit_code(_result([v])) == 0


def test_render_json_schema() -> None:
    v = Violation(3, 3, "document", "consistency", "warning", 0.8, "t")
    out = render_json(_result([v]))
    assert out["file"] == "f.md"
    assert out["violations"][0]["lines"] == [3, 3]
    assert out["violations"][0]["scope"] == "document"
