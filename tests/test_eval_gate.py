"""P0-3 评测门禁纯函数测试（不依赖现场数据库）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.eval_gate import compare_summary  # noqa: E402


@pytest.mark.eval_smoke
def test_gate_rejects_errors_and_short_sample():
    verdict = compare_summary({"cases": 2, "errors": 1}, {"exact_rate": 1.0}, min_cases=3)
    assert not verdict["ok"]
    assert any("数据不足" in x for x in verdict["failures"])
    assert any("错误" in x for x in verdict["failures"])


@pytest.mark.eval_smoke
def test_gate_rejects_five_point_regression():
    baseline = {"exact_rate": 0.9, "f1": 0.8}
    verdict = compare_summary({"cases": 20, "errors": 0, "exact_rate": 0.84, "f1": 0.9},
                              baseline, min_cases=20)
    assert not verdict["ok"]
    assert any("exact_rate" in x for x in verdict["failures"])


@pytest.mark.eval_smoke
def test_gate_allows_small_drop():
    baseline = {"exact_rate": 0.9, "false_positive_rate": 0.1}
    verdict = compare_summary({"cases": 20, "errors": 0, "exact_rate": 0.86,
                               "false_positive_rate": 0.12}, baseline, min_cases=20)
    assert verdict["ok"]


@pytest.mark.eval
def test_baseline_fixture_has_metrics():
    data = json.loads((ROOT / "tests/fixtures/eval_baseline.json").read_text(encoding="utf-8"))
    for key in ("exact_rate", "f1", "precision", "recall", "false_positive_rate"):
        assert key in data
