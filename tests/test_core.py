"""核心纯函数与安全边界测试；只依赖标准库 + vendor PyYAML。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import Budget, BudgetExceeded, Usage, extract_json  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.security import hash_password, verify_password  # noqa: E402
from bbu.skills import Matcher, SkillRegistry  # noqa: E402
from bbu.storage import extract_archive  # noqa: E402


class PasswordTests(unittest.TestCase):
    def test_hash_verify_roundtrip(self):
        stored = hash_password("S3cret!pass")
        self.assertTrue(verify_password("S3cret!pass", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertTrue(stored.startswith("scrypt$"))


class AIJsonTests(unittest.TestCase):
    def test_fenced_json(self):
        text = "说明\n```json\n{\"candidates\": [{\"skill\": \"a\"}]}\n```\n结束"
        self.assertEqual(extract_json(text)["candidates"][0]["skill"], "a")

    def test_json_inside_prose(self):
        text = "前面 {\"ok\": true, \"n\": 2} 后面"
        self.assertEqual(extract_json(text)["n"], 2)

    def test_invalid_returns_none(self):
        self.assertIsNone(extract_json("not json"))


class BudgetTests(unittest.TestCase):
    def test_call_limit(self):
        budget = Budget(max_calls=1, max_tokens=100)
        budget.check()
        budget.record(Usage(total_tokens=3))
        with self.assertRaises(BudgetExceeded):
            budget.check()

    def test_token_limit(self):
        budget = Budget(max_calls=10, max_tokens=10)
        budget.record(Usage(total_tokens=11))
        with self.assertRaises(BudgetExceeded):
            budget.check()

    def test_atomic_reservation_prevents_parallel_overrun(self):
        budget = Budget(max_calls=1, max_tokens=100)
        reservation = budget.reserve(10)
        with self.assertRaises(BudgetExceeded):
            budget.reserve(10)
        budget.release(reservation)
        reservation = budget.reserve(10)
        budget.settle(reservation, Usage(total_tokens=10))
        self.assertEqual(budget.calls, 1)
        self.assertEqual(budget.snapshot()["pending_calls"], 0)
        with self.assertRaises(BudgetExceeded):
            budget.reserve(10)


class MatcherTests(unittest.TestCase):
    def test_example_clock_skill_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            registry = SkillRegistry(db, ROOT / "examples" / "skills")
            registry.scan()
            results = Matcher(registry).match(
                "NE_A 上报 CLK032 时钟失锁，PLL 无法锁定，设备 ITBBU", ["clockstatuslog.txt"])
            self.assertTrue(results)
            self.assertEqual(results[0].skill.name, "demo-bbu-clock-loss")
            self.assertGreaterEqual(results[0].score, 30)


class ExtractSafetyTests(unittest.TestCase):
    def test_zip_traversal_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "evil.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "should not be written")
                zf.writestr("safe/inside.txt", "ok")
            dest = tmp_path / "out"
            stats = extract_archive(archive, dest, {"max_files": 10, "max_bytes": 1024 * 1024, "max_depth": 1})
            self.assertFalse((tmp_path / "escape.txt").exists())
            self.assertTrue((dest / "safe" / "inside.txt").exists())
            self.assertTrue(any("escape" in item for item in stats["skipped"]))


if __name__ == "__main__":
    unittest.main()
