"""scripts/eval_boundaries.py の計測ロジックのテスト(issue #2 Stage 1)。
API 呼出しはモックの noul_batch に差し替え、実 API は使わない。"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from jevapan.engine import Engine, NoulResult, Usage


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


def _window_data(n_pairs: int = 20) -> dict[str, Any]:
    return {
        "window": {"source": "w.md", "lines": [f"L{i}" for i in range(60)]},
        "labels": [
            {"doc": "window", "i": i, "j": i + 1, "label": "either"}
            for i in range(n_pairs)
        ],
    }


async def test_ec_variants_on_fixed_window() -> None:
    """E-C: 固定窓 W のペアを Q 個ずつ分割し質問文5案 × R 回。
    state は毎回 W 全体で不変(E-A と同一素材)。否定形は
    polarity を反転メタデータとして記録する。"""
    mod = _load()
    eng = _FakeEngine()
    runs = await mod.exp_ec(eng, _window_data(20), r=2, q=20)
    variants = {run["variant"] for run in runs}
    assert variants == set(mod.QUESTION_VARIANTS)
    neg = next(r for r in runs if r["variant"] == "negative")
    assert neg["polarity"] == -1
    cur = next(r for r in runs if r["variant"] == "current")
    assert cur["polarity"] == 1
    # Q=20 → 1 req/run、variant ごとに rep 分の run
    assert cur["n_requests"] == 1
    assert len(cur["requests"][0]["state"]["lines"]) == 60
    assert sum(1 for r in runs if r["variant"] == "current") == 2
    # Q を小さくすると req が分割される
    runs = await mod.exp_ec(eng, _window_data(20), r=1, q=5)
    assert runs[0]["n_requests"] == 4


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


def test_shrink_state_lines_keeps_pair_lines_and_hits_budget(
    tmp_path: Any,
) -> None:
    mod = _load()
    # 各行20字×100行の文書、ペア端点は 10,20,30,40
    lines = [f"line-{k:03d}-" + "x" * 10 for k in range(100)]
    pair_lines = {10, 20, 30, 40}
    full = mod._shrink_state_lines(lines, pair_lines, 0, 99, 10**9)
    assert full == list(range(100))
    shrunk = mod._shrink_state_lines(lines, pair_lines, 0, 99, 500)
    assert set(pair_lines) <= set(shrunk)
    assert mod._state_chars([lines[k] for k in shrunk]) <= 500
    # 予算を絞るほど行数が単調に減る
    smaller = mod._shrink_state_lines(lines, pair_lines, 0, 99, 250)
    assert len(smaller) <= len(shrunk)
    # 極小予算でもペア端点は残る
    tiny = mod._shrink_state_lines(lines, pair_lines, 0, 99, 1)
    assert set(pair_lines) <= set(tiny)


async def test_eb_runs_levels_and_remaps_question_indices(
    tmp_path: Any, monkeypatch: Any
) -> None:
    mod = _load()
    # ペア span が (5, 14) の窓を作る: 行 0-19、ペア (5,7),(7,9),(12,14)
    doc = tmp_path / "doc.md"
    doc.write_text("\n".join(f"text {k}" for k in range(20)), encoding="utf-8")
    fake_window = type(
        "W", (), {"start": 0, "end": 19, "pairs": [(5, 7), (7, 9), (12, 14)]}
    )()
    monkeypatch.setattr(mod, "build_windows", lambda lines, cands: ([fake_window], []))
    monkeypatch.setattr(
        mod, "find_boundary_candidates", lambda lines, ex, s: [(5, 7), (7, 9), (12, 14)]
    )
    eng = _FakeEngine(probs={"b5": 0.9, "b7": 0.5, "b12": 0.3})
    runs = await mod.exp_eb(eng, str(doc), r=2, levels=[10**9, 120], q=2, span=(5, 14))
    assert len(runs) == 4  # 2 levels × 2 reps
    assert all(r["experiment"] == "E-B" for r in runs)
    big = runs[0]
    assert big["params"]["n_state_lines"] == 20
    # q=2 → 2 req/run、30ではなく3ペア → 2+1
    assert big["n_requests"] == 2
    small = runs[2]
    assert small["params"]["n_state_lines"] < 20
    # 質問 index は state 内位置に再写像される(キーは doc index のまま)
    q0 = small["requests"][0]["questions"]["b5"]
    assert "lines[0]" in q0  # ペア端点 5 は縮小 state の先頭付近
    # pairs メタは doc 行 index のまま保持
    assert small["requests"][0]["pairs"] == [[5, 7], [7, 9]]


class _FakeTypedClient:
    """engine._call 経由の Question オブジェクトに応答するスタブ。

    回答型は questions のキー名で決める: g* → choice、それ以外 → noul。"""

    def __init__(self, p: float = 0.5) -> None:
        self._p = p
        self.sent: list[dict[str, Any]] = []

    async def system_one(self, state: Any, questions: dict[str, Any]) -> Any:
        self.sent.append(questions)
        answers = {}
        for k, q in questions.items():
            if getattr(q, "type", None) == "choice":
                labels = list(q.criteria)
                answers[k] = type(
                    "A",
                    (),
                    {
                        "type": "choice",
                        "choice": labels[0],
                        "confidence": 0.7,
                        "probabilities": {lab: self._p / len(labels) for lab in labels},
                    },
                )()
            else:
                answers[k] = type("A", (), {"type": "noul", "noul": self._p})()
        return type(
            "R",
            (),
            {
                "answers": answers,
                "model": "m1",
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 2})(),
            },
        )()


def _eb_window_stub(mod: ModuleType, monkeypatch: Any, pairs: list[Any]) -> Any:
    w = type("W", (), {"start": 0, "end": 19, "pairs": pairs})()
    monkeypatch.setattr(mod, "build_windows", lambda lines, cands: ([w], []))
    monkeypatch.setattr(mod, "find_boundary_candidates", lambda lines, ex, s: [])
    return w


async def test_ecrit_uses_noul_criteria_and_labeled_pairs(
    tmp_path: Any, monkeypatch: Any
) -> None:
    mod = _load()
    doc = tmp_path / "doc.md"
    doc.write_text("\n".join(f"text {k}" for k in range(20)), encoding="utf-8")
    _eb_window_stub(mod, monkeypatch, [(5, 7), (7, 9), (12, 14)])
    labels_ext = {
        "labels": [
            {"doc": "plan_eb", "i": 5, "j": 7, "label": "boundary"},
            {"doc": "plan_eb", "i": 7, "j": 9, "label": "continue"},
            {"doc": "plan_eb", "i": 12, "j": 14, "label": "continue"},
            {"doc": "plan_eb", "i": 999, "j": 1000, "label": "boundary"},
        ]
    }
    eng = Engine(client=_FakeTypedClient(0.6), sem=None)
    runs = await mod.exp_ecrit(eng, str(doc), labels_ext, r=2, q=20, span=(5, 14))
    assert len(runs) == 4  # 2 variants × 2 reps
    assert {r["variant"] for r in runs} == {"criteria", "role_criteria"}
    assert runs[0]["n_requests"] == 1  # 3 pairs ≤ q=20 → 1 req
    req = runs[0]["requests"][0]
    # 窓外ペア (999,1000) は除外される
    assert sorted(req["question_keys"]) == ["b12", "b5", "b7"]
    q = req["questions"]["b5"]
    assert q["type"] == "noul"
    assert q["criteria"]["true"] == mod.ECRIT_TRUE
    assert "見出しとその直後の本文" in q["criteria"]["false"]
    assert req["probs"]["b5"] == 0.6
    # 質問 index は窓先頭からの相対位置
    assert "lines[5]" in q["instructions"]


async def test_echoice_groups_pairs_and_records_choices(
    tmp_path: Any, monkeypatch: Any
) -> None:
    mod = _load()
    doc = tmp_path / "doc.md"
    doc.write_text("\n".join(f"text {k}" for k in range(20)), encoding="utf-8")
    _eb_window_stub(mod, monkeypatch, [(5, 7), (7, 9), (12, 14)])
    eng = Engine(client=_FakeTypedClient(0.9), sem=None)
    runs = await mod.exp_echoice(eng, str(doc), r=2, group_size=2, span=(5, 14))
    assert len(runs) == 2  # 2 reps
    assert all(r["experiment"] == "E-CHOICE" for r in runs)
    assert runs[0]["params"]["n_groups"] == 2
    assert runs[0]["n_requests"] == 2  # 1群=1リクエスト
    req = runs[0]["requests"][0]
    assert req["question_keys"] == ["g0"]
    crit = req["questions"]["g0"]["criteria"]
    assert set(crit) == {"b5", "b7", "none"}  # none 退避先あり
    ch = req["choices"]["g0"]
    assert ch["choice"] == "b5"
    assert set(ch["probabilities"]) == {"b5", "b7", "none"}
    assert req["pairs"] == [[5, 7], [7, 9]]


def _choice2_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    """E-CHOICE2 のラベルプールを作る最小データ。各 doc に十分な
    boundary/continue を用意する。"""
    data = {
        "window": {
            "source": "plan.md",
            "doc_lines": [11, 20],
            "lines": [f"w{k}" for k in range(10)],
        },
        "labels": [
            {"doc": "window", "i": 0, "j": 1, "label": "boundary"},
            {"doc": "window", "i": 2, "j": 3, "label": "continue"},
            *[
                {"doc": "design", "i": k, "j": k + 1, "label": "boundary"}
                for k in range(6)
            ],
            *[
                {"doc": "design", "i": k + 10, "j": k + 11, "label": "continue"}
                for k in range(15)
            ],
        ],
    }
    lex = {
        "labels": [
            {"doc": "plan_eb", "i": 1, "j": 2, "label": "boundary"},
            {"doc": "plan_eb", "i": 3, "j": 4, "label": "boundary"},
            *[
                {"doc": "plan_eb", "i": k + 20, "j": k + 21, "label": "continue"}
                for k in range(16)
            ],
            *[
                {"doc": "readme", "i": k, "j": k + 1, "label": "boundary"}
                for k in range(5)
            ],
            *[
                {"doc": "readme", "i": k + 10, "j": k + 11, "label": "continue"}
                for k in range(5)
            ],
            *[
                {"doc": "bad", "i": k, "j": k + 1, "label": "boundary"}
                for k in range(6)
            ],
            *[
                {"doc": "bad", "i": k + 10, "j": k + 11, "label": "continue"}
                for k in range(4)
            ],
            {"doc": "plan_eb", "i": 40, "j": 41, "label": "either"},
        ]
    }
    return data, lex


def test_choice2_label_pools_merge_window_and_ext() -> None:
    """plan は plan_eb(doc index)と window ラベル(doc_lines オフセットで
    写像)を併合する。either は選択肢プールに入らない。"""
    mod = _load()
    data, lex = _choice2_fixture()
    pools = mod._choice2_label_pools(data, lex)
    # window i=0 → doc index 10(doc_lines[0]-1=10)
    assert (10, 11) in pools["plan"]["boundary"]
    assert (1, 2) in pools["plan"]["boundary"]
    assert (12, 13) in pools["plan"]["continue"]
    # either は集めない
    assert (40, 41) not in pools["plan"]["boundary"]
    assert len(pools["design"]["boundary"]) == 6
    assert len(pools["bad"]["continue"]) == 4


def test_choice2_questions_group_composition() -> None:
    """9問の群構成: boundary は全問相異なる1件、continue は未使用優先・
    不足時に再利用を記録、correct_key は boundary 選択肢を指す。"""
    mod = _load()
    data, lex = _choice2_fixture()
    pools = mod._choice2_label_pools(data, lex)
    specs = mod._choice2_questions(pools)
    assert len(specs) == 9
    assert [s["doc"] for s in specs] == (
        ["plan"] * 3 + ["design"] * 3 + ["readme"] * 2 + ["bad"]
    )
    bounds = [
        (s["doc"], tuple(o["pair"]))
        for s in specs
        for o in s["options"]
        if o["label"] == "boundary"
    ]
    assert len(set(bounds)) == 9  # 正解は全問相異なる(doc 別 junction)
    for s in specs:
        correct = [o for o in s["options"] if o["key"] == s["correct_key"]]
        assert len(correct) == 1 and correct[0]["label"] == "boundary"
        assert all(o["key"] in "abcdef" for o in s["options"])
        assert sum(o["label"] == "boundary" for o in s["options"]) == 1
    # readme は continue 5件しかないので2問目は再利用
    readme2 = next(s for s in specs if s["doc"] == "readme" and s["index"] == 7)
    assert readme2["reused_continues"]
    # bad は continue 4件のみ → 5選択肢に縮小を記録
    bad = specs[-1]
    assert bad["truncated_options"] and len(bad["options"]) == 5


async def test_echoice2_sends_one_request_per_question(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """1問=1リクエスト(束ねない)。criteria キーは中立、none なし。"""
    mod = _load()
    data, lex = _choice2_fixture()
    docs = {"plan": "p.md", "design": "d.md", "readme": "r.md", "bad": "b.md"}
    for name in docs.values():
        (tmp_path / name).write_text(
            "\n".join(f"text {k}" for k in range(60)), encoding="utf-8"
        )
    monkeypatch.setattr(
        mod, "ECHOICE2_DOCS", {k: str(tmp_path / v) for k, v in docs.items()}
    )
    eng = Engine(client=_FakeTypedClient(0.9), sem=None)
    runs = await mod.exp_echoice2(eng, data, lex)
    run = runs[0]
    assert run["experiment"] == "E-CHOICE2"
    assert run["n_requests"] == 9  # 1問1リクエスト
    for req in run["requests"]:
        assert len(req["question_keys"]) == 1
        q = next(iter(req["questions"].values()))
        assert q["type"] == "choice"
        assert "none" not in q["criteria"]
        assert all(k in "abcdef" for k in q["criteria"])
        for key in ("correct_key", "seed", "options", "state_range"):
            assert key in req
        # state は選択肢を含む範囲+余白、説明文は state 内 index
        assert req["state"]["lines"]
    assert "n_correct" in run


async def test_echoice2_control_runs_only_when_significant(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """対照は4/9以上の正解が出た場合のみ実行される。"""
    mod = _load()
    data, lex = _choice2_fixture()
    docs = {"plan": "p.md", "design": "d.md", "readme": "r.md", "bad": "b.md"}
    for name in docs.values():
        (tmp_path / name).write_text(
            "\n".join(f"text {k}" for k in range(60)), encoding="utf-8"
        )
    monkeypatch.setattr(
        mod, "ECHOICE2_DOCS", {k: str(tmp_path / v) for k, v in docs.items()}
    )

    class CorrectClient(_FakeTypedClient):
        async def system_one(self, state: Any, questions: dict[str, Any]) -> Any:
            self.sent.append(questions)
            answers = {}
            for k, q in questions.items():
                labels = list(q.criteria)
                answers[k] = type(
                    "A",
                    (),
                    {
                        "type": "choice",
                        "choice": "a",  # 正解キーとは限らない
                        "confidence": 0.9,
                        "probabilities": {lab: 1.0 / len(labels) for lab in labels},
                    },
                )()
            return type(
                "R",
                (),
                {
                    "answers": answers,
                    "model": "m1",
                    "usage": type("U", (), {"input_tokens": 1, "output_tokens": 2})(),
                },
            )()

    # 常に先頭キーを選ぶクライアント: 正解数は shuffle 次第 → 対照条件を
    # 直接制御するため n_correct のゲートをテストする
    eng = Engine(client=CorrectClient(), sem=None)
    runs = await mod.exp_echoice2(eng, data, lex)
    assert len(runs) == (2 if runs[0]["n_correct"] >= 4 else 1)
    if len(runs) == 2:
        ctrl = runs[1]
        assert ctrl["variant"] == "choice2_control"
        assert ctrl["n_requests"] == 3
        assert (
            ctrl["params"]["note"]
            in next(iter(ctrl["requests"][0]["questions"].values()))["instructions"]
        )


def test_state_hash_stable() -> None:
    mod = _load()
    h1 = mod.state_hash({"lines": ["a", "b"]})
    h2 = mod.state_hash({"lines": ["a", "b"]})
    h3 = mod.state_hash({"lines": ["a", "c"]})
    assert h1 == h2 and h1 != h3
