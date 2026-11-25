"""规划器与经验库的单元测试：校验边界、门槛、停止条件和经验检索。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import DEFAULTS
from bbu.db import Database
from bbu.learning import ExperienceStore
from bbu.planner import (deterministic_plan, gate_allows, needs_replan, normalize_plan,
                         plan_candidates, plan_skill_names)


class _Config:
    def get(self, key, default=None):
        return DEFAULTS.get(key, default)


CATALOG = [
    {"skill": "demo-bbu-clock-loss", "title": "时钟"},
    {"skill": "demo-bbu-optical-rx", "title": "光模块"},
    {"skill": "demo-bbu-other", "title": "其他"},
]


class PlanValidationTests(unittest.TestCase):
    def test_normalize_caps_and_drops_unknown(self):
        data = {
            "in_scope": True, "summary": "测试",
            "hypothesis": ["A", "B"],
            "steps": [
                {"id": "P1", "title": "首波", "objective": "o",
                 "skills": ["demo-bbu-clock-loss", "not-exists", "demo-bbu-clock-loss"],
                 "gate": "always", "reason": "r"},
                {"id": "P2", "title": "补证", "objective": "o",
                 "skills": ["demo-bbu-optical-rx"], "gate": "previous_unmatched", "reason": "r"},
            ],
            "missing_info": [],
        }
        plan, errors, warnings = normalize_plan(data, CATALOG, _Config(), "deep")
        self.assertEqual(errors, [])
        self.assertEqual(plan["steps"][0]["skills"], ["demo-bbu-clock-loss"])
        self.assertEqual(len(plan["steps"]), 2)
        self.assertTrue(any("目录外技能" in x for x in warnings))
        self.assertEqual([s["gate_label"] for s in plan["steps"]], ["无条件", "前序未命中"])

    def test_first_gate_forced_to_always(self):
        data = {
            "in_scope": True, "summary": "测试", "hypothesis": [],
            "steps": [{"id": "P1", "title": "首波", "objective": "o",
                       "skills": ["demo-bbu-clock-loss"],
                       "gate": "previous_gap", "reason": "r"}],
            "missing_info": [],
        }
        plan, errors, warnings = normalize_plan(data, CATALOG, _Config(), "fast")
        self.assertEqual(errors, [])
        self.assertEqual(plan["steps"][0]["gate"], "always")
        self.assertTrue(any("首步" in x for x in warnings))

    def test_empty_initial_plan_is_error_but_replan_allowed(self):
        data = {"in_scope": True, "summary": "无新增", "hypothesis": [],
                "steps": [], "missing_info": []}
        plan, errors, _ = normalize_plan(data, CATALOG, _Config(), "fast")
        self.assertTrue(errors)
        plan2, errors2, _ = normalize_plan(data, CATALOG, _Config(), "fast",
                                           replan=True, allow_empty=True)
        self.assertEqual(errors2, [])
        self.assertEqual(plan2["steps"], [])

    def test_out_of_scope_is_valid(self):
        data = {"in_scope": False, "summary": "超出范围", "hypothesis": [],
                "steps": [], "missing_info": ["需要电源采集包"]}
        plan, errors, _ = normalize_plan(data, CATALOG, _Config(), "fast")
        self.assertEqual(errors, [])
        self.assertFalse(plan["in_scope"])
        self.assertIn("需要电源采集包", plan["missing_info"])


class PlanBehaviorTests(unittest.TestCase):
    def test_deterministic_plan_and_candidates(self):
        candidates = [{"skill": "demo-bbu-clock-loss", "score": 70},
                      {"skill": "demo-bbu-optical-rx", "score": 40}]
        plan = deterministic_plan(candidates, _Config(), "fast")
        self.assertEqual(plan["source"], "deterministic")
        self.assertEqual(len(plan_skill_names(plan)), 2)
        expanded = plan_candidates(plan, {"demo-bbu-clock-loss": 70,
                                          "demo-bbu-optical-rx": 40}, 30)
        self.assertEqual([x["skill"] for x in expanded],
                         ["demo-bbu-clock-loss", "demo-bbu-optical-rx"])

    def test_gate_behaviour(self):
        matched = {"ok": True, "status": "matched",
                   "verdict": {"status": "matched"},
                   "root_cause": {"name": "R1", "confidence": "high"}}
        unmatched = {"ok": True, "status": "unmatched", "verdict": {"status": "unmatched"},
                     "root_cause": None, "limitations": ["glob 兜底"]}
        self.assertTrue(gate_allows("previous_unmatched", [matched, unmatched]))
        self.assertFalse(gate_allows("previous_unmatched", [matched]))
        self.assertTrue(gate_allows("previous_gap", [matched, unmatched]))
        self.assertFalse(gate_allows("previous_conflict", [matched]))
        self.assertTrue(gate_allows("previous_low_confidence",
                                    [{"verdict": {"status": "matched"},
                                      "root_cause": {"confidence": "medium"}}]))

    def test_needs_replan_only_when_unresolved(self):
        high = {"ok": True, "status": "matched", "verdict": {"status": "matched"},
                "root_cause": {"name": "R1", "confidence": "high"}}
        medium = {"ok": True, "status": "matched", "verdict": {"status": "matched"},
                  "root_cause": {"name": "R1", "confidence": "medium"}}
        self.assertFalse(needs_replan([high], ["skill-b"])[0])
        self.assertTrue(needs_replan([medium], ["skill-b"])[0])
        self.assertFalse(needs_replan([medium], [])[0])


class ExperienceTests(unittest.TestCase):
    def test_record_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "bbu.db")
            store = ExperienceStore(db)
            task = {"id": "T1", "description": "NE_A 上报 CLK032 时钟失锁，PLL 无法锁定",
                    "status": "running"}
            result = {
                "verdict": {"status": "matched", "root_cause_name": "参考时钟源失锁",
                            "confidence": "high", "primary": "R1"},
                "arbitration": {"chosen_skill": "demo-bbu-clock-loss",
                                "root_cause_name": "参考时钟源失锁", "confidence": "high",
                                "reason": "直接证据", "missing_info": []},
                "plan": {"source": "ai", "summary": "先验时钟",
                         "steps": [{"id": "P1", "title": "时钟", "skills": ["demo-bbu-clock-loss"]}]},
                "inventory": {"detected_hints": ["clockstatuslog"]},
                "skills": [{"skill_name": "demo-bbu-clock-loss", "status": "matched",
                            "root_cause": {"name": "参考时钟源失锁"}, "score": 80}],
                "stats": {"duration_sec": 1.2, "llm_calls": 2},
            }
            store.record(task, result)
            rows = db.all("SELECT * FROM plan_experiences")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "done")
            found = store.search("CLK032 时钟失锁，PLL 锁不住", ["clockstatuslog"],
                                 "demo-bbu-clock-loss", include_unreviewed=True)
            self.assertTrue(found)
            self.assertEqual(found[0]["root_cause"], "参考时钟源失锁")
            self.assertIn("match_score", found[0])
            # 未复核经验默认不进入检索，避免污染线上诊断
            self.assertFalse(store.search("CLK032 时钟失锁，PLL 锁不住", ["clockstatuslog"],
                                          "demo-bbu-clock-loss"))


if __name__ == "__main__":
    unittest.main()
