import re

from jevapan.engine import Engine
from jevapan.models import Block, Violation
from jevapan.ruleset import Category

LOCATE_THRESHOLD = 0.5


def split_candidates(
    block: Block, text_lines: list[str]
) -> list[tuple[tuple[int, int], int, str]]:
    """ブロック内の候補文。(行範囲, 行内オフセット, 文)のリスト。
    箇条書き等は行単位で候補にする。"""
    cands: list[tuple[tuple[int, int], int, str]] = []
    for off, line in enumerate(text_lines):
        lineno = block.start + off
        if line.lstrip().startswith(("- ", "* ", "+ ")):
            t = line.strip()
            cands.append(((lineno, lineno), line.index(t), t))
            continue
        pos = 0
        for p in re.split(r"(?<=。)\s*", line.strip()):
            if not p:
                continue
            col = line.index(p, pos)
            cands.append(((lineno, lineno), col, p))
            pos = col + len(p)
    return cands


async def locate_in_block(
    engine: Engine, block: Block, category: Category, context: str
) -> list[Violation]:
    if not category.locate:
        return []
    cands = split_candidates(block, block.text.splitlines())
    if not cands:
        return []
    probs = await engine.noul_batch(
        {"context": context[:2000], "candidates": [t for _, _, t in cands]},
        {
            f"s{i}": f"{category.locate} Candidate: `candidates[{i}]`"
            for i in range(len(cands))
        },
    )
    return [
        Violation(
            start=lr[0],
            end=lr[1],
            scope="block",
            category=category.name,
            severity=category.severity.value,
            probability=probs[f"s{i}"],
            text=t,
            col=col,
        )
        for i, (lr, col, t) in enumerate(cands)
        if probs[f"s{i}"] >= LOCATE_THRESHOLD
    ]


async def locate_in_document(
    engine: Engine, text: str, category: Category, all_lines: list[str]
) -> list[Violation]:
    if not category.locate:
        return []
    pseudo = Block(1, len(all_lines), text)
    cands = split_candidates(pseudo, all_lines)
    if not cands:
        return []
    probs = await engine.noul_batch(
        {"document": text[:16000], "candidates": [t for _, _, t in cands]},
        {
            f"s{i}": f"{category.locate} Candidate: `candidates[{i}]`"
            for i in range(len(cands))
        },
    )
    return [
        Violation(
            start=lr[0],
            end=lr[1],
            scope="document",
            category=category.name,
            severity=category.severity.value,
            probability=probs[f"s{i}"],
            text=t,
            col=col,
        )
        for i, (lr, col, t) in enumerate(cands)
        if probs[f"s{i}"] >= LOCATE_THRESHOLD
    ]
