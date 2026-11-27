"""执行轨迹底座：JSONL、模型请求/回复、失败摘要、管理员下载边界与清理。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from bbu.ai import AIGateway
from bbu.config import Config
from bbu.db import Database
from bbu.security import SecretBox
from bbu.trace import TraceStore


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.db = Database(self.tmp / "test.db")
        self.config = Config(self.tmp)
        self.config.values["data_dir"] = str(self.tmp / "data")
        self.box = SecretBox(self.tmp)
        now = "2026-11-26T00:00:00Z"
        self.db.execute(
            "INSERT INTO model_configs(name,provider,base_url,api_key,model,roles,max_tokens,"
            "temperature,timeout_sec,enabled,priority,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("mock", "mock", "", "", "mock", json.dumps(["default"]), 4096, 0.2, 5,
             1, 0, "{}", now, now))
        self.store = TraceStore(self.db, self.config)

    def test_model_request_response_and_run_end_are_recorded(self):
        run = self.store.start(kind="diagnosis", ref_id="T-1", user_id=1,
                               meta={"title": "测试任务"})
        gw = AIGateway(self.db, self.config, secret_box=self.box)
        gw.complete("default", "系统提示词", "用户完整输入",
                    json_schema={"type": "object"}, purpose="test", trace=run)
        run.finish("success")
        events = [e for e in self.store.events(run.run_id)["items"] if e.get("type") != "run_end"]
        self.assertIn("model_request", [e["type"] for e in events])
        request = next(e for e in events if e["type"] == "model_request")
        self.assertEqual(request["request"]["system"], "系统提示词")
        self.assertEqual(request["request"]["user"], "用户完整输入")
        response = next(e for e in events if e["type"] == "model_response")
        self.assertIn("parsed", response["response"])
        self.assertTrue(response["response"]["text"])
        run_row = self.store.get(run.run_id)
        self.assertEqual(run_row["status"], "success")
        self.assertGreater(run_row["model_calls"], 0)
        path = Path(run_row["trace_path_abs"])
        self.assertTrue(path.is_file())
        for raw in path.read_text(encoding="utf-8").splitlines():
            self.assertEqual(json.loads(raw)["run_id"], run.run_id)

    def test_failed_validation_is_first_error_and_only_failed_finds_it(self):
        run = self.store.start(kind="workshop", ref_id="W-1", user_id=2, meta={})
        run.validation(phase="review", name="profile_check", ok=False,
                       errors=["缺少 profile"], details={"text": "bad"})
        run.finish("failed", error_code="validation", error_summary="profile 校验失败")
        summary = self.store.summary(run.run_id)
        self.assertEqual(summary["first_error"]["type"], "validation")
        self.assertEqual(summary["first_error"]["name"], "profile_check")
        failed = self.store.events(run.run_id, only_failed=True)["items"]
        self.assertTrue(any(e["type"] == "validation" for e in failed))

    def test_cleanup_removes_expired_trace_file_and_row(self):
        run = self.store.start(kind="diagnosis", ref_id="T-old", user_id=1, meta={})
        run.finish("success")
        row = self.store.get(run.run_id)
        self.db.execute("UPDATE trace_runs SET started_at='2020-01-01T00:00:00Z' WHERE id=?",
                        (run.run_id,))
        removed = self.store.cleanup()
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(Path(row["trace_path_abs"]).exists())
        self.assertIsNone(self.store.get(run.run_id))

    def test_download_path_is_inside_trace_root(self):
        run = self.store.start(kind="diagnosis", ref_id="T-2", user_id=1, meta={})
        row = self.store.get(run.run_id)
        self.assertTrue(str(row["trace_path_abs"]).startswith(str(self.tmp / "data" / "traces")))
        self.assertEqual(os.stat(row["trace_path_abs"]).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
