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
from jevapan.models import Limits, LintResult
from jevapan.pipeline import lint_text
from jevapan.report import exit_code, fail_under_exit_code, render_human, render_json
from jevapan.ruleset import load_effective_ruleset


def _load_dotenv(path: Path) -> None:
    """path の .env を簡易パースして os.environ に読み込む(issue #10)。

    受理する記法は最小サブセットのみ: KEY=VALUE・空行・# コメント行・
    export 接頭辞・単純な引用符('...' / "...")。キーは ASCII 識別子
    のみとし、妥当でないキーの行は無視する。引用符なし値の " #" 以降・
    閉じ引用符以降はコメントとして除去し、閉じ引用符の無い行は
    壊れた値を入れず無視する。複数行値・変数展開・
    エスケープシーケンス等は対応しない割り切り。既存の環境変数は
    上書きしない(環境変数優先)。ファイルが無ければ何もしない。
    BOM 付きでも読める。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not (key.isascii() and key.isidentifier()):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            if end == -1:
                continue
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = value


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
    ".claude",
    ".devin",
    ".agents",
    ".cursor",
)


def _is_excluded(
    f: Path, root: Path, excludes: Sequence[str], use_defaults: bool
) -> bool:
    """走査で見つけたファイル f が除外対象か。既定除外は走査ルート相対の
    各パス要素に、--exclude パターンは走査ルート相対・cwd 相対・basename
    の和集合に fnmatchcase でマッチさせる(* は / をまたぐ)。
    明示指定のファイル・ディレクトリ(走査ルート自身)には適用しない。"""
    rel = f.relative_to(root)
    if use_defaults and any(
        fnmatch.fnmatchcase(part, pat) for part in rel.parts for pat in DEFAULT_EXCLUDES
    ):
        return True
    # 走査ルート相対: パス全体・各要素(basename 含む)・祖先ディレクトリ
    cands = {rel.as_posix(), *rel.parts}
    cands.update(p.as_posix() for p in rel.parents if p.as_posix() != ".")
    # cwd 相対でも同様にマッチさせる(どちらの書き方でも効く)
    rel_cwd = Path(os.path.relpath(f, Path.cwd()))
    cands.add(rel_cwd.as_posix())
    cands.update(rel_cwd.parts)
    cands.update(p.as_posix() for p in rel_cwd.parents if p.as_posix() != ".")
    return any(fnmatch.fnmatchcase(c, pat) for c in cands for pat in excludes)


def _iter_inputs(
    paths: list[str],
    recursive: bool,
    excludes: Sequence[str] = (),
    use_defaults: bool = True,
) -> list[tuple[str, str]]:
    """(label, text) のリストを返す。'-' は stdin。
    除外はディレクトリ走査でのみ適用し、明示指定のファイル・
    ディレクトリ(走査ルート自身)には適用しない。"""
    out: list[tuple[str, str]] = []
    for p in paths:
        if p == "-":
            out.append(("<stdin>", sys.stdin.read()))
            continue
        path = Path(p)
        if path.is_dir():
            if not recursive:
                raise ValueError(f"{p} is a directory; pass -r to recurse")
            n_excluded = 0
            for f in sorted(path.rglob("*.md")) + sorted(path.rglob("*.txt")):
                if _is_excluded(f, path, excludes, use_defaults):
                    n_excluded += 1
                    continue
                out.append((str(f), f.read_text(encoding="utf-8")))
            if n_excluded:
                print(f"jvp: warning: {p}: 除外 {n_excluded} 件", file=sys.stderr)
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
    limit_args = {
        "score_state": ("--score-state-limit", args.score_state_limit),
        "locate_state": ("--locate-state-limit", args.locate_state_limit),
        "doc_state": ("--doc-state-limit", args.doc_state_limit),
        "window_state": ("--window-state-limit", args.window_state_limit),
    }
    for flag, v in limit_args.values():
        if v is not None and v < 1:
            print(f"jevapan: {flag} must be >= 1", file=sys.stderr)
            return 2
    limits = Limits(**{k: v for k, (_, v) in limit_args.items() if v is not None})
    # cwd の .env を自動読込する(秘密値の先祖遡及はしない。issue #10)
    _load_dotenv(Path.cwd() / ".env")
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
        inputs = _iter_inputs(
            args.paths,
            args.recursive,
            args.exclude,
            use_defaults=not args.no_default_excludes,
        )
    except (ValueError, OSError) as e:
        print(f"jevapan: {e}", file=sys.stderr)
        return 2
    engine = _make_engine(args.concurrency)
    results = await asyncio.gather(
        *[lint_text(engine, text, ruleset, label, limits) for label, text in inputs]
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
        help=(
            "ディレクトリ走査から除外するパターン(走査ルートまたは "
            "cwd からの相対パス/名前にマッチ。* は / をまたぐ。複数指定可)"
        ),
    )
    chk.add_argument(
        "--no-default-excludes",
        action="store_true",
        help=(
            "既定の除外セット(.git/_build/node_modules 等)を無効化。"
            "--exclude は引き続き有効"
        ),
    )
    chk.add_argument("--ruleset", default=None)
    chk.add_argument("--config", default=None, help="jevapan.yaml path")
    chk.add_argument("--format", choices=["auto", "human", "json"], default="auto")
    chk.add_argument("--concurrency", type=int, default=20)
    chk.add_argument("--fail-under", type=float, default=None)
    chk.add_argument(
        "--score-state-limit",
        type=int,
        default=None,
        help="block 採点ペイロードの文字数上限(既定 32000)",
    )
    chk.add_argument(
        "--locate-state-limit",
        type=int,
        default=None,
        help="locate ペイロードの文字数上限(既定 32000)",
    )
    chk.add_argument(
        "--doc-state-limit",
        type=int,
        default=None,
        help="document scope の文字数上限(既定 32000)",
    )
    chk.add_argument(
        "--window-state-limit",
        type=int,
        default=None,
        help="境界判定ウィンドウの文字数上限(既定 16000)",
    )
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
