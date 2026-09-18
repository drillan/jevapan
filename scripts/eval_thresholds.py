"""locate の生確率を全候補分ダンプし、閾値ごとの採用件数を比較する。

noul_batch をラップして locate 呼び出し(質問文に "Candidate:" を含む)を
記録し、各閾値(0.5/0.6/0.7/0.8)で flag される候補数を集計する。
生確率・model・usage は JSON に保存する(閾値評価の再現性のため)。

使い方:
    TYPESAFE_API_KEY=... uv run python scripts/eval_thresholds.py samples/bad.md
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from jevapan.cli import _make_engine
from jevapan.pipeline import lint_text
from jevapan.ruleset import Ruleset, load_effective_ruleset

THRESHOLDS = [0.5, 0.6, 0.7, 0.8]


async def run(args: argparse.Namespace, ruleset: Ruleset) -> dict[str, Any]:
    engine = _make_engine(args.concurrency)
    records: list[dict[str, Any]] = []
    orig = engine.noul_batch

    async def rec(state: Any, questions: dict[str, str]) -> Any:
        res = await orig(state, questions)
        # locate 呼び出しは質問文に "Candidate:" を含む(境界質問は除外)
        if any("Candidate:" in q for q in questions.values()):
            records.append(
                {
                    "model": res.model,
                    "usage": asdict(res.usage),
                    "scope": (
                        "document"
                        if isinstance(state, dict) and "document" in state
                        else "block"
                    ),
                    "questions": questions,
                    "probs": res.probs,
                }
            )
        return res

    engine.noul_batch = rec  # type: ignore[method-assign]
    locate_of = {c.locate: c.name for c in ruleset.categories if c.locate}

    out: dict[str, Any] = {"files": []}
    for p in args.paths:
        records.clear()
        result = await lint_text(
            engine, Path(p).read_text(encoding="utf-8"), ruleset, p
        )
        candidates: list[dict[str, Any]] = []
        for r in records:
            instr = next(iter(r["questions"].values())).split(" Candidate:", 1)[0]
            category = locate_of.get(instr, "?")
            for qid, prob in sorted(r["probs"].items()):
                candidates.append(
                    {
                        "category": category,
                        "scope": r["scope"],
                        "question": r["questions"][qid],
                        "prob": round(prob, 4),
                    }
                )

        counts = {t: sum(1 for c in candidates if c["prob"] >= t) for t in THRESHOLDS}
        print(
            f"\n{p}: {len(result.violations)} violations, "
            f"{len(candidates)} locate candidates"
        )
        print("  all: " + "  ".join(f">={t}: {n}" for t, n in counts.items()))
        by_cat: dict[str, list[float]] = {}
        for c in candidates:
            by_cat.setdefault(c["category"], []).append(c["prob"])
        for cat, probs in sorted(by_cat.items()):
            line = " ".join(
                f">={t}:{sum(1 for x in probs if x >= t)}" for t in THRESHOLDS
            )
            print(f"  {cat}: {line} (n={len(probs)})")

        out["files"].append(
            {
                "file": p,
                "violations": len(result.violations),
                "threshold_counts": {str(t): n for t, n in counts.items()},
                "candidates": candidates,
                "calls": [
                    {"model": r["model"], "usage": r["usage"], "scope": r["scope"]}
                    for r in records
                ],
            }
        )
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
