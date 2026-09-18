"""ブロックの補助文脈(直近見出し + 直前の prose 段落)の抽出。

scorer(採点)と locator(違反文特定)の両段で同じ文脈を共有する。
補助文脈は評価対象本文(body/block)とは別フィールドで渡し、
除外行(コード等)が文脈の先頭を埋めないよう非空・非除外行だけを
末尾側から集める。"""

from collections.abc import Set as AbstractSet

# 補助文脈の文字数上限(概算)。直前段落を優先しつつ state を小さく保つ
CONTEXT_CHARS = 2000


def nearest_heading(
    lines: list[str], before: int, excluded: AbstractSet[int] = frozenset()
) -> str:
    """before(1始まり行番号)より前の直近の見出し行(# 始まり)を返す。
    除外行(コード内コメント等)は見出しにしない。"""
    for idx in range(min(before - 2, len(lines) - 1), -1, -1):
        if idx in excluded:
            continue
        if lines[idx].lstrip().startswith("#"):
            return lines[idx].strip().lstrip("#").strip()
    return ""


def preceding_context(
    lines: list[str],
    before: int,
    excluded: AbstractSet[int] = frozenset(),
    limit: int = CONTEXT_CHARS,
) -> str:
    """before(1始まり行番号)より前の非空・非除外行を末尾側から
    ~limit 字集めて返す。直前段落が優先され、長いコード等の除外行で
    文脈が埋まることはない。"""
    buf: list[str] = []
    total = 0
    for idx in range(min(before - 2, len(lines) - 1), -1, -1):
        ln = lines[idx]
        if idx in excluded or not ln.strip():
            continue
        if buf and total + len(ln) + 1 > limit:
            break
        buf.append(ln)
        total += len(ln) + 1
    return "\n".join(reversed(buf))[-limit:]
