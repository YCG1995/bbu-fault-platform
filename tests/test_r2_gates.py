"""R2 发布覆盖（O4）与来源覆盖（O7）测试。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bbu.evidence import assess_evidence, source_coverage  # noqa: E402
from bbu.workshop import WorkshopService  # noqa: E402


def _quality_result(files: list[str], required: list[dict]) -> dict:
    evidence = [
        {"rule_id": "E1", "file": f, "line_no": 1, "text": "CLK032 PLL unlocked",
         "timestamp": "2026-01-01 00:00:00 UTC"}
        for f in files
    ]
    evidence.append({"rule_id": "E2", "file": files[0], "line_no": 2,
                     "text": "alarm active", "timestamp": "2026-01-01 00:00:01 UTC"})
    return {
        "root_cause": {"id": "R1", "confidence": "high"},
        "rule_meta": {"E1": {"weight": 20, "roots": ["R1"]},
                      "E2": {"weight": 20, "roots": ["R1"]}},
        "evidence": evidence,
        "required_sources": required,
    }


def test_o7_missing_alarm_source_caps_confidence():
    required = [
        {"id": "clock", "title": "时钟状态日志", "globs": ["**/clockstatuslog*.txt"], "required": True},
        {"id": "alarm", "title": "当前/历史告警", "globs": ["**/fm-active*", "**/fm-history*"], "required": True},
    ]
    res = _quality_result(["siteA/clockstatuslog.txt"], required)
    quality = assess_evidence(res)
    assert quality["confidence"] in ("medium", "low")
    assert quality["missing_required_sources"] == ["alarm"]
    assert quality["source_coverage"][1]["missing"] is True


def test_o7_all_sources_restores_high_or_medium():
    required = [
        {"id": "clock", "title": "时钟状态日志", "globs": ["**/clockstatuslog*.txt"], "required": True},
        {"id": "alarm", "title": "当前/历史告警", "globs": ["**/fm-active*", "**/fm-history*"], "required": True},
    ]
    res = _quality_result(["siteA/clockstatuslog.txt", "siteA/fm-active.txt"], required)
    quality = assess_evidence(res)
    assert quality["missing_required_sources"] == []
    assert quality["confidence"] in ("medium", "high")


def test_o7_source_coverage_ignores_glob_fallback():
    result = {
        "required_sources": [{"id": "clock", "title": "时钟", "globs": ["**/clockstatuslog*.txt"]}],
        "evidence": [{"file": "clockstatuslog.txt", "line": "x"}],
        "glob_fallback": ["clockstatuslog.txt"],
    }
    rows = source_coverage(result)
    assert rows[0]["missing"] is True and rows[0]["eligible"] == 0


def _service(tmp: Path):
    svc = SimpleNamespace(config=SimpleNamespace(cases_dir=tmp / "cases"))
    svc._publish_coverage = WorkshopService._publish_coverage.__get__(svc, SimpleNamespace)
    return svc


def _write_case(root: Path, name: str, kind: str, root_cause: str, hit: list[str], not_hit: list[str]):
    d = root / "cases" / "skill-x" / name
    d.mkdir(parents=True)
    (d / "case.yaml").write_text(yaml.safe_dump({
        "id": name, "skill": "skill-x", "kind": kind,
        "expected": {"root_cause": root_cause, "hit_rules": hit, "not_hit_rules": not_hit},
    }, allow_unicode=True), encoding="utf-8")


def test_o4_publish_coverage_ok_with_positive_and_negative():
    tmp = Path(tempfile.mkdtemp())
    _write_case(tmp, "pos", "positive", "R1", ["E1"], [])
    _write_case(tmp, "neg", "negative", "NONE", [], ["E1"])
    svc = _service(tmp)
    row = {"name": "skill-x", "profile_yaml": yaml.safe_dump({
        "analysis": {"steps": [{"rules": [{"id": "E1", "type": "text",
                                           "globs": ["**/*.log"], "any_of": ["ERR"]}]}]},
        "root_causes": [{"id": "R1", "condition": "E1", "actions": ["x"]}],
    }, allow_unicode=True)}
    cov = svc._publish_coverage(row)
    assert cov["ok"] is True and cov["cases"] == 2


def test_o4_publish_coverage_blocks_missing_negative():
    tmp = Path(tempfile.mkdtemp())
    _write_case(tmp, "pos", "positive", "R1", ["E1"], [])
    svc = _service(tmp)
    row = {"name": "skill-x", "profile_yaml": yaml.safe_dump({
        "analysis": {"steps": [{"rules": [{"id": "E1", "type": "text",
                                           "globs": ["**/*.log"], "any_of": ["ERR"]}]}]},
        "root_causes": [{"id": "R1", "condition": "E1", "actions": ["x"]}],
    }, allow_unicode=True)}
    cov = svc._publish_coverage(row)
    assert cov["ok"] is False
    assert any("至少需要 1 个故障正例和 1 个正常/反例" in x for x in cov["blocking"])


def test_o4_publish_coverage_blocks_critical_without_negative():
    tmp = Path(tempfile.mkdtemp())
    _write_case(tmp, "pos", "positive", "R1", ["E1"], [])
    svc = _service(tmp)
    row = {"name": "skill-x", "profile_yaml": yaml.safe_dump({
        "analysis": {"steps": [{"rules": [{"id": "E1", "type": "text", "critical": True,
                                           "globs": ["**/*.log"], "any_of": ["ERR"]}]}]},
        "root_causes": [{"id": "R1", "condition": "E1", "actions": ["x"]}],
    }, allow_unicode=True)}
    cov = svc._publish_coverage(row)
    assert cov["ok"] is False
    assert any("关键规则 E1" in x for x in cov["blocking"])
