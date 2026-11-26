import tempfile
import unittest
from pathlib import Path

from bbu.agent import BoundedAgent
from bbu.calibration import CalibrationStore
from bbu.claims import validate_claims, validate_narrative
from bbu.config import Config
from bbu.db import Database
from bbu.decision_cards import build_decision_cards
from bbu.util import iso_now

ROOT = Path(__file__).resolve().parent.parent


class R4TrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-r4-")
        self.db = Database(Path(self.tmp.name) / "bbu.db")
        self.config = Config(ROOT)
        self.config.values.update({"data_dir": self.tmp.name, "agent_engine_mode": "shadow",
                                   "agent_max_steps": 2, "agent_max_skills": 4,
                                   "agent_max_replans": 0,
                                   "calibration_min_samples": 2})

    def tearDown(self):
        self.tmp.cleanup()

    def _eval(self, split: str, rows: list[tuple[str, bool]], name: str = "e1"):
        self.db.execute(
            "INSERT INTO evaluations(id,name,split,status,config_json,summary_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (name, name, split, "done", "{}", "{}", iso_now()))
        for idx, (confidence, exact) in enumerate(rows):
            import json
            self.db.execute(
                "INSERT INTO evaluation_cases(evaluation_id,case_id,case_name,status,"
                "expected_json,predicted_json,metrics_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (name, f"c{idx}", f"case{idx}", "ok", "[]",
                 json.dumps({"confidence": confidence}),
                 json.dumps({"exact": exact}), iso_now()))

    def test_calibration_uses_dev_only_and_marks_small_samples(self):
        self._eval("dev", [("high", True), ("high", False), ("low", False)])
        self._eval("hidden", [("high", True)] * 10, "hidden-eval")
        store = CalibrationStore(self.db, self.config)
        snap = store.fit("dev")
        self.assertEqual(snap["sample_count"], 3)
        self.assertEqual(snap["status"], "calibrated")
        self.assertEqual(snap["levels"]["high"]["accuracy"], 0.5)
        with self.assertRaises(ValueError):
            store.fit("hidden")
        empty = CalibrationStore(self.db, self.config)
        self.assertEqual(empty.annotate("high")["status"], "calibrated")
        # 未拟合的新库必须保守
        fresh = Database(Path(self.tmp.name) / "fresh.db")
        self.assertEqual(CalibrationStore(fresh, self.config).annotate("high")["status"], "uncalibrated")

    def test_bounded_agent_blocks_large_plan_and_unknown_skill(self):
        agent = BoundedAgent(self.db, self.config)
        catalog = [{"name": "demo"}, {"name": "other"}]
        plan = {"steps": [{"id": "S1", "skills": ["demo", "evil"]},
                          {"id": "S2", "skills": ["demo"]},
                          {"id": "S3", "skills": ["other"]}], "replan": [{}]}
        trace = agent.review_plan({"id": "T1"}, plan, catalog, [])
        self.assertEqual(trace["mode"], "shadow")
        self.assertTrue(any("未注册" in x for x in trace["violations"]))
        self.assertTrue(any("波次" in x for x in trace["violations"]))
        # shadow 不改变执行，bounded 才拒绝
        self.config.values["agent_engine_mode"] = "bounded"
        bounded = BoundedAgent(self.db, self.config).review_plan({"id": "T1"}, plan, catalog, [])
        self.assertEqual(bounded["mode"], "bounded")
        self.assertTrue(bounded["violations"])

    def test_claims_require_evidence_and_reject_fake_numbers(self):
        bundle = {"claims": [], "evidence_ids": ["E1", "E2"]}
        check = validate_claims(bundle, evidence=[], arbitration={})
        self.assertTrue(check["ok"])
        bad = {"claims": [{"id": "CL1", "kind": "root_cause", "text": "根因是电源",
                           "evidence_ids": [], "value": "电源"}], "evidence_ids": ["E1"]}
        result = validate_claims(bad, evidence=[], arbitration={})
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "missing_evidence")
        fake_number = {
            "claims": [{"id": "CL1", "kind": "number", "text": "数字：999",
                        "evidence_ids": ["E1"], "value": "999"}],
            "evidence_ids": ["E1"],
            "evidence_texts": {"E1": "温度 42℃"},
        }
        number_check = validate_claims(
            fake_number, evidence=[{"evidence_id": "E1", "text": "温度 42℃"}],
            arbitration={})
        self.assertFalse(number_check["ok"])
        self.assertEqual(number_check["errors"][0]["code"], "unsupported_number")
        good_number = {**fake_number,
                       "claims": [{**fake_number["claims"][0], "text": "数字：42",
                                   "value": "42"}],
                       "evidence_texts": {"E1": "温度 42℃"}}
        self.assertTrue(validate_claims(
            good_number, evidence=[{"evidence_id": "E1", "text": "温度 42℃"}],
            arbitration={})["ok"])
        narrative = {"headline": "温度达到 999℃", "conclusion": "根因是电源"}
        ncheck = validate_narrative(narrative, {"claims": []}, evidence=[])
        self.assertFalse(ncheck["ok"])

    def test_decision_cards_distinguish_missing_from_contradicted(self):
        class SkillStub:
            profile = {
                "root_causes": [{"id": "R1", "name": "电源跌落", "condition": "E1 and not E2"}],
                "evidence_rules": [{"id": "E1", "description": "电源告警"},
                                   {"id": "E2", "description": "看门狗复位"}],
                "symptoms": {"lookalikes": [{"fault": "CPU 中断", "differentiator": "无 caterr"}]},
                "domain": {"version_scope": "V1"},
            }
        result = {"evidence": [{"rule_id": "E2", "hit": True}]}
        cards = build_decision_cards(SkillStub(), result)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["state"], "contradicted")
        missing = build_decision_cards(SkillStub(), {"evidence": []})
        self.assertEqual(missing[0]["state"], "missing_data")
        self.assertTrue(missing[0]["limitations"])


if __name__ == "__main__":
    unittest.main()
