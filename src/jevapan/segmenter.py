from jevapan.engine import Engine
from jevapan.models import Block


def _boundary_question(i: int, j: int) -> str:
    return (
        "The document is given as numbered lines in state.lines. "
        "Does a new block — a change of topic, role, or structure such as "
        "a new section, list, or paragraph — begin between "
        f"`lines[{i}]` and `lines[{j}]`?"
    )


BOUNDARY_THRESHOLD = 0.5


def find_boundary_candidates(lines: list[str]) -> list[tuple[int, int]]:
    """各非空行 i と次の非空行 j のペア(0始まり)を返す。空行は飛ばす。"""
    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    return list(zip(nonempty, nonempty[1:], strict=False))


async def segment(engine: Engine, text: str) -> list[Block]:
    lines = text.splitlines() or [""]
    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    if not nonempty:
        return [Block(start=1, end=len(lines), text="\n".join(lines))]
    candidates = find_boundary_candidates(lines)
    if candidates:
        probs = await engine.noul_batch(
            {"lines": lines},
            {f"b{i}": _boundary_question(i, j) for i, j in candidates},
        )
    else:
        probs = {}
    cuts = {j for i, j in candidates if probs[f"b{i}"] >= BOUNDARY_THRESHOLD}
    # 切断点は j の直前。空行は境界自体に属し、ブロック行範囲は非空行の範囲。
    spans: list[tuple[int, int]] = []
    start = nonempty[0]
    for i, j in zip(nonempty, nonempty[1:], strict=False):
        if j in cuts:
            spans.append((start, i))
            start = j
    spans.append((start, nonempty[-1]))
    return [
        Block(start=s + 1, end=e + 1, text="\n".join(lines[s : e + 1]))
        for s, e in spans
    ]
