#!/usr/bin/env python3
"""诊断推理数据层的单元测试：编译、目录、报告、启动种子。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))


from bbu.curated import seed_curated_experiences  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.learning import ExperienceStore  # noqa: E402
from bbu.reports import compose_markdown  # noqa: E402
from bbu.skills import Skill  # noqa: E402
from bbu.workshop_spec import compile_profile, parse_source  # noqa: E402


class ReasoningCompileTests(unittest.TestCase):
    def setUp(self):
        self.spec = parse_source(
            (ROOT / "tests/fixtures/intake/vsw-watchdog.txt").read_text(encoding="utf-8"))
        self.profile = compile_profile(self.spec, "bbu-fault-vsw-watchdog")

    def test_every_root_has_competing_hypothesis(self):
        reasoning = self.profile["diagnostic_reasoning"]
        root_ids = {r["id"] for r in self.profile["root_causes"]}
        hypothesis_roots = {h["root_cause"] for h in reasoning["hypotheses"]}
        self.assertEqual(root_ids, hypothesis_roots)

    def test_decision_order_only_references_real_rules(self):
        reasoning = self.profile["diagnostic_reasoning"]
        rule_ids = {r["id"] for s in self.profile["analysis"]["steps"] for r in s["rules"]}
        for step in reasoning["decision_order"]:
            self.assertTrue(set(step["rule_ids"]) <= rule_ids)
        self.assertTrue(reasoning["strategy"])
        self.assertGreaterEqual(len(reasoning["pitfalls"]), 3)

    def test_catalog_exposes_reasoning_brief(self):
        skill = Skill(name=self.profile["skill"]["name"], path=ROOT, title=self.profile["skill"]["title"],
                      profile=self.profile, aliases=self.profile["fault"]["aliases"])
        entry = skill.catalog_entry()
        self.assertIn("diagnostic_reasoning", entry)
        self.assertIn("MCE", entry["diagnostic_reasoning"]["strategy"])
        self.assertGreaterEqual(len(entry["diagnostic_reasoning"]["hypotheses"]), 3)


class ReasoningReportTests(unittest.TestCase):
    def test_report_contains_evidence_bound_reasoning_path(self):
        profile = compile_profile(parse_source(
            (ROOT / "tests/fixtures/intake/vsw-watchdog.txt").read_text(encoding="utf-8")),
            "bbu-fault-vsw-watchdog")
        root = next(r for r in profile["root_causes"] if r["id"] == "R2")
        result = {
            "skill_name": profile["skill"]["name"], "score": 5,
            "match_counts": {"E1": 0, "E2": 0, "E3": 1, "E4": 0},
            "evidence": [{"rule_id": "E3", "file": "BoardInit.log", "line_no": 4,
                          "text": "0x634=0x00000010"}],
            "root_cause": root, "status": "done",
            "verdict": {"groups": {"ALL": {"hit_rules": ["E3"], "root_causes": [
                {"id": "R1", "established": False},
                {"id": "R2", "established": True},
                {"id": "R3", "established": False}]}}},
            "diagnostic_reasoning": profile["diagnostic_reasoning"], "limitations": [],
        }
        md = compose_markdown(
            {"id": "T1", "username": "u", "created_at": "2026-11-25", "mode": "deep"},
            [result],
            {"chosen_skill": profile["skill"]["name"], "status": "matched", "confidence": "high"},
            {"files": [], "total_members": 0, "total_input_bytes": 0}, {}, None, None)
        self.assertIn("## 三、研判路径", md)
        self.assertIn("总体策略", md)
        self.assertIn("条件不成立，排除", md)
        self.assertIn("| H2 |", md)


class CuratedSeedTests(unittest.TestCase):
    def test_seed_and_search_playbook(self):
        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "x.db")
            count = seed_curated_experiences(db, ROOT / "knowledge/reasoning_playbooks.json")
            self.assertEqual(count, 3)
            hits = ExperienceStore(db).search("VSW 看门狗复位，EPLD 电源寄存器非 0", limit=2)
            self.assertTrue(hits)
            self.assertEqual(hits[0]["plan"]["source"], "curated_reasoning")
            self.assertIn("板内电源跌落导致看门狗复位", hits[0]["plan"]["hypothesis"])
            # 已在库中时不重复插入
            self.assertEqual(seed_curated_experiences(db, ROOT / "knowledge/reasoning_playbooks.json"), 0)


if __name__ == "__main__":
    unittest.main()
