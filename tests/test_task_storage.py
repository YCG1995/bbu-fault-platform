"""任务空间统计和共享输入释放。"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from bbu.config import Config
from bbu.db import Database
from bbu.task_storage import TaskStorageService


class TaskStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = Config(self.tmp)
        cfg.values.update({
            "data_dir": str(self.tmp / "data"),
            "skills_dir": str(self.tmp / "skills"),
            "cases_dir": str(self.tmp / "cases"),
            "storage_max_workers": 1,
            "storage_max_queue": 8,
        })
        cfg.ensure_dirs()
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.service = TaskStorageService(self.db, cfg)

    def tearDown(self):
        self.service.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, task_id, path, status="done"):
        self.db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
            "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, 1, task_id, "", status, "done", 100, "quick",
             json.dumps([{"name": "a.log", "stored_path": str(path)}]), "{}", "2026-01-01T00:00:00Z"))

    def _wait(self, job_id):
        deadline = time.time() + 5
        while time.time() < deadline:
            job = self.service.get(job_id)
            if job["status"] in ("done", "failed", "interrupted"):
                return job
            time.sleep(0.05)
        self.fail("storage job timeout")

    def test_shared_inputs_are_only_deleted_by_last_reference(self):
        src = self.cfg.data_dir / "uploads" / "T1" / "a.log"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"x" * 100)
        self._task("T1", src)
        self._task("T2", src)
        overview = self.service.overview(1, is_admin=False)
        by_id = {x["id"]: x for x in overview["items"]}
        self.assertEqual(by_id["T1"]["shared_input_paths"], 1)
        self.assertEqual(by_id["T1"]["deletable_bytes"], 0)

        first = self._wait(self.service.create("T1", 1)["id"])
        self.assertEqual(first["status"], "done")
        self.assertEqual(first["summary"]["files"], 0)
        self.assertTrue(src.exists())
        self.assertEqual(self.db.one("SELECT inputs_state FROM tasks WHERE id='T1'")["inputs_state"],
                         "released")

        second = self._wait(self.service.create("T2", 1)["id"])
        self.assertEqual(second["status"], "done")
        self.assertEqual(second["summary"]["freed_bytes"], 100)
        self.assertFalse(src.exists())
        self.assertEqual(self.db.one("SELECT inputs_state FROM tasks WHERE id='T2'")["inputs_state"],
                         "released")

    def test_release_extracted_keeps_reports_and_raw_inputs(self):
        src = self.cfg.data_dir / "uploads" / "T5" / "a.log"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"raw-input")
        self._task("T5", src)
        task_dir = self.cfg.data_dir / "tasks" / "T5"
        unpacked = task_dir / "skills" / "vsw" / "unpacked" / "logs"
        unpacked.mkdir(parents=True)
        (unpacked / "huge.log").write_bytes(b"z" * 4096)
        report = task_dir / "report.md"
        report.write_text("report", encoding="utf-8")
        detail = self.service.overview(1, False)["items"][0]
        self.assertGreaterEqual(detail["extracted_bytes"], 4096)
        self.assertEqual(detail["extracted_dirs"], 1)
        self.assertTrue(detail["can_release_extracted"])

        job = self._wait(self.service.create("T5", 1, mode="extracted")["id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["mode"], "extracted")
        self.assertGreaterEqual(job["summary"]["extracted_freed_bytes"], 4096)
        self.assertFalse(unpacked.exists())
        self.assertTrue(report.exists())
        self.assertTrue(src.exists())
        self.assertEqual(self.db.one("SELECT inputs_state FROM tasks WHERE id='T5'")["inputs_state"],
                         "available")

    def test_artifacts_are_kept_and_running_task_is_rejected(self):
        src = self.cfg.data_dir / "uploads" / "T3" / "a.log"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"y" * 20)
        self._task("T3", src)
        artifact = self.cfg.data_dir / "tasks" / "T3" / "report.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("report", encoding="utf-8")
        self.assertGreater(self.service.overview(1, False)["items"][0]["artifact_bytes"], 0)
        self._wait(self.service.create("T3", 1)["id"])
        self.assertTrue(artifact.exists())

        src2 = self.cfg.data_dir / "uploads" / "T4" / "b.log"
        src2.parent.mkdir(parents=True)
        src2.write_bytes(b"z")
        self._task("T4", src2, status="running")
        with self.assertRaisesRegex(ValueError, "正在运行"):
            self.service.create("T4", 1)


if __name__ == "__main__":
    unittest.main()
