import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.backups import BackupManager


class ConfigStub:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class BackupManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-backups-")
        self.work = Path(self.tmp.name)
        self.data = self.work / "data"
        self.data.mkdir()
        self.manager = BackupManager(None, ConfigStub(self.data))
        self.root = self.work / "backups"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _upgrade(self, name="upgrade-u1", phase="succeeded", job="u1"):
        backup = self.root / name
        backup.mkdir()
        (backup / "backup.json").write_text(json.dumps({
            "created_at": "2026-11-20T08:00:00+00:00", "old_version": "1.10.0",
            "new_version": "1.11.0", "has_state_backup": True,
        }), encoding="utf-8")
        (backup / "bbu.db").write_bytes(b"x" * 128)
        updates = self.data.parent / "updates" / job
        updates.mkdir(parents=True)
        (updates / "status.json").write_text(json.dumps({
            "id": job, "phase": phase, "backup_dir": str(backup),
        }), encoding="utf-8")
        return backup

    def test_overview_marks_kind_size_and_active_job(self):
        backup = self._upgrade(phase="queued")
        (self.root / "import-job2").mkdir()
        (self.root / "import-job2" / "note.txt").write_bytes(b"12")
        (self.root / "manual.zip").write_bytes(b"abc")
        data = self.manager.overview()
        items = {x["name"]: x for x in data["items"]}
        self.assertEqual(items["upgrade-u1"]["kind"], "upgrade")
        self.assertEqual(items["upgrade-u1"]["size_bytes"], 128 + len(
            (backup / "backup.json").read_bytes()))
        self.assertTrue(items["upgrade-u1"]["in_use"])
        self.assertIn("u1", items["upgrade-u1"]["active_jobs"])
        self.assertEqual(items["import-job2"]["kind"], "import")
        self.assertEqual(items["manual.zip"]["kind"], "manual")
        self.assertEqual(data["summary"]["count"], 3)

    def test_delete_removes_backup_but_rejects_active_reference(self):
        self._upgrade(phase="queued")
        with self.assertRaisesRegex(ValueError, "正在被升级任务使用"):
            self.manager.delete("upgrade-u1")
        # 升级结束后同一快照可作为历史记录删除。
        status = self.data.parent / "updates" / "u1" / "status.json"
        status.write_text(json.dumps({"id": "u1", "phase": "succeeded",
                                     "backup_dir": str(self.root / "upgrade-u1")}),
                          encoding="utf-8")
        result = self.manager.delete("upgrade-u1")
        self.assertTrue(result["ok"])
        self.assertFalse((self.root / "upgrade-u1").exists())

    def test_path_traversal_and_symlink_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "非法"):
            self.manager.delete("../data/bbu.db")
        outside = self.work / "outside"
        outside.mkdir()
        link = self.root / "linked"
        os.symlink(outside, link, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.manager.delete("linked")
        listed = {x["name"] for x in self.manager.overview()["items"]}
        self.assertNotIn("linked", listed)


if __name__ == "__main__":
    unittest.main()
