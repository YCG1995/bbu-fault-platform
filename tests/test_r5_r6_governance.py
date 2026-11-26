import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bbu.case_matrix import (build_matrix, decide_candidate, latest_matrix,
                             record_candidate, save_matrix, verify_matrix)
from bbu.config import Config
from bbu.db import Database
from bbu.evidence_store import EvidenceStore
from bbu.provenance import audit_provenance, lock_fields
from bbu.recovery import compare, create_run, get_run, list_runs, observe, review
from bbu.skills import SkillRegistry
from bbu.supply import sign_skill
from bbu.workshop_spec import merge_specs, parse_source, public_spec
from bbu.util import iso_now

ROOT = Path(__file__).resolve().parent.parent


class R5ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "tests/fixtures/intake/vsw-ssd.txt").read_text(encoding="utf-8")
        self.spec = parse_source(self.text)

    def test_fields_trace_to_source_and_audit_counts(self):
        prov = self.spec["provenance"]
        self.assertTrue(prov)
        self.assertIn("/fault_name", prov)
        audit = audit_provenance(self.spec)
        self.assertGreater(audit["hard_fields"], 10)
        self.assertEqual(audit["version"], "bbu-provenance/1")
        self.assertTrue(audit["source_revision"])
        self.assertTrue(any(x["validation_status"] == "verified" for x in prov.values()))
        public = public_spec(self.spec)
        self.assertNotIn("_source_text", public)
        self.assertIn("provenance", public)

    def test_hard_field_source_coverage_is_complete_for_three_intakes(self):
        for name in ("vsw-ssd.txt", "vsw-usbflash.txt", "vsw-watchdog.txt"):
            text = (ROOT / "tests/fixtures/intake" / name).read_text(encoding="utf-8")
            spec = parse_source(text)
            audit = audit_provenance(spec)
            self.assertTrue(audit["ok"], (name, audit["missing"]))
            self.assertEqual(audit["coverage"], 1.0, (name, audit["missing"]))
            self.assertGreater(audit["hard_fields"], 20)
            for path, item in spec["provenance"].items():
                if item.get("hard") and item.get("validation_status") == "derived":
                    self.assertTrue(item.get("quote"), (name, path))
                    self.assertTrue(item.get("derivation"), (name, path))

    def test_lock_survives_model_merge(self):
        lock_fields(self.spec, ["/fault_name"], True)
        self.assertTrue(self.spec["provenance"]["/fault_name"]["locked"])
        original = self.spec["fault_name"]
        merged = merge_specs(self.spec, {"fault_name": "模型改写的新故障名"})
        self.assertTrue(merged["provenance"]["/fault_name"]["locked"])
        # 锁定的不是“locked 标记”，而是字段值本身。
        self.assertEqual(merged["fault_name"], original)
        self.assertIn("/fault_name", merged.get("locked_restored") or [])
        self.assertEqual(merged["provenance"]["/fault_name"]["locked"], True)


class R5ManifestTrustTests(unittest.TestCase):
    def test_verified_requires_case_regression_not_just_signature(self):
        with tempfile.TemporaryDirectory(prefix="bbu-trust-") as tmp_name:
            tmp = Path(tmp_name)
            src = ROOT / "examples" / "skills" / "demo-bbu-clock-loss"
            target = tmp / "skills" / "demo-bbu-clock-loss"
            target.parent.mkdir(parents=True)
            shutil.copytree(src, target)
            # 示例技能没有案例目录；补一个最小 cases 声明，使 semantic_verified 成立。
            with (target / "assets" / "fault-profile.yaml").open("a", encoding="utf-8") as fh:
                fh.write('\ncases:\n  - id: "TC-smoke"\n    expected_root: "时钟失锁"\n')
            cfg = Config(tmp)
            cfg.values.update({"skills_dir": str(tmp / "skills"),
                               "data_dir": str(tmp / "data")})
            db = Database(tmp / "data" / "bbu.db")
            registry = SkillRegistry(db, tmp / "skills", tmp / "data", manifest_mode="shadow")
            unsigned = next(s for s in registry.scan() if s.name == "demo-bbu-clock-loss")
            self.assertFalse(unsigned.verified)
            self.assertEqual(unsigned.manifest_status, "missing")

            sign_skill(target, data_dir=tmp / "data", name="demo-bbu-clock-loss", version="1.0.0",
                       verification={"status": "quarantined", "ok": False})
            quarantined = next(s for s in registry.scan() if s.name == "demo-bbu-clock-loss")
            self.assertEqual(quarantined.manifest_status, "verified")
            self.assertFalse(quarantined.verified)

            sign_skill(target, data_dir=tmp / "data", name="demo-bbu-clock-loss", version="1.0.0",
                       verification={"status": "case_regression", "ok": True})
            trusted = next(s for s in registry.scan() if s.name == "demo-bbu-clock-loss")
            self.assertTrue(trusted.verified)

            (target / "scripts" / "run_analysis.py").write_text("print('tamper')\n",
                                                                encoding="utf-8")
            tampered = next(s for s in registry.scan() if s.name == "demo-bbu-clock-loss")
            self.assertFalse(tampered.verified)
            self.assertEqual(tampered.status, "incompatible")
            self.assertEqual(tampered.manifest_status, "tampered")


