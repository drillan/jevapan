import json
import os
from typing import Any

import pytest

from jevapan.cli import main

pytestmark = pytest.mark.skipif(
    not os.environ.get("TYPESAFE_API_KEY"), reason="requires TYPESAFE_API_KEY"
)


def test_bad_doc_flags_violations(capsys: Any) -> None:
    assert main(["check", "samples/bad.md", "--format", "json"]) in (0, 1)
    out = json.loads(capsys.readouterr().out)
    violations = [v for f in out["files"] for v in f["violations"]]
    assert len(violations) > 0
    # 正解カテゴリの存在確認: bad.md は substance/structure の違反を含む
    assert {v["category"] for v in violations} & {"substance", "structure"}


def test_good_doc_mostly_clean(capsys: Any) -> None:
    main(["check", "samples/good.md", "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert out["files"][0]["violations"] == []
