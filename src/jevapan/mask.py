"""構文領域マスク。文書全体で一度だけ analyze_syntax を呼び、
除外行(0始まり index)を source map として共有する。
行削除・再採番は行わず、除外領域をまたぐ文の連結もしない。"""

import re
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet

# ``` または ~~~ のフェンス行。CommonMark 準拠:
# - 行頭インデントは半角空白3文字まで(タブは4桁相当→ indented code で fence にならない)
# - 開き backtick fence の info string は backtick を含めない
# - 閉じは同種・同長以上のフェンス文字のみで、末尾は空白以外許さない
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

MASK_PLACEHOLDER = "[除外]"


class ExcludedSet(frozenset[int]):
    """除外行 index の集合 + 各行の除外種別("コードブロック"/"表"/
    "フロントマター")のマッピング。analyze_syntax が返す。

    `in`/`==`/len 等の集合としての振る舞いは frozenset と同一で、
    種別は kinds 属性(行 index → 種別の日本語表示文字列)で保持する。
    種別文字列はプレースホルダの表示にそのまま使われる。"""

    kinds: dict[int, str]

    def __new__(
        cls,
        iterable: Iterable[int] = (),
        kinds: Mapping[int, str] | None = None,
    ) -> "ExcludedSet":
        obj = super().__new__(cls, iterable)
        obj.kinds = dict(kinds) if kinds else {}
        return obj


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


def analyze_syntax(lines: list[str]) -> ExcludedSet:
    """除外領域の行 index(0始まり)を ExcludedSet で返す。

    kinds 属性に行 index → 除外種別("フロントマター"/
    "コードブロック"/"表")のマッピングを保持し、masked_text が
    自己説明型のプレースホルダ `[除外: <種別>]` を生成するために使う。
    種別は日本語表記で、英単語を数えるカテゴリ(anglicism 等)が
    プレースホルダ自体を違反として数える衝突を構造的に防ぐ。

    対象:
    - front matter: 先頭行の `---` から次の `---` まで(閉じがなければ保留)
    - fenced code block: ``` / ~~~ の開閉(同種・同長以上で閉じる、閉じなければ文末まで)
    - 表行: `|` 始まりの行を行単位で除外(製品仕様。セル内 prose は評価しない)
    """
    excluded: set[int] = set()
    kinds: dict[int, str] = {}
    start = 0
    if len(lines) >= 2 and lines[0].strip() == "---":
        for k in range(1, len(lines)):
            if lines[k].strip() == "---":
                excluded.update(range(k + 1))
                kinds.update(dict.fromkeys(range(k + 1), "フロントマター"))
                start = k + 1
                break

    fence: tuple[str, int] | None = None
    for i in range(start, len(lines)):
        if fence is not None:
            excluded.add(i)
            kinds[i] = "コードブロック"
            if _close_fence(lines[i], fence):
                fence = None
            continue
        opened = _open_fence(lines[i])
        if opened is not None:
            fence = opened
            excluded.add(i)
            kinds[i] = "コードブロック"
        elif lines[i].lstrip().startswith("|"):
            excluded.add(i)
            kinds[i] = "表"
    return ExcludedSet(excluded, kinds)


def masked_slice(lines: list[str], start: int, excluded: AbstractSet[int]) -> str:
    """doc 行番号 start(1始まり)から始まる行断片を、doc レベルの
    除外集合でマスクしたテキストを返す(ブロック採点の body 構築用)。
    除外領域を内包するブロックのコード等を scorer に見せない。"""
    offs: AbstractSet[int] = {
        off for off in range(len(lines)) if start - 1 + off in excluded
    }
    kinds = getattr(excluded, "kinds", None)
    if kinds:
        offs = ExcludedSet(
            offs,
            {off: kinds[start - 1 + off] for off in offs if start - 1 + off in kinds},
        )
    return masked_text(lines, offs)


def masked_text(lines: list[str], excluded: AbstractSet[int]) -> str:
    """除外行を連続領域ごとに1行のプレースホルダへ置き換えたテキストを返す。
    採点対象の本文用。行番号の対応は原文(lines)側で保持する。

    除外集合が種別情報(ExcludedSet.kinds)を持つ場合は自己説明型の
    `[除外: <種別>]` を出す。種別が得られない入力(素の set 等)は
    `[除外]` にフォールバックする。連続する除外領域が
    種別をまたぐ場合は種別ごとに別々のプレースホルダを出す。"""
    kinds = getattr(excluded, "kinds", None) or {}
    out: list[str] = []
    prev_kind: str | None = None
    in_excluded = False
    for i, ln in enumerate(lines):
        if i in excluded:
            kind = kinds.get(i)
            if not in_excluded or kind != prev_kind:
                out.append(f"[除外: {kind}]" if kind else MASK_PLACEHOLDER)
            in_excluded = True
            prev_kind = kind
        else:
            out.append(ln)
            in_excluded = False
            prev_kind = None
    return "\n".join(out)
