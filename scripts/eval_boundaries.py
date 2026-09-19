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
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from itertools import batched
from pathlib import Path
from typing import Any

from jevapan.cli import _make_engine
from jevapan.engine import Engine, NoulResult
from jevapan.mask import analyze_syntax
from jevapan.segmenter import (
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
EC_PAIRS = 20
EA_QS = (1, 5, 20)


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
    data = None
    if {"ec", "ea"} & stages:
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
        choices=["cache", "ee", "ec", "ea"],
        default=["cache", "ee", "ec", "ea"],
    )
    ap.add_argument("--rep-ee", type=int, default=R_EE)
    ap.add_argument("--rep-ec", type=int, default=R_EC)
    ap.add_argument("--rep-ea", type=int, default=R_EA)
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