class R5CaseMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-r5-")
        self.db = Database(Path(self.tmp.name) / "bbu.db")
        self.db.execute("INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
                        "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ("T1", 1, "矩阵", "测试", "done", "done", 100, "fast", "[]", "{}", iso_now()))
        for i in range(3):
            tid = f"T{i + 1}"
            if i:
                self.db.execute(
                    "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
                    "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, 1, f"矩阵{i}", "测试", "done", "done", 100, "fast",
                     "[]", "{}", iso_now()))
            verdict = {"root_cause_name": "SSD寿命耗尽" if i < 2 else "__unmatched__",
                       "confidence": "high" if i == 0 else "low"}
            evidence = [{"rule_id": "E1" if i != 1 else "E3", "file": f"f{i}.log", "hit": True}]
            self.db.execute(
                "INSERT INTO task_skills(task_id,skill_name,skill_version,status,score,"
                "result_json,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                (tid, "demo-ssd", "1.0", "matched" if i < 2 else "unmatched", 80,
                 json.dumps({"verdict": verdict, "evidence": evidence}), iso_now(), iso_now()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_matrix_aggregates_and_signature_binds_content(self):
        payload = build_matrix(self.db, "demo-ssd", "build-1", {"version_scope": "V1"})
        self.assertEqual(payload["summary"]["cases"], 3)
        self.assertEqual(payload["summary"]["roots"], 1)
        save_matrix(self.db, self.tmp.name, payload)
        latest = latest_matrix(self.db, "demo-ssd")
        self.assertIsNotNone(latest)
        self.assertTrue(verify_matrix(self.tmp.name, latest)["ok"])
        latest["tiles"][0]["cases"] = 999
        self.assertEqual(verify_matrix(self.tmp.name, latest)["reason"], "content_tampered")

    def test_candidate_is_idempotent_and_reviewable(self):
        candidate = record_candidate(self.db, task_id="T1", skill_name="demo-ssd",
                                     root_cause="SSD寿命耗尽", old_root="SSD器件故障",
                                     decision="correct", note="专家改判")
        again = record_candidate(self.db, task_id="T1", skill_name="demo-ssd",
                                 root_cause="SSD寿命耗尽", old_root="SSD器件故障",
                                 decision="correct", note="专家改判")
        self.assertEqual(candidate["id"], again["id"])
        decided = decide_candidate(self.db, candidate["id"], "approved", "expert", "通过")
        self.assertEqual(decided["status"], "approved")
        with self.assertRaises(ValueError):
            decide_candidate(self.db, candidate["id"], "bad", "expert")


class R6RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-r6-")
        self.db = Database(Path(self.tmp.name) / "bbu.db")
        self.before = {"verdict": {"status": "matched", "root_cause_name": "SSD寿命耗尽"},
                       "evidence": [{"rule_id": "E1", "file": "before.log"}]}
        self.after = {"verdict": {"status": "matched", "root_cause_name": ""},
                      "evidence": [{"rule_id": "E1", "file": "after.log", "hit": True}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_compare_and_learning_gate_requires_independent_evidence(self):
        self.assertEqual(compare(self.before, self.after)["status"], "recovered")
        self.assertEqual(compare(self.before, self.before)["status"], "still_fault")
        self.assertEqual(compare({}, self.after)["status"], "inconclusive")
        run = create_run(self.db, "T1", self.before, "operator")
        run = observe(self.db, run["id"], self.after)
        self.assertEqual(run["comparison"]["status"], "recovered")
        self.assertTrue(run["comparison"]["evidence_event_id"])
        events = EvidenceStore(self.db).list("T1")
        self.assertTrue(any(e["name"] == "recovery.verification" for e in events), events)
        reviewed = review(self.db, run["id"], "expert", "confirmed", "已恢复")
        self.assertTrue(reviewed["learning_gate"]["eligible"])
        self.assertEqual(len(list_runs(self.db, "T1")), 1)
        # 无独立文件证据时不允许学习，也不能再判 recovered
        no_evidence = dict(self.after)
        no_evidence["evidence"] = [{"rule_id": "E1", "hit": True}]
        run2 = create_run(self.db, "T1", self.before, "operator")
        run2 = observe(self.db, run2["id"], no_evidence)
        self.assertEqual(run2["comparison"]["after_independent_evidence"], 0)
        self.assertEqual(run2["comparison"]["status"], "inconclusive")
        reviewed2 = review(self.db, run2["id"], "expert", "confirmed")
        self.assertFalse(reviewed2["learning_gate"]["eligible"])


if __name__ == "__main__":
    unittest.main()
