import io
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from jevapan.cli import main


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
