from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from jevapan.engine import Engine


def _resp(answers: dict[str, Any], model: str = "", usage: Any = None) -> Any:
    return SimpleNamespace(answers=answers, model=model, usage=usage)


async def test_noul_returns_probability() -> None:
    client = AsyncMock()
    client.system_one = AsyncMock(return_value=_resp({"q": SimpleNamespace(noul=0.7)}))
    eng = Engine(client=client, sem=None)
    res = await eng.noul({"doc": "x"}, "is it good?")
    assert res.probs["q"] == 0.7
    _, kw = client.system_one.call_args
    assert kw["state"] == {"doc": "x"}


async def test_noul_batch_keeps_model_and_usage() -> None:
    """応答の model と usage が NoulResult に保持される(閾値評価の再現性)。"""
    client = AsyncMock()
    client.system_one = AsyncMock(
        return_value=_resp(
            {"q": SimpleNamespace(noul=0.7)},
            model="jev-1.13",
            usage=SimpleNamespace(input_tokens=120, output_tokens=3),
        )
    )
    eng = Engine(client=client, sem=None)
    res = await eng.noul_batch({"doc": "x"}, {"q": "is it good?"})
    assert res.probs["q"] == 0.7
    assert res.model == "jev-1.13"
    assert res.usage.input_tokens == 120 and res.usage.output_tokens == 3


async def test_score_batch_keeps_model_and_usage() -> None:
    client = AsyncMock()
    client.system_one = AsyncMock(
        return_value=_resp(
            {"q": SimpleNamespace(score=1.0, confidence=0.5)},
            model="jev-1.13",
            usage=SimpleNamespace(input_tokens=50, output_tokens=4),
        )
    )
    eng = Engine(client=client, sem=None)
    out = await eng.score_batch("doc", {"q": ("instr", ["a", "b"])})
    assert out["q"].model == "jev-1.13"
    assert out["q"].usage.input_tokens == 50


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
