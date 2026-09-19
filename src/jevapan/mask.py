"""構文領域マスク。文書全体で一度だけ analyze_syntax を呼び、
除外行(0始まり index)を source map として共有する。
行削除・再採番は行わず、除外領域をまたぐ文の連結もしない。"""

import re
from collections.abc import Set as AbstractSet

# ``` または ~~~ のフェンス行。CommonMark 準拠:
# - 行頭インデントは半角空白3文字まで(タブは4桁相当→ indented code で fence にならない)
# - 開き backtick fence の info string は backtick を含めない
# - 閉じは同種・同長以上のフェンス文字のみで、末尾は空白以外許さない
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

MASK_PLACEHOLDER = "[excluded]"

# scorer/locator の質問文に付す説明。masked 入力に現れる
# プレースホルダを評価対象の文章と誤認させないための1文
PLACEHOLDER_NOTE = (
    f"{MASK_PLACEHOLDER} はコードブロック・表・front matter を"
    "置き換えた目印であり評価対象の文章ではない。"
)


def _open_fence(line: str) -> tuple[str, int] | None:
    m = _FENCE_RE.match(line)
    if not m:
        return None
    ch, info = m.group(1), m.group(2)
    if ch[0] == "`" and "`" in info:
        return None
    return (ch[0], len(ch))


def _close_fence(line: str, fence: tuple[str, int]) -> bool:
    m = _FENCE_RE.match(line)
    if not m:
        return False
    ch, info = m.group(1), m.group(2)
    return ch[0] == fence[0] and len(ch) >= fence[1] and not info.strip()


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
        if fence is not None:
            excluded.add(i)
            if _close_fence(lines[i], fence):
                fence = None
            continue
        opened = _open_fence(lines[i])
        if opened is not None:
            fence = opened
            excluded.add(i)
        elif lines[i].lstrip().startswith("|"):
            excluded.add(i)
    return frozenset(excluded)


def masked_slice(lines: list[str], start: int, excluded: AbstractSet[int]) -> str:
    """doc 行番号 start(1始まり)から始まる行断片を、doc レベルの
    除外集合でマスクしたテキストを返す(ブロック採点の body 構築用)。
    除外領域を内包するブロックのコード等を scorer に見せない。"""
    return masked_text(
        lines, {off for off in range(len(lines)) if start - 1 + off in excluded}
    )


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
