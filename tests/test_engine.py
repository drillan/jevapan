from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from jevapan.engine import Engine


def _resp(answers: dict[str, Any]) -> Any:
    return SimpleNamespace(answers=answers)


async def test_noul_returns_probability() -> None:
    client = AsyncMock()
    client.system_one = AsyncMock(return_value=_resp({"q": SimpleNamespace(noul=0.7)}))
    eng = Engine(client=client, sem=None)
    assert await eng.noul({"doc": "x"}, "is it good?") == 0.7
    _, kw = client.system_one.call_args
    assert kw["state"] == {"doc": "x"}


async def test_score_batch_splits_over_limit() -> None:
    client = AsyncMock()
    qs = {f"q{i}": ("instr", ["a", "b"]) for i in range(51)}
    eng = Engine(client=client, sem=None)
    client.system_one.side_effect = [
        _resp({k: SimpleNamespace(score=1.0, confidence=0.5) for k in list(qs)[:50]}),
        _resp({"q50": SimpleNamespace(score=2.0, confidence=0.9)}),
    ]
    out = await eng.score_batch("doc", qs)
    assert client.system_one.call_count == 2
    assert out["q50"].score == 2.0
