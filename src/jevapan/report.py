import itertools
from typing import Any

from jevapan.models import LintResult


def render_json(result: LintResult) -> dict[str, Any]:
    return {
        "file": result.file,
        "blocks": [
            {
                "lines": list(b.lines),
                "scores": {
                    n: {"score": s.score, "confidence": s.confidence}
                    for n, s in sb.scores.items()
                },
            }
            for b, sb in zip(result.blocks, result.block_scores, strict=False)
        ],
        "doc_scores": {
            n: {"score": s.score, "confidence": s.confidence}
            for n, s in result.doc_scores.items()
        },
        "violations": [
            {
                "lines": list(v.lines),
                "col": v.col,
                "scope": v.scope,
                "category": v.category,
                "severity": v.severity,
                "probability": round(v.probability, 3),
                "text": v.text,
            }
            for v in result.violations
        ],
        "skipped": result.skipped,
        "summary": result.summary,
    }


def render_human(result: LintResult) -> str:
    out = [
        f"{result.file}: {result.summary['blocks']} blocks, "
        f"{result.summary['violations']} violations"
    ]
    for v in result.violations:
        out.append(
            f"  L{v.start}-{v.end} [{v.severity}] {v.category} "
            f"P={v.probability:.2f} {v.text[:70]}"
        )
    for s in result.skipped:
        name = s.get("category", s.get("stage", "?"))
        loc = f" L{s['lines'][0]}-{s['lines'][1]}" if "lines" in s else ""
        out.append(f"  skipped: {name} ({s['reason']}){loc}")
    return "\n".join(out)


def exit_code(result: LintResult) -> int:
    return 1 if any(v.severity == "error" for v in result.violations) else 0


def fail_under_exit_code(results: list[LintResult], fail_under: float) -> int:
    """--fail-under の終了コード。error severity の violation があれば
    score に関係なく exit 1(非指定時と同じ契約を優先)。"""
    if any(exit_code(r) for r in results):
        return 1
    worst = min(
        itertools.chain(
            (
                s.score
                for r in results
                for sb in r.block_scores
                for s in sb.scores.values()
            ),
            (s.score for r in results for s in r.doc_scores.values()),
        ),
        default=3.0,
    )
    return 1 if worst < fail_under else 0
