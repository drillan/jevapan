import asyncio
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

from jevapan.engine import Engine, payload_chars
from jevapan.models import Block
from jevapan.ruleset import Category, Scope

# block 採点の実ペイロード(state+questions)の文字数予算(概算。根拠は
# engine.payload_chars 参照)。超過時はそのブロックを採点せず skipped に明示
SCORE_STATE_LIMIT = 32000


@dataclass
class CategoryScore:
    category: str
    score: float
    confidence: float
    scope: str


@dataclass
class ScoredBlock:
    block: Block
    scores: dict[str, CategoryScore] = field(default_factory=dict)


def _block_cats(cats: list[Category]) -> list[Category]:
    return [c for c in cats if c.enabled and c.scope in (Scope.block, Scope.both)]


def _doc_cats(cats: list[Category]) -> list[Category]:
    return [c for c in cats if c.enabled and c.scope in (Scope.document, Scope.both)]


def _nearest_heading(
    lines: list[str], before: int, excluded: AbstractSet[int] = frozenset()
) -> str:
    """before(1始まり行番号)より前の直近の見出し行(# 始まり)を返す。
    除外行(コード内コメント等)は見出しにしない。"""
    for idx in range(min(before - 2, len(lines) - 1), -1, -1):
        if idx in excluded:
            continue
        if lines[idx].lstrip().startswith("#"):
            return lines[idx].strip().lstrip("#").strip()
    return ""


async def score_blocks(
    engine: Engine,
    blocks: list[Block],
    cats: list[Category],
    doc_lines: list[str] | None = None,
    excluded: AbstractSet[int] = frozenset(),
    skipped: list[dict[str, Any]] | None = None,
) -> list[ScoredBlock]:
    targets = _block_cats(cats)
    questions = {c.name: (c.description, c.levels) for c in targets}
    if not questions:
        return [ScoredBlock(block=b) for b in blocks]

    async def one(b: Block) -> ScoredBlock:
        heading = _nearest_heading(doc_lines, b.start, excluded) if doc_lines else ""
        state = {"heading": heading, "body": b.text}
        if payload_chars(state, questions) > SCORE_STATE_LIMIT:
            if skipped is not None:
                skipped.extend(
                    {
                        "category": c.name,
                        "reason": "score_state_too_large",
                        "lines": [b.start, b.end],
                    }
                    for c in targets
                )
            return ScoredBlock(block=b)
        res = await engine.score_batch(state=state, questions=questions)
        return ScoredBlock(
            block=b,
            scores={
                c.name: CategoryScore(
                    c.name, res[c.name].score, res[c.name].confidence, "block"
                )
                for c in targets
            },
        )

    return list(await asyncio.gather(*[one(b) for b in blocks]))


async def score_document(
    engine: Engine, text: str, cats: list[Category]
) -> dict[str, CategoryScore]:
    targets = _doc_cats(cats)
    questions = {
        c.name: (c.description, c.levels_document or c.levels) for c in targets
    }
    if not questions:
        return {}
    res = await engine.score_batch(state=text, questions=questions)
    return {
        c.name: CategoryScore(
            c.name, res[c.name].score, res[c.name].confidence, "document"
        )
        for c in targets
    }
