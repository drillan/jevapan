"""locate の生確率を「locate を実行した全候補」分ダンプし、閾値ごとの
採用件数を比較する。

注意: 記録される候補は score ゲート(score < threshold)を通過して
locate が実行されたものに限定される。ゲートで落ちた候補は含まれない
(score ゲートの recall を測定するには別途正解集合が必要)。

- noul_batch をラップし、locator の LOCATE_META contextvar で
  カテゴリ名・scope・候補の原文位置をメタデータとして受け取る
  (質問文の逆引きは同名 locate を持つカテゴリを区別できないため使わない)
- 判定は生確率(丸めは表示のみ)。block/document の同一候補は
  (行範囲, col, text, category) で統合した違反件数も別に出力する
- 生確率・候補本文・原文位置・model・usage・入力hash は JSON に保存する

使い方:
    TYPESAFE_API_KEY=... uv run python scripts/eval_thresholds.py samples/bad.md
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from jevapan.cli import _make_engine
from jevapan.locator import LOCATE_META
from jevapan.pipeline import lint_text
from jevapan.ruleset import Ruleset, load_effective_ruleset

THRESHOLDS = [0.5, 0.6, 0.7, 0.8]

Candidate = dict[str, Any]


def merged_flag_counts(
    cands: list[Candidate], thresholds: list[float]
) -> dict[float, tuple[int, int]]:
    """各閾値で (採用質問件数, block/document 統合後の違反件数) を返す。
    判定は生確率(丸めない)。統合キーは pipeline と同じ
    (行範囲, col, text, category) で probability は大きい方。"""
    out: dict[float, tuple[int, int]] = {}
    for t in thresholds:
        raw = sum(1 for c in cands if c["prob"] >= t)
        merged_probs: dict[tuple[Any, ...], float] = {}
        for c in cands:
            key = (c["start"], c["end"], c["col"], c["text"], c["category"])
            merged_probs[key] = max(merged_probs.get(key, 0.0), c["prob"])
        out[t] = (raw, sum(1 for p in merged_probs.values() if p >= t))
    return out


async def run(args: argparse.Namespace, ruleset: Ruleset) -> dict[str, Any]:
    engine = _make_engine(args.concurrency)
    records: list[dict[str, Any]] = []
    call_id = 0
    orig = engine.noul_batch

    async def rec(state: Any, questions: dict[str, str]) -> Any:
        nonlocal call_id
        res = await orig(state, questions)
        meta = LOCATE_META.get()
        if meta is not None:
            call_id += 1
            category, scope, cands = meta
            records.append(
                {
                    "call_id": call_id,
                    "category": category,
                    "scope": scope,
                    "model": res.model,
                    "usage": asdict(res.usage),
                    "cands": cands,
                    "probs": res.probs,
                }
            )
        return res

    engine.noul_batch = rec  # type: ignore[method-assign]

    out: dict[str, Any] = {"files": []}
    for p in args.paths:
        records.clear()
        text = Path(p).read_text(encoding="utf-8")
        result = await lint_text(engine, text, ruleset, p)

        candidates: list[Candidate] = []
        for r in records:
            for i, (lr, col, t) in enumerate(r["cands"]):
                candidates.append(
                    {
                        "call_id": r["call_id"],
                        "category": r["category"],
                        "scope": r["scope"],
                        "start": lr[0],
                        "end": lr[1],
                        "col": col,
                        "text": t,
                        "prob": r["probs"][f"s{i}"],
                    }
                )

        counts = merged_flag_counts(candidates, THRESHOLDS)
        print(
            f"\n{p}: {len(result.violations)} violations, "
            f"{len(candidates)} locate candidates"
        )
        print("  (candidates = score ゲートを通過し locate が実行されたものに限定)")
        print("  threshold: flagged questions / merged violations")
        for t in THRESHOLDS:
            raw, merged = counts[t]
            print(f"  >= {t}: {raw} / {merged}")
        by_cat: dict[str, list[Candidate]] = {}
        for c in candidates:
            by_cat.setdefault(c["category"], []).append(c)
        for cat, rows in sorted(by_cat.items()):
            cc = merged_flag_counts(rows, THRESHOLDS)
            line = "  ".join(f">={t}:{cc[t][0]}/{cc[t][1]}" for t in THRESHOLDS)
            print(f"  {cat}: {line} (n={len(rows)})")

        out["files"].append(
            {
                "file": p,
                "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "violations": len(result.violations),
                "threshold_counts": {
                    str(t): {"questions": counts[t][0], "merged": counts[t][1]}
                    for t in THRESHOLDS
                },
                "candidates": candidates,
                "calls": [
                    {
                        k: r[k]
                        for k in ("call_id", "category", "scope", "model", "usage")
                    }
                    for r in records
                ],
            }
        )
    out["ruleset"] = args.ruleset or "base"
    out["thresholds"] = THRESHOLDS
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="dump locate probabilities and compare thresholds"
    )
    ap.add_argument("paths", nargs="+", help="markdown files to lint")
    ap.add_argument("--ruleset", default=None, help="preset name or yaml path")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--out", default="eval_dump.json", help="raw dump output path")
    args = ap.parse_args()

    if not os.environ.get("TYPESAFE_API_KEY"):
        print("TYPESAFE_API_KEY が未設定です", file=sys.stderr)
        return 2
    ruleset = load_effective_ruleset(args.ruleset, Path.cwd())
    out = asyncio.run(run(args, ruleset))
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nraw dump: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
