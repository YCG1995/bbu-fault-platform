"""真实任务卡 Schema、hidden 隔离和标签哈希回归。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.task_card import SCHEMA_VERSION, label_hash, validate_task_card  # noqa: E402


def _card(**overrides):
    card = {
        "schema_version": SCHEMA_VERSION, "card_id": "TC-001",
        "title": "NE_A 时钟失锁", "description": "CLK032 告警，PLL 无法锁定。",
        "device_type": "ITBBU", "software_version": "V5.0", "site": "NE_A",
        "labeler": "expert-1", "labeled_at": "2026-11-25", "status": "golden",
        "split": "hidden", "expected_roots": ["参考时钟源失锁"], "excluded_roots": [],
        "allowed_refusal": True, "forbidden_tools": ["web_search"],
        "required_evidence": [{"kind": "log_line", "description": "CLK032 alarm"}],
        "input_hashes": [{"name": "clock.zip", "sha256": "a" * 64, "size": 1024}],
        "hidden_isolated": True, "origin": "real",
    }
    card.update(overrides)
    return card


class TaskCardTests(unittest.TestCase):
    def test_valid_hidden_card_and_label_hash(self):
        card = _card()
        result = validate_task_card(card)
        self.assertTrue(result["ok"])
        self.assertEqual(result["label_hash"], label_hash(card))
        self.assertEqual(result["public"]["split"], "hidden")

    def test_hidden_card_requires_isolation_and_real_origin(self):
        with self.assertRaisesRegex(ValueError, "hidden_isolated"):
            validate_task_card(_card(hidden_isolated=False))
        with self.assertRaisesRegex(ValueError, "合成"):
            validate_task_card(_card(origin="synthetic"))

    def test_hashes_and_required_evidence_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "sha256"):
            validate_task_card(_card(input_hashes=[{"name": "x", "sha256": "bad", "size": 1}]))
        with self.assertRaisesRegex(ValueError, "required_evidence"):
            validate_task_card(_card(required_evidence=[]))

    def test_label_hash_ignores_description_and_inputs(self):
        one = validate_task_card(_card(description="描述 A"))
        two = validate_task_card(_card(description="描述 B", input_hashes=[
            {"name": "other.zip", "sha256": "b" * 64, "size": 2}]))
        self.assertEqual(one["label_hash"], two["label_hash"])


if __name__ == "__main__":
    unittest.main()
