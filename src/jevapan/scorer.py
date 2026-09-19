import asyncio
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

from jevapan.context import nearest_heading, preceding_context
from jevapan.engine import Engine, payload_chars
from jevapan.mask import PLACEHOLDER_NOTE, masked_slice
from jevapan.models import Block
from jevapan.ruleset import Category, Scope

# block 採点の実ペイロード(state+questions)の文字数予算(概算。根拠は
# engine.payload_chars 参照)。超過時はそのブロックを採点せず skipped に明示
SCORE_STATE_LIMIT = 32000

# locate と同じ評価対象条件を Score の採点基準にも適用し、採点と特定の
# 対象契約を一致させる(引用・悪文の説明例・ルール定義を著者の悪文と
# 同一視しない)。全カテゴリに一貫適用するためコード側で付与する
AUTHOR_SCOPE_CLAUSE = (
    " 評価対象は著者自身の記述のみとし、引用・悪文の説明例・ルール定義の"
    "中の文は採点対象としない。" + PLACEHOLDER_NOTE
)


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
    engine: Engine,
    blocks: list[Block],
    cats: list[Category],
    doc_lines: list[str] | None = None,
    excluded: AbstractSet[int] = frozenset(),
    skipped: list[dict[str, Any]] | None = None,
) -> list[ScoredBlock]:
    targets = _block_cats(cats)
    questions = {
        c.name: (c.description + AUTHOR_SCOPE_CLAUSE, c.levels) for c in targets
    }
    if not questions:
        return [ScoredBlock(block=b) for b in blocks]

    async def one(b: Block) -> ScoredBlock:
        heading = nearest_heading(doc_lines, b.start, excluded) if doc_lines else ""
        # 補助文脈(直前 prose 段落)は locator と共有し、本文(body)と分離
        context = preceding_context(doc_lines, b.start, excluded) if doc_lines else ""
        # 除外領域を内包するブロックでは body の除外行をマスクする。
        # Block.text 自体は行番号忠実性のため生テキストを維持する
        body = masked_slice(b.text.splitlines(), b.start, excluded)
        state = {"heading": heading, "context": context, "body": body}
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
        c.name: (c.description + AUTHOR_SCOPE_CLAUSE, c.levels_document or c.levels)
        for c in targets
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
