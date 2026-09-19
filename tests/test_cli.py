import io
import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from jevapan.cli import _load_dotenv, main


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


def test_load_dotenv_parses_supported_syntax(monkeypatch: Any, tmp_path: Any) -> None:
    """KEY=VALUE・空行・# コメント・export 接頭辞・単純引用符を受理(issue #10)。"""
    env: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", env)
    (tmp_path / ".env").write_text(
        "# コメント行\n"
        "\n"
        "TYPESAFE_API_KEY=abc123\n"
        "export OTHER=v1\n"
        'DQUOTED="a b # c"\n'
        "SQUOTED='x'\n"
        "TRAIL=v2 # 行末コメント\n"
        "NO_EQUALS\n"
        "=nokey\n",
        encoding="utf-8",
    )
    _load_dotenv(tmp_path / ".env")
    assert env == {
        "TYPESAFE_API_KEY": "abc123",
        "OTHER": "v1",
        "DQUOTED": "a b # c",
        "SQUOTED": "x",
        "TRAIL": "v2",
    }


def test_load_dotenv_does_not_override_existing_env(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """既存の環境変数は .env で上書きしない(環境変数優先)。"""
    env = {"TYPESAFE_API_KEY": "orig"}
    monkeypatch.setattr(os, "environ", env)
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=from-dotenv\n")
    _load_dotenv(tmp_path / ".env")
    assert env["TYPESAFE_API_KEY"] == "orig"


def test_load_dotenv_missing_file_is_noop(monkeypatch: Any, tmp_path: Any) -> None:
    env: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", env)
    _load_dotenv(tmp_path / ".env")
    assert env == {}


def test_check_loads_dotenv_from_cwd(
    monkeypatch: Any, capsys: Any, tmp_path: Any
) -> None:
    """cwd の .env から TYPESAFE_API_KEY を拾って check が走る(issue #10)。"""
    env = dict(os.environ)
    env.pop("TYPESAFE_API_KEY", None)
    monkeypatch.setattr(os, "environ", env)
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("text\n"))
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
    assert env["TYPESAFE_API_KEY"] == "from-dotenv"
