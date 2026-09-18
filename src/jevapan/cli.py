import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from jevapan.engine import Engine
from jevapan.pipeline import lint_text
from jevapan.report import exit_code, render_human, render_json
from jevapan.ruleset import load_effective_ruleset


def _make_engine(concurrency: int) -> Engine:
    return Engine(
        client=AsyncTypeSafeClient(retry=RetryPolicy(max_retries=3)),
        sem=asyncio.Semaphore(concurrency),
    )


def _iter_inputs(paths: list[str], recursive: bool) -> list[tuple[str, str]]:
    """(label, text) のリストを返す。'-' は stdin。"""
    out: list[tuple[str, str]] = []
    for p in paths:
        if p == "-":
            out.append(("<stdin>", sys.stdin.read()))
            continue
        path = Path(p)
        if path.is_dir():
            if not recursive:
                raise ValueError(f"{p} is a directory; pass -r to recurse")
            for f in sorted(path.rglob("*.md")) + sorted(path.rglob("*.txt")):
                out.append((str(f), f.read_text(encoding="utf-8")))
        else:
            out.append((str(path), path.read_text(encoding="utf-8")))
    return out


async def _run(args: argparse.Namespace) -> int:
    if not os.environ.get("TYPESAFE_API_KEY"):
        print(
            "TYPESAFE_API_KEY が未設定です。環境変数に設定してください",
            file=sys.stderr,
        )
        return 2
    try:
        ruleset = load_effective_ruleset(
            args.ruleset,
            Path.cwd(),
            config_path=Path(args.config) if args.config else None,
        )
        inputs = _iter_inputs(args.paths, args.recursive)
    except (ValueError, OSError) as e:
        print(f"jevapan: {e}", file=sys.stderr)
        return 2
    engine = _make_engine(args.concurrency)
    results = await asyncio.gather(
        *[lint_text(engine, text, ruleset, label) for label, text in inputs]
    )
    if args.format == "json" or (args.format == "auto" and not sys.stdout.isatty()):
        print(
            json.dumps({"files": [render_json(r) for r in results]}, ensure_ascii=False)
        )
    else:
        for r in results:
            print(render_human(r))
    if args.fail_under is not None:
        worst = min(
            (
                s.score
                for r in results
                for sb in r.block_scores
                for s in sb.scores.values()
            ),
            default=3.0,
        )
        return 1 if worst < args.fail_under else 0
    return max((exit_code(r) for r in results), default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jvp")
    sub = parser.add_subparsers(dest="cmd")
    chk = sub.add_parser("check", help="lint files")
    chk.add_argument("paths", nargs="+", help="files, dirs, or '-' for stdin")
    chk.add_argument("-r", "--recursive", action="store_true")
    chk.add_argument("--ruleset", default=None)
    chk.add_argument("--config", default=None, help="jevapan.yaml path")
    chk.add_argument("--format", choices=["auto", "human", "json"], default="auto")
    chk.add_argument("--concurrency", type=int, default=20)
    chk.add_argument("--fail-under", type=float, default=None)
    args = parser.parse_args(argv)
    if args.cmd != "check":
        parser.print_help(sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args))
    except Exception as e:  # API エラー等は失敗として伝播
        print(f"jevapan: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
