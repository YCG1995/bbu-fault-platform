"""AI 协议适配测试：本地假 endpoint 验证 OpenAI 兼容与 Anthropic 原生请求/解析。"""
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

from bbu.ai import AIGateway  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.security import SecretBox  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        _Handler.seen.append({"path": self.path,
                              "headers": {k.lower(): v for k, v in self.headers.items()},
                              "payload": payload})
        if self.path.endswith("/messages"):
            body = {
                "content": [{"type": "text", "text": json.dumps({"ok": True})}],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
        else:
            body = {
                "choices": [{"message": {"content": json.dumps({"ok": True})}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            }
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class AIProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.box = SecretBox(Path(self.tmp.name))
        _Handler.seen.clear()

    def _add_model(self, provider: str, base_url: str):
        key = self.box.encrypt("test-key")
        now = "2026-11-24T00:00:00Z"
        self.db.execute(
            "INSERT INTO model_configs(name,provider,base_url,api_key,model,roles,max_tokens,"
            "temperature,timeout_sec,enabled,priority,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{provider}-test", provider, base_url, key, "test-model",
             json.dumps(["default"]), 4096, 0.2, 5, 1, 0, "{}", now, now))

    def test_openai_compatible(self):
        self._add_model("openai", f"http://127.0.0.1:{self.port}/v1")
        result = AIGateway(self.db, None, secret_box=self.box).complete(
            "default", "你是测试助手", "只输出 JSON", json_schema={"type": "object"})
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(result.usage.total_tokens, 10)
        call = _Handler.seen[-1]
        self.assertEqual(call["path"], "/v1/chat/completions")
        self.assertEqual(call["headers"].get("authorization"), "Bearer test-key")

    def test_anthropic_native(self):
        self._add_model("anthropic", f"http://127.0.0.1:{self.port}")
        result = AIGateway(self.db, None, secret_box=self.box).complete(
            "default", "你是测试助手", "只输出 JSON", json_schema={"type": "object"})
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(result.usage.total_tokens, 10)
        call = _Handler.seen[-1]
        self.assertEqual(call["path"], "/v1/messages")
        self.assertEqual(call["headers"].get("x-api-key"), "test-key")
        self.assertTrue(call["headers"].get("anthropic-version"))


if __name__ == "__main__":
    unittest.main()
