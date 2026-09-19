"""境界分割の Stage 1 計測(issue #2)。

実験構成(advisor 計画に基づく):
- cache_check: 同一リクエストを連続2回送り、生確率が完全一致するか
  確認(最優先)。キャッシュがあれば反復実験の設計を見直すため結果を記録
- E-E: 現行設定(候補列挙→build_windows→現行質問文)を対象文書に R 回
- E-C: 固定窓 W(scripts/boundary_eval_data.json)のラベル付きペアを
  Q 個ずつ分割し、質問文5案(現行/確信度明示/接合基準明確化/
  役割言語化/否定形)で R 回。state は毎回 W 全体で不変
- E-A: 固定窓 W のラベル付きペアを Q∈{1,5,20} で分割して R 回。
  state は毎回 W 全体で不変

記録(eval_thresholds.py の設計踏襲):
- リクエスト単位で state hash・質問キー・質問文・生確率・model・usage を
  JSON に保存。判定は生確率(丸めは表示のみ)
- 反復は run 単位で分け、入力 hash を記録。run 内で model が混在した
  場合は discard=true でマークする(破棄判断は分析側)

使い方:
    TYPESAFE_API_KEY=... uv run python scripts/eval_boundaries.py
    TYPESAFE_API_KEY=... uv run python scripts/eval_boundaries.py --plan
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from itertools import batched
from pathlib import Path
from typing import Any

from typesafe_sdk import Choice, Noul, NoulCriteria

from jevapan.cli import _make_engine
from jevapan.engine import Engine, NoulResult, _model_of, _usage_of
from jevapan.mask import PLACEHOLDER_NOTE, analyze_syntax
from jevapan.segmenter import (
    WINDOW_MARGIN_LINES,
    _boundary_question,
    build_windows,
    find_boundary_candidates,
)

DEFAULT_DOCS = [
    "docs/superpowers/plans/2026-09-18-jevapan.md",
    "docs/superpowers/specs/2026-09-18-jevapan-design.md",
]
DEFAULT_DATA = "scripts/boundary_eval_data.json"

R_EE = 5
R_EC = 3
R_EA = 3
R_EB = 3
EC_PAIRS = 20
EA_QS = (1, 5, 20)
# E-B: state 縮小実験。対象窓はペア span が EB_SPAN に一致する窓
# (plan.md の build_windows 出力、state 16,855字・30ペアで定数化が観測
# された窓)。水準は state JSON サイズ(文字数)の上限。
EB_SPAN = (1250, 1736)
EB_LEVELS = (16855, 8000, 4000, 2000)
DEFAULT_LABELS_EXT = "scripts/boundary_eval_labels_ext.json"

R_ECRIT = 3
R_ECHOICE = 3
# E-CRIT: Noul.criteria の true/false 説明文(hub 裁定済み規則を明示)
ECRIT_TRUE = "新しいブロックが始まる。話題または役割が変わる点"
ECRIT_FALSE = (
    "同一ブロックの継続。見出しとその直後の本文、Run: とその Expected:、"
    "ラベル行とその中身は同一ブロック"
)
# E-CHOICE: ペア群の既定サイズ(5-6件群=5-6群、10件群=3群で9 req)
EB_GROUP_SIZE = 10

# E-CHOICE v2(hub 再設計): 予備実験 exp_echoice とは別系統。
# 1問=Choice1件(1 req ずつ、バッチ文脈の混入を防ぐため束ねない)。
# 各問は「ちょうど1つが境界」の6選択肢(boundary1+continue5)。
# none 選択肢は入れない。文書配分は plan3/design3/readme2/bad1。
ECHOICE2_INSTRUCTIONS = (
    "The document is given as numbered lines in state.lines. "
    "Exactly one of the following junctions begins a new block — "
    "a change of topic, role, or structure. Which one?"
)
ECHOICE2_SEED = 20260919
ECHOICE2_DOCS = {
    "plan": DEFAULT_DOCS[0],
    "design": DEFAULT_DOCS[1],
    "readme": "README.md",
    "bad": "samples/bad.md",
}
ECHOICE2_ALLOC = {"plan": 3, "design": 3, "readme": 2, "bad": 1}
ECHOICE2_N_OPTIONS = 6
# 対照(+3req): 4/9 以上の正解が出た場合のみ、同じ群を無関係な一文
# (PLACEHOLDER_NOTE)の有無だけ変えて問い直す対応のある比較。
# 対象は本テストで confidence が高かった上位3群(確信のある選択が
# 覆るか見る。確信の低い群では変化がノイズと区別できない)
ECHOICE2_CONTROL_N = 3
ECHOICE2_SIGNIFICANT = 4  # 4/9 以上 = p=0.048 で有意(事前登録)


def _q_confidence(i: int, j: int) -> str:
    return (
        _boundary_question(i, j)
        + " Report the calibrated probability, not just yes or no."
    )


def _q_junction(i: int, j: int) -> str:
    return (
        "The document is given as numbered lines in state.lines. "
        f"Judge only the junction between `lines[{i}]` and `lines[{j}]`: "
        "does the topic, role, or structure change there? "
        "Do not judge any other pair."
    )


def _q_role(i: int, j: int) -> str:
    return (
        "The document is given as numbered lines in state.lines. "
        f"Does the role of the text change between `lines[{i}]` and "
        f"`lines[{j}]` — a shift in topic, a move between prose and "
        "a list, or the start of a new section?"
    )


def _q_negative(i: int, j: int) -> str:
    return (
        "The document is given as numbered lines in state.lines. "
        f"Do `lines[{i}]` and `lines[{j}]` belong to the same continuous "
        "block, with no boundary between them?"
    )


# 質問文バリアント。polarity は確率の向き(否定形は boundary の逆を聞く)
QUESTION_VARIANTS: dict[str, dict[str, Any]] = {
    "current": {"fn": _boundary_question, "polarity": 1},
    "confidence": {"fn": _q_confidence, "polarity": 1},
    "junction": {"fn": _q_junction, "polarity": 1},
    "role": {"fn": _q_role, "polarity": 1},
    "negative": {"fn": _q_negative, "polarity": -1},
}

# E-CRIT の instructions バリアント。criteria は両腕で同一(ECRIT_TRUE/
# ECRIT_FALSE)を付し、instructions の wording 差のみを比較する
ECRIT_VARIANTS: dict[str, Callable[[int, int], str]] = {
    "criteria": _boundary_question,
    "role_criteria": _q_role,
}


def state_hash(state: Any) -> str:
    """state の正準 JSON の SHA-256。同一 state の識別用。"""
    canon = json.dumps(state, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def _input_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _ask(
    engine: Engine,
    requests: list[dict[str, Any]],
    state: Any,
    questions: dict[str, str],
    meta: dict[str, Any] | None = None,
) -> NoulResult:
    """1リクエストを送り、state hash・質問・生確率・model・usage を記録。"""
    res = await engine.noul_batch(state, questions)
    requests.append(
        {
            "state": state,
            "state_sha256": state_hash(state),
            "question_keys": sorted(questions),
            "questions": questions,
            "probs": res.probs,
            "model": res.model,
            "usage": asdict(res.usage),
            **(meta or {}),
        }
    )
    return res


async def _ask_typed(
    engine: Engine,
    requests: list[dict[str, Any]],
    state: Any,
    questions: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> Any:
    """Noul/Choice 等の Question オブジェクトを直接送り、回答を記録。

    noul は probs[key]=確率、choice は choices[key]={choice, confidence,
    probabilities} で記録する。questions は wire 形式(model_dump)で保存。"""
    resp = await engine._call(state, questions)
    probs: dict[str, float] = {}
    choices: dict[str, Any] = {}
    for k in questions:
        a = resp.answers[k]
        if hasattr(a, "choice"):
            choices[k] = {
                "choice": a.choice,
                "confidence": float(getattr(a, "confidence", 0.0)),
                "probabilities": {str(x): float(y) for x, y in a.probabilities.items()},
            }
        else:
            probs[k] = float(a.noul)
    requests.append(
        {
            "state": state,
            "state_sha256": state_hash(state),
            "question_keys": sorted(questions),
            "questions": {k: v.model_dump(mode="json") for k, v in questions.items()},
            "probs": probs,
            "choices": choices,
            "model": _model_of(resp),
            "usage": asdict(_usage_of(resp)),
            **(meta or {}),
        }
    )
    return resp


def _finish_run(run: dict[str, Any]) -> dict[str, Any]:
    """run を締める: リクエスト数・model 一貫性を確定。model 混在は
    discard=true でマーク(データは残し破棄判断は分析側に委ねる)。"""
    models = sorted({r["model"] for r in run["requests"]})
    run["models"] = models
    run["n_requests"] = len(run["requests"])
    run["model_consistent"] = len(models) <= 1
    run["discard"] = not run["model_consistent"]
    return run


async def cache_check(
    engine: Engine, state: Any, questions: dict[str, str]
) -> dict[str, Any]:
    """同一リクエストを連続2回送り、生確率が完全一致するか確認する。
    キャッシュがあれば反復実験の分散が測れないため結果を必ず記録。"""
    r1 = await engine.noul_batch(state, questions)
    r2 = await engine.noul_batch(state, questions)
    return {
        "identical": r1.probs == r2.probs,
        "probs_first": r1.probs,
        "probs_second": r2.probs,
        "models": [r1.model, r2.model],
        "usage": [asdict(r1.usage), asdict(r2.usage)],
    }


def _doc_inputs(
    doc: str,
) -> tuple[list[str], frozenset[int], list[tuple[int, int]]]:
    lines = Path(doc).read_text(encoding="utf-8").splitlines() or [""]
    excluded = analyze_syntax(lines)
    candidates = find_boundary_candidates(lines, excluded, set())
    return lines, excluded, candidates


async def exp_ee(
    engine: Engine, docs: Sequence[str], r: int = R_EE
) -> list[dict[str, Any]]:
    """E-E: 現行設定(現行の候補列挙・窓構築・質問文)を R 回反復。"""
    runs: list[dict[str, Any]] = []
    for doc in docs:
        text = Path(doc).read_text(encoding="utf-8")
        lines, _, candidates = _doc_inputs(doc)
        windows, uninspected = build_windows(lines, candidates)
        for rep in range(r):
            run: dict[str, Any] = {
                "experiment": "E-E",
                "doc": doc,
                "rep": rep,
                "input_sha256": _input_hash(text),
                "n_candidates": len(candidates),
                "n_windows": len(windows),
                "uninspected": [list(p) for p in uninspected],
                "variant": "current",
                "params": {"max_pairs": None},
                "requests": [],
            }
            for w in windows:
                await _ask(
                    engine,
                    run["requests"],
                    {"lines": lines[w.start : w.end + 1]},
                    {
                        f"b{i}": _boundary_question(i - w.start, j - w.start)
                        for i, j in w.pairs
                    },
                    meta={
                        "window": [w.start, w.end],
                        "pairs": [list(p) for p in w.pairs],
                    },
                )
            runs.append(_finish_run(run))
    return runs


async def exp_ec(
    engine: Engine, data: dict[str, Any], r: int = R_EC, q: int = EC_PAIRS
) -> list[dict[str, Any]]:
    """E-C: 固定窓 W のラベル付きペアを Q 個ずつ分割し、質問文バリアントを
    R 回反復。state は毎回 W 全体で不変(E-A と同一素材で直接比較する
    ため全実験で同一の固定窓を使い交絡を切る)。"""
    window_lines = data["window"]["lines"]
    labels = [lb for lb in data["labels"] if lb["doc"] == "window"]
    pairs = [(lb["i"], lb["j"]) for lb in labels]
    runs: list[dict[str, Any]] = []
    for name, spec in QUESTION_VARIANTS.items():
        fn: Callable[[int, int], str] = spec["fn"]
        for rep in range(r):
            run: dict[str, Any] = {
                "experiment": "E-C",
                "doc": data["window"]["source"],
                "rep": rep,
                "input_sha256": _input_hash(json.dumps(data, sort_keys=True)),
                "variant": name,
                "polarity": spec["polarity"],
                "params": {"q": q, "labels": labels},
                "requests": [],
            }
            for chunk in batched(pairs, q, strict=False):
                await _ask(
                    engine,
                    run["requests"],
                    {"lines": window_lines},
                    {f"b{i}": fn(i, j) for i, j in chunk},
                    meta={"pairs": [list(p) for p in chunk]},
                )
            runs.append(_finish_run(run))
    return runs


async def exp_ea(
    engine: Engine,
    data: dict[str, Any],
    r: int = R_EA,
    qs: Sequence[int] = EA_QS,
) -> list[dict[str, Any]]:
    """E-A: 固定窓 W のラベル付きペアを Q 個ずつ分割して R 回反復。
    state は毎回 W 全体で不変。ラベルは doc=="window" のもののみ使う。"""
    window_lines = data["window"]["lines"]
    labels = [lb for lb in data["labels"] if lb["doc"] == "window"]
    pairs = [(lb["i"], lb["j"]) for lb in labels]
    runs: list[dict[str, Any]] = []
    for q in qs:
        for rep in range(r):
            run: dict[str, Any] = {
                "experiment": "E-A",
                "doc": data["window"]["source"],
                "rep": rep,
                "input_sha256": _input_hash(json.dumps(data, sort_keys=True)),
                "variant": "current",
                "polarity": 1,
                "params": {"q": q, "labels": labels},
                "requests": [],
            }
            for chunk in batched(pairs, q, strict=False):
                await _ask(
                    engine,
                    run["requests"],
                    {"lines": window_lines},
                    {f"b{i}": _boundary_question(i, j) for i, j in chunk},
                    meta={"pairs": [list(p) for p in chunk]},
                )
            runs.append(_finish_run(run))
    return runs


def _state_chars(state_lines: list[str]) -> int:
    """state の JSON シリアライズ文字数(サイズ水準の計測単位)。"""
    return len(json.dumps({"lines": state_lines}, ensure_ascii=False))


def _shrink_state_lines(
    lines: list[str],
    pair_lines: set[int],
    w_start: int,
    w_end: int,
    budget: int,
) -> list[int]:
    """E-B の state 縮小: ペア端点行は必ず保持し、周辺のコンテキスト行を
    端点からの距離 r で拡張しながら、state JSON サイズが budget 以下に
    なる最大の行 index 集合を返す。最小集合(端点行のみ)でも超過する
    場合は端点行のみを返す。"""
    full = set(range(w_start, w_end + 1))

    def size(sel: set[int]) -> int:
        return _state_chars([lines[k] for k in sorted(sel)])

    if size(full) <= budget:
        return sorted(full)
    sel = set(pair_lines)
    r = 1
    while sel != full:
        cand = {
            x
            for k in pair_lines
            for x in range(max(w_start, k - r), min(w_end, k + r) + 1)
        }
        if size(cand) > budget:
            return sorted(sel)
        sel = cand
        r += 1
    return sorted(sel)


def _eb_window(lines: list[str], candidates: Any, span: tuple[int, int]) -> Any:
    """ペア span が一致する build_windows 窓を返す(E-B 系実験の共通)。"""
    windows, _ = build_windows(lines, candidates)
    targets = [
        w
        for w in windows
        if w.pairs and w.pairs[0][0] == span[0] and w.pairs[-1][1] == span[1]
    ]
    if not targets:
        msg = f"pair span {span} の窓が見つからない"
        raise ValueError(msg)
    return targets[0]


async def exp_eb(
    engine: Engine,
    doc: str,
    r: int = R_EB,
    levels: Sequence[int] = EB_LEVELS,
    q: int = EC_PAIRS,
    span: tuple[int, int] = EB_SPAN,
) -> list[dict[str, Any]]:
    """E-B: 定数化した窓の state を複数水準に縮め、窓内SDが回復するか
    検証する。質問文・Q・R は E-C/E-A と同一(現行文・Q=20・R=3)。
    state の縮小に伴い質問文中の行 index は state 内位置に再写像する
    (確率キーはペアの doc 行 index を維持し突き合わせ可能にする)。"""
    text = Path(doc).read_text(encoding="utf-8")
    lines, _, candidates = _doc_inputs(doc)
    w = _eb_window(lines, candidates, span)
    pair_lines = {k for p in w.pairs for k in p}
    runs: list[dict[str, Any]] = []
    for level in levels:
        sel = _shrink_state_lines(lines, pair_lines, w.start, w.end, level)
        state = {"lines": [lines[k] for k in sel]}
        pos = {k: idx for idx, k in enumerate(sel)}
        chars = _state_chars(state["lines"])
        for rep in range(r):
            run: dict[str, Any] = {
                "experiment": "E-B",
                "doc": doc,
                "rep": rep,
                "input_sha256": _input_hash(text),
                "variant": "current",
                "polarity": 1,
                "params": {
                    "level": level,
                    "q": q,
                    "state_chars": chars,
                    "n_state_lines": len(sel),
                    "pair_span": list(span),
                },
                "requests": [],
            }
            for chunk in batched(w.pairs, q, strict=False):
                await _ask(
                    engine,
                    run["requests"],
                    state,
                    {f"b{i}": _boundary_question(pos[i], pos[j]) for i, j in chunk},
                    meta={
                        "pairs": [list(p) for p in chunk],
                        "state_chars": chars,
                        "n_state_lines": len(sel),
                    },
                )
            runs.append(_finish_run(run))
    return runs


async def exp_ecrit(
    engine: Engine,
    doc: str,
    labels_ext: dict[str, Any],
    r: int = R_ECRIT,
    q: int = EC_PAIRS,
    span: tuple[int, int] = EB_SPAN,
) -> list[dict[str, Any]]:
    """E-CRIT(優先度2): Noul.criteria 実験。現行 segmenter は
    instructions のみで criteria 未使用 — true/false に裁定済み規則を
    明示し、定数化した E-B 窓で回復するかを検証する。
    対象は labels_ext の doc=="plan_eb" のラベル付きペア(窓ペアと
    交差)。state は窓全体(E-B と同一素材)。評価は窓内SD(主)+AUC(従)。"""
    text = Path(doc).read_text(encoding="utf-8")
    lines, _, candidates = _doc_inputs(doc)
    w = _eb_window(lines, candidates, span)
    pair_set = set(w.pairs)
    pairs = [
        (lb["i"], lb["j"])
        for lb in labels_ext["labels"]
        if lb.get("doc") == "plan_eb" and (lb["i"], lb["j"]) in pair_set
    ]
    if not pairs:
        msg = f"plan_eb ラベルが窓ペアと交差しない: {doc} span={span}"
        raise ValueError(msg)
    state = {"lines": lines[w.start : w.end + 1]}
    crit = NoulCriteria(true=ECRIT_TRUE, false=ECRIT_FALSE)
    runs: list[dict[str, Any]] = []
    for name, fn in ECRIT_VARIANTS.items():
        for rep in range(r):
            run: dict[str, Any] = {
                "experiment": "E-CRIT",
                "doc": doc,
                "rep": rep,
                "input_sha256": _input_hash(text),
                "variant": name,
                "polarity": 1,
                "params": {
                    "q": q,
                    "criteria": {"true": ECRIT_TRUE, "false": ECRIT_FALSE},
                    "n_pairs": len(pairs),
                    "pair_span": list(span),
                },
                "requests": [],
            }
            for chunk in batched(pairs, q, strict=False):
                await _ask_typed(
                    engine,
                    run["requests"],
                    state,
                    {
                        f"b{i}": Noul(
                            instructions=fn(i - w.start, j - w.start),
                            criteria=crit,
                        )
                        for i, j in chunk
                    },
                    meta={"pairs": [list(p) for p in chunk]},
                )
            runs.append(_finish_run(run))
    return runs


async def exp_echoice(
    engine: Engine,
    doc: str,
    r: int = R_ECHOICE,
    group_size: int = EB_GROUP_SIZE,
    span: tuple[int, int] = EB_SPAN,
) -> list[dict[str, Any]]:
    """E-CHOICE(優先度3): Choice 定式化。30ペアを group_size 件ずつの
    選択肢群に分け、「この中でどこが新ブロックの開始か」の強制比較に
    変更 — 定数化が構造的に起きない形式。1群=1リクエスト(hub 見積り
    「3群なら9 req」に整合)。選択肢には none(どれも境界でない)を
    付す — continue のみの群でも強制誤検出しないための退避先。"""
    text = Path(doc).read_text(encoding="utf-8")
    lines, _, candidates = _doc_inputs(doc)
    w = _eb_window(lines, candidates, span)
    groups = list(batched(w.pairs, group_size, strict=False))
    state = {"lines": lines[w.start : w.end + 1]}
    instructions = (
        "The document is given as numbered lines in state.lines. "
        "Among the junctions below, at which one does a new block "
        "begin — where the topic or the role of the text changes? "
        "Choose exactly one; choose 'none' if none of them does."
    )
    runs: list[dict[str, Any]] = []
    for rep in range(r):
        run: dict[str, Any] = {
            "experiment": "E-CHOICE",
            "doc": doc,
            "rep": rep,
            "input_sha256": _input_hash(text),
            "variant": "choice",
            "polarity": 1,
            "params": {
                "group_size": group_size,
                "n_groups": len(groups),
                "pair_span": list(span),
            },
            "requests": [],
        }
        for gi, grp in enumerate(groups):
            criteria: dict[str, Any] = {
                f"b{i}": (
                    f"junction between lines[{i - w.start}] and lines[{j - w.start}]"
                )
                for i, j in grp
            }
            criteria["none"] = "どの接続点も新しいブロックの開始ではない"
            await _ask_typed(
                engine,
                run["requests"],
                state,
                {f"g{gi}": Choice(instructions=instructions, criteria=criteria)},
                meta={"pairs": [list(p) for p in grp], "group": gi},
            )
        runs.append(_finish_run(run))
    return runs


def _choice2_label_pools(
    data: dict[str, Any], labels_ext: dict[str, Any]
) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """doc -> label -> ソート済みユニークペア(doc 行 index)。

    plan は labels_ext の plan_eb(doc index)と data の window ラベル
    (window.doc_lines[0]-1 をオフセットに doc index へ写像)を併合する。
    either は選択肢に使えないため boundary/continue のみ集める。"""
    pools: dict[str, dict[str, set[tuple[int, int]]]] = {
        d: {"boundary": set(), "continue": set()} for d in ECHOICE2_DOCS
    }
    for lb in labels_ext["labels"]:
        doc = "plan" if lb["doc"] == "plan_eb" else lb["doc"]
        if doc in pools and lb["label"] in pools[doc]:
            pools[doc][lb["label"]].add((int(lb["i"]), int(lb["j"])))
    woff = int(data["window"]["doc_lines"][0]) - 1
    for lb in data["labels"]:
        if lb["doc"] == "window":
            doc, i, j = "plan", int(lb["i"]) + woff, int(lb["j"]) + woff
        elif lb["doc"] == "design":
            doc, i, j = "design", int(lb["i"]), int(lb["j"])
        else:
            continue
        if lb["label"] in pools[doc]:
            pools[doc][lb["label"]].add((i, j))
    return {d: {k: sorted(v) for k, v in m.items()} for d, m in pools.items()}


def _choice2_questions(
    pools: dict[str, dict[str, list[tuple[int, int]]]],
    alloc: dict[str, int] = ECHOICE2_ALLOC,
    n_options: int = ECHOICE2_N_OPTIONS,
    seed: int = ECHOICE2_SEED,
) -> list[dict[str, Any]]:
    """各問の群構成を決定的に構築する(正解位置のみ seed でシャッフル)。

    boundary は全問で相異なるものを等間隔に選ぶ。continue は未使用を
    優先し、尽きたら先頭から再利用する(reused_continues に記録)。
    continue が n_options-1 件に満たない doc は群を縮小する
    (truncated_options に記録)。"""
    specs: list[dict[str, Any]] = []
    qi = 0
    for doc, n_q in alloc.items():
        bounds = pools[doc]["boundary"]
        conts = pools[doc]["continue"]
        if not bounds:
            msg = f"{doc}: boundary ラベルなし"
            raise ValueError(msg)
        used: set[tuple[int, int]] = set()
        for k in range(n_q):
            b = bounds[k * len(bounds) // n_q]
            fresh = [c for c in conts if c not in used]
            take = fresh[: n_options - 1]
            take += [c for c in conts if c not in take][: n_options - 1 - len(take)]
            opts = [("boundary", b)] + [("continue", c) for c in take]
            order = list(range(len(opts)))
            random.Random(seed + qi).shuffle(order)
            keys = "abcdef"[: len(opts)]
            options = [
                {"key": keys[t], "pair": list(opts[i][1]), "label": opts[i][0]}
                for t, i in enumerate(order)
            ]
            specs.append(
                {
                    "index": qi,
                    "doc": doc,
                    "options": options,
                    "correct_key": next(
                        o["key"] for o in options if o["label"] == "boundary"
                    ),
                    "seed": seed + qi,
                    "reused_continues": sorted(c for c in take if c in used),
                    "truncated_options": len(opts) < n_options,
                }
            )
            used.update(take)
            qi += 1
    return specs


async def exp_echoice2(
    engine: Engine,
    data: dict[str, Any],
    labels_ext: dict[str, Any],
    alloc: dict[str, int] = ECHOICE2_ALLOC,
    n_options: int = ECHOICE2_N_OPTIONS,
    seed: int = ECHOICE2_SEED,
) -> list[dict[str, Any]]:
    """E-CHOICE v2(hub 再設計): 9問 × Choice1件(1問1リクエスト)。

    各問は boundary1+continue5 の「ちょうど1つが境界」群(none なし)。
    state は現行 segmenter と同形 {"lines": [...]}(全選択肢を含む
    最小範囲+WINDOW_MARGIN_LINES の余白)。criteria キーは中立の
    a-f で、説明文は「lines[i] と lines[j] の間」(state 内 index)。
    4/9 以上の正解が出た場合のみ、instructions に PLACEHOLDER_NOTE
    (無関係な一文)を足した対照3reqを追加実行する。"""
    pools = _choice2_label_pools(data, labels_ext)
    specs = _choice2_questions(pools, alloc, n_options, seed)
    doc_texts = {
        d: Path(p).read_text(encoding="utf-8") for d, p in ECHOICE2_DOCS.items()
    }

    def state_for(spec: dict[str, Any]) -> tuple[Any, int, int]:
        lines = doc_texts[spec["doc"]].splitlines()
        ends = {k for o in spec["options"] for k in o["pair"]}
        s0 = max(0, min(ends) - WINDOW_MARGIN_LINES)
        s1 = min(len(lines), max(ends) + WINDOW_MARGIN_LINES + 1)
        return {"lines": lines[s0:s1]}, s0, s1

    async def ask(run: dict[str, Any], spec: dict[str, Any], instructions: str) -> None:
        state, s0, s1 = state_for(spec)
        criteria = {
            o["key"]: f"lines[{o['pair'][0] - s0}] と lines[{o['pair'][1] - s0}] の間"
            for o in spec["options"]
        }
        await _ask_typed(
            engine,
            run["requests"],
            state,
            {f"q{spec['index']}": Choice(instructions=instructions, criteria=criteria)},
            meta={**spec, "state_range": [s0, s1]},
        )

    run: dict[str, Any] = {
        "experiment": "E-CHOICE2",
        "rep": 0,
        "input_sha256": {d: _input_hash(t) for d, t in doc_texts.items()},
        "variant": "choice2",
        "polarity": 1,
        "params": {
            "alloc": dict(alloc),
            "n_options": n_options,
            "n_questions": len(specs),
            "seed": seed,
        },
        "requests": [],
    }
    for spec in specs:
        await ask(run, spec, ECHOICE2_INSTRUCTIONS)
    runs = [_finish_run(run)]

    # 事前登録: 4/9 以上の正解が出た場合のみ対照を実行
    n_correct = sum(
        1
        for req, spec in zip(run["requests"], specs, strict=True)
        if req["choices"][f"q{spec['index']}"]["choice"] == spec["correct_key"]
    )
    run["n_correct"] = n_correct
    if n_correct >= ECHOICE2_SIGNIFICANT:
        # 対応のある比較: 同じ群を PLACEHOLDER_NOTE の有無だけ変えて
        # 問い直す(別群は群違いと交絡するため不可)。対象は本テストで
        # confidence が高かった上位3群、判定は本テストの選択キーとの一致
        main_choice = {
            spec["index"]: req["choices"][f"q{spec['index']}"]
            for req, spec in zip(run["requests"], specs, strict=True)
        }
        top = sorted(
            main_choice,
            key=lambda ix: main_choice[ix]["confidence"],
            reverse=True,
        )[:ECHOICE2_CONTROL_N]
        ctrl: dict[str, Any] = {
            "experiment": "E-CHOICE2",
            "rep": 0,
            "input_sha256": run["input_sha256"],
            "variant": "choice2_control",
            "polarity": 1,
            "params": {
                "note": PLACEHOLDER_NOTE,
                "question_indices": top,
                "main_confidences": {ix: main_choice[ix]["confidence"] for ix in top},
            },
            "requests": [],
        }
        for ix in top:
            await ask(ctrl, specs[ix], ECHOICE2_INSTRUCTIONS + " " + PLACEHOLDER_NOTE)
        ctrl["unchanged"] = all(
            c["choices"][f"q{s['index']}"]["choice"]
            == main_choice[s["index"]]["choice"]
            for c, s in zip(ctrl["requests"], (specs[ix] for ix in top), strict=True)
        )
        runs.append(_finish_run(ctrl))
    return runs


async def run_all(args: argparse.Namespace) -> dict[str, Any]:
    engine = _make_engine(args.concurrency)
    out: dict[str, Any] = {
        "inputs": {},
        "cache_check": None,
        "runs": [],
        "stages": args.stages,
    }
    stages = set(args.stages)
    for doc in args.docs:
        text = Path(doc).read_text(encoding="utf-8")
        _, _, candidates = _doc_inputs(doc)
        out["inputs"][doc] = {
            "sha256": _input_hash(text),
            "n_candidates": len(candidates),
        }

    if "cache" in stages and args.docs:
        # 最優先チェック: 最初の文書の最初の窓を使う
        doc = args.docs[0]
        lines, _, candidates = _doc_inputs(doc)
        windows, _ = build_windows(lines, candidates)
        if windows:
            w = windows[0]
            out["cache_check"] = await cache_check(
                engine,
                {"lines": lines[w.start : w.end + 1]},
                {
                    f"b{i}": _boundary_question(i - w.start, j - w.start)
                    for i, j in w.pairs
                },
            )
        else:
            out["cache_check"] = {"skipped": "no windows"}

    if "ee" in stages:
        out["runs"].extend(await exp_ee(engine, args.docs, r=args.rep_ee))
    # E-C/E-A は同一の固定窓 W を使う(交絡を切るため全実験で同一素材)
    # E-CHOICE2 は W ラベル(plan 文書への写像用)も使う
    data = None
    if {"ec", "ea", "choice2"} & stages:
        data_path = Path(args.data)
        if data_path.exists():
            raw = data_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            out["inputs"][str(data_path)] = {"sha256": _input_hash(raw)}
        else:
            out["data_missing"] = f"data file not found: {data_path}"
    if "ec" in stages and data is not None:
        out["runs"].extend(await exp_ec(engine, data, r=args.rep_ec, q=args.q))
    if "ea" in stages and data is not None:
        out["runs"].extend(await exp_ea(engine, data, r=args.rep_ea))
    if "eb" in stages:
        out["runs"].extend(
            await exp_eb(
                engine,
                args.eb_doc,
                r=args.rep_eb,
                levels=args.eb_levels,
                q=args.q,
            )
        )
    labels_ext = None
    if {"crit", "choice2"} & stages:
        lex_path = Path(args.labels_ext)
        if lex_path.exists():
            raw_lex = lex_path.read_text(encoding="utf-8")
            labels_ext = json.loads(raw_lex)
            out["inputs"][str(lex_path)] = {"sha256": _input_hash(raw_lex)}
        else:
            out["labels_ext_missing"] = f"labels file not found: {lex_path}"
    if "crit" in stages and labels_ext is not None:
        out["runs"].extend(
            await exp_ecrit(
                engine,
                args.eb_doc,
                labels_ext,
                r=args.rep_ecrit,
                q=args.q,
            )
        )
    if "choice" in stages:
        out["runs"].extend(
            await exp_echoice(
                engine,
                args.eb_doc,
                r=args.rep_echoice,
                group_size=args.eb_group_size,
            )
        )
    if "choice2" in stages and data is not None and labels_ext is not None:
        out["runs"].extend(await exp_echoice2(engine, data, labels_ext))
    return out


def plan(args: argparse.Namespace) -> dict[str, int]:
    """API を呼ばずに各ステージのリクエスト数見積りを返す(--plan)。"""
    counts: dict[str, int] = {}
    if "cache" in args.stages:
        counts["cache_check"] = 2
    if "ee" in args.stages:
        n = 0
        for doc in args.docs:
            lines, _, candidates = _doc_inputs(doc)
            windows, _ = build_windows(lines, candidates)
            n += len(windows)
        counts["E-E"] = n * args.rep_ee
    n_pairs = -1
    if {"ec", "ea"} & set(args.stages):
        data_path = Path(args.data)
        if data_path.exists():
            data = json.loads(data_path.read_text(encoding="utf-8"))
            n_pairs = sum(1 for lb in data["labels"] if lb["doc"] == "window")
    if "ec" in args.stages:
        counts["E-C"] = (
            -(-n_pairs // args.q) * len(QUESTION_VARIANTS) * args.rep_ec
            if n_pairs >= 0
            else -1
        )
    if "ea" in args.stages:
        counts["E-A"] = (
            sum(-(-n_pairs // q) for q in args.ea_qs) * args.rep_ea
            if n_pairs >= 0
            else -1
        )
    if "eb" in args.stages:
        lines, _, candidates = _doc_inputs(args.eb_doc)
        windows, _ = build_windows(lines, candidates)
        n_pairs_eb = sum(
            len(w.pairs)
            for w in windows
            if w.pairs and w.pairs[0][0] == EB_SPAN[0] and w.pairs[-1][1] == EB_SPAN[1]
        )
        counts["E-B"] = (
            -(-n_pairs_eb // args.q) * len(args.eb_levels) * args.rep_eb
            if n_pairs_eb
            else -1
        )
    if {"crit", "choice"} & set(args.stages):
        lines, _, candidates = _doc_inputs(args.eb_doc)
        try:
            w_eb = _eb_window(lines, candidates, EB_SPAN)
        except ValueError:
            w_eb = None
        if "crit" in args.stages:
            n_crit = -1
            lex_path = Path(args.labels_ext)
            if w_eb is not None and lex_path.exists():
                lex = json.loads(lex_path.read_text(encoding="utf-8"))
                pair_set = set(w_eb.pairs)
                n_crit = sum(
                    1
                    for lb in lex["labels"]
                    if lb.get("doc") == "plan_eb" and (lb["i"], lb["j"]) in pair_set
                )
            counts["E-CRIT"] = (
                -(-n_crit // args.q) * len(ECRIT_VARIANTS) * args.rep_ecrit
                if n_crit > 0
                else -1
            )
        if "choice" in args.stages:
            counts["E-CHOICE"] = (
                -(-len(w_eb.pairs) // args.eb_group_size) * args.rep_echoice
                if w_eb is not None
                else -1
            )
    if "choice2" in args.stages:
        # 9問(1問1req)。4/9 以上なら対照3reqが追加される(最大12)
        counts["E-CHOICE2"] = sum(ECHOICE2_ALLOC.values()) + ECHOICE2_CONTROL_N
    counts["total"] = sum(v for v in counts.values() if v > 0)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(
        description="boundary segmentation eval (issue #2 stage 1)"
    )
    ap.add_argument("--docs", nargs="*", default=list(DEFAULT_DOCS))
    ap.add_argument("--data", default=DEFAULT_DATA, help="E-C/E-A 固定窓データ JSON")
    ap.add_argument("--out", default="eval_boundaries.json")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument(
        "--stages",
        nargs="+",
        choices=["cache", "ee", "ec", "ea", "eb", "crit", "choice", "choice2"],
        default=["cache", "ee", "ec", "ea"],
    )
    ap.add_argument("--rep-ee", type=int, default=R_EE)
    ap.add_argument("--rep-ec", type=int, default=R_EC)
    ap.add_argument("--rep-ea", type=int, default=R_EA)
    ap.add_argument("--rep-eb", type=int, default=R_EB)
    ap.add_argument("--rep-ecrit", type=int, default=R_ECRIT)
    ap.add_argument("--rep-echoice", type=int, default=R_ECHOICE)
    ap.add_argument("--eb-doc", default=DEFAULT_DOCS[0], help="E-B 系の対象文書")
    ap.add_argument(
        "--labels-ext",
        default=DEFAULT_LABELS_EXT,
        help="E-CRIT の plan_eb ラベル JSON",
    )
    ap.add_argument(
        "--eb-group-size",
        type=int,
        default=EB_GROUP_SIZE,
        help="E-CHOICE のペア群サイズ",
    )
    ap.add_argument(
        "--eb-levels",
        type=int,
        nargs="+",
        default=list(EB_LEVELS),
        help="E-B の state 文字数水準",
    )
    ap.add_argument("--q", type=int, default=EC_PAIRS, help="E-C のペア数/req")
    ap.add_argument("--ea-qs", type=int, nargs="+", default=list(EA_QS))
    ap.add_argument(
        "--plan",
        action="store_true",
        help="API を呼ばずリクエスト数見積りのみ表示",
    )
    args = ap.parse_args()

    if args.plan:
        for k, v in plan(args).items():
            print(f"  {k}: {v} requests" if v >= 0 else f"  {k}: (data missing)")
        return 0

    if not os.environ.get("TYPESAFE_API_KEY"):
        print("TYPESAFE_API_KEY が未設定です", file=sys.stderr)
        return 2
    out = asyncio.run(run_all(args))
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    cc = out.get("cache_check")
    if cc:
        print(f"cache_check identical: {cc.get('identical')}")
    n_req = sum(r["n_requests"] for r in out["runs"])
    n_discard = sum(1 for r in out["runs"] if r["discard"])
    print(f"runs: {len(out['runs'])}, requests: {n_req}, discard: {n_discard}")
    print(f"raw dump: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
