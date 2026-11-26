"""同任务补证与续跑的 HTTP + 编排集成回归。"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "vendor"))

from api_full_smoke import Client  # noqa: E402
from bbu.api import App  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.httpd import AppServer  # noqa: E402


class KvClient(Client):
    def multipart_key(self, path: str, fields: dict[str, str],
                      files: list[tuple[str, str, bytes, str]], idem: str,
                      expected: int = 201) -> tuple[int, dict]:
        boundary = "----bbu-r2-" + uuid.uuid4().hex
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode())
        for field, filename, content, ctype in files:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Idempotency-Key": idem}
        if self.csrf:
            headers["X-CSRF"] = self.csrf
        status, raw, _ = self.request("POST", path, b"".join(parts), headers)
        if status != expected:
            raise AssertionError(f"POST {path} 期望 {expected}，实际 {status}: {raw[:400]!r}")
        return status, json.loads(raw or b"{}")


class SessionContinueHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-r2-http-")
        root = Path(self.tmp.name)
        self.cfg = Config(ROOT)
        self.cfg.values.update({
            "data_dir": str(root / "data"), "work_dir": str(root / "work"),
            "skills_dir": str(root / "skills"), "cases_dir": str(root / "cases"),
            "examples_dir": str(root / "examples"), "meta_skill_dir": str(root / "meta"),
            "install_demo_skills": False, "session_continue_enabled": True,
            "max_queue": 8, "max_workers": 1, "learning_autostart": False,
        })
        self.cfg.ensure_dirs()
        shutil.copytree(ROOT / "examples" / "skills" / "demo-bbu-clock-loss",
                        self.cfg.skills_dir / "demo-bbu-clock-loss")
        shutil.copytree(ROOT / "examples" / "skills" / "demo-bbu-optical-rx",
                        self.cfg.skills_dir / "demo-bbu-optical-rx")
        # 测试中收窄光模块技能的日志 glob，验证“按 glob 选择性失效”而不是全量失效。
        optical_profile = (self.cfg.skills_dir / "demo-bbu-optical-rx"
                           / "assets" / "fault-profile.yaml")
        optical_profile.write_text(
            optical_profile.read_text(encoding="utf-8")
            .replace('"**/*.log"', '"**/*optical*.log"')
            .replace('"**/*.txt"', '"**/*optical*.txt"'),
            encoding="utf-8")
        self.app = App(self.cfg)
        self.app.worker.shutdown()
        self.server = AppServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.2}, daemon=True)
        self.thread.start()
        self.client = KvClient("127.0.0.1", self.port)
        self.initial = self.app.admin_password
        self.assertIsNotNone(self.initial)
        self.client.login("admin", self.initial)
        _, changed = self.client.json("POST", "/api/auth/password", {
            "old_password": self.initial, "new_password": "Session!2026x"})
        self.client.csrf = changed["csrf"]
        self.uid = self.app.db.one("SELECT id FROM users WHERE username='admin'")["id"]
        self.task_id = self._seed_first_round()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.shutdown_services()
        self.tmp.cleanup()

    def _seed_first_round(self) -> str:
        task_id = "T-R2-CONT"
        upload = self.cfg.data_dir / "uploads" / task_id
        upload.mkdir(parents=True, exist_ok=True)
        target = upload / "normal-demo.zip"
        shutil.copy2(ROOT / "tests" / "fixtures" / "logs" / "normal-demo.zip", target)
        self.app.db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
            "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, self.uid, "现场异常待定位", "设备偶发异常，暂未提供时钟专项日志",
             "queued", "queued", 0, "fast",
             json.dumps([{"name": target.name, "stored_name": target.name,
                          "stored_path": str(target), "size": target.stat().st_size,
                          "sha256": ""}], ensure_ascii=False), "{}", "2026-01-01T00:00:00Z"))
        result = self.app.pipeline.run(task_id, threading.Event())
        task = self.app.db.one("SELECT status,result_json FROM tasks WHERE id=?", (task_id,))
        self.assertEqual(task["status"], "needs_data")
        self.assertEqual(json.loads(task["result_json"])["verdict"]["status"], "needs_data")
        return task_id

    def test_first_round_supplement_continue_reaches_same_task_conclusion(self):
        _, detail = self.client.json("GET", f"/api/tasks/{self.task_id}")
        self.assertEqual(detail["status"], "needs_data")
        self.assertTrue(detail["session"]["revision"] >= 3)
        self.assertTrue(detail["questions"])
        revision = detail["session"]["revision"]
        self.app.db.execute(
            "INSERT INTO task_skills(task_id,skill_name,skill_version,status,score,"
            "started_at,finished_at,result_json) VALUES(?,?,?,?,?,?,?,?)",
            (self.task_id, "demo-bbu-clock-loss", "1.0", "done", 80,
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "{}"))
        self.app.db.execute(
            "INSERT INTO task_skills(task_id,skill_name,skill_version,status,score,"
            "started_at,finished_at,result_json) VALUES(?,?,?,?,?,?,?,?)",
            (self.task_id, "demo-bbu-optical-rx", "1.0", "done", 70,
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "{}"))
        clock = (ROOT / "tests" / "fixtures" / "logs" / "clock-demo.zip").read_bytes()
        answers = {"Q1": "设备上报 CLK032 时钟失锁，PLL 无法锁定，请结合时钟日志定位"}
        fields = {"expected_revision": str(revision),
                  "answers": json.dumps(answers, ensure_ascii=False)}
        files = [("files", "clock-demo.zip", clock, "application/zip")]
        idem = "test-supplement-" + uuid.uuid4().hex
        _, saved = self.client.multipart_key(
            f"/api/tasks/{self.task_id}/inputs", fields, files, idem, expected=201)
        self.assertFalse(saved["duplicate"])
        self.assertEqual(saved["revision"], revision + 1)
        self.assertEqual(saved["supplement"]["invalidated_skills"], ["demo-bbu-clock-loss"])
        self.assertEqual(
            saved["supplement"]["invalidation_reasons"]["demo-bbu-clock-loss"]["reason"],
            "glob_match")
        self.assertEqual(saved["resolved_gaps"][0]["question_id"], "Q1")
        invalid = self.app.db.one(
            "SELECT invalidated FROM task_skills WHERE task_id=? AND skill_name=?",
            (self.task_id, "demo-bbu-clock-loss"))
        self.assertEqual(invalid["invalidated"], 1)
        unrelated = self.app.db.one(
            "SELECT invalidated FROM task_skills WHERE task_id=? AND skill_name=?",
            (self.task_id, "demo-bbu-optical-rx"))
        self.assertEqual(unrelated["invalidated"], 0)

        # 同一幂等键重放必须返回原结果，即使会话已前进；换内容必须 409。
        _, replay = self.client.multipart_key(
            f"/api/tasks/{self.task_id}/inputs", fields, files, idem, expected=200)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["revision"], revision + 1)
        self.client.multipart_key(
            f"/api/tasks/{self.task_id}/inputs",
            {**fields, "answers": json.dumps({"Q1": "不同回答"})},
            files, idem, expected=409)
        self.client.json("POST", f"/api/tasks/{self.task_id}/inputs",
                         {"expected_revision": revision, "answers": {"Q1": "x"},
                          "idempotency_key": "rev-test-" + uuid.uuid4().hex},
                         expected=409)

        client2 = KvClient("127.0.0.1", self.port)
        client2.cookie = self.client.cookie
        client2.csrf = self.client.csrf
        _, queued = client2.json("POST", f"/api/tasks/{self.task_id}/continue",
                                 {"expected_revision": saved["revision"]}, expected=202)
        self.assertEqual(queued["task"]["status"], "queued")
        self.assertEqual(queued["task"]["id"], self.task_id)
        counts = self.app.db.one(
            "SELECT continuation_count,resume_count FROM tasks WHERE id=?", (self.task_id,))
        self.assertEqual(counts["continuation_count"], 1)
        self.assertEqual(counts["resume_count"], 0)
        _, replay_continue = client2.json(
            "POST", f"/api/tasks/{self.task_id}/continue",
            {"expected_revision": saved["revision"]}, expected=200)
        self.assertTrue(replay_continue["duplicate"])

        result = self.app.pipeline.run(self.task_id, threading.Event())
        task = self.app.db.one("SELECT status,verdict_json,result_json FROM tasks WHERE id=?",
                               (self.task_id,))
        self.assertEqual(task["status"], "done")
        verdict = json.loads(task["verdict_json"])
        self.assertEqual(verdict["status"], "matched")
        self.assertEqual(verdict["skill"], "demo-bbu-clock-loss")
        snapshot = self.app.db.one(
            "SELECT event_type,payload_json FROM session_events WHERE task_id=? "
            "ORDER BY revision DESC LIMIT 1", (self.task_id,))
        self.assertEqual(json.loads(snapshot["payload_json"])["state"], "resolved")
        self.assertGreater(self.app.evidence_store.count(self.task_id), 0)
        _, session = client2.json("GET", f"/api/tasks/{self.task_id}/session")
        hypotheses = session.get("hypotheses") or {}
        self.assertTrue(hypotheses, session)
        self.assertTrue(any(h.get("status") == "confirmed" for h in hypotheses.values()),
                        hypotheses)
        self.assertTrue(any(e["event_type"] == "tool_started"
                            for e in session.get("events") or []), session)
        self.assertTrue(any(e["event_type"] == "evidence_added"
                            for e in session.get("events") or []), session)
        self.assertTrue(session.get("budget"), session)

    def test_session_and_evidence_get_routes_expose_versions(self):
        _, session = self.client.json("GET", f"/api/tasks/{self.task_id}/session")
        self.assertTrue(session["can_continue"])
        self.assertIn("events", session)
        _, questions = self.client.json("GET", f"/api/tasks/{self.task_id}/questions")
        self.assertEqual(questions["task_id"], self.task_id)
        _, evidence = self.client.json("GET", f"/api/tasks/{self.task_id}/evidence")
        self.assertEqual(evidence["total"], 0)


if __name__ == "__main__":
    unittest.main()
