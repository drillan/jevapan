from jevapan.engine import Engine
from jevapan.models import Block


def _boundary_question(i: int) -> str:
    return (
        "The document is given as numbered lines in state.lines. "
        "Does a new block — a change of topic, role, or structure such as "
        "a new section, list, or paragraph — begin between "
        f"`lines[{i}]` and `lines[{i + 1}]`?"
    )


BOUNDARY_THRESHOLD = 0.5


def find_boundary_candidates(lines: list[str]) -> list[int]:
    """隣接する非空行ペアの先頭行 index(0始まり)を返す。"""
    return [
        i for i in range(len(lines) - 1) if lines[i].strip() and lines[i + 1].strip()
    ]


async def segment(engine: Engine, text: str) -> list[Block]:
    lines = text.splitlines() or [""]
    candidates = find_boundary_candidates(lines)
    if candidates:
        probs = await engine.noul_batch(
            {"lines": lines},
            {f"b{i}": _boundary_question(i) for i in candidates},
        )
    else:
        probs = {}
    cuts = {i for i in candidates if probs[f"b{i}"] >= BOUNDARY_THRESHOLD}
    blocks, start = [], 0
    for i in sorted(cuts):
        blocks.append((start, i))
        start = i + 1
    blocks.append((start, len(lines) - 1))
    return [
        Block(start=s + 1, end=e + 1, text="\n".join(lines[s : e + 1]))
        for s, e in blocks
    ]
