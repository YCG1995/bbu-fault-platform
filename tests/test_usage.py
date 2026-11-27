"""用量统计：采集、按用户隔离、每日聚合与管理员/普通用户权限。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.api import App, h_usage_overview, h_usage_tasks  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.httpd import HTTPError  # noqa: E402
from bbu.security import create_session, hash_password  # noqa: E402
from bbu.usage import UsageStore  # noqa: E402


class _Req:
    def __init__(self, query: dict[str, str] | None = None, token: str = "") -> None:
        self.method = "GET"
        self.query = dict(query or {})
        self.headers = {"Cookie": f"bbu_session={token}"} if token else {}
        self.ip = "127.0.0.1"
        self.body = b""


def _event(user_id: int, task_id: str, model_id: str, model_name: str,
           scope: str, tokens: int = 10, status: str = "success") -> dict:
    return {
        "user_id": user_id, "task_id": task_id, "task_kind": "diagnosis",
        "skill_name": "demo-bbu-clock-loss", "model_id": model_id,
        "model_name": model_name, "provider": "mock", "model": model_name,
        "scope": scope, "purpose": "planning", "stage": "planning",
        "status": status, "input_tokens": tokens, "output_tokens": tokens,
        "total_tokens": tokens * 2, "estimated_input_tokens": 0, "latency_ms": 12,
        "error": "",
    }


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-usage-")
        self.cfg = Config(ROOT)
        self.cfg.values.update({
            "data_dir": str(Path(self.tmp.name) / "data"),
            "work_dir": str(Path(self.tmp.name) / "work"),
            "local_tz_offset": 8,
        })
        self.cfg.ensure_dirs()
        self.app = App(self.cfg)
        self.app.worker.shutdown()
        self.app.learning.shutdown()
        self.app.workshop_jobs.shutdown()
        self.admin_id = self.app.db.one("SELECT id FROM users WHERE role='admin'")["id"]
        self.engineer_id = self.app.db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,must_change,active,"
            "created_at,status,domain,notification_prefs) VALUES(?,?,?,?,0,1,?, 'active','','{}')",
            ("usage-eng", "用量工程师", "engineer", hash_password("Usage!2026x"),
             "2026-11-01T00:00:00Z"))

    def tearDown(self):
        self.app.shutdown_services()
        self.tmp.cleanup()

    def test_records_all_scopes_and_separates_users(self):
        store = self.app.usage
        store.record(_event(self.admin_id, "T-admin", "1", "共享模型", "admin", 3))
        store.record(_event(self.admin_id, "T-admin", "personal:1", "个人模型", "personal", 4))
        store.record(_event(self.engineer_id, "T-eng", "1", "共享模型", "admin", 5, "error"))
        all_data = store.overview(days=30, all_users=True, include_directory=True)
        self.assertEqual(all_data["totals"]["calls"], 3)
        self.assertEqual(all_data["totals"]["failed_calls"], 1)
        self.assertEqual(all_data["totals"]["total_tokens"], 24)
        self.assertEqual(len(all_data["daily"]), 30)
        self.assertEqual(sum(d["calls"] for d in all_data["daily"]), 3)
        self.assertEqual({m["scope"] for m in all_data["models"]}, {"admin", "personal"})
        self.assertEqual(len(all_data["directory"]), 2)
        self.assertEqual({u["user_id"] for u in all_data["directory"]},
                         {self.admin_id, self.engineer_id})
        self.assertEqual(sum(d["calls"] for d in all_data["user_daily"]), 3)
        self.assertEqual(len(all_data["user_daily"]), 1)
        self.assertEqual(len(all_data["user_daily"][0]["users"]), 2)
        for day in all_data["user_daily"]:
            self.assertEqual(day["calls"], sum(u["calls"] for u in day["users"]))

        own = store.overview(days=30, user_id=self.engineer_id, include_directory=True)
        self.assertEqual(own["totals"]["calls"], 1)
        self.assertEqual(own["totals"]["total_tokens"], 10)
        self.assertEqual(own["models"][0]["failed_calls"], 1)
        self.assertEqual(len(own["user_daily"]), 1)
        self.assertEqual(own["user_daily"][0]["users"][0]["user_id"], self.engineer_id)
        # 管理员目录不受筛选影响，仍可切换用户。
        self.assertEqual({u["user_id"] for u in own["directory"]}, {self.admin_id, self.engineer_id})

    def test_daily_aggregation_same_day_and_models_do_not_collide(self):
        store = self.app.usage
        for i in range(5):
            store.record(_event(self.admin_id, f"T{i}", "1", "共享模型", "admin", i + 1))
        data = store.overview(days=7, user_id=self.admin_id)
        today = data["daily"][-1]
        self.assertEqual(today["calls"], 5)
        self.assertEqual(today["total_tokens"], sum((i + 1) * 2 for i in range(5)))
        self.assertEqual(len(today["models"]), 1)
        self.assertEqual(len(data["user_daily"]), 1)
        self.assertEqual(data["user_daily"][0]["users"][0]["calls"], 5)

    def test_task_rows_uses_event_detail_and_falls_back_for_old_tasks(self):
        from bbu.util import iso_now
        now = iso_now()
        self.app.db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,mode,input_json,"
            "stats_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("TASK-1", self.admin_id, "旧任务", "", "done", "fast", "[]",
             json.dumps({"llm_calls": 2, "input_tokens": 7, "output_tokens": 9,
                         "total_tokens": 16}), now))
        self.app.db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,mode,input_json,"
            "stats_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("TASK-2", self.engineer_id, "新任务", "", "done", "fast", "[]", "{}", now))
        self.app.usage.record(_event(self.engineer_id, "TASK-2", "1", "共享模型", "admin", 6))
        page = self.app.usage.task_rows(days=30, all_users=True, page=1, page_size=20)
        items = {item["task_id"]: item for item in page["items"]}
        self.assertEqual(items["TASK-1"]["calls"], 2)
        self.assertEqual(items["TASK-1"]["total_tokens"], 16)
        self.assertFalse(items["TASK-1"]["has_events"])
        self.assertEqual(items["TASK-2"]["calls"], 1)
        self.assertEqual(items["TASK-2"]["total_tokens"], 12)
        own = self.app.usage.task_rows(days=30, user_id=self.engineer_id)
        self.assertEqual([i["task_id"] for i in own["items"]], ["TASK-2"])

    def test_api_permissions_and_validation(self):
        self.app.usage.record(_event(self.admin_id, "T1", "1", "共享模型", "admin", 3))
        self.app.usage.record(_event(self.engineer_id, "T2", "1", "共享模型", "admin", 4))
        admin_token = create_session(self.app.db, self.admin_id, 8)["token"]
        eng_token = create_session(self.app.db, self.engineer_id, 8)["token"]

        status, _headers, body = h_usage_overview(self.app, _Req(token=admin_token))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["scope"]["mode"], "all")
        self.assertEqual(data["totals"]["calls"], 2)
        self.assertEqual(sum(d["calls"] for d in data["user_daily"]), 2)
        self.assertEqual(len({u["user_id"] for d in data["user_daily"] for u in d["users"]}), 2)

        status, _headers, body = h_usage_overview(
            self.app, _Req({"user_id": str(self.engineer_id)}, token=admin_token))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["totals"]["calls"], 1)
        self.assertEqual({u["user_id"] for d in json.loads(body)["user_daily"] for u in d["users"]},
                         {self.engineer_id})

        status, _headers, body = h_usage_overview(self.app, _Req(token=eng_token))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["scope"]["user_id"], self.engineer_id)

        with self.assertRaises(HTTPError) as ctx:
            h_usage_overview(self.app, _Req({"user_id": str(self.admin_id)}, token=eng_token))
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(HTTPError) as ctx:
            h_usage_overview(self.app, _Req({"days": "0"}, token=eng_token))
        self.assertEqual(ctx.exception.status, 400)
        status, _headers, body = h_usage_tasks(self.app, _Req(token=eng_token))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 0)

    def test_browser_transport_is_aggregated_in_overview_and_tasks(self):
        from bbu.util import iso_now
        self.app.db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,mode,input_json,"
            "stats_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("T-BROWSER", self.engineer_id, "浏览器任务", "", "done", "fast", "[]",
             "{}", iso_now()))
        event = _event(self.engineer_id, "T-BROWSER", "9", "浏览器模型", "admin", 6)
        event["transport"] = "browser"
        self.app.usage.record(event)
        data = self.app.usage.overview(days=30, user_id=self.engineer_id)
        self.assertEqual(data["totals"]["browser_calls"], 1)
        model = next(m for m in data["models"] if m["model_id"] == "9")
        self.assertEqual(model["transports"], ["browser"])
        self.assertEqual(model["browser_calls"], 1)
        page = self.app.usage.task_rows(days=30, user_id=self.engineer_id)
        item = next(i for i in page["items"] if i["task_id"] == "T-BROWSER")
        self.assertEqual(item["transports"], ["browser"])
        self.assertEqual(item["browser_calls"], 1)

    def test_ai_gateway_emits_usage_once_per_attempt(self):
        self.app.db.execute(
            "INSERT INTO model_configs(name,provider,base_url,api_key,model,roles,max_tokens,"
            "temperature,timeout_sec,enabled,priority,created_at,updated_at) "
            "VALUES('mock','mock','','','mock','[\"default\"]',100,0.2,30,1,0,?,?)",
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        with self.app.gateway.usage_context(self.engineer_id, task_id="T-ai",
                                            task_kind="diagnosis"):
            self.app.gateway.complete("default", "sys", "hello", purpose="planning")
        rows = self.app.db.all("SELECT * FROM llm_usage_events WHERE task_id='T-ai'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], self.engineer_id)
        self.assertEqual(rows[0]["scope"], "admin")
        self.assertGreater(rows[0]["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
