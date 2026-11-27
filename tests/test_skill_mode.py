"""指定技能诊断模式：单一技能合同、草稿往返与确定性计划。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "vendor"))

from api_full_smoke import Client  # noqa: E402
from bbu.api import App  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.httpd import AppServer  # noqa: E402


class SkillModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-skill-mode-")
        root = Path(self.tmp.name)
        self.cfg = Config(ROOT)
        self.cfg.values.update({
            "data_dir": str(root / "data"), "work_dir": str(root / "work"),
            "skills_dir": str(root / "skills"), "cases_dir": str(root / "cases"),
            "examples_dir": str(root / "examples"), "meta_skill_dir": str(root / "meta"),
            "install_demo_skills": False, "max_queue": 8, "max_workers": 1,
            "learning_autostart": False,
        })
        self.cfg.ensure_dirs()
        shutil.copytree(ROOT / "examples" / "skills" / "demo-bbu-clock-loss",
                        self.cfg.skills_dir / "demo-bbu-clock-loss")
        self.app = App(self.cfg)
        self.app.worker.shutdown()
        self.server = AppServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.2}, daemon=True)
        self.thread.start()
        self.client = Client("127.0.0.1", self.port)
        self.client.login("admin", self.app.admin_password)
        _, changed = self.client.json("POST", "/api/auth/password", {
            "old_password": self.app.admin_password, "new_password": "Skill!2026x"})
        self.client.csrf = changed["csrf"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.shutdown_services()
        self.tmp.cleanup()

    def _zip_bytes(self) -> bytes:
        return (ROOT / "tests" / "fixtures" / "logs" / "clock-demo.zip").read_bytes()

    def test_single_skill_draft_roundtrip_and_fast_ignores_it(self):
        _, created = self.client.json("POST", "/api/task-drafts", {
            "description": "CLK032 PLL unlock", "mode": "skill",
            "skill_name": "demo-bbu-clock-loss"}, expected=201)
        draft = created["draft"]
        self.assertEqual(draft["mode"], "skill")
        self.assertEqual(draft["skill_name"], "demo-bbu-clock-loss")
        _, current = self.client.json("GET", "/api/task-drafts/current")
        self.assertEqual(current["draft"]["skill_name"], "demo-bbu-clock-loss")
        _, updated = self.client.json("PUT", f"/api/task-drafts/{draft['id']}", {
            "description": "改用快速定位", "mode": "fast",
            "skill_name": "demo-bbu-clock-loss"})
        self.assertEqual(updated["draft"]["mode"], "fast")
        self.assertEqual(updated["draft"]["skill_name"], "")
        _, bad = self.client.json("POST", "/api/task-drafts", {
            "description": "bad", "mode": "skill", "skill_name": "missing-skill"},
            expected=400)
        self.assertIn("不存在", str(bad))

    def test_task_create_requires_one_ready_skill_and_plan_is_user_selected(self):
        _, bad = self.client.multipart("/api/tasks", {
            "description": "CLK032 PLL unlock", "mode": "skill"},
            [("files", "clock-demo.zip", self._zip_bytes(), "application/zip")],
            expected=400)
        self.assertIn("必须选择一个技能", str(bad))
        _, created = self.client.multipart("/api/tasks", {
            "description": "CLK032 PLL unlock，指定时钟技能复核",
            "mode": "skill", "skill_name": "demo-bbu-clock-loss"},
            [("files", "clock-demo.zip", self._zip_bytes(), "application/zip")],
            expected=201)
        task_id = created["id"]
        row = self.app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        options = json.loads(row["options_json"])
        self.assertEqual(row["mode"], "skill")
        self.assertEqual(options["skill_name"], "demo-bbu-clock-loss")
        self.assertEqual(options["skill_names"], ["demo-bbu-clock-loss"])
        result = self.app.pipeline.run(task_id, threading.Event())
        self.assertEqual(result["plan"]["source"], "user_selected")
        self.assertEqual(result["plan"]["selected_skills"], ["demo-bbu-clock-loss"])
        planned = [name for step in result["plan"]["steps"] for name in step.get("skills") or []]
        self.assertEqual(planned, ["demo-bbu-clock-loss"])
        self.assertFalse(result["plan"].get("replan"))

    def test_deep_and_fast_do_not_accept_skill_names_legacy_field(self):
        _, fast = self.client.multipart("/api/tasks", {
            "description": "CLK032 PLL unlock", "mode": "fast",
            "skill_names": "demo-bbu-clock-loss"},
            [("files", "clock-demo.zip", self._zip_bytes(), "application/zip")],
            expected=201)
        row = self.app.db.one("SELECT options_json FROM tasks WHERE id=?", (fast["id"],))
        options = json.loads(row["options_json"])
        self.assertEqual(options["skill_names"], [])
        self.assertEqual(options["skill_name"], "")


if __name__ == "__main__":
    unittest.main()
