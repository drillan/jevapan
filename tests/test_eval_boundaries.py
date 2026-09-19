"""scripts/eval_boundaries.py の計測ロジックのテスト(issue #2 Stage 1)。
API 呼出しはモックの noul_batch に差し替え、実 API は使わない。"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from jevapan.engine import NoulResult, Usage


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "eval_boundaries",
        Path(__file__).parent.parent / "scripts" / "eval_boundaries.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeEngine:
    """noul_batch を差し替え可能な最小スタブ。"""

    def __init__(self, probs: dict[str, float] | None = None, model: str = "m1"):
        self._probs = probs or {}
        self._model = model
        self.calls: list[tuple[Any, dict[str, str]]] = []

    async def noul_batch(self, state: Any, questions: dict[str, str]) -> NoulResult:
        self.calls.append((state, questions))
        probs = self._probs or {k: 0.5 for k in questions}
        return NoulResult(probs=dict(probs), model=self._model, usage=Usage(1, 2))


async def test_cache_check_detects_identical_and_different() -> None:
    mod = _load()
    eng = _FakeEngine(probs={"b0": 0.42})
    res = await mod.cache_check(eng, {"lines": ["a", "b"]}, {"b0": "q?"})
    assert res["identical"] is True
    assert len(eng.calls) == 2  # 同一リクエストをちょうど2回

    seq = iter([0.1, 0.9])

    class Flaky(_FakeEngine):
        async def noul_batch(self, state: Any, questions: dict[str, str]) -> NoulResult:
            return NoulResult(probs={"b0": next(seq)}, model="m1", usage=Usage())

    res = await mod.cache_check(Flaky(), {"lines": ["a"]}, {"b0": "q?"})
    assert res["identical"] is False


async def test_ee_runs_repeated_and_records_requests(tmp_path: Any) -> None:
    """E-E: 現行設定の窓列を反復 R 回。run ごとに入力hash・各リクエストの
    state hash・質問・生確率・model・usage を記録する。"""
    mod = _load()
    doc = tmp_path / "doc.md"
    doc.write_text("\n".join(f"文{i}。" for i in range(4)), encoding="utf-8")
    eng = _FakeEngine()
    runs = await mod.exp_ee(eng, [str(doc)], r=2)
    assert len(runs) == 2  # rep ごとに run を分ける
    run = runs[0]
    assert run["experiment"] == "E-E" and run["rep"] == 0
    assert run["input_sha256"]
    assert run["requests"], "少なくとも1窓"
    rec = run["requests"][0]
    for key in (
        "state_sha256",
        "question_keys",
        "questions",
        "probs",
        "model",
        "usage",
    ):
        assert key in rec
    assert run["model_consistent"] is True and run["discard"] is False


async def test_mixed_model_run_is_marked_discard(tmp_path: Any) -> None:
    """run 内のリクエストで model が混在した場合は discard=true で
    マークする(データは残す)。"""
    mod = _load()

    class TwoModels(_FakeEngine):
        n = 0

        async def noul_batch(self, state: Any, questions: dict[str, str]) -> NoulResult:
            self.n += 1
            return NoulResult(
                probs={k: 0.1 for k in questions},
                model="m1" if self.n == 1 else "m2",
                usage=Usage(),
            )

    # 31ペア以上 → 1 rep 内に複数窓(複数リクエスト)が発生する文書
    doc = tmp_path / "doc.md"
    doc.write_text("\n".join(f"文{i}。" for i in range(40)), encoding="utf-8")
    eng = TwoModels()
    runs = await mod.exp_ee(eng, [str(doc)], r=1)
    assert len(runs[0]["requests"]) == 2
    assert runs[0]["model_consistent"] is False
    assert runs[0]["discard"] is True


async def test_ec_variants_and_chunking(tmp_path: Any) -> None:
    """E-C: 質問文5案 × Q=20 固定 × R 回。variant 名と質問文を記録し、
    否定形は polarity を反転メタデータとして記録する。"""
    mod = _load()
    doc = tmp_path / "doc.md"
    # 21ペア以上 → Q=20 で2窓に割れる
    doc.write_text("\n\n".join(f"段落{i}。" for i in range(22)), encoding="utf-8")
    eng = _FakeEngine()
    runs = await mod.exp_ec(eng, [str(doc)], r=2, q=20)
    variants = {run["variant"] for run in runs}
    assert variants == set(mod.QUESTION_VARIANTS)
    neg = next(r for r in runs if r["variant"] == "negative")
    assert neg["polarity"] == -1
    cur = next(r for r in runs if r["variant"] == "current")
    assert cur["polarity"] == 1
    # rep ごとに run が分かれる
    assert sum(1 for r in runs if r["variant"] == "current") == 2


async def test_ea_chunks_pairs_by_q_and_keeps_state() -> None:
    """E-A: 固定窓 W の20ペアを Q∈{1,5,20} で分割。state は毎回
    W 全体で不変。ラベルは run の params に保存する。"""
    mod = _load()
    data = {
        "window": {"source": "w.md", "lines": [f"L{i}" for i in range(60)]},
        "labels": [
            {"doc": "window", "i": i, "j": i + 1, "label": "either"} for i in range(20)
        ]
        + [{"doc": "design", "i": 0, "j": 1, "label": "boundary"}],
    }
    eng = _FakeEngine()
    runs = await mod.exp_ea(eng, data, r=2, qs=(1, 5, 20))
    by_q: dict[int, list[dict[str, Any]]] = {}
    for run in runs:
        by_q.setdefault(run["params"]["q"], []).append(run)
    assert sorted(by_q) == [1, 5, 20]
    # Q=1→20 req, Q=5→4 req, Q=20→1 req、それぞれ R 回
    assert {q: len(by_q[q][0]["requests"]) for q in by_q} == {1: 20, 5: 4, 20: 1}
    assert all(len(v) == 2 for v in by_q.values())
    # state は常に W 全体(60行)
    req = runs[0]["requests"][0]
    assert len(req["state"]["lines"]) == 60
    # doc=="window" のラベルのみ対象(20ペア)
    assert len(runs[0]["params"]["labels"]) == 20


def test_state_hash_stable() -> None:
    mod = _load()
    h1 = mod.state_hash({"lines": ["a", "b"]})
    h2 = mod.state_hash({"lines": ["a", "b"]})
    h3 = mod.state_hash({"lines": ["a", "c"]})
    assert h1 == h2 and h1 != h3
