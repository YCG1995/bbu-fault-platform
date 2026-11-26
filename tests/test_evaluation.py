import shutil
import tempfile
import unittest
from pathlib import Path

from bbu.config import Config
from bbu.db import Database
from bbu.evaluation import EvaluationService, RealCaseStore

ROOT = Path(__file__).resolve().parent.parent


class EvaluationInfrastructureTests(unittest.TestCase):
    def test_labeled_positive_and_negative_cases_are_scored_and_unlabeled_ignored(self):
        with tempfile.TemporaryDirectory(prefix="bbu-eval-test-") as tmp:
            tmp = Path(tmp)
            db = Database(tmp / "bbu.db")
            config = Config(ROOT)
            # 评测只依赖示例技能，不应依赖开发机根目录 skills/ 是否已安装 demo。
            config.values["skills_dir"] = str(tmp / "skills")
            config.values["cases_dir"] = str(tmp / "cases")
            shutil.copytree(ROOT / "examples/skills", tmp / "skills")
            store = RealCaseStore(db, tmp)
            store.ingest(
                [ROOT / "tests/fixtures/logs/clock-demo.zip"],
                name="selftest-clock", title="时钟样例",
                description="NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
                split="test", status="golden", expected_roots=["参考时钟源失锁"],
                origin="synthetic")
            store.ingest(
                [ROOT / "tests/fixtures/logs/normal-demo.zip"],
                name="selftest-normal", title="正常样例",
                description="设备运行正常，无告警。",
                split="test", status="golden", expected_roots=["__unknown__"],
                origin="synthetic")
            store.ingest(
                [ROOT / "tests/fixtures/logs/mixed-demo.zip"],
                name="selftest-unlabeled", title="未标注样例",
                description="混合样例。", split="test", status="unlabeled",
                origin="real")
            self.assertEqual(store.get("selftest-unlabeled")["origin"], "real")

            service = EvaluationService(db, config, data_dir=tmp, case_store=store)
            result = service.evaluate(split="test", name="selftest")
            summary = result["summary"]
            self.assertEqual(summary["cases"], 2)
            self.assertEqual(summary["exact"], 2)
            self.assertEqual(summary["false_positive_rate"], 0.0)
            self.assertEqual(summary["errors"], 0)
            self.assertEqual({c["case"]["name"] for c in result["cases"]},
                             {"selftest-clock", "selftest-normal"})
            self.assertIsInstance(summary["exact_rate_ci95"], list)
            self.assertEqual(summary["llm_mode"], "off")
            self.assertEqual(summary["llm"]["calls"], 0)
            self.assertTrue(summary["leakage"]["ok"])
            self.assertFalse(summary["hidden_gate"]["applied"])
            self.assertTrue((tmp / "evaluations" / result["id"] / "summary.json").exists())
            self.assertTrue((tmp / "evaluations" / result["id"] / "report.md").exists())

            with self.assertRaisesRegex(ValueError, "llm_mode"):
                service.evaluate(split="test", llm_mode="bogus")
            with self.assertRaisesRegex(ValueError, "真实模型"):
                service.evaluate(split="test", llm_mode="full")

    def test_llm_usage_counts_narrative_and_model_calls(self):
        items = [
            {"result": {"stats": {"llm_calls": 2, "total_tokens": 300,
                                  "models": ["model-a"]},
                        "narrative": {"headline": "ok"},
                        "narrative_check": {"ok": True, "skipped": False, "errors": []}}},
            {"result": {"stats": {"llm_calls": 1, "total_tokens": 100,
                                  "models": ["model-a"]},
                        "narrative": None,
                        "narrative_check": {"ok": False, "skipped": False, "errors": [{"code": "x"}]}}},
        ]
        self.assertEqual(EvaluationService._llm_usage(items), {
            "calls": 3, "tokens": 400, "models": ["model-a"], "used_cases": 2,
            "narrative_used": 1, "narrative_fallback": 1})

    def test_cross_split_leakage_is_detected_and_hidden_gate_blocks_sparse_sets(self):
        with tempfile.TemporaryDirectory(prefix="bbu-eval-leak-") as tmp:
            tmp = Path(tmp)
            db = Database(tmp / "bbu.db")
            config = Config(ROOT)
            config.values.update({"skills_dir": str(tmp / "skills"), "cases_dir": str(tmp / "cases"),
                                  "evaluation_hidden_min_cases": 2,
                                  "evaluation_hidden_min_f1": 0.9})
            store = RealCaseStore(db, tmp)
            store.ingest([ROOT / "tests/fixtures/logs/clock-demo.zip"],
                         name="clock-dev", title="时钟样例 A",
                         description="NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
                         split="dev", status="golden", expected_roots=["参考时钟源失锁"])
            store.ingest([ROOT / "tests/fixtures/logs/clock-demo.zip"],
                         name="clock-hidden", title="时钟样例 A",
                         description="NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
                         split="hidden", status="golden", expected_roots=["参考时钟源失锁"])
            store.ingest([ROOT / "tests/fixtures/logs/normal-demo.zip"],
                         name="normal-test", title="时钟样例 A",
                         description="NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
                         split="test", status="golden", expected_roots=["__unknown__"])
            service = EvaluationService(db, config, data_dir=tmp, case_store=store)
            audit = service.audit_splits()
            self.assertFalse(audit["ok"])
            kinds = {item["kind"] for item in audit["leaks"]}
            self.assertIn("exact_input", kinds)
            self.assertIn("near_description", kinds)
            with self.assertRaisesRegex(ValueError, "泄漏"):
                service.evaluate(split="hidden")

            clean_db = Database(tmp / "clean" / "bbu.db")
            clean = RealCaseStore(clean_db, tmp / "clean")
            clean.ingest([ROOT / "tests/fixtures/logs/normal-demo.zip"],
                         name="only-hidden", title="normal", description="设备运行正常，无告警。",
                         split="hidden", status="golden", expected_roots=["__unknown__"])
            sparse = EvaluationService(clean_db, config, data_dir=tmp / "clean", case_store=clean)
            with self.assertRaisesRegex(ValueError, "至少需要"):
                sparse.evaluate(split="hidden")

    def test_case_review_assignment_label_history_and_queue_filters(self):
        with tempfile.TemporaryDirectory(prefix="bbu-case-review-") as tmp:
            tmp = Path(tmp)
            db = Database(tmp / "bbu.db")
            store = RealCaseStore(db, tmp)
            case = store.ingest(
                [ROOT / "tests/fixtures/logs/clock-demo.zip"],
                name="review-queue-clock", title="待复核时钟样例",
                description="CLK032 时钟失锁。", split="unlabeled", status="unlabeled",
                origin="real")
            reviewer = db.execute(
                "INSERT INTO users(username,display_name,password_hash,role,active,status,created_at) "
                "VALUES(?,?,?,?,?,?,datetime('now'))",
                ("reviewer-r5", "R5 复核员", "x", "reviewer", 1, "active"))
            assigned = store.assign_reviewer(case["id"], reviewer, "reviewer-r5", actor="admin")
            self.assertEqual(assigned["review_status"], "pending")
            self.assertEqual(assigned["reviewer_id"], reviewer)
            self.assertEqual(len(store.list(reviewer_id=reviewer, review_status="pending")), 1)
            self.assertEqual(len(store.list(reviewer_id=reviewer, review_status="confirmed")), 0)
            note = db.one("SELECT COUNT(*) AS n FROM notifications WHERE user_id=?", (reviewer,))
            self.assertEqual(note["n"], 1)
            with self.assertRaises(ValueError):
                store.update_labels(case["id"], status="golden", split="unlabeled",
                                    expected_roots=["时钟源失锁"])
            updated = store.update_labels(
                case["id"], expected_roots=["时钟源失锁"], excluded_roots=["光模块故障"],
                split="test", status="golden",
                labels={"evidence_window": {"start": "2025-01-01 00:00", "end": "2025-01-01 00:10"},
                        "recovery_result": "已恢复"},
                actor_id=reviewer, actor_name="reviewer-r5", action="approve",
                review_status="confirmed", note="现场确认")
            self.assertEqual(updated["review_status"], "confirmed")
            self.assertEqual(updated["split"], "test")
            history = store.history(case["id"])
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["action"], "approve")
            self.assertEqual(history[-1]["action"], "assign")
            self.assertEqual(history[0]["new"]["review_status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
