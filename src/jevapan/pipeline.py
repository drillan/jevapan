import asyncio
from collections.abc import Coroutine
from typing import Any

from jevapan.engine import Engine
from jevapan.locator import (
    StateTooLargeError,
    locate_in_block,
    locate_in_document,
)
from jevapan.mask import analyze_syntax, masked_text
from jevapan.models import LintResult, Violation
from jevapan.ruleset import Ruleset, Scope
from jevapan.scorer import score_blocks, score_document
from jevapan.segmenter import segment

__all__ = ["LintResult", "STATE_DOC_LIMIT", "lint_text"]

STATE_DOC_LIMIT = 32000  # document scope に渡す文字数の上限(概算)


def _merge_duplicates(violations: list[Violation]) -> list[Violation]:
    """同一 (行範囲, オフセット, text, category) の Violation を1件に集約する。
    scope は実際の出現 scope 集合から求め、{block,document} なら 'both'、
    単一ならそのまま使う。probability は大きい方を採用。"""
    groups: dict[tuple[int, int, int, str, str], list[Violation]] = {}
    for v in violations:
        groups.setdefault((v.start, v.end, v.col, v.text, v.category), []).append(v)
    out = []
    for (start, end, col, text, cat), vs in groups.items():
        scopes = {v.scope for v in vs}
        probs = [v.probability for v in vs if v.probability is not None]
        out.append(
            Violation(
                start=start,
                end=end,
                scope="both" if len(scopes) > 1 else vs[0].scope,
                category=cat,
                severity=vs[0].severity,
                probability=max(probs) if probs else None,
                text=text,
                col=col,
                score=next((v.score for v in vs if v.score is not None), None),
                confidence=next(
                    (v.confidence for v in vs if v.confidence is not None), None
                ),
            )
        )
    return out


async def lint_text(
    engine: Engine, text: str, ruleset: Ruleset, file_label: str
) -> LintResult:
    lines = text.splitlines() or [""]
    cats = [c for c in ruleset.categories if c.enabled]
    # 構文領域は文書全体で一度だけ解析し、行番号を保つ source map として共有
    excluded = analyze_syntax(lines)
    masked = masked_text(lines, excluded)

    # サイズ上限チェックは全 API 呼出しの前に行う
    skipped: list[dict[str, Any]] = []
    doc_cats = [c for c in cats if c.scope in (Scope.document, Scope.both)]
    doc_oversize = bool(doc_cats) and len(masked) > STATE_DOC_LIMIT
    if doc_oversize:
        skipped += [{"category": c.name, "reason": "state_too_large"} for c in doc_cats]

    # 評価対象 prose が0(空文書・コードのみ等)なら採点・locate を行わない。
    # [excluded] は除外領域の目印であり文章品質の評価対象ではない
    prose_empty = not any(
        ln.strip() and i not in excluded for i, ln in enumerate(lines)
    )
    if prose_empty:
        skipped += [
            {"category": c.name, "reason": "no_evaluable_prose"} for c in doc_cats
        ]
        return LintResult(
            file=file_label,
            blocks=[],
            block_scores=[],
            doc_scores={},
            violations=[],
            skipped=skipped,
            summary={"blocks": 0, "violations": 0, "errors": 0},
        )

    blocks = await segment(engine, text, excluded, skipped=skipped)
    block_scores = await score_blocks(
        engine, blocks, cats, doc_lines=lines, excluded=excluded, skipped=skipped
    )
    doc_scores = {} if doc_oversize else await score_document(engine, masked, cats)

    # flag → locate(locate 未指定ならブロック/文書単位の violation を生成)
    tasks: list[Coroutine[Any, Any, list[Violation]]] = []
    task_cats: list[str] = []
    violations: list[Violation] = []
    for sb in block_scores:
        for name, cs in sb.scores.items():
            cat = next(c for c in cats if c.name == name)
            if cs.score >= cat.threshold:
                continue
            if cat.locate:
                tasks.append(locate_in_block(engine, sb.block, cat, lines, excluded))
                task_cats.append(name)
            else:
                # Score 由来の flag: probability は持たず score/confidence を
                # 別フィールドに保持(confidence は違反確率ではない)
                violations.append(
                    Violation(
                        start=sb.block.start,
                        end=sb.block.end,
                        scope="block",
                        category=name,
                        severity=cat.severity.value,
                        probability=None,
                        text=sb.block.text,
                        score=cs.score,
                        confidence=cs.confidence,
                    )
                )
    for name, cs in doc_scores.items():
        cat = next(c for c in cats if c.name == name)
        if cs.score >= cat.threshold:
            continue
        if cat.locate:
            tasks.append(locate_in_document(engine, text, cat, lines, excluded))
            task_cats.append(name)
        else:
            violations.append(
                Violation(
                    start=1,
                    end=len(lines),
                    scope="document",
                    category=name,
                    severity=cat.severity.value,
                    probability=None,
                    text="",
                    score=cs.score,
                    confidence=cs.confidence,
                )
            )
    nested = await asyncio.gather(*tasks, return_exceptions=True)
    for name, res in zip(task_cats, nested, strict=True):
        if isinstance(res, StateTooLargeError):
            skipped.append({"category": name, "reason": "locate_state_too_large"})
        elif isinstance(res, BaseException):
            raise res
        else:
            violations.extend(res)
    violations = _merge_duplicates(violations)

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
