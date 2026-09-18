import asyncio
from dataclasses import dataclass, field

from jevapan.engine import Engine
from jevapan.models import Block
from jevapan.ruleset import Category, Scope


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


async def score_blocks(
    engine: Engine, blocks: list[Block], cats: list[Category]
) -> list[ScoredBlock]:
    targets = _block_cats(cats)
    questions = {c.name: (c.description, c.levels) for c in targets}

    async def one(b: Block) -> ScoredBlock:
        res = await engine.score_batch(
            state={"heading": "", "body": b.text}, questions=questions
        )
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
