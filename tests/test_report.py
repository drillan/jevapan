from jevapan.models import Block, Violation
from jevapan.pipeline import LintResult
from jevapan.report import exit_code, fail_under_exit_code, render_human, render_json
from jevapan.scorer import CategoryScore, ScoredBlock


def _result(violations: list[Violation]) -> LintResult:
    return LintResult(
        file="f.md",
        blocks=[Block(1, 3, "t")],
        block_scores=[],
        doc_scores={},
        violations=violations,
        skipped=[],
        summary={"blocks": 1, "violations": len(violations), "errors": 0},
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


def _scored(score: float) -> LintResult:
    r = _result([])
    r.block_scores = [
        ScoredBlock(
            block=Block(1, 1, "t"),
            scores={"c": CategoryScore("c", score, 0.9, "block")},
        )
    ]
    return r


def test_fail_under_fails_on_low_block_score() -> None:
    assert fail_under_exit_code([_scored(1.0)], 1.5) == 1


def test_fail_under_passes_on_high_score() -> None:
    assert fail_under_exit_code([_scored(1.9)], 1.5) == 0


def test_fail_under_includes_document_scores() -> None:
    r = _result([])
    r.doc_scores = {"consistency": CategoryScore("consistency", 0.5, 0.9, "document")}
    assert fail_under_exit_code([r], 1.5) == 1


def test_fail_under_error_violation_takes_priority() -> None:
    # error severity の violation があれば score 条件を満たしても exit 1
    r = _scored(2.0)
    r.violations = [Violation(1, 1, "block", "c", "error", 0.9, "x")]
    assert fail_under_exit_code([r], 1.5) == 1


def test_score_flagged_violation_has_no_probability() -> None:
    """locate 未指定 flag は Noul 確率を持たず、score/confidence を別
    フィールドに持つ(P= 表示は Noul 由来のみ)。"""
    v = Violation(
        1, 2, "block", "c", "warning", None, "body", score=0.5, confidence=0.9
    )
    out = render_json(_result([v]))["violations"][0]
    assert out["probability"] is None
    assert out["score"] == 0.5 and out["confidence"] == 0.9
    assert "P=" not in render_human(_result([v]))
