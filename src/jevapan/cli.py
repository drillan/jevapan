import argparse
import asyncio
import fnmatch
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from jevapan.engine import Engine
from jevapan.models import LintResult
from jevapan.pipeline import lint_text
from jevapan.report import exit_code, fail_under_exit_code, render_human, render_json
from jevapan.ruleset import load_effective_ruleset


def _make_engine(concurrency: int) -> Engine:
    return Engine(
        client=AsyncTypeSafeClient(retry=RetryPolicy(max_retries=3)),
        sem=asyncio.Semaphore(concurrency),
    )


# ディレクトリ走査で既定除外するディレクトリ名のパターン(パス要素への
# fnmatch)。VCS・ビルド生成物・仮想環境・各種キャッシュ。生成物を
# lint してトークンを浪費する実害があった(issue #9)
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    "_build",
    "build",
    "dist",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "*.egg-info",
    ".worktrees",
)


def _is_excluded(rel: Path, excludes: Sequence[str]) -> bool:
    """走査で見つけた相対パスが除外対象か。既定除外は各パス要素に、
    --exclude パターンは相対パスと各パス要素(basename 含む)に
    fnmatch でマッチさせる。明示指定のファイルには適用しない。"""
    if any(
        fnmatch.fnmatch(part, pat) for part in rel.parts for pat in DEFAULT_EXCLUDES
    ):
        return True
    rel_posix = rel.as_posix()
    return any(
        fnmatch.fnmatch(rel_posix, pat)
        or any(fnmatch.fnmatch(part, pat) for part in rel.parts)
        # 'docs/superpowers' のような複数要素のディレクトリ指定が
        # 配下の全ファイルに効くよう祖先ディレクトリにもマッチさせる
        or any(fnmatch.fnmatch(parent.as_posix(), pat) for parent in rel.parents)
        for pat in excludes
    )


def _iter_inputs(
    paths: list[str], recursive: bool, excludes: Sequence[str] = ()
) -> list[tuple[str, str]]:
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
                if _is_excluded(f.relative_to(path), excludes):
                    continue
                out.append((str(f), f.read_text(encoding="utf-8")))
        else:
            out.append((str(path), path.read_text(encoding="utf-8")))
    return out


def _warn_skipped(results: list[LintResult]) -> None:
    """skipped エントリを stderr に警告として出す。出力本体に埋もれないようにする。"""
    for r in results:
        for s in r.skipped:
            target = s.get("category") or s.get("stage", "?")
            loc = f" L{s['lines'][0]}-L{s['lines'][1]}" if "lines" in s else ""
            print(
                f"jvp: warning: {r.file}: {target}{loc} skipped ({s['reason']})",
                file=sys.stderr,
            )


async def _run(args: argparse.Namespace) -> int:
    if args.concurrency < 1:
        print("jevapan: --concurrency must be >= 1", file=sys.stderr)
        return 2
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
        inputs = _iter_inputs(args.paths, args.recursive, args.exclude)
    except (ValueError, OSError) as e:
        print(f"jevapan: {e}", file=sys.stderr)
        return 2
    engine = _make_engine(args.concurrency)
    results = await asyncio.gather(
        *[lint_text(engine, text, ruleset, label) for label, text in inputs]
    )
    _warn_skipped(results)
    if args.format == "json" or (args.format == "auto" and not sys.stdout.isatty()):
        print(
            json.dumps({"files": [render_json(r) for r in results]}, ensure_ascii=False)
        )
    else:
        for r in results:
            print(render_human(r))
    if args.fail_under is not None:
        return fail_under_exit_code(results, args.fail_under)
    return max((exit_code(r) for r in results), default=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jvp")
    sub = parser.add_subparsers(dest="cmd")
    chk = sub.add_parser("check", help="lint files")
    chk.add_argument("paths", nargs="+", help="files, dirs, or '-' for stdin")
    chk.add_argument("-r", "--recursive", action="store_true")
    chk.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="ディレクトリ走査から除外するパターン(相対パス/名前、複数指定可)",
    )
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
