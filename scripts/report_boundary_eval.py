"""Stage 1 集計(事前登録仕様, issue #2)。

eval_boundaries.py が収集した生 JSON を集計する。API は呼ばない。

事前登録仕様:
- p* = p if polarity==1 else 1-p を最初に作り、以降すべて p* で計算
  (negative 変種の極性補正)
- 帯質量は |p*-0.5|<0.1 の対称定義(極性不変)。変種比較の主指標
- either ラベルは AUC/Brier/ECE/flip から除外(件数併記)。
  帯質量・反復間SDは全ラベル件で計算し、either vs 確定で分割して出す
- AUC は争点(note に「見出し」を含む continue ラベル)を除いた
  縮小版も併記 — 変種の section 文言有無と AUC が交絡するため
- flip(t): t∈[0.30,0.70] を 0.02 刻みで掃引。
  flip(t) = (1/N)Σ[0 < #{r: p*_ir ≥ t} < R]
- E-A は Q ごとに帯質量・SD・AUC を並べる
- 全リクエストの model 同一性を事後検証し、混在 run は破棄

使い方:
    uv run python scripts/report_boundary_eval.py \
        --ee eval_ee.json --ec-ea eval_ec_ea.json \
        --cache eval_cache_check.json \
        --data scripts/boundary_eval_data.json
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_DATA = "scripts/boundary_eval_data.json"
FLIP_T_MIN = 0.30
FLIP_T_MAX = 0.70
FLIP_T_STEP = 0.02
BAND_HALF = 0.1
N_ECE_BINS = 10
CONTESTED_NOTE = "見出し"

# (doc_label, i, j) — ラベル付きペアの識別子
PairKey = tuple[str, int, int]
# (experiment, group, pair_key) — group は変種名または Q
SeriesKey = tuple[str, str, PairKey]


def _sd(xs: list[float]) -> float:
    """標本標準偏差(n-1)。n<2 なら 0。"""
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _auc(pos: list[float], neg: list[float]) -> float | None:
    """rank-based AUC(pos=boundary 側)。tie は 0.5。"""
    if not pos or not neg:
        return None
    wins = sum(1 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def _brier(scores: list[float], ys: list[int]) -> float | None:
    if not scores:
        return None
    return sum((p - y) ** 2 for p, y in zip(scores, ys, strict=True)) / len(scores)


def _ece(scores: list[float], ys: list[int], n_bins: int = N_ECE_BINS) -> float | None:
    """Expected Calibration Error(等幅ビン)。"""
    if not scores:
        return None
    ece = 0.0
    n = len(scores)
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        idx = [
            k
            for k, p in enumerate(scores)
            if lo <= p < hi or (b == n_bins - 1 and p == 1.0)
        ]
        if not idx:
            continue
        conf = sum(scores[k] for k in idx) / len(idx)
        acc = sum(ys[k] for k in idx) / len(idx)
        ece += len(idx) / n * abs(conf - acc)
    return ece


def _band_mass(ps: list[float]) -> float | None:
    """|p-0.5|<0.1 の質量(両端開区間。浮動小数点誤差で境界値が
    帯内に入らないよう isclose で境界を除外する)。"""
    if not ps:
        return None
    inside = sum(
        1
        for p in ps
        if abs(p - 0.5) < BAND_HALF and not math.isclose(abs(p - 0.5), BAND_HALF)
    )
    return inside / len(ps)


def _band_mass_legacy(ps: list[float]) -> float | None:
    """旧定義 [0.4,0.6) の質量。対称定義との差が結果を動かすかの確認用。"""
    if not ps:
        return None
    return sum(1 for p in ps if 0.4 <= p < 0.6) / len(ps)


def _summary(xs: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(xs),
        "mean": sum(xs) / len(xs) if xs else None,
        "median": sorted(xs)[len(xs) // 2] if xs else None,
        "max": max(xs) if xs else None,
    }


def _window_offset(data: dict[str, Any]) -> int:
    """窓 index → source 文書の 0-based 行 index のオフセット。

    data['window']['doc_lines'] は [start, end] の 1-based 行番号。
    実ファイルとの一致で検証し、ずれていれば full match を探索する。"""
    w = data["window"]
    lines = Path(w["source"]).read_text(encoding="utf-8").splitlines()
    start, end = w["doc_lines"]
    off = int(start) - 1
    if lines[off : off + len(w["lines"])] == w["lines"]:
        return off
    for i in range(len(lines) - len(w["lines"]) + 1):
        if lines[i : i + len(w["lines"])] == w["lines"]:
            return i
    msg = f"window slice not found in {w['source']} (doc_lines={w['doc_lines']})"
    raise ValueError(msg)


def collect_series(
    files: list[str], data: dict[str, Any]
) -> tuple[dict[SeriesKey, list[float]], dict[str, Any]]:
    """run 群から (exp, group, pair_key) -> [p* の rep 列] を集める。

    group は変種(E-C)または Q(E-A)または 'current'(E-E)。
    pair_key は (doc_label, i, j)。E-E の窓ペアは doc 行 index に
    オフセット変換してラベルと突き合わせる。
    model 混在 run は破棄する。
    """
    off = _window_offset(data)
    win_source = data["window"]["source"]
    series: dict[SeriesKey, list[float]] = defaultdict(list)
    meta: dict[str, Any] = {"n_runs": 0, "n_requests": 0, "discarded_runs": []}
    for f in files:
        doc_data = json.loads(Path(f).read_text(encoding="utf-8"))
        for run in doc_data["runs"]:
            meta["n_runs"] += 1
            models = {req["model"] for req in run["requests"]}
            if len(models) != 1:
                meta["discarded_runs"].append(
                    {
                        "experiment": run["experiment"],
                        "doc": run["doc"],
                        "rep": run["rep"],
                        "models": sorted(models),
                    }
                )
                continue
            meta["n_requests"] += len(run["requests"])
            meta.setdefault("models", set()).update(models)
            pol = run.get("polarity", 1)
            group = (
                run["variant"]
                if run["experiment"] == "E-C"
                else str(run["params"].get("q"))
                if run["experiment"] == "E-A"
                else "current"
            )
            is_window_doc = run["doc"] == win_source
            for req in run["requests"]:
                for (i, j), (key, p) in zip(
                    req["pairs"], req["probs"].items(), strict=True
                ):
                    assert key == f"b{i}", (key, i)
                    pstar = p if pol == 1 else 1.0 - p
                    if run["experiment"] == "E-E" and is_window_doc:
                        # doc 行 index → 窓 index に逆変換できるものだけ
                        # ラベルと突き合わせ可能(窓外のペアは別 doc_label)
                        pair_key = ("window", i - off, j - off)
                    elif run["experiment"] == "E-E":
                        pair_key = ("design", i, j)
                    else:
                        pair_key = ("window", i, j)
                    series[(run["experiment"], group, pair_key)].append(pstar)
    meta["models"] = sorted(meta.get("models", set()))
    return series, meta


def _labels_by_key(data: dict[str, Any]) -> dict[PairKey, dict[str, Any]]:
    return {(lb["doc"], lb["i"], lb["j"]): lb for lb in data["labels"]}


def _aggregate_group(
    series: dict[PairKey, list[float]],
    label_map: dict[PairKey, dict[str, Any]],
    labeled_only: bool,
) -> dict[str, Any]:
    """1グループ分の series を集計。labeled_only ならラベル付きペアのみ。"""
    pairs = {k: v for k, v in series.items() if not labeled_only or k in label_map}
    all_obs = [p for v in pairs.values() for p in v]
    pair_mean = {k: sum(v) / len(v) for k, v in pairs.items()}
    sds = [_sd(v) for v in pairs.values()]

    confirmed = {
        k: v
        for k, v in pairs.items()
        if k in label_map and label_map[k]["label"] in ("boundary", "continue")
    }
    contested = {
        k
        for k in confirmed
        if label_map[k]["label"] == "continue"
        and CONTESTED_NOTE in label_map[k].get("note", "")
    }
    either_keys = {
        k for k in pairs if k in label_map and label_map[k]["label"] == "either"
    }

    def _auc_set(keys: set[PairKey]) -> tuple[float | None, int, int]:
        pos = [pair_mean[k] for k in keys if label_map[k]["label"] == "boundary"]
        neg = [pair_mean[k] for k in keys if label_map[k]["label"] == "continue"]
        return _auc(pos, neg), len(pos), len(neg)

    conf_keys = set(confirmed)
    auc_all, n_pos, n_neg = _auc_set(conf_keys)
    auc_nocont, n_pos2, n_neg2 = _auc_set(conf_keys - contested)

    scores = [pair_mean[k] for k in conf_keys]
    ys = [1 if label_map[k]["label"] == "boundary" else 0 for k in conf_keys]

    # flip(t): 確定ラベル付きペアのみ(either 除外)
    flips = {}
    t = FLIP_T_MIN
    while t <= FLIP_T_MAX + 1e-9:
        t_r = round(t, 2)
        n_flip = sum(
            1
            for k in conf_keys
            if 0 < sum(1 for p in pairs[k] if p >= t_r) < len(pairs[k])
        )
        flips[f"{t_r:.2f}"] = n_flip / len(conf_keys) if conf_keys else None
        t += FLIP_T_STEP

    # either vs 確定の帯質量・SD 分割
    def _split(keys: set[PairKey]) -> dict[str, Any]:
        obs = [p for k in keys for p in pairs[k]]
        return {
            "n_pairs": len(keys),
            "band_mass": _band_mass(obs),
            "sd": _summary([_sd(pairs[k]) for k in keys]),
        }

    return {
        "n_pairs": len(pairs),
        "n_obs": len(all_obs),
        "n_either_pairs": len(either_keys),
        "n_confirmed_pairs": len(conf_keys),
        "n_contested_pairs": len(contested),
        "band_mass": _band_mass(all_obs),
        "band_mass_legacy": _band_mass_legacy(all_obs),
        "band_mass_pair_mean": _band_mass(list(pair_mean.values())),
        "rep_sd": _summary(sds),
        "split_either": _split(either_keys),
        "split_confirmed": _split(conf_keys),
        "auc": auc_all,
        "auc_n_pos_neg": [n_pos, n_neg],
        "auc_no_contested": auc_nocont,
        "auc_no_contested_n_pos_neg": [n_pos2, n_neg2],
        "brier": _brier(scores, ys),
        "ece": _ece(scores, ys),
        "flip_curve": flips,
    }


def build_report(files: list[str], data: dict[str, Any]) -> dict[str, Any]:
    series, meta = collect_series(files, data)
    label_map = _labels_by_key(data)
    win_confirmed = {
        k
        for k, lb in label_map.items()
        if lb["doc"] == "window" and lb["label"] in ("boundary", "continue")
    }

    out: dict[str, Any] = {"meta": meta, "experiments": {}}

    # E-E: 全52ラベル件が分析フレーム(窓外の未ラベルペアは対象外)
    ee_series = {k[2]: v for k, v in series.items() if k[0] == "E-E"}
    out["experiments"]["E-E"] = _aggregate_group(
        ee_series, label_map, labeled_only=True
    )
    # 参考: E-E 全測定ペア(未ラベル含む)の帯質量・SD
    ee_all = _aggregate_group(ee_series, label_map, labeled_only=False)
    out["experiments"]["E-E"]["all_measured"] = {
        "n_pairs": ee_all["n_pairs"],
        "band_mass": ee_all["band_mass"],
        "rep_sd": ee_all["rep_sd"],
    }
    # doc 別分割(design 32 件は either 0、素の順序性能を単独で見る)
    for doc_label in ("design", "window"):
        sub = {k: v for k, v in ee_series.items() if k[0] == doc_label}
        out["experiments"]["E-E"][f"{doc_label}_only"] = _aggregate_group(
            sub, label_map, labeled_only=True
        )

    # E-C: 変種ごと(窓ラベル=全ペアがラベル付き)
    out["experiments"]["E-C"] = {}
    for var in sorted({k[1] for k in series if k[0] == "E-C"}):
        sub = {k[2]: v for k, v in series.items() if k[0] == "E-C" and k[1] == var}
        out["experiments"]["E-C"][var] = _aggregate_group(
            sub, label_map, labeled_only=True
        )

    # E-A: Q ごと(帯質量・SD・AUC を並べて単調性確認)
    out["experiments"]["E-A"] = {}
    for q in sorted({k[1] for k in series if k[0] == "E-A"}, key=int):
        sub = {k[2]: v for k, v in series.items() if k[0] == "E-A" and k[1] == q}
        out["experiments"]["E-A"][q] = _aggregate_group(
            sub, label_map, labeled_only=True
        )

    out["label_counts"] = {
        "total": len(label_map),
        "boundary": sum(1 for lb in label_map.values() if lb["label"] == "boundary"),
        "continue": sum(1 for lb in label_map.values() if lb["label"] == "continue"),
        "either": sum(1 for lb in label_map.values() if lb["label"] == "either"),
        "window_confirmed": len(win_confirmed),
    }
    return out


def _fmt(x: float | None, nd: int = 3) -> str:
    return f"{x:.{nd}f}" if isinstance(x, float) else "n/a"


def print_report(rep: dict[str, Any]) -> None:
    m = rep["meta"]
    print("=== meta ===")
    print(
        f"runs={m['n_runs']} requests={m['n_requests']} "
        f"models={m['models']} discarded={len(m['discarded_runs'])}"
    )
    lc = rep["label_counts"]
    print(
        f"labels: total={lc['total']} boundary={lc['boundary']} "
        f"continue={lc['continue']} either={lc['either']}"
    )
    for exp, groups in rep["experiments"].items():
        print(f"\n=== {exp} ===")
        if exp == "E-E":
            groups = {"current": groups}
        for name, g in groups.items():
            print(
                f"[{name}] pairs={g['n_pairs']} "
                f"(confirmed={g['n_confirmed_pairs']} "
                f"either={g['n_either_pairs']} "
                f"contested={g['n_contested_pairs']})"
            )
            print(
                f"  band_mass={_fmt(g['band_mass'])} "
                f"(legacy {_fmt(g['band_mass_legacy'])}, "
                f"pair-mean {_fmt(g['band_mass_pair_mean'])})  "
                f"rep_sd mean={_fmt(g['rep_sd']['mean'])} "
                f"median={_fmt(g['rep_sd']['median'])} "
                f"max={_fmt(g['rep_sd']['max'])}"
            )
            se, sc = g["split_either"], g["split_confirmed"]
            print(
                f"  split either(n={se['n_pairs']}): band={_fmt(se['band_mass'])} "
                f"sd={_fmt(se['sd']['mean'])} | "
                f"confirmed(n={sc['n_pairs']}): band={_fmt(sc['band_mass'])} "
                f"sd={_fmt(sc['sd']['mean'])}"
            )
            print(
                f"  auc={_fmt(g['auc'])} n={g['auc_n_pos_neg']} "
                f"no_contested={_fmt(g['auc_no_contested'])} "
                f"n={g['auc_no_contested_n_pos_neg']}  "
                f"brier={_fmt(g['brier'])} ece={_fmt(g['ece'])}"
            )
            fc = g["flip_curve"]
            print("  flip: " + " ".join(f"{t}:{_fmt(v, 2)}" for t, v in fc.items()))
        if exp == "E-E":
            am = rep["experiments"]["E-E"]["all_measured"]
            print(
                f"  [all measured] pairs={am['n_pairs']} "
                f"band={_fmt(am['band_mass'])} "
                f"sd_mean={_fmt(am['rep_sd']['mean'])}"
            )
            for doc_label in ("design", "window"):
                sg = rep["experiments"]["E-E"][f"{doc_label}_only"]
                print(
                    f"  [{doc_label} only] pairs={sg['n_pairs']} "
                    f"contested={sg['n_contested_pairs']} "
                    f"band={_fmt(sg['band_mass'])} "
                    f"auc={_fmt(sg['auc'])} "
                    f"no_contested={_fmt(sg['auc_no_contested'])} "
                    f"n={sg['auc_no_contested_n_pos_neg']} "
                    f"brier={_fmt(sg['brier'])} ece={_fmt(sg['ece'])}"
                )


def main() -> int:
    ap = argparse.ArgumentParser(description="stage-1 boundary eval report")
    ap.add_argument("--ee", nargs="*", default=["eval_ee.json"])
    ap.add_argument("--ec-ea", nargs="*", default=["eval_ec_ea.json"])
    ap.add_argument("--cache", default="eval_cache_check.json")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--out", default="eval_report.json")
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    cc = cache.get("cache_check", cache)
    print("=== cache_check ===")
    print(f"identical: {cc.get('identical')}")
    print(f"models: {cc.get('models')} usage: {cc.get('usage')}")
    p1, p2 = cc.get("probs_first", {}), cc.get("probs_second", {})
    n_diff = sum(1 for k in p1 if p1[k] != p2.get(k))
    print(f"differing keys: {n_diff}/{len(p1)}")

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    rep = build_report(list(args.ee) + list(args.ec_ea), data)
    print_report(rep)
    Path(args.out).write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nreport dump: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
