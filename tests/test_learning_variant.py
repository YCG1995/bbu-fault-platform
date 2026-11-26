"""自学习同根因变体的最小回归。

验证：同一根因、不同日志输入进入 same_root_variant 候选；完全相同输入只
返回 duplicate_root，不制造候选洪水。
"""
import json
import tempfile
import unittest
from pathlib import Path

from bbu.config import Config
from bbu.db import Database
from bbu.learning import LearningService
from bbu.util import iso_now


class LearningVariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-learning-")
        self.root = Path(self.tmp.name)
        self.cfg = Config(self.root)
        self.db = Database(self.root / "data" / "bbu.db")
        self.service = LearningService(self.db, self.cfg, object(), object())
        self._task("T1", "sha-a")
        self._task("T2", "sha-b")
        self._task("T3", "sha-a", name="T1.zip")
        # 先有一个同根因的自学习草稿，模拟历史任务已经沉淀。
        self.db.execute(
            "INSERT INTO workshop_drafts(id,owner_id,name,title,fault_name,category,status,"
            "profile_yaml,description,source_text,analysis_json,check_json,build_json,"
            "source_task_id,name_auto,name_locked,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("D1", 1, "draft-clock", "时钟", "参考时钟源失锁", "自学习", "editing",
             "", "d", "", "{}", "{}", "{}", "T1", 1, 0, iso_now(), iso_now()))

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, tid: str, sha: str, name: str = ""):
        payload = [{"name": name or f"{tid}.zip", "sha256": sha, "size": 10}]
        result = {"verdict": {"status": "matched", "root_cause_name": "参考时钟源失锁",
                              "confidence": "high", "primary": "R1"},
                  "arbitration": {"chosen_skill": "demo-bbu-clock-loss",
                                  "root_cause_name": "参考时钟源失锁",
                                  "confidence": "high", "reason": "直接证据"}}
        self.db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
            "input_json,options_json,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, 1, tid, f"{tid} 时钟失锁", "done", "done", 100, "fast",
             json.dumps(payload, ensure_ascii=False), "{}",
             json.dumps(result, ensure_ascii=False), iso_now()))

    def test_same_root_new_variant_is_candidate(self):
        learning = self.service.after_task("T2")
        self.assertEqual(learning["status"], "candidate_recorded")
        self.assertEqual(learning["draft_id"], "D1")
        self.assertIn("candidate_id", learning)
        row = self.db.one("SELECT * FROM experience_candidates WHERE id=?",
                          (learning["candidate_id"],))
        self.assertEqual(row["kind"], "same_root_variant")
        self.assertEqual(row["status"], "pending")
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["payload"]["draft_id"], "D1")

    def test_same_root_same_input_is_duplicate(self):
        learning = self.service.after_task("T3")
        self.assertEqual(learning["status"], "duplicate_root")
        self.assertEqual(learning["draft_id"], "D1")
        self.assertEqual(self.db.all("SELECT * FROM experience_candidates"), [])


if __name__ == "__main__":
    unittest.main()
