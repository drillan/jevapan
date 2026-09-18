import asyncio
from collections.abc import Coroutine
from typing import Any

from jevapan.engine import Engine
from jevapan.locator import locate_in_block, locate_in_document
from jevapan.models import LintResult, Violation
from jevapan.ruleset import Ruleset, Scope
from jevapan.scorer import score_blocks, score_document
from jevapan.segmenter import segment

__all__ = ["LintResult", "STATE_DOC_LIMIT", "lint_text"]

STATE_DOC_LIMIT = 32000  # document scope に渡す文字数の上限(概算)


def _merge_duplicates(violations: list[Violation]) -> list[Violation]:
    """同一 (start, end, category) が block/document 両スコープで出た場合1件に
    集約する。scope='both'、probability は大きい方を採用。"""
    merged: dict[tuple[int, int, str], Violation] = {}
    for v in violations:
        key = (v.start, v.end, v.category)
        prev = merged.get(key)
        if prev is None:
            merged[key] = v
        else:
            merged[key] = Violation(
                start=v.start,
                end=v.end,
                scope="both",
                category=v.category,
                severity=v.severity,
                probability=max(prev.probability, v.probability),
                text=prev.text,
            )
    return list(merged.values())


async def lint_text(
    engine: Engine, text: str, ruleset: Ruleset, file_label: str
) -> LintResult:
    lines = text.splitlines() or [""]
    blocks = await segment(engine, text)
    cats = [c for c in ruleset.categories if c.enabled]

    block_scores = await score_blocks(engine, blocks, cats)
    skipped: list[dict[str, Any]] = []
    doc_cats = [c for c in cats if c.scope in (Scope.document, Scope.both)]
    if doc_cats and len(text) > STATE_DOC_LIMIT:
        skipped += [{"category": c.name, "reason": "state_too_large"} for c in doc_cats]
        doc_scores = {}
    else:
        doc_scores = await score_document(engine, text, cats)

    # flag → locate
    tasks: list[Coroutine[Any, Any, list[Violation]]] = []
    for sb in block_scores:
        for name, cs in sb.scores.items():
            cat = next(c for c in cats if c.name == name)
            if cs.score < cat.threshold and cat.locate:
                tasks.append(locate_in_block(engine, sb.block, cat, text[:2000]))
    for name, cs in doc_scores.items():
        cat = next(c for c in cats if c.name == name)
        if cs.score < cat.threshold and cat.locate:
            tasks.append(locate_in_document(engine, text, cat, lines))
    nested = await asyncio.gather(*tasks)
    violations = _merge_duplicates([v for lst in nested for v in lst])

    return LintResult(
        file=file_label,
        blocks=blocks,
        block_scores=block_scores,
        doc_scores=doc_scores,
        violations=violations,
        skipped=skipped,
        summary={
            "blocks": len(blocks),
            "violations": len(violations),
            "errors": sum(1 for v in violations if v.severity == "error"),
        },
    )
