"""scripts/report_boundary_eval.py の集計ロジックのテスト(issue #2 Stage 1)。
事前登録仕様: p* 極性補正・帯質量 |p-0.5|<0.1・either/争点除外・flip・
model 混在 run 破棄・E-E の窓オフセット対応。実 API は使わない。"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "report_boundary_eval",
        Path(__file__).parent.parent / "scripts" / "report_boundary_eval.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_data(tmp_path: Path) -> Path:
    """10行の source 文書と 5行の窓(doc_lines は 1-based [3,7])。"""
    src = tmp_path / "doc.md"
    src.write_text("\n".join(f"line{k}" for k in range(10)), encoding="utf-8")
    data = {
        "window": {
            "source": str(src),
            "doc_lines": [3, 7],
            "lines": [f"line{k}" for k in range(2, 7)],
        },
        "labels": [
            {"doc": "window", "i": 0, "j": 1, "label": "boundary", "note": "b"},
            {
                "doc": "window",
                "i": 1,
                "j": 2,
                "label": "continue",
                "note": "見出しと本文",
            },
            {"doc": "window", "i": 2, "j": 3, "label": "continue", "note": "c"},
            {"doc": "window", "i": 3, "j": 4, "label": "either", "note": "e"},
        ],
    }
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _run(
    experiment: str,
    doc: str,
    variant: str = "current",
    polarity: int = 1,
    q: int = 20,
    rep: int = 0,
    pairs: list[list[int]] | None = None,
    probs: dict[str, float] | None = None,
    model: str = "m1",
) -> dict[str, Any]:
    pairs = pairs or []
    probs = probs or {}
    return {
        "experiment": experiment,
        "doc": doc,
        "rep": rep,
        "variant": variant,
        "polarity": polarity,
        "params": {"q": q},
        "requests": [
            {
                "pairs": pairs,
                "probs": probs,
                "model": model,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ],
        "models": [model],
        "n_requests": 1,
        "model_consistent": True,
        "discard": False,
    }


def _write_runs(tmp_path: Path, name: str, runs: list[dict[str, Any]]) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"runs": runs}), encoding="utf-8")
    return p


def test_polarity_normalization_and_band_mass(tmp_path: Path) -> None:
    mod = _load()
    data_p = _write_data(tmp_path)
    data = json.loads(data_p.read_text())
    src = data["window"]["source"]
    pairs = [[0, 1], [1, 2], [2, 3], [3, 4]]
    # negative: p=0.1 → p*=0.9 (boundary 方向に高い)
    runs = [
        _run(
            "E-C",
            src,
            variant="negative",
            polarity=-1,
            pairs=pairs,
            probs={"b0": 0.1, "b1": 0.5, "b2": 0.5, "b3": 0.45},
        ),
        _run(
            "E-C",
            src,
            variant="current",
            pairs=pairs,
            probs={"b0": 0.9, "b1": 0.4, "b2": 0.6, "b3": 0.55},
        ),
    ]
    f = _write_runs(tmp_path, "ec.json", runs)
    series, meta = mod.collect_series([str(f)], data)
    # negative の b0: p*=0.9
    key = ("E-C", "negative", ("window", 0, 1))
    assert series[key] == [0.9]
    rep = mod.build_report([str(f)], data)
    neg = rep["experiments"]["E-C"]["negative"]
    # |p-0.5|<0.1 は開区間: 0.9→外, 0.5→内, 0.5→内, 0.45→内 → 3/4
    assert neg["band_mass"] == 0.75
    cur = rep["experiments"]["E-C"]["current"]
    # 0.9 外, 0.4 外(境界), 0.6 外(境界), 0.55 内 → 1/4
    assert cur["band_mass"] == 0.25


def test_either_and_contested_exclusion(tmp_path: Path) -> None:
    mod = _load()
    data_p = _write_data(tmp_path)
    data = json.loads(data_p.read_text())
    src = data["window"]["source"]
    pairs = [[0, 1], [1, 2], [2, 3], [3, 4]]
    runs = [
        _run(
            "E-C",
            src,
            pairs=pairs,
            probs={"b0": 0.9, "b1": 0.8, "b2": 0.2, "b3": 0.99},
        )
    ]
    f = _write_runs(tmp_path, "ec.json", runs)
    rep = mod.build_report([str(f)], data)
    g = rep["experiments"]["E-C"]["current"]
    assert g["n_pairs"] == 4
    assert g["n_either_pairs"] == 1
    assert g["n_confirmed_pairs"] == 3
    assert g["n_contested_pairs"] == 1  # continue + 見出し
    # AUC: 確定3件 = boundary1(b0:0.9) vs continue2(b1:0.8, b2:0.2)
    # → 0.9>0.8, 0.9>0.2 → AUC=1.0
    assert g["auc"] == 1.0
    # 争点除外: continue は b2(0.2) のみ → 0.9>0.2 → AUC=1.0
    assert g["auc_no_contested"] == 1.0
    assert g["auc_no_contested_n_pos_neg"] == [1, 1]


def test_flip_curve_and_rep_sd(tmp_path: Path) -> None:
    mod = _load()
    data_p = _write_data(tmp_path)
    data = json.loads(data_p.read_text())
    src = data["window"]["source"]
    pairs = [[0, 1], [2, 3]]
    # b0 は rep 間で 0.5 を跨ぐ(0.45, 0.55, 0.9)、b2 は安定(0.1)
    runs = [
        _run("E-C", src, rep=r, pairs=pairs, probs=p)
        for r, p in enumerate(
            [
                {"b0": 0.45, "b2": 0.1},
                {"b0": 0.55, "b2": 0.1},
                {"b0": 0.9, "b2": 0.1},
            ]
        )
    ]
    f = _write_runs(tmp_path, "ec.json", runs)
    rep = mod.build_report([str(f)], data)
    g = rep["experiments"]["E-C"]["current"]
    # t=0.50: b0 は 2/3 rep が ≥0.5 → 0<2<3 で flip、b2 は 0 rep → 非 flip
    assert g["flip_curve"]["0.50"] == 0.5
    # t=0.44: b0 は 3/3 rep が ≥0.44 → 非 flip
    assert g["flip_curve"]["0.44"] == 0.0
    # t=0.70: b0 は 1/3 rep → flip
    assert g["flip_curve"]["0.70"] == 0.5
    # flip 曲線は 0.30〜0.70 を 0.02 刻みで 21 点
    assert len(g["flip_curve"]) == 21
    # rep_sd: b0 の SD は正、b2 は 0
    assert g["rep_sd"]["max"] > 0


def test_model_mixed_run_discarded(tmp_path: Path) -> None:
    mod = _load()
    data_p = _write_data(tmp_path)
    data = json.loads(data_p.read_text())
    src = data["window"]["source"]
    run = _run("E-C", src, pairs=[[0, 1]], probs={"b0": 0.9}, model="m1")
    run["requests"].append(
        {
            "pairs": [[0, 1]],
            "probs": {"b0": 0.9},
            "model": "m2",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    f = _write_runs(tmp_path, "ec.json", [run])
    series, meta = mod.collect_series([str(f)], data)
    assert len(meta["discarded_runs"]) == 1
    assert not series  # 破棄されたので series は空


def test_ee_window_offset_mapping(tmp_path: Path) -> None:
    mod = _load()
    data_p = _write_data(tmp_path)
    data = json.loads(data_p.read_text())
    src = data["window"]["source"]
    off = 2  # doc_lines [3,7] → 0-based offset 2
    # E-E: doc 行 index でキーを持つ。窓内ペアは (off+i, off+j)
    pairs = [[off + 0, off + 1], [off + 2, off + 3], [0, 1]]  # 3つ目は窓外
    runs = [
        _run(
            "E-E",
            src,
            pairs=pairs,
            probs={"b2": 0.9, "b4": 0.2, "b0": 0.5},
        )
    ]
    f = _write_runs(tmp_path, "ee.json", runs)
    series, meta = mod.collect_series([str(f)], data)
    # 窓内ペアは window ラベルキーに変換される
    assert series[("E-E", "current", ("window", 0, 1))] == [0.9]
    assert series[("E-E", "current", ("window", 2, 3))] == [0.2]
    # 窓外ペアは負の窓 index → ラベルと一致せず labeled 集計から外れる
    rep = mod.build_report([str(f)], data)
    g = rep["experiments"]["E-E"]
    assert g["n_pairs"] == 2  # ラベル付きのみ
    assert g["all_measured"]["n_pairs"] == 3
