import re
from collections.abc import Set as AbstractSet

from jevapan.engine import Engine
from jevapan.models import Block, Violation
from jevapan.ruleset import Category

LOCATE_THRESHOLD = 0.5
# document locate の state(document スライス + 候補列)の文字数予算(概算)
LOCATE_STATE_LIMIT = 32000


class StateTooLargeError(Exception):
    """locate 用 state が上限を超えた。候補列の黙った切り詰めはしない。"""


def split_candidates(
    block: Block,
    text_lines: list[str],
    excluded: AbstractSet[int] = frozenset(),
) -> list[tuple[tuple[int, int], int, str]]:
    """ブロック内の候補文。(行範囲, 行内オフセット, 文)のリスト。
    箇条書き等は行単位で候補にする。除外行(0始まり行 index)は候補にしない。"""
    cands: list[tuple[tuple[int, int], int, str]] = []
    for off, line in enumerate(text_lines):
        lineno = block.start + off
        if lineno - 1 in excluded:
            continue
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
    engine: Engine,
    block: Block,
    category: Category,
    doc_lines: list[str],
    excluded: AbstractSet[int] = frozenset(),
) -> list[Violation]:
    if not category.locate:
        return []
    cands = split_candidates(block, block.text.splitlines(), excluded)
    if not cands:
        return []
    # 対象ブロック直前の文脈(末尾側 ~2000字)を context に渡す
    context = "\n".join(doc_lines[: block.start - 1])[-2000:]
    probs = await engine.noul_batch(
        state={
            "context": context,
            "block": block.text,
            "candidates": [t for _, _, t in cands],
        },
        questions={
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
    engine: Engine,
    text: str,
    category: Category,
    all_lines: list[str],
    excluded: AbstractSet[int] = frozenset(),
) -> list[Violation]:
    if not category.locate:
        return []
    pseudo = Block(1, len(all_lines), text)
    cands = split_candidates(pseudo, all_lines, excluded)
    if not cands:
        return []
    document = text[:16000]
    cand_texts = [t for _, _, t in cands]
    if len(document) + sum(len(t) for t in cand_texts) > LOCATE_STATE_LIMIT:
        raise StateTooLargeError(
            f"locate state exceeds {LOCATE_STATE_LIMIT} chars for {category.name}"
        )
    probs = await engine.noul_batch(
        {"document": document, "candidates": cand_texts},
        questions={
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
