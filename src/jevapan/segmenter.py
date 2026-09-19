import asyncio
import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from jevapan.engine import Engine, NoulResult
from jevapan.models import Block

_BULLET_RE = re.compile(r"^\s{0,3}([-+*])(?:\s|$)")
_ORDERED_RE = re.compile(r"^\s{0,3}\d{1,9}([.)])(?:\s|$)")


def _list_marker(line: str) -> str | None:
    """Markdown リスト項目のマーカ種別を返す(非リスト行は None)。
    同種マーカの連続は同一リストの項目継続とみなす。CommonMark と同じく
    異なる bullet 文字・異なる ordered 区切りは別リストとみなす。"""
    m = _BULLET_RE.match(line)
    if m:
        return m.group(1)
    m = _ORDERED_RE.match(line)
    if m:
        return "ordered" + m.group(1)
    return None


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
    空行は飛ばしても連結を維持するが、除外行は連結を断つ。
    同種リストマーカの連続(同一リストの項目継続)は構文的に同ブロックと
    決まるため候補にしない(実測で退化1行ブロックの FP 温床だった)。"""
    cands: list[tuple[int, int]] = []
    prev: int | None = None
    for i, ln in enumerate(lines):
        if i in excluded:
            prev = None
            continue
        if not ln.strip():
            continue
        if prev is not None:
            m = _list_marker(lines[prev])
            if m is None or m != _list_marker(ln):
                cands.append((prev, i))
        prev = i
    return cands


def build_windows(
    lines: list[str],
    candidates: list[tuple[int, int]],
    max_pairs: int = WINDOW_PAIRS,
    margin: int = WINDOW_MARGIN_LINES,
    budget: int = WINDOW_STATE_CHARS,
) -> tuple[list[Window], list[tuple[int, int]]]:
    """境界ペアを連続したグループに一意に割当て、各グループの対象範囲+
    前後余白を共有ウィンドウにする。余白内のペアは重複判定しない。

    core(担当ペアが張る行範囲)自体を予算で分割する。単一ペアでも
    予算に収まらない場合は未検査として第2戻り値に返す(呼出し側で
    skipped に明示し、黙って「境界なし」扱いにしない)。"""
    windows: list[Window] = []
    uninspected: list[tuple[int, int]] = []

    def span_chars(lo: int, hi: int) -> int:
        return sum(len(lines[k]) + 1 for k in range(lo, hi + 1))

    group: list[tuple[int, int]] = []

    def flush() -> None:
        lo, hi = group[0][0], group[-1][1]
        total = span_chars(lo, hi)
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
        windows.append(Window(start=start, end=end, pairs=list(group)))

    for p in candidates:
        if span_chars(p[0], p[1]) > budget:
            uninspected.append(p)
            continue
        if group and (
            len(group) >= max_pairs or span_chars(group[0][0], p[1]) > budget
        ):
            flush()
            group = []
        group.append(p)
    if group:
        flush()
    return windows, uninspected


async def segment(
    engine: Engine,
    text: str,
    excluded: AbstractSet[int] = frozenset(),
    skipped: list[dict[str, Any]] | None = None,
) -> list[Block]:
    lines = text.splitlines() or [""]
    prose = [i for i, ln in enumerate(lines) if ln.strip() and i not in excluded]
    if not prose:
        return []
    candidates = find_boundary_candidates(lines, excluded)
    windows, uninspected = build_windows(lines, candidates)
    if skipped is not None:
        skipped.extend(
            {
                "stage": "segment",
                "reason": "window_state_too_large",
                "lines": [i + 1, j + 1],
            }
            for i, j in uninspected
        )

    async def ask(w: Window) -> NoulResult:
        # 質問はウィンドウ内 index で参照。元行番号との対応はコードが管理
        return await engine.noul_batch(
            {"lines": lines[w.start : w.end + 1]},
            {f"b{i}": _boundary_question(i - w.start, j - w.start) for i, j in w.pairs},
        )

    results = await asyncio.gather(*(ask(w) for w in windows))
    probs = {k: v for r in results for k, v in r.probs.items()}
    # 未検査ペア(probs に回答なし)は切断しない。skipped で明示済み
    asked = [p for w in windows for p in w.pairs]
    cuts = {j for i, j in asked if probs[f"b{i}"] >= BOUNDARY_THRESHOLD}
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
