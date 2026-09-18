from collections.abc import Set as AbstractSet

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


def find_boundary_candidates(
    lines: list[str], excluded: AbstractSet[int] = frozenset()
) -> list[tuple[int, int]]:
    """各非空・非除外行 i と次の非空・非除外行 j のペア(0始まり)を返す。
    空行は飛ばしても連結を維持するが、除外行は連結を断つ。"""
    cands: list[tuple[int, int]] = []
    prev: int | None = None
    for i, ln in enumerate(lines):
        if i in excluded:
            prev = None
            continue
        if not ln.strip():
            continue
        if prev is not None:
            cands.append((prev, i))
        prev = i
    return cands


async def segment(
    engine: Engine, text: str, excluded: AbstractSet[int] = frozenset()
) -> list[Block]:
    lines = text.splitlines() or [""]
    prose = [i for i, ln in enumerate(lines) if ln.strip() and i not in excluded]
    if not prose:
        return []
    candidates = find_boundary_candidates(lines, excluded)
    if candidates:
        probs = await engine.noul_batch(
            {"lines": lines},
            {f"b{i}": _boundary_question(i, j) for i, j in candidates},
        )
    else:
        probs = {}
    cuts = {j for i, j in candidates if probs[f"b{i}"] >= BOUNDARY_THRESHOLD}
    # 切断点は j の直前。空行・除外行は境界自体に属し、ブロック行範囲は
    # 非空・非除外行の範囲を使う。除外領域は連結を断つので必ず切断する。
    spans: list[tuple[int, int]] = []
    start = prose[0]
    for prev, cur in zip(prose, prose[1:], strict=False):
        excluded_between = any(k in excluded for k in range(prev + 1, cur))
        if cur in cuts or excluded_between:
            spans.append((start, prev))
            start = cur
    spans.append((start, prose[-1]))
    return [
        Block(start=s + 1, end=e + 1, text="\n".join(lines[s : e + 1]))
        for s, e in spans
    ]
