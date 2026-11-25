"""服务重启后的任务恢复语义。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.config import Config
from bbu.db import Database
from bbu.worker import TaskWorker


class DummyPipeline:
    def run(self, task_id, event):
        return {}


class RecoveryTests(unittest.TestCase):
    def test_running_task_is_requeued_once_then_failed(self):
        cfg = Config(ROOT)
        tmp = Path(tempfile.mkdtemp(prefix="bbu-recovery-"))
        cfg.values.update({"data_dir": str(tmp), "max_queue": 4, "max_task_resume": 1})
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,created_at) "
            "VALUES(?,?,?,?,?)", ("u", "u", "admin", "x", "2026-01-01T00:00:00Z"))
        uid = db.one("SELECT id FROM users")["id"]
        for task_id, resume in (("T-RESUME", 0), ("T-EXHAUST", 1)):
            db.execute(
                "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
                "input_json,options_json,resume_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, uid, task_id, "test", "running", "fanout", 50, "fast",
                 "[]", "{}", resume, "2026-01-01T00:00:00Z"))
        worker = TaskWorker(db, cfg, DummyPipeline())
        try:
            worker._recover()
            resumed = db.one("SELECT status,resume_count FROM tasks WHERE id='T-RESUME'")
            exhausted = db.one("SELECT status,error FROM tasks WHERE id='T-EXHAUST'")
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["resume_count"], 1)
            self.assertEqual(worker.stats()["queued"], 1)
            self.assertEqual(exhausted["status"], "failed")
            self.assertIn("自动恢复次数", exhausted["error"])
        finally:
            worker.shutdown()


if __name__ == "__main__":
    unittest.main()
