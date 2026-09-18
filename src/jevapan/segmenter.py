import asyncio
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from jevapan.engine import Engine, NoulResult
from jevapan.models import Block


def _boundary_question(i: int, j: int) -> str:
    return (
        "The document is given as numbered lines in state.lines. "
        "Does a new block — a change of topic, role, or structure such as "
        "a new section, list, or paragraph — begin between "
        f"`lines[{i}]` and `lines[{j}]`?"
    )


BOUNDARY_THRESHOLD = 0.5
# 1ウィンドウが担当する境界ペア数(目安20〜50)
WINDOW_PAIRS = 30
# ウィンドウ前後の余白行数の上限
WINDOW_MARGIN_LINES = 20
# ウィンドウ state の文字数予算(概算)。余白はこの範囲でのみ広げる
WINDOW_STATE_CHARS = 16000


@dataclass
class Window:
    start: int  # 0始まりの開始行(含む)
    end: int  # 0始まりの終了行(含む)
    pairs: list[tuple[int, int]]  # 担当ペア(元の行 index)


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


def build_windows(
    lines: list[str],
    candidates: list[tuple[int, int]],
    max_pairs: int = WINDOW_PAIRS,
    margin: int = WINDOW_MARGIN_LINES,
    budget: int = WINDOW_STATE_CHARS,
) -> list[Window]:
    """境界ペアを連続したグループに一意に割当て、各グループの対象範囲+
    前後余白を共有ウィンドウにする。余白内のペアは重複判定しない。"""
    windows: list[Window] = []
    for g in range(0, len(candidates), max_pairs):
        pairs = candidates[g : g + max_pairs]
        lo, hi = pairs[0][0], pairs[-1][1]
        total = sum(len(lines[k]) + 1 for k in range(lo, hi + 1))
        start, end = lo, hi
        for _ in range(margin):
            if start == 0 or total + len(lines[start - 1]) + 1 > budget:
                break
            start -= 1
            total += len(lines[start]) + 1
        for _ in range(margin):
            if end >= len(lines) - 1 or total + len(lines[end + 1]) + 1 > budget:
                break
            end += 1
            total += len(lines[end]) + 1
        windows.append(Window(start=start, end=end, pairs=pairs))
    return windows


async def segment(
    engine: Engine, text: str, excluded: AbstractSet[int] = frozenset()
) -> list[Block]:
    lines = text.splitlines() or [""]
    prose = [i for i, ln in enumerate(lines) if ln.strip() and i not in excluded]
    if not prose:
        return []
    candidates = find_boundary_candidates(lines, excluded)
    windows = build_windows(lines, candidates)

    async def ask(w: Window) -> NoulResult:
        # 質問はウィンドウ内 index で参照。元行番号との対応はコードが管理
        return await engine.noul_batch(
            {"lines": lines[w.start : w.end + 1]},
            {f"b{i}": _boundary_question(i - w.start, j - w.start) for i, j in w.pairs},
        )

    results = await asyncio.gather(*(ask(w) for w in windows))
    probs = {k: v for r in results for k, v in r.probs.items()}
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
