"""证据质量、拒判与多根因的回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.ai import AIGateway, Budget
from bbu.config import Config
from bbu.db import Database
from bbu.evidence import assess_evidence
from bbu.learning import ExperienceStore
from bbu.pipeline import TaskPipeline
from bbu.skills import SkillRegistry, SkillRunner


def _result(skill: str, root_id: str, root_name: str, matched: bool = True) -> dict:
    meta = {rule: {"description": f"证据{rule}", "weight": 35, "verified": True,
                   "roots": [root_id]} for rule in ("E1", "E2")}
    evidence = [
        {"rule_id": "E1", "file": f"{skill}-a.log", "line_no": 1,
         "text": f"{root_name} direct evidence alpha 111111111"},
        {"rule_id": "E2", "file": f"{skill}-b.log", "line_no": 2,
         "text": f"{root_name} direct evidence beta 222222222"},
    ]
    return {
        "skill_name": skill, "ok": True, "score": 60,
        "status": "matched" if matched else "no_evidence",
        "verdict": {"status": "matched" if matched else "no_evidence",
                    "groups": {"ALL": {"hit_rules": ["E1", "E2"] if matched else []}}},
        "primary": root_id if matched else None,
        "root_cause": {"id": root_id, "name": root_name,
                       "confidence": "high"} if matched else None,
        "evidence": evidence, "rule_meta": meta, "limitations": [],
    }


class EvidenceQualityTests(unittest.TestCase):
    def test_single_repeated_log_does_not_become_high_confidence(self):
        result = _result("s", "R1", "根因甲")
        result["evidence"] = [dict(result["evidence"][0]) for _ in range(20)]
        quality = assess_evidence(result)
        self.assertLessEqual(quality["quality_score"], 44)
        self.assertIn("重复", " ".join(quality["reasons"]))

    def test_different_files_and_rules_get_high_quality(self):
        quality = assess_evidence(_result("s", "R1", "根因甲"))
        self.assertGreaterEqual(quality["quality_score"], 70)
        self.assertEqual(quality["confidence"], "high")
        self.assertEqual(len(quality["independent_files"]), 2)

    def test_multiple_independent_roots_are_not_merged(self):
        cfg = Config(ROOT)
        tmp = Path(tempfile.mkdtemp(prefix="bbu-evidence-"))
        cfg.values["data_dir"] = str(tmp)
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        registry = SkillRegistry(db, cfg.skills_dir)
        registry.scan()
        pipe = TaskPipeline(db, cfg, registry, AIGateway(db, cfg), SkillRunner(cfg),
                            experiences=ExperienceStore(db))
        arb = pipe._arbitrate({"id": "T1", "description": "mixed"},
                              [_result("skill-a", "R1", "根因甲"),
                               _result("skill-b", "R2", "根因乙")],
                              Budget(0, 0), set())
        self.assertEqual(arb["status"], "multiple_causes")
        self.assertEqual(len(arb["roots"]), 2)
        self.assertNotEqual(arb["roots"][0]["name"], arb["roots"][1]["name"])

    def test_abnormal_evidence_without_known_root_is_unknown_fault(self):
        cfg = Config(ROOT)
        tmp = Path(tempfile.mkdtemp(prefix="bbu-evidence-"))
        cfg.values["data_dir"] = str(tmp)
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        registry = SkillRegistry(db, cfg.skills_dir)
        registry.scan()
        pipe = TaskPipeline(db, cfg, registry, AIGateway(db, cfg), SkillRunner(cfg),
                            experiences=ExperienceStore(db))
        result = _result("s", "R1", "未知根因", matched=False)
        result["status"] = "unmatched"
        arb = pipe._arbitrate({"id": "T1", "description": "unknown"}, [result],
                              Budget(0, 0), set())
        self.assertEqual(arb["status"], "unknown_fault")
        self.assertEqual(arb["root_cause"], "")


if __name__ == "__main__":
    unittest.main()
