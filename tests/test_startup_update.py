"""Windows 网页上传更新的启动阶段替换测试。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bbu import startup

OLD_VERSION = "1.0.0"
NEW_VERSION = "9.9.9"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class StartupUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-startup-update-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "platform"
        self.state = self.tmp / "state"
        self.root.mkdir()
        write(self.root / "config.json", json.dumps({
            "data_dir": str(self.state / "data"),
            "skills_dir": str(self.state / "skills"),
            "cases_dir": str(self.state / "cases"),
            "examples_dir": str(self.root / "examples"),
        }))
        write(self.root / "data-previous.txt", "will be preserved only if named data")
        (self.state / "data").mkdir(parents=True)
        (self.state / "skills").mkdir(parents=True)
        (self.state / "cases").mkdir(parents=True)
        write(self.state / "data" / "precious.txt", "keep-me")
        write(self.root / "run.py", "print('old')\n")
        write(self.root / "bbu" / "__init__.py", f'__version__ = "{OLD_VERSION}"\n')
        write(self.root / "web" / "index.html", "old-web")
        self.job = self.state / "updates" / "U1"
        write(self.job / "status.json", json.dumps({
            "id": "U1", "phase": "waiting_restart", "message": "ready",
            "progress": 100, "current_version": OLD_VERSION,
            "target_version": NEW_VERSION, "backup_dir": "", "error": "",
            "created_at": "2026-01-01T00:00:00+08:00",
            "updated_at": "2026-01-01T00:00:00+08:00",
        }))
        self.source = self.job / "release" / "bbu-fault-platform"
        self.make_source(self.source)

    def make_source(self, source: Path, *, broken_web: bool = False) -> None:
        write(source / "run.py", "print('new')\n")
        write(source / "bbu" / "__init__.py", f'__version__ = "{NEW_VERSION}"\n')
        if broken_web:
            path = source / "web"
            if path.exists():
                shutil.rmtree(path)
            path.mkdir(parents=True)
            (path / "index.html").symlink_to(self.tmp / "missing-target")
        else:
            write(source / "web" / "index.html", "new-web")

    def pending(self) -> Path:
        return self.root / startup.PENDING_NAME

    def write_pending(self) -> None:
        write(self.pending(), json.dumps({
            "action": "update", "job_id": "U1", "job_dir": str(self.job),
            "source_dir": str(self.source), "target_version": NEW_VERSION,
            "package_sha256": "0" * 64, "prefix": str(self.root),
            "state_dir": str(self.state),
        }))

    def test_apply_update_replaces_code_and_preserves_data_config(self):
        self.write_pending()
        result = startup.apply_pending(self.root)
        self.assertTrue(result and result["ok"], result)
        self.assertEqual(result["action"], "update")
        self.assertFalse(self.pending().exists())
        self.assertIn("new", (self.root / "run.py").read_text(encoding="utf-8"))
        self.assertIn(NEW_VERSION, (self.root / "bbu" / "__init__.py").read_text(encoding="utf-8"))
        self.assertEqual((self.state / "data" / "precious.txt").read_text(), "keep-me")
        self.assertEqual((self.root / "config.json").read_text(), json.dumps({
            "data_dir": str(self.state / "data"),
            "skills_dir": str(self.state / "skills"),
            "cases_dir": str(self.state / "cases"),
            "examples_dir": str(self.root / "examples"),
        }))
        status = json.loads((self.job / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["phase"], "succeeded")
        self.assertTrue(Path(status["backup_dir"]).is_dir())

    def test_apply_update_restores_old_code_on_copy_failure(self):
        self.make_source(self.source, broken_web=True)
        self.write_pending()
        result = startup.apply_pending(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("old", (self.root / "run.py").read_text(encoding="utf-8"))
        self.assertIn(OLD_VERSION, (self.root / "bbu" / "__init__.py").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "web" / "index.html").is_file())
        self.assertFalse(self.pending().exists())
        status = json.loads((self.job / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["phase"], "failed")


if __name__ == "__main__":
    unittest.main()
