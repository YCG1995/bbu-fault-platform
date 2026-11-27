"""AI 交互流程硬化回归：重试/预算、Schema 熔断、上下文、端点、私网与补规划门禁。"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bbu.agent import BoundedAgent
from bbu.ai import (AIGateway, AIError, AIResult, Budget, EndpointGate,
                    Usage, estimate_tokens, fit_prompt_to_context,
                    trim_text_to_tokens)
from bbu.config import Config
from bbu.db import Database
from bbu.netguard import (OutboundPolicy, OutboundPolicyError, _allowlist_match,
                          endpoint_target, policy_from_config,
                          validate_outbound_url)
from bbu.security import SecretBox


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.db = Database(self.tmp / "test.db")
        self.config = Config(self.tmp)
        self.config.values["data_dir"] = str(self.tmp / "data")
        self.box = SecretBox(self.tmp)
        self.now = "2026-11-27T00:00:00Z"

    def add_model(self, name: str = "test", provider: str = "openai",
                  base_url: str = "http://127.0.0.1/v1", extra: dict | None = None,
                  roles: list[str] | None = None, priority: int = 0) -> dict:
        key = self.box.encrypt("test-key")
        self.db.execute(
            "INSERT INTO model_configs(name,provider,base_url,api_key,model,roles,max_tokens,"
            "temperature,timeout_sec,enabled,priority,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, provider, base_url, key, "test-model",
             json.dumps(roles or ["default"]), 4096, 0.2, 5, 1, priority,
             json.dumps(extra or {}), self.now, self.now))
        gateway = AIGateway(self.db, self.config, secret_box=self.box)
        return gateway.choose("default")[0]

    def gateway(self) -> AIGateway:
        return AIGateway(self.db, self.config, secret_box=self.box)


class RetryAndBudgetTests(_Base):
    def test_non_retryable_error_is_not_retried(self):
        self.config.values["model_schema_mode"] = "off"
        self.add_model()
        attempts = []
        with patch("bbu.ai._post_json",
                   side_effect=AIError("HTTP 400: invalid request", http_status=400,
                                       retryable=False)) as post, \
             patch("bbu.ai.time.sleep") as sleep:
            with self.assertRaises(AIError):
                self.gateway().complete("default", "sys", "user",
                                        retries_per_model=2,
                                        attempt_sink=attempts.append)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(attempts[0]["status"], "error")
        self.assertEqual(attempts[0]["http_status"], 400)

    def test_retryable_error_uses_per_model_retry_budget(self):
        self.config.values["model_schema_mode"] = "off"
        self.add_model()
        with patch("bbu.ai._post_json",
                   side_effect=AIError("HTTP 503: upstream error", http_status=503,
                                       retryable=True)) as post, \
             patch("bbu.ai.time.sleep"):
            with self.assertRaises(AIError):
                self.gateway().complete("default", "sys", "user", retries_per_model=2)
        self.assertEqual(post.call_count, 3)

    def test_429_switches_to_another_model_without_repeating_same_endpoint(self):
        self.config.values["model_schema_mode"] = "off"
        self.add_model(name="limited", priority=0)
        self.add_model(name="backup", priority=1)
        gateway = self.gateway()
        calls = []

        def fake_call(model, system, user, schema, max_tokens, temperature,
                      purpose, stream):
            calls.append(model["name"])
            if model["name"] == "limited":
                raise AIError("HTTP 429: Too many requests", http_status=429,
                              retryable=True, retry_after=7)
            return AIResult(text="backup-ok", usage=Usage(total_tokens=2))

        gateway._call = fake_call
        with patch("bbu.ai.time.sleep") as sleep:
            result = gateway.complete("default", "sys", "user", retries_per_model=2)
        self.assertEqual(result.text, "backup-ok")
        self.assertEqual(calls, ["limited", "backup"])
        sleep.assert_not_called()

    def test_failed_attempt_counts_against_call_budget(self):
        self.config.values["model_schema_mode"] = "off"
        self.add_model()
        budget = Budget(max_calls=1, max_tokens=10000)
        with patch("bbu.ai._post_json",
                   side_effect=AIError("HTTP 400: bad", http_status=400,
                                       retryable=False)):
            with self.assertRaises(AIError):
                self.gateway().complete("default", "sys", "user", max_tokens=64,
                                        budget=budget, retries_per_model=0)
        self.assertEqual(budget.calls, 1)
        self.assertEqual(budget.snapshot()["pending_calls"], 0)
        with self.assertRaises(Exception):
            budget.reserve(1)

    def test_actual_usage_over_limit_is_marked_and_blocks_next_call(self):
        budget = Budget(max_calls=5, max_tokens=10)
        reservation = budget.reserve(5)
        budget.settle(reservation, Usage(total_tokens=20))
        self.assertTrue(budget.over_budget)
        with self.assertRaises(Exception):
            budget.reserve(1)

    def test_failed_repair_does_not_retry_same_model(self):
        self.config.values["model_schema_mode"] = "off"
        self.add_model(extra={"capabilities": {"json_mode": True, "strict_schema": True}})
        calls = []

        def fake_call(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return AIResult(text="not json", usage=Usage(total_tokens=3))
            raise AIError("repair transport down", retryable=True)

        gateway = self.gateway()
        gateway._call = fake_call
        with patch("bbu.ai.time.sleep") as sleep:
            with self.assertRaises(AIError):
                gateway.complete("default", "sys", "user",
                                 json_schema={"type": "object"}, retries_per_model=2)
        self.assertEqual(len(calls), 2)
        sleep.assert_not_called()


    def test_complete_preserves_explicit_output_when_not_declared(self):
        self.config.values["model_schema_mode"] = "off"
        self.add_model(extra={"capabilities": {"json_mode": True}})
        gateway = self.gateway()
        seen = []

        def fake_call(model, system, user, schema, max_tokens, temperature,
                      purpose, stream):
            seen.append(max_tokens)
            return AIResult(text="ok", usage=Usage(total_tokens=2))

        gateway._call = fake_call
        result = gateway.complete("default", "sys", "user", max_tokens=12000,
                                  retries_per_model=0)
        self.assertEqual(result.text, "ok")
        self.assertEqual(seen, [12000])


class SchemaBreakerTests(_Base):
    def test_repeated_schema_failures_trip_breaker_and_mark_degraded(self):
        model = self.add_model(extra={"capabilities": {"json_mode": True,
                                                       "strict_schema": True}})
        gateway = self.gateway()
        body = {"choices": [{"message": {"content": "not-json"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
        with patch("bbu.ai._post_json", return_value=(body, 200)):
            for _ in range(2):
                with self.assertRaises(AIError):
                    gateway.complete("default", "sys", "user",
                                     json_schema={"type": "object"}, retries_per_model=0)
        self.assertTrue(gateway.schema_breaker.open_keys())
        caps = gateway.capabilities.cached(model) or {}
        self.assertTrue(caps.get("degraded"))
        self.assertTrue((gateway.capabilities.public(model) or {}).get("degraded"))
        self.assertIn("JSON", str(caps.get("probe_error") or ""))
        self.assertEqual(gateway.choose("default"), [])
        with self.assertRaises(Exception):
            gateway.complete("default", "sys", "user",
                             json_schema={"type": "object"}, retries_per_model=0)


    def test_probe_queue_rejection_does_not_mark_endpoint_degraded(self):
        model = self.add_model()
        gateway = self.gateway()

        def queue_rejected(_model):
            raise AIError("模型端点排队已满", retryable=False, circuit_break=False)

        gateway.capabilities.call_probe = queue_rejected
        caps = gateway.capabilities.get(model, probe=True)
        self.assertFalse(caps.get("degraded"))
        self.assertIn("排队", caps.get("probe_error") or "")
        self.assertIsNone(gateway.capabilities.cached(model))


    def test_probe_result_marks_database_max_tokens_as_non_declared(self):
        model = self.add_model()
        gateway = self.gateway()
        body = {"choices": [{"message": {"content": "{\"ok\":true}"}}]}
        with patch("bbu.ai._post_json", return_value=(body, 200)):
            caps = gateway._probe_capabilities(model)
        self.assertEqual(caps["max_output"], 4096)
        self.assertFalse(caps["max_output_declared"])

class ContextWindowTests(unittest.TestCase):
    def test_context_fit_preserves_tail_and_respects_window(self):
        system = "系统提示词"
        user = "开始数据" + ("日志内容" * 2000) + "\nJSON Schema：{\"type\":\"object\"}"
        fitted = fit_prompt_to_context(system, user, context_window=1024,
                                       max_output=256, requested_max_tokens=256,
                                       margin_tokens=64)
        self.assertGreater(fitted["trimmed_chars"], 0)
        self.assertIn("[已按模型上下文窗口裁剪]", fitted["user"])
        self.assertIn("JSON Schema", fitted["user"][-400:])
        self.assertLessEqual(fitted["input_tokens"] + fitted["max_tokens"], 1024)
        self.assertLessEqual(fitted["max_tokens"], 256)

    def test_explicit_output_request_survives_non_declared_default(self):
        fitted = fit_prompt_to_context("sys", "user", context_window=32768,
                                       max_output=4096, requested_max_tokens=12000,
                                       max_output_declared=False, margin_tokens=256)
        self.assertEqual(fitted["max_tokens"], 12000)
        fitted = fit_prompt_to_context("sys", "user", context_window=32768,
                                       max_output=4096, requested_max_tokens=12000,
                                       max_output_declared=True, margin_tokens=256)
        self.assertEqual(fitted["max_tokens"], 4096)

    def test_small_window_and_zero_margin_never_overrun(self):
        fitted = fit_prompt_to_context("sys", "user", context_window=512,
                                       max_output=4096, requested_max_tokens=12000,
                                       margin_tokens=256)
        self.assertEqual(fitted["context_window"], 512)
        self.assertLessEqual(fitted["input_tokens"] + fitted["max_tokens"], 512)
        self.assertLessEqual(fitted["max_tokens"], 256)

        fitted = fit_prompt_to_context("系统提示词", "用户内容" * 1000,
                                       context_window=1024, max_output=960,
                                       requested_max_tokens=960, margin_tokens=0)
        self.assertLessEqual(fitted["input_tokens"] + fitted["max_tokens"], 1024)

    def test_trim_text_never_exceeds_budget(self):
        text = "中" * 2000
        trimmed = trim_text_to_tokens(text, 100)
        self.assertLessEqual(estimate_tokens(trimmed), 100)

    def test_complete_uses_trimmed_prompt_and_effective_output(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        db = Database(tmp / "test.db")
        config = Config(tmp)
        config.values["data_dir"] = str(tmp / "data")
        box = SecretBox(tmp)
        now = "2026-11-27T00:00:00Z"
        db.execute(
            "INSERT INTO model_configs(name,provider,base_url,api_key,model,roles,max_tokens,"
            "temperature,timeout_sec,enabled,priority,extra,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ctx", "openai", "http://127.0.0.1/v1", box.encrypt("k"), "m",
             json.dumps(["default"]), 4096, 0.2, 5, 1, 0,
             json.dumps({"capabilities": {"json_mode": True, "strict_schema": True,
                                          "context_window": 1024, "max_output": 256}}),
             now, now))
        gateway = AIGateway(db, config, secret_box=box)
        captured = {}

        def fake_call(model, system, user, schema, max_tokens, temperature, purpose,
                      stream=False):
            captured.update({"system": system, "user": user,
                             "max_tokens": max_tokens})
            return AIResult(text="{\"ok\": true}", usage=Usage(total_tokens=10))

        gateway._call = fake_call
        result = gateway.complete("default", "sys", "日志" * 4000,
                                  json_schema={"type": "object"}, max_tokens=256)
        self.assertEqual(result.data, {"ok": True})
        self.assertGreater(result.context_trimmed_chars, 0)
        self.assertIn("[已按模型上下文窗口裁剪]", captured["user"])
        self.assertLessEqual(captured["max_tokens"], 256)


class EndpointAndSelectionTests(_Base):
    def test_endpoint_gate_times_out_when_saturated(self):
        gate = EndpointGate(concurrency=1, queue_limit=2, queue_timeout_sec=0.05)
        gate.acquire()
        try:
            with self.assertRaises(AIError) as ctx:
                gate.acquire()
            self.assertIn("排队", str(ctx.exception))
            self.assertFalse(getattr(ctx.exception, "circuit_break", True))
        finally:
            gate.release()

    def test_queue_limit_refuses_waiters(self):
        gate = EndpointGate(concurrency=1, queue_limit=1, queue_timeout_sec=0.05)
        gate.acquire()
        started = threading.Event()
        waiter_done = threading.Event()

        def waiter():
            started.set()
            try:
                gate.acquire()
            except AIError:
                pass
            finally:
                waiter_done.set()
                gate.release()

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        started.wait(0.2)
        time.sleep(0.02)
        try:
            with self.assertRaises(AIError) as ctx:
                gate.acquire()
            self.assertIn("已满", str(ctx.exception))
        finally:
            gate.release()
            waiter_done.wait(0.5)

    def test_equal_priority_models_rotate(self):
        self.add_model("m1", priority=0)
        self.add_model("m2", priority=0)
        gateway = self.gateway()
        first = [m["name"] for m in gateway.choose("default")]
        second = [m["name"] for m in gateway.choose("default")]
        self.assertEqual(set(first), {"m1", "m2"})
        self.assertEqual(set(second), {"m1", "m2"})
        self.assertNotEqual(first[0], second[0])

    def test_private_allowlist_limits_private_targets(self):
        strict = OutboundPolicy(allow_private=True,
                                private_allowlist=("10.0.0.0/8", "127.0.0.1"))
        validate_outbound_url("http://10.1.2.3/v1", strict)
        validate_outbound_url("http://127.0.0.1/v1", strict)
        with self.assertRaises(OutboundPolicyError):
            validate_outbound_url("http://192.168.1.10/v1", strict)
        open_policy = OutboundPolicy(allow_private=True, private_allowlist=())
        validate_outbound_url("http://192.168.1.10/v1", open_policy)

    def test_allowlist_host_patterns_and_config(self):
        self.assertTrue(_allowlist_match("llm.corp.example",  # host pattern
                                         __import__("ipaddress").ip_address("10.0.0.1"),
                                         ("*.corp.example",)))
        cfg = {"model_private_allowlist": "10.0.0.0/8, *.corp.example"}
        policy = policy_from_config(cfg)
        self.assertEqual(policy.private_allowlist, ("10.0.0.0/8", "*.corp.example"))
        self.assertEqual(endpoint_target("http://10.1.2.3:8080/v1/chat/completions"),
                         ("10.1.2.3", "/v1/chat/completions"))


class BoundedReplanGateTests(_Base):
    def test_replan_bool_does_not_break_limits_and_violations_are_reported(self):
        self.config.values["agent_engine_mode"] = "bounded"
        self.config.values["agent_max_replans"] = 1
        agent = BoundedAgent(self.db, self.config)
        catalog = [{"name": "clock"}]
        clean = {"replan": True, "steps": [
            {"id": "S1", "title": "时钟检查", "skills": ["clock"], "gate": "always"}]}
        trace = agent.review_plan({"id": "T-1"}, clean, catalog, [])
        self.assertEqual(trace["violations"], [])

        bad = {"replan": True, "steps": [
            {"id": "S1", "title": "执行 shell", "skills": ["not-registered"],
             "gate": "always"}]}
        trace = agent.review_plan({"id": "T-2"}, bad, catalog, [])
        self.assertTrue(trace["violations"])
        self.assertTrue(any("未注册" in x or "开放动作" in x
                            for x in trace["violations"]))

    def test_replan_count_over_limit_is_rejected(self):
        self.config.values["agent_engine_mode"] = "bounded"
        self.config.values["agent_max_replans"] = 0
        agent = BoundedAgent(self.db, self.config)
        trace = agent.review_plan({"id": "T-3"},
                                  {"replan": True, "steps": [
                                      {"id": "S1", "skills": ["clock"],
                                       "gate": "always"}]},
                                  [{"name": "clock"}], [], replan_count=1)
        self.assertIn("本次不允许重规划", trace["violations"])


if __name__ == "__main__":
    unittest.main()
