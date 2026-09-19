import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from jevapan.engine import Engine
from jevapan.models import LintResult
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
        inputs = _iter_inputs(args.paths, args.recursive)
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
