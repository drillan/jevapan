"""構文領域マスク。文書全体で一度だけ analyze_syntax を呼び、
除外行(0始まり index)を source map として共有する。
行削除・再採番は行わず、除外領域をまたぐ文の連結もしない。"""

import re
from collections.abc import Set as AbstractSet

# ``` または ~~~ のフェンス行(行頭の空白は3文字まで許容)
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

MASK_PLACEHOLDER = "[excluded]"


def analyze_syntax(lines: list[str]) -> frozenset[int]:
    """除外領域の行 index(0始まり)を返す。

    対象:
    - front matter: 先頭行の `---` から次の `---` まで(閉じがなければ保留)
    - fenced code block: ``` / ~~~ の開閉(同種・同長以上で閉じる、閉じなければ文末まで)
    - 表行: `|` 始まりの行を行単位で除外(製品仕様。セル内 prose は評価しない)
    """
    excluded: set[int] = set()
    start = 0
    if len(lines) >= 2 and lines[0].strip() == "---":
        for k in range(1, len(lines)):
            if lines[k].strip() == "---":
                excluded.update(range(k + 1))
                start = k + 1
                break

    fence: tuple[str, int] | None = None
    for i in range(start, len(lines)):
        m = _FENCE_RE.match(lines[i])
        if fence is not None:
            excluded.add(i)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            continue
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            excluded.add(i)
        elif lines[i].lstrip().startswith("|"):
            excluded.add(i)
    return frozenset(excluded)


def masked_text(lines: list[str], excluded: AbstractSet[int]) -> str:
    """除外行を連続領域ごとに1行のプレースホルダへ置き換えたテキストを返す。
    採点対象の本文用。行番号の対応は原文(lines)側で保持する。"""
    out: list[str] = []
    in_excluded = False
    for i, ln in enumerate(lines):
        if i in excluded:
            if not in_excluded:
                out.append(MASK_PLACEHOLDER)
            in_excluded = True
        else:
            out.append(ln)
            in_excluded = False
    return "\n".join(out)
