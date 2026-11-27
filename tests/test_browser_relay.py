"""浏览器代理（relay）核心、网关 transport 与错误边界回归。

运行：python3 -m pytest tests/test_browser_relay.py -q
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIGateway, NoModelError  # noqa: E402
from bbu.db import Database, SCHEMA_VERSION  # noqa: E402
from bbu.relay import (  # noqa: E402
    RelayAbsentError, RelayCallConsumedError, RelayCrossUserError, RelayExecError,
    RelayHub, RelayOversizeError, RelaySpec, RelayWindowEvictedError,
    validate_browser_url,
)
from bbu.security import SecretBox  # noqa: E402


class _Config:
    def __init__(self, root: Path) -> None:
        self.data_dir = root
        self.values = {
            "relay_enabled": True,
            "relay_lease_ttl_sec": 10,
            "relay_status_poll_sec": 1,
            "relay_max_windows_per_user": 2,
            "relay_max_response_bytes": 4096,
            "relay_allow_insecure_http": False,
            "relay_claim_timeout_sec": 5,
            "relay_exec_timeout_sec": 30,
            "model_discovery_allow_private": False,
            "llm_endpoint_concurrency": 4,
            "llm_endpoint_queue_limit": 16,
            "llm_endpoint_queue_timeout_sec": 10,
            "llm_schema_fail_threshold": 2,
            "llm_schema_cooldown_sec": 30,
            "llm_context_margin_tokens": 64,
        }

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class BrowserRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = None

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-relay-")
        root = Path(self.tmp.name)
        self.cfg = _Config(root)
        self.db = Database(root / "test.db")
        self.box = SecretBox(root)
        now = "2026-11-27T00:00:00Z"
        self.uid = self.db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,must_change,active,"
            "created_at,status,domain,notification_prefs) "
            "VALUES(?,?,?,?,0,1,?, 'active','','{}')",
            ("relay-engineer", "中继工程师", "engineer", "x", now))
        self.other_uid = self.db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,must_change,active,"
            "created_at,status,domain,notification_prefs) "
            "VALUES(?,?,?,?,0,1,?, 'active','','{}')",
            ("relay-reviewer", "中继复核", "reviewer", "x", now))
        self.hub = RelayHub(self.db, self.cfg, start_cleaner=False)
        self.gateway = AIGateway(self.db, self.cfg, secret_box=self.box, relay=self.hub)
        self._threads: list[threading.Thread] = []
        self._errors: list[BaseException] = []

    def tearDown(self) -> None:
        self.hub.shutdown()
        for thread in self._threads:
            thread.join(timeout=3)
        self.tmp.cleanup()

    # ---------- 测试工具 ----------
    def _spec(self, *, url: str = "https://maas.example.com/v1/chat/completions",
              max_bytes: int = 4096, auth: dict[str, str] | None = None) -> RelaySpec:
        return RelaySpec(
            url=url, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer secret-key"},
            body='{"model":"demo","messages":[]}',
            auth=auth or {"header": "Authorization", "value": "Bearer secret-key"},
            max_bytes=max_bytes,
        )

    def _model(self, *, provider: str = "openai", transport: str = "browser",
               priority: int = 0, roles: list[str] | None = None,
               allow_roles: list[str] | None = None) -> int:
        now = "2026-11-27T00:00:00Z"
        extra = {"transport": "browser", "browser_relay": {
            "max_concurrent": 2, "claim_timeout_sec": 5, "exec_timeout_sec": 30,
            "allow_roles": allow_roles or [],
        }} if transport == "browser" else {}
        row = self.db.execute(
            "INSERT INTO model_configs(name,provider,base_url,api_key,model,roles,max_tokens,"
            "temperature,timeout_sec,enabled,priority,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"relay-{transport}-{priority}", provider,
             "https://maas.example.com/v1" if transport == "browser"
             else "http://127.0.0.1:9/v1",
             self.box.encrypt("browser-secret-key" if transport == "browser" else "server-key"),
             "demo-model", json.dumps(roles or ["default"]), 512, 0.2, 30, 1, priority,
             json.dumps(extra, ensure_ascii=False), now, now))
        return int(row)

    def _spawn(self, target) -> threading.Thread:
        thread = threading.Thread(target=target, daemon=True)
        self._threads.append(thread)
        thread.start()
        return thread

    def _answer_worker(self, *, body: str | None = None, error_kind: str = "",
                       hold: threading.Event | None = None,
                       expect: type[BaseException] | None = None):
        def worker() -> None:
            try:
                call = self.hub.claim(self.uid, self.window_id, wait_sec=5)
                if call is None:
                    return
                if hold is not None:
                    hold.wait(3)
                self.hub.complete(
                    self.uid, call.call_id, 200,
                    body if body is not None else '{"choices":[{"message":{"content":"OK"}}]}',
                    7, error_kind=error_kind)
            except BaseException as exc:  # noqa: BLE001 - 线程内断言转交主线程
                if expect is None or not isinstance(exc, expect):
                    self._errors.append(exc)
        return self._spawn(worker)

    def _register(self) -> str:
        self.window_id = self.hub.register_window(self.uid, "w-test")["window_id"]
        return self.window_id

    # ---------- 核心 ----------
    def test_schema_24_has_transport_column(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 25)
        with sqlite3.connect(self.db.path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage_events)")}
        self.assertIn("transport", columns)
        # 同一路径再次初始化必须幂等，不能因列已存在而失败。
        Database(self.db.path)

    def test_browser_url_guard(self) -> None:
        self.assertTrue(validate_browser_url("https://maas.example.com/v1"))
        self.assertFalse(validate_browser_url("http://maas.example.com/v1"))
        self.assertTrue(validate_browser_url("http://10.20.30.40/v1", allow_http=True))
        for bad in (
            "ftp://maas.example.com/v1",
            "https://user:pass@maas.example.com/v1",
            "https://localhost/v1",
            "https://127.0.0.1/v1",
            "https://[::1]/v1",
            "https://169.254.1.1/v1",
            "https://[fe80::1]/v1",
        ):
            self.assertFalse(validate_browser_url(bad), bad)

    def test_status_payload_registers_window_and_models(self) -> None:
        self._model(allow_roles=["engineer"])
        payload = self.hub.status_payload(self.uid, "w-status")
        self.assertEqual(payload["window"]["window_id"], "w-status")
        self.assertEqual(payload["window"]["ttl_sec"], 10)
        self.assertEqual(payload["poll_sec"], 1)
        self.assertEqual(payload["pending"], 0)
        self.assertEqual(payload["max_response_bytes"], 4096)
        self.assertEqual([m["transport"] for m in payload["models"]], ["browser"])

    def test_submit_claim_and_result(self) -> None:
        self._model()
        self._register()
        self._answer_worker(body='{"choices":[{"message":{"content":"OK"}}]}')
        result = self.hub.submit(
            self.uid, {"id": 1, "name": "demo", "model": "demo-model"}, self._spec(),
            purpose="diagnosis", claim_timeout=5, exec_timeout=30)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.text, '{"choices":[{"message":{"content":"OK"}}]}')
        self.assertFalse(self._errors, self._errors)
        self.assertEqual(self.hub.snapshot()["pending"], 0)

    def test_offline_submit_raises_absent(self) -> None:
        with self.assertRaises(RelayAbsentError):
            self.hub.submit(
                self.uid, {"id": 1, "name": "demo", "model": "demo-model"}, self._spec(),
                claim_timeout=5, exec_timeout=30)

    def test_auth_is_separated_and_consumed_once(self) -> None:
        self._model()
        self._register()
        spec = self._spec()
        self.assertNotIn("Authorization", spec.headers)
        self.assertEqual(spec.auth["value"], "Bearer secret-key")
        call = self.hub._enqueue(self.uid, {"id": 1, "name": "demo"}, spec, "test", 5, 30)
        claimed = self.hub.claim(self.uid, self.window_id, wait_sec=0)
        self.assertIs(claimed, call)
        self.assertEqual(claimed.spec.auth["value"], "Bearer secret-key")
        self.hub.consume_auth(call.call_id)
        self.assertEqual(claimed.spec.auth, {})
        self.assertEqual(claimed.spec.public()["headers"], {"Content-Type": "application/json"})
        self.assertNotIn("secret-key", json.dumps(claimed.spec.public(), ensure_ascii=False))
        self.hub.complete(self.uid, call.call_id, 200, "{}", 1)

    def test_duplicate_claim_is_consumed(self) -> None:
        self._model()
        self._register()
        hold = threading.Event()
        self._answer_worker(hold=hold)
        submit_thread = self._spawn(lambda: self.hub.submit(
            self.uid, {"id": 1, "name": "demo"}, self._spec(), claim_timeout=5, exec_timeout=30))
        for _ in range(50):
            if self.hub.snapshot()["claimed"]:
                break
            time.sleep(0.02)
        with self.assertRaises(RelayCallConsumedError):
            self.hub.claim(self.uid, self.window_id, wait_sec=0, call_id=self._last_call_id())
        hold.set()
        submit_thread.join(timeout=3)
        self.assertFalse(self._errors, self._errors)

    def _last_call_id(self) -> str:
        with self.hub._lock:
            return next(iter(self.hub._calls))

    def test_cross_user_result_is_rejected(self) -> None:
        self._model()
        self._register()
        hold = threading.Event()
        self._answer_worker(hold=hold)
        submit_thread = self._spawn(lambda: self.hub.submit(
            self.uid, {"id": 1, "name": "demo"}, self._spec(), claim_timeout=5, exec_timeout=30))
        for _ in range(50):
            if self.hub.snapshot()["claimed"]:
                break
            time.sleep(0.02)
        call_id = self._last_call_id()
        with self.assertRaises(RelayCrossUserError):
            self.hub.complete(self.other_uid, call_id, 200, "{}", 1)
        hold.set()
        submit_thread.join(timeout=3)
        self.assertFalse(self._errors, self._errors)
        self.assertEqual(self.hub.call_owner(call_id), self.uid)

    def test_oversize_result_is_rejected(self) -> None:
        self._model()
        self._register()
        self._answer_worker(body="x" * 5000, expect=RelayOversizeError)
        with self.assertRaises(RelayExecError) as ctx:
            self.hub.submit(
                self.uid, {"id": 1, "name": "demo"}, self._spec(max_bytes=4096),
                claim_timeout=5, exec_timeout=30)
        self.assertIn("超过上限", str(ctx.exception))
        self.assertFalse(self._errors, self._errors)

    def test_release_window_fails_pending_submit(self) -> None:
        self._model()
        self._register()
        def submit_after_release():
            try:
                self.hub.submit(
                    self.uid, {"id": 1, "name": "demo"}, self._spec(),
                    claim_timeout=5, exec_timeout=30)
            except BaseException as exc:  # noqa: BLE001
                self._errors.append(exc)
        submit_thread = self._spawn(submit_after_release)
        for _ in range(50):
            if self.hub.pending_count(self.uid):
                break
            time.sleep(0.02)
        self.assertTrue(self.hub.pending_count(self.uid))
        self.assertTrue(self.hub.release_window(self.uid, self.window_id))
        submit_thread.join(timeout=3)
        self.assertFalse(submit_thread.is_alive())
        self.assertEqual(len(self._errors), 1, self._errors)
        self.assertIsInstance(self._errors[0], RelayAbsentError)

    def test_window_limit_evicts_oldest(self) -> None:
        self._model()
        self.hub.register_window(self.uid, "w-one")
        self.hub.register_window(self.uid, "w-two")
        self.assertEqual(self.hub.window_count(self.uid), 2)
        self.hub.register_window(self.uid, "w-three")
        self.assertEqual(self.hub.window_count(self.uid), 2)
        with self.assertRaises(RelayWindowEvictedError):
            self.hub.claim(self.uid, "w-one", wait_sec=0)
        with self.assertRaises(RelayWindowEvictedError):
            self.hub.register_window(self.uid, "w-one")

    def test_network_error_is_exec_failure(self) -> None:
        self._model()
        self._register()
        self._answer_worker(error_kind="network", expect=RelayExecError)
        with self.assertRaises(RelayExecError):
            self.hub.submit(
                self.uid, {"id": 1, "name": "demo"}, self._spec(),
                claim_timeout=5, exec_timeout=30)
        self.assertFalse(self._errors, self._errors)

    def test_role_filter(self) -> None:
        model = {"extra": {"browser_relay": {"allow_roles": ["engineer"]}}}
        self.assertTrue(self.hub.role_allowed(model, self.uid))
        self.assertFalse(self.hub.role_allowed(model, self.other_uid))
        unconditional = {"extra": {}}
        self.assertTrue(self.hub.role_allowed(unconditional, self.other_uid))

    # ---------- 网关 ----------
    def test_gateway_uses_browser_transport_and_records_usage(self) -> None:
        self._model(priority=0)
        self._register()
        events: list[dict] = []
        self.gateway.on_usage = events.append
        self._answer_worker()
        with self.gateway.user_context(self.uid):
            result = self.gateway.complete("default", "系统", "用户", purpose="diagnosis")
        self.assertEqual(result.text, "OK")
        self.assertEqual(events[-1]["transport"], "browser")
        self.assertEqual(events[-1]["user_id"], self.uid)
        self.assertFalse(self._errors, self._errors)

    def test_gateway_falls_back_to_server_when_browser_offline(self) -> None:
        self._model(provider="mock", transport="browser", priority=0)
        self._model(provider="mock", transport="server", priority=1)
        events: list[dict] = []
        self.gateway.on_usage = events.append
        with self.gateway.user_context(self.uid):
            result = self.gateway.complete("default", "系统", "用户", purpose="diagnosis")
        self.assertTrue(result.text)
        self.assertEqual(events[-1]["transport"], "server")

    def test_gateway_orders_server_before_browser_on_same_priority(self) -> None:
        self._model(provider="mock", transport="browser", priority=5)
        self._model(provider="mock", transport="server", priority=5)
        self._register()
        chosen = self.gateway.choose("default", user_id=self.uid)
        self.assertEqual([m["transport"] for m in chosen], ["server", "browser"])

    def test_gateway_raises_no_model_when_only_browser_offline(self) -> None:
        self._model(provider="mock", transport="browser", priority=0)
        with self.gateway.user_context(self.uid):
            with self.assertRaises(NoModelError):
                self.gateway.complete("default", "系统", "用户")


if __name__ == "__main__":
    unittest.main()
