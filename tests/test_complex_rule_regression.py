"""复杂规则回归：时间窗、多站点隔离、数值边界、CSV/XLSX、多根因冲突。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SEARCH = ROOT / "examples" / "skills" / "demo-bbu-clock-loss" / "scripts" / "search_evidence.py"


def _write_rules(path: Path, rules: list[dict], temporal: list[dict] | None = None,
                 roots: list[dict] | None = None) -> None:
    path.write_text(yaml.safe_dump({"rules": rules, "temporal": temporal or [],
                                    "root_causes": roots or []}, allow_unicode=True,
                                   sort_keys=False), encoding="utf-8")


def _run(root: Path, rules: Path) -> dict:
    out = root / "evidence.json"
    subprocess.run([sys.executable, str(SEARCH), str(root), "--rules", str(rules),
                    "--json", str(out)], check=True, capture_output=True, text=True,
                   timeout=60)
    return json.loads(out.read_text(encoding="utf-8"))


TEXT_TE = {"pattern": r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", "group": 1,
           "formats": ["%Y-%m-%d %H:%M:%S"]}


class ComplexRuleRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_time_window_inside_and_outside(self):
        rules = [
            {"id": "E1", "type": "text", "globs": ["**/*.log"], "any_of": ["PLL unlock"],
             "time_extract": TEXT_TE, "min_matches": 1},
            {"id": "E2", "type": "text", "globs": ["**/*.log"], "any_of": ["clock stable"],
             "time_extract": TEXT_TE, "min_matches": 1},
        ]
        temporal = [{"id": "T1", "first": "E1", "second": "E2",
                     "min_gap_minutes": 5, "max_gap_minutes": 15}]
        roots = [{"id": "R1", "name": "参考时钟失锁", "condition": "E1 and E2",
                  "confidence": "high"}]
        for name, second, status in (("inside", "00:10:00", "matched"),
                                      ("outside", "00:40:00", "temporal_conflict")):
            root = self.tmp / name
            root.mkdir()
            (root / "clock.log").write_text(
                f"2026-01-01 00:00:00 PLL unlock\n2026-01-01 {second} clock stable\n",
                encoding="utf-8")
            rules_path = self.tmp / f"{name}.yaml"
            _write_rules(rules_path, rules, temporal, roots)
            data = _run(root, rules_path)
            self.assertEqual(data["verdict"]["status"], status, data["verdict"])
            self.assertEqual(data["verdict"]["primary"], "R1" if status == "matched" else "NONE")

    def test_multisite_temporal_evidence_does_not_cross_sites(self):
        rules = [
            {"id": "E1", "type": "text", "globs": ["**/*.log"], "any_of": ["PLL unlock"],
             "associate_by": {"site": True}, "time_extract": TEXT_TE},
            {"id": "E2", "type": "text", "globs": ["**/*.log"], "any_of": ["clock stable"],
             "associate_by": {"site": True}, "time_extract": TEXT_TE},
        ]
        temporal = [{"id": "T1", "first": "E1", "second": "E2",
                     "min_gap_minutes": 5, "max_gap_minutes": 15}]
        roots = [{"id": "R1", "name": "参考时钟失锁", "condition": "E1 and E2"}]
        for site, second in (("siteA", "00:10:00"), ("siteB", "00:40:00")):
            path = self.tmp / site / "BspLog"
            path.mkdir(parents=True)
            (path / "clock.log").write_text(
                f"2026-01-01 00:00:00 PLL unlock\n2026-01-01 {second} clock stable\n",
                encoding="utf-8")
        rules_path = self.tmp / "multi.yaml"
        _write_rules(rules_path, rules, temporal, roots)
        data = _run(self.tmp, rules_path)
        verdict = data["verdict"]
        self.assertTrue(verdict["grouped_by_site"])
        groups = {k: v for k, v in verdict["groups"].items() if k != "ALL"}
        self.assertEqual(len(groups), 2)
        self.assertEqual(set(groups), {"siteA", "siteB"})
        site_a, site_b = groups["siteA"], groups["siteB"]
        self.assertEqual(site_a["status"], "matched")
        self.assertEqual(site_a["primary"], "R1")
        self.assertEqual(site_b["status"], "temporal_conflict")
        self.assertEqual(site_b["primary"], "NONE")

    def test_numeric_threshold_is_strict_and_sustained(self):
        rules = [{"id": "E1", "type": "numeric_trend", "globs": ["**/*.log"],
                  "value_pattern": r"temp=([0-9.]+)", "threshold": 80,
                  "trend": "sustained_above", "sustained_count": 3,
                  "time_extract": TEXT_TE}]
        roots = [{"id": "R1", "name": "温度越界", "condition": "E1"}]
        for name, values, hit in (("boundary", [79, 80, 81, 80], False),
                                   ("sustained", [79, 81, 82, 83], True)):
            root = self.tmp / name
            root.mkdir()
            (root / "temp.log").write_text("\n".join(
                f"2026-01-01 00:00:0{i} temp={v}" for i, v in enumerate(values)) + "\n",
                encoding="utf-8")
            rules_path = self.tmp / f"{name}.yaml"
            _write_rules(rules_path, rules, [], roots)
            data = _run(root, rules_path)
            self.assertIn("E1", data["hit_rules"] if hit else data["miss_rules"])
            self.assertEqual(data["verdict"]["status"], "matched" if hit else "unmatched")

    def test_csv_and_xlsx_columns(self):
        root = self.tmp / "tables"
        root.mkdir()
        (root / "alarm.csv").write_text(
            "Time,Alarm,Detail\n2026-01-01 00:00:00,CLK032,PLL unlock\n", encoding="utf-8")
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["Time", "Alarm", "Detail"])
            ws.append(["2026-01-01 00:00:00", "CLK032", "PLL unlock"])
            wb.save(root / "alarm.xlsx")
        except ImportError:
            self.skipTest("openpyxl not installed")
        rules = [
            {"id": "E1", "type": "text", "format": "csv", "columns": ["Alarm"],
             "globs": ["**/*.csv"], "any_of": ["CLK032"]},
            {"id": "E2", "type": "text", "format": "xlsx", "columns": ["Alarm"],
             "globs": ["**/*.xlsx"], "any_of": ["CLK032"]},
        ]
        roots = [{"id": "R1", "name": "时钟告警", "condition": "E1 and E2"}]
        rules_path = self.tmp / "tables.yaml"
        _write_rules(rules_path, rules, [], roots)
        data = _run(root, rules_path)
        self.assertEqual(set(data["hit_rules"]), {"E1", "E2"})
        self.assertEqual(data["verdict"]["status"], "matched")

    def test_multiple_root_causes_are_reported_as_conflict(self):
        root = self.tmp / "conflict"
        root.mkdir()
        (root / "fault.log").write_text(
            "2026-01-01 00:00:00 PLL unlock\n2026-01-01 00:00:00 optical rx low\n",
            encoding="utf-8")
        rules = [
            {"id": "E1", "type": "text", "globs": ["**/*.log"], "any_of": ["PLL unlock"]},
            {"id": "E2", "type": "text", "globs": ["**/*.log"], "any_of": ["optical rx low"]},
        ]
        roots = [
            {"id": "R1", "name": "时钟失锁", "condition": "E1", "confidence": "high"},
            {"id": "R2", "name": "光模块收光异常", "condition": "E2", "confidence": "high"},
        ]
        rules_path = self.tmp / "conflict.yaml"
        _write_rules(rules_path, rules, [], roots)
        data = _run(root, rules_path)
        group = data["verdict"]["groups"]["ALL"]
        self.assertEqual(group["status"], "matched")
        self.assertEqual(group["primary"], "R1")
        self.assertEqual(group["others"], ["R2"])


if __name__ == "__main__":
    unittest.main()
