"""个人 MaaS Key：加密存储、用户优先、默认回退与友好提示。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIGateway, NoModelError  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.security import SecretBox  # noqa: E402


class _MockHandler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        _MockHandler.seen.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization") or "",
            "payload": payload,
        })
        body = {"choices": [{"message": {"content": json.dumps({"ok": True})}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class _Config:
    def __init__(self, root: Path) -> None:
        self.data_dir = root
        self.values = {
            "personal_model_base_url": "https://maas.example.com/v1",
            "personal_model_default": "gpt-4o-mini",
            "personal_model_models": ["gpt-4o-mini", "enterprise-lite-v7.0"],
            "personal_api_key_url": "https://maas.example.com/personal-api-key",
        }

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class PersonalModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = Database(root / "test.db")
        self.box = SecretBox(root)
        self.cfg = _Config(root)
        now = "2026-11-25T00:00:00Z"
        self.uid = self.db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,active,created_at,status) "
            "VALUES(?,?,?,?,?,?,?)",
            ("member", "成员", "engineer", "x", 1, now, "active"))
        self.other = self.db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,active,created_at,status) "
            "VALUES(?,?,?,?,?,?,?)",
            ("other", "其他成员", "engineer", "x", 1, now, "active"))
        self.gateway = AIGateway(self.db, self.cfg, secret_box=self.box)
        _MockHandler.seen.clear()

    def _admin_model(self, provider="mock", name="管理员默认"):
        now = "2026-11-25T00:00:00Z"
        self.db.execute(
            "INSERT INTO model_configs(name,provider,base_url,api_key,model,roles,max_tokens,"
            "temperature,timeout_sec,enabled,priority,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, provider, f"http://127.0.0.1:{self.port}/v1", self.box.encrypt("admin-key"),
             "admin-model", json.dumps(["default"]), 4096, 0.2, 5, 1, 0, "{}", now, now))

    def _personal_model(self, uid, model="gpt-4o-mini", key="personal-secret-key"):
        now = "2026-11-25T00:00:00Z"
        self.db.execute(
            "INSERT INTO user_model_configs(user_id,name,provider,base_url,api_key,model,"
            "max_tokens,temperature,timeout_sec,enabled,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, "个人 MaaS", "openai", "https://maas.example.com/v1",
             self.box.encrypt(key), model, 4096, 0.2, 180, 1, "{}", now, now))

    def test_personal_config_is_encrypted_and_preferred(self):
        self._admin_model()
        self._personal_model(self.uid)
        row = self.db.one("SELECT api_key FROM user_model_configs WHERE user_id=?", (self.uid,))
        self.assertNotIn("personal-secret-key", row["api_key"])
        chosen = self.gateway.choose("default", user_id=self.uid)
        self.assertEqual(chosen[0]["source"], "personal")
        self.assertEqual(chosen[0]["model"], "gpt-4o-mini")
        self.assertIn("api_key", chosen[0])
        # 其他成员不会串用该 Key
        self.assertEqual(self.gateway.choose("default", user_id=self.other)[0]["source"], "admin")
        self.assertFalse(self.gateway.model_status(self.other)["personal"]["configured"])

    def test_complete_uses_personal_key_for_current_user(self):
        self._admin_model(provider="openai")
        self._personal_model(self.uid, key="member-own-key")
        self.db.execute("UPDATE user_model_configs SET base_url=? WHERE user_id=?",
                        (f"http://127.0.0.1:{self.port}/v1", self.uid))
        with self.gateway.user_context(self.uid):
            result = self.gateway.complete("default", "系统", "用户", json_schema={"type": "object"})
        self.assertEqual(result.data, {"ok": True})
        call = _MockHandler.seen[-1]
        self.assertEqual(call["path"], "/v1/chat/completions")
        self.assertEqual(call["authorization"], "Bearer member-own-key")
        # 另一个成员仍使用管理员配置，不携带个人 Key
        with self.gateway.user_context(self.other):
            self.gateway.complete("default", "系统", "用户")
        self.assertEqual(_MockHandler.seen[-1]["authorization"], "Bearer admin-key")

    def test_thread_user_context_is_used_by_complete(self):
        self._admin_model()
        self._personal_model(self.uid)
        previous = self.gateway.set_user(self.uid)
        try:
            chosen = self.gateway.choose("default")
        finally:
            self.gateway.set_user(previous)
        self.assertEqual(chosen[0]["source"], "personal")
        self.assertEqual(self.gateway.choose("default")[0]["source"], "admin")

    def test_no_personal_and_no_default_returns_friendly_key_notice(self):
        status = self.gateway.model_status(self.uid)
        self.assertFalse(status["usable"])
        self.assertEqual(status["notice"]["code"], "personal_key_required")
        self.assertIn("personal-api-key", status["notice"]["url"])
        with self.assertRaises(NoModelError) as ctx:
            self.gateway.complete("default", "sys", "user")
        self.assertIn("个人 MaaS API Key", str(ctx.exception))

    def test_default_breaker_open_suggests_personal_key(self):
        self._admin_model(provider="openai")
        global_model = self.gateway.choose("default", user_id=self.uid)[0]
        for _ in range(3):
            self.gateway.breaker.fail(f"{global_model['id']}:{global_model['name']}")
        status = self.gateway.model_status(self.uid)
        self.assertFalse(status["usable"])
        self.assertEqual(status["notice"]["code"], "default_model_error")
        self.assertIn("个人 MaaS API Key", status["notice"]["message"])

    def test_personal_failure_falls_back_to_admin_default(self):
        self._admin_model()
        self._personal_model(self.uid)
        personal = self.gateway.choose("default", user_id=self.uid)[0]
        for _ in range(3):
            self.gateway.breaker.fail(f"{personal['id']}:{personal['name']}")
        status = self.gateway.model_status(self.uid)
        self.assertTrue(status["usable"])
        self.assertEqual(status["source"], "admin")
        chosen = self.gateway.choose("default", user_id=self.uid)
        self.assertEqual(chosen[0]["source"], "admin")

    def test_corrupted_personal_key_is_not_used(self):
        self._admin_model()
        now = "2026-11-25T00:00:00Z"
        self.db.execute(
            "INSERT INTO user_model_configs(user_id,name,provider,base_url,api_key,model,"
            "max_tokens,temperature,timeout_sec,enabled,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.uid, "个人 MaaS", "openai", "https://maas.example.com/v1",
             "enc:v1:broken", "gpt-4o-mini", 4096, 0.2, 180, 1, "{}", now, now))
        status = self.gateway.model_status(self.uid)
        self.assertTrue(status["personal"]["configured"])
        self.assertFalse(status["personal"]["available"])
        self.assertEqual(status["source"], "admin")
        self.assertEqual(self.gateway.choose("default", user_id=self.uid)[0]["source"], "admin")


if __name__ == "__main__":
    unittest.main()
