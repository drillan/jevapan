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
        out.append(f"  skipped: {s['category']} ({s['reason']})")
    return "\n".join(out)


def exit_code(result: LintResult) -> int:
    return 1 if any(v.severity == "error" for v in result.violations) else 0
