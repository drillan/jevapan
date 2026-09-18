"""scripts/eval_thresholds.py の集計ロジックのテスト。
生確率で判定し、block/document の同一候補は統合後件数を別に数える。"""

import importlib.util
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "eval_thresholds",
        Path(__file__).parent.parent / "scripts" / "eval_thresholds.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_merged_flag_counts_dedupes_both_scopes() -> None:
    """同一 (行範囲, col, text, category) が両スコープで flag されたとき、
    採用質問件数と統合後違反件数を分けて数える。判定は生確率。"""
    mod = _load()
    cands = [
        {
            "category": "concision",
            "scope": "block",
            "start": 3,
            "end": 3,
            "col": 0,
            "text": "x。",
            "prob": 0.59996,
        },
        {
            "category": "concision",
            "scope": "document",
            "start": 3,
            "end": 3,
            "col": 0,
            "text": "x。",
            "prob": 0.59996,
        },
        {
            "category": "clarity",
            "scope": "block",
            "start": 5,
            "end": 5,
            "col": 0,
            "text": "y。",
            "prob": 0.7,
        },
    ]
    counts = mod.merged_flag_counts(cands, [0.5, 0.6])
    # 0.5: raw 3件 / 統合後 2件(同一文の両スコープを1件に)
    assert counts[0.5] == (3, 2)
    # 0.6: 0.59996 は丸めず未満 → 0.7 のみ。raw=統合後=1
    assert counts[0.6] == (1, 1)
