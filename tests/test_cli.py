import io
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from jevapan.cli import _iter_inputs, main


def test_check_requires_file() -> None:
    with pytest.raises(SystemExit):  # argparse が usage エラーで終了
        main(["check"])


def test_check_stdin_json(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr("sys.stdin", io.StringIO("# doc\n\ntext\n"))
    fake = AsyncMock()
    fake.summary = {"blocks": 1, "violations": 0, "errors": 0}
    fake.violations = []
    fake.skipped = []
    fake.blocks = []
    fake.block_scores = []
    fake.doc_scores = {}
    fake.file = "<stdin>"
    with (
        patch("jevapan.cli._make_engine") as me,
        patch("jevapan.cli.lint_text", AsyncMock(return_value=fake)),
    ):
        me.return_value = AsyncMock()
        assert main(["check", "-", "--format", "json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["files"][0]["file"] == "<stdin>"


def test_check_warns_skipped_on_stderr(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr("sys.stdin", io.StringIO("text\n"))
    fake = AsyncMock()
    fake.summary = {"blocks": 1, "violations": 0, "errors": 0}
    fake.violations = []
    fake.skipped = [
        {"category": "consistency", "reason": "state_too_large"},
        {"stage": "segment", "reason": "window_state_too_large", "lines": [10, 12]},
    ]
    fake.blocks = []
    fake.block_scores = []
    fake.doc_scores = {}
    fake.file = "<stdin>"
    with (
        patch("jevapan.cli._make_engine") as me,
        patch("jevapan.cli.lint_text", AsyncMock(return_value=fake)),
    ):
        me.return_value = AsyncMock()
        assert main(["check", "-", "--format", "json"]) == 0
        err = capsys.readouterr().err
        assert "consistency" in err and "state_too_large" in err
        assert "L10-L12" in err


def test_check_missing_api_key(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert main(["check", "-"]) == 2


def test_check_concurrency_zero_is_validation_error(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    assert main(["check", "-", "--concurrency", "0"]) == 2
    assert "concurrency" in capsys.readouterr().err


def test_check_concurrency_negative_is_validation_error(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    assert main(["check", "-", "--concurrency", "-1"]) == 2
    assert "concurrency" in capsys.readouterr().err


def _mkfile(path: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def test_iter_inputs_excludes_generated_dirs(tmp_path: Any) -> None:
    """既定除外: VCS・ビルド生成物・仮想環境・キャッシュのディレクトリ名が
    パス要素に現れたら配下を lint しない(issue #9: _build 配下の
    Sphinx 生成物が 824 calls/131万 tokens を消費した実害)。"""
    _mkfile(tmp_path / "keep" / "f.md")
    for d in (
        "_build/html/_sources",
        "node_modules/pkg",
        ".git",
        ".venv/lib",
        "dist",
        "__pycache__",
        "x.egg-info",
        ".claude",
        ".devin",
        ".agents",
        ".cursor",
    ):
        _mkfile(tmp_path / d / "f.md")
        _mkfile(tmp_path / d / "f.txt")
    out = _iter_inputs([str(tmp_path)], True)
    assert [label for label, _ in out] == [str(tmp_path / "keep" / "f.md")]


def test_iter_inputs_exclude_glob_matches_rel_and_basename(
    tmp_path: Any,
) -> None:
    """--exclude GLOB は入力ディレクトリからの相対パスと basename
    (パス要素)の両方に fnmatch でマッチする。深い階層にも効く。"""
    _mkfile(tmp_path / "docs" / "superpowers" / "design.md")
    _mkfile(tmp_path / "docs" / "guide.md")
    _mkfile(tmp_path / "sub" / "deep" / "gen.md.txt")
    _mkfile(tmp_path / "sub" / "note.txt")
    out = _iter_inputs([str(tmp_path)], True, excludes=["docs/superpowers", "*.md.txt"])
    assert sorted(label for label, _ in out) == [
        str(tmp_path / "docs" / "guide.md"),
        str(tmp_path / "sub" / "note.txt"),
    ]


def test_iter_inputs_explicit_file_bypasses_excludes(tmp_path: Any) -> None:
    """明示指定したファイルは既定除外・--exclude を適用しない
    (ユーザ意図を優先)。"""
    _mkfile(tmp_path / "node_modules" / "x.md")
    out = _iter_inputs([str(tmp_path / "node_modules" / "x.md")], False)
    assert [label for label, _ in out] == [str(tmp_path / "node_modules" / "x.md")]
    out = _iter_inputs(
        [str(tmp_path / "node_modules" / "x.md")], False, excludes=["*.md"]
    )
    assert len(out) == 1


def test_iter_inputs_warns_excluded_count(tmp_path: Any, capsys: Any) -> None:
    """走査で除外したファイル数を stderr に「除外 N 件」と警告する
    (除外の可観測化)。"""
    _mkfile(tmp_path / "keep" / "f.md")
    _mkfile(tmp_path / "_build" / "a.md")
    _mkfile(tmp_path / "_build" / "b.md")
    _mkfile(tmp_path / "node_modules" / "c.txt")
    out = _iter_inputs([str(tmp_path)], True)
    assert [label for label, _ in out] == [str(tmp_path / "keep" / "f.md")]
    assert "除外 3 件" in capsys.readouterr().err


def test_iter_inputs_no_default_excludes(tmp_path: Any) -> None:
    """use_defaults=False で既定除外を無効化できる。--exclude は
    引き続き効く。"""
    _mkfile(tmp_path / "_build" / "f.md")
    _mkfile(tmp_path / "keep" / "g.md")
    out = _iter_inputs([str(tmp_path)], True, use_defaults=False)
    assert len(out) == 2
    out = _iter_inputs([str(tmp_path)], True, excludes=["*.md"], use_defaults=False)
    assert out == []


def test_iter_inputs_exclude_matches_cwd_relative(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """--exclude は走査ルート相対と cwd 相対の両方にマッチする。"""
    _mkfile(tmp_path / "sub" / "skip" / "a.md")
    _mkfile(tmp_path / "sub" / "keep" / "b.md")
    monkeypatch.chdir(tmp_path)
    # cwd 相対 'sub/skip' でも効く(走査ルート相対だけでは効かない書き方)
    out = _iter_inputs(["sub"], True, excludes=["sub/skip"])
    assert [label for label, _ in out] == ["sub/keep/b.md"]


def test_iter_inputs_explicit_dir_bypasses_excludes(tmp_path: Any) -> None:
    """明示指定のディレクトリ(走査ルート自身)は除外対象にならない。
    node_modules を直接指定すれば中身は lint される。"""
    _mkfile(tmp_path / "node_modules" / "x.md")
    out = _iter_inputs([str(tmp_path / "node_modules")], True)
    assert len(out) == 1


def test_check_no_default_excludes_flag(monkeypatch: Any, tmp_path: Any) -> None:
    """check --no-default-excludes で既定除外が無効化される。"""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    _mkfile(tmp_path / "_build" / "a.md")
    _mkfile(tmp_path / "b.md")
    fake = AsyncMock()
    fake.summary = {"blocks": 1, "violations": 0, "errors": 0}
    fake.violations = []
    fake.skipped = []
    fake.blocks = []
    fake.block_scores = []
    fake.doc_scores = {}
    seen: list[str] = []

    async def spy(engine: Any, text: str, ruleset: Any, label: str) -> Any:
        seen.append(label)
        fake.file = label
        return fake

    with (
        patch("jevapan.cli._make_engine") as me,
        patch("jevapan.cli.lint_text", spy),
    ):
        me.return_value = AsyncMock()
        assert (
            main(
                [
                    "check",
                    str(tmp_path),
                    "-r",
                    "--format",
                    "json",
                    "--no-default-excludes",
                ]
            )
            == 0
        )
    assert str(tmp_path / "_build" / "a.md") in seen


def test_check_exclude_option(monkeypatch: Any, tmp_path: Any) -> None:
    """check --exclude が収集に反映される(複数指定可)。"""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    _mkfile(tmp_path / "a.md")
    _mkfile(tmp_path / "skip" / "b.md")
    fake = AsyncMock()
    fake.summary = {"blocks": 1, "violations": 0, "errors": 0}
    fake.violations = []
    fake.skipped = []
    fake.blocks = []
    fake.block_scores = []
    fake.doc_scores = {}
    seen: list[str] = []

    async def spy(engine: Any, text: str, ruleset: Any, label: str) -> Any:
        seen.append(label)
        fake.file = label
        return fake

    with (
        patch("jevapan.cli._make_engine") as me,
        patch("jevapan.cli.lint_text", spy),
    ):
        me.return_value = AsyncMock()
        assert (
            main(
                [
                    "check",
                    str(tmp_path),
                    "-r",
                    "--format",
                    "json",
                    "--exclude",
                    "skip",
                ]
            )
            == 0
        )
    assert seen == [str(tmp_path / "a.md")]
