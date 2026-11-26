"""AI 网关：OpenAI 兼容 / Anthropic 原生 / Mock。

设计要点（对应“AI 要强、不能一直循环”）：
- 角色路由（router/arbiter/writer/workshop）+ 优先级 + 失败回退；
- 每次模型调用都受任务级预算约束（调用数 / token / 墙钟）；
- 结构化输出本地校验，最多一次修复，绝不无限重试；
- 连续失败熔断，避免一个坏 endpoint 拖死整个任务。
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from contextlib import contextmanager
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .db import Database
from .model_capabilities import ModelCapabilityStore
from .netguard import (
    OutboundContentTypeError,
    OutboundPolicy,
    OutboundPolicyError,
    OutboundResponseTooLarge,
    error_body,
    iter_limited,
    open_url,
    policy_from_config,
    read_limited,
    require_json_content_type,
)
from .schema_validate import validate_schema
from .util import utc_now


class AIError(RuntimeError):
    """模型错误保留结构化 HTTP 信息，轨迹页需要原始响应体定位网关问题。"""

    def __init__(self, message: str, *, http_status: int = 0,
                 response_body: str = "", retryable: bool = True) -> None:
        super().__init__(message)
        self.http_status = int(http_status or 0)
        self.response_body = str(response_body or "")
        self.retryable = bool(retryable)


class NoModelError(AIError):
    pass


class BudgetExceeded(AIError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens or (other.input_tokens + other.output_tokens)


@dataclass
class AIResult:
    text: str
    data: Any = None
    model_name: str = ""
    provider: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    purpose: str = ""
    stream_used: bool = False


class Budget:
    """任务级预算；线程安全。"""

    def __init__(self, max_calls: int, max_tokens: int, deadline: float | None = None,
                 on_event: Callable[[str, str, dict], None] | None = None,
                 prior: dict[str, Any] | None = None) -> None:
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.deadline = deadline
        prior = prior or {}
        self.calls = max(0, int(prior.get("calls") or 0))
        self.usage = Usage(
            input_tokens=max(0, int(prior.get("input_tokens") or 0)),
            output_tokens=max(0, int(prior.get("output_tokens") or 0)),
            total_tokens=max(0, int(prior.get("total_tokens") or 0)),
        )
        self._lock = threading.Lock()
        self.on_event = on_event
        self._pending_calls = 0
        self._pending_tokens = 0

    def _reserve_locked(self, estimated_tokens: int) -> dict[str, int]:
        est = max(0, int(estimated_tokens or 0))
        if self.calls + self._pending_calls >= self.max_calls:
            raise BudgetExceeded(f"模型调用次数已达上限 {self.max_calls}")
        if self.usage.total_tokens + self._pending_tokens + est > self.max_tokens:
            raise BudgetExceeded(
                f"token 预算不足（已用 {self.usage.total_tokens}，预留 {self._pending_tokens}，"
                f"上限 {self.max_tokens}）")
        if self.deadline and time.time() > self.deadline:
            raise BudgetExceeded("任务墙钟时间已耗尽")
        self._pending_calls += 1
        self._pending_tokens += est
        return {"estimated_tokens": est}

    def reserve(self, estimated_tokens: int = 0) -> dict[str, int]:
        """原子预占一次调用和估算 token；必须 settle 或 release。"""
        with self._lock:
            return self._reserve_locked(estimated_tokens)

    def settle(self, reservation: dict[str, int], usage: Usage) -> None:
        with self._lock:
            est = int((reservation or {}).get("estimated_tokens") or 0)
            self._pending_calls = max(0, self._pending_calls - 1)
            self._pending_tokens = max(0, self._pending_tokens - est)
            self.calls += 1
            self.usage.add(usage)
            if self.on_event:
                self.on_event("model_usage", "模型调用完成", {
                    "calls": self.calls,
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                    "total_tokens": self.usage.total_tokens,
                })

    def release(self, reservation: dict[str, int]) -> None:
        with self._lock:
            est = int((reservation or {}).get("estimated_tokens") or 0)
            self._pending_calls = max(0, self._pending_calls - 1)
            self._pending_tokens = max(0, self._pending_tokens - est)

    def check(self, estimated_tokens: int = 0) -> None:
        with self._lock:
            if self.calls + self._pending_calls >= self.max_calls:
                raise BudgetExceeded(f"模型调用次数已达上限 {self.max_calls}")
            if self.usage.total_tokens + self._pending_tokens + estimated_tokens > self.max_tokens:
                raise BudgetExceeded(
                    f"token 预算不足（已用 {self.usage.total_tokens}，上限 {self.max_tokens}）")
            if self.deadline and time.time() > self.deadline:
                raise BudgetExceeded("任务墙钟时间已耗尽")

    def record(self, usage: Usage) -> None:
        with self._lock:
            self.calls += 1
            self.usage.add(usage)
            if self.on_event:
                self.on_event("model_usage", "模型调用完成", {
                    "calls": self.calls,
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                    "total_tokens": self.usage.total_tokens,
                })

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "max_calls": self.max_calls,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "total_tokens": self.usage.total_tokens,
            "max_tokens": self.max_tokens,
            "pending_calls": self._pending_calls,
            "pending_tokens": self._pending_tokens,
        }

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.calls - self._pending_calls)


_JSON_FENCE_RE = re.compile(r"```(?:json|yaml|yml)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """从模型输出中提取第一个完整 JSON 对象/数组。失败返回 None。"""
    if not text:
        return None
    candidates = [m.group(1) for m in _JSON_FENCE_RE.finditer(text)]
    candidates.append(text)
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = candidate.find(opener)
            if start < 0:
                continue
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(candidate)):
                ch = candidate[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[start:i + 1])
                        except json.JSONDecodeError:
                            break
    return None


class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown: int = 120) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def is_open(self, key: str) -> bool:
        with self._lock:
            count, opened_at = self._state.get(key, (0, 0.0))
            if count >= self.threshold and time.time() - opened_at < self.cooldown:
                return True
            if count >= self.threshold:
                self._state.pop(key, None)
            return False

    def fail(self, key: str) -> None:
        with self._lock:
            count, _ = self._state.get(key, (0, 0.0))
            self._state[key] = (count + 1, time.time())

    def ok(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def open_keys(self) -> list[str]:
        with self._lock:
            now = time.time()
            return [k for k, (count, opened_at) in self._state.items()
                    if count >= self.threshold and now - opened_at < self.cooldown]


class AIGateway:
    def __init__(self, db: Database, config: Any,
                 on_event: Callable[[str, str, dict], None] | None = None,
                 secret_box: Any | None = None) -> None:
        self.db = db
        self.config = config
        self.on_event = on_event
        self.on_usage: Callable[[dict[str, Any]], None] | None = None
        self.breaker = CircuitBreaker()
        self._cache: dict[int, dict[str, Any]] = {}
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, Any] = {"calls": 0, "failures": 0, "recent": [],
                                         "by_model": {}}
        self._health_window = max(5, int(config.get("llm_health_window", 20)) if config else 20)
        self._local = threading.local()
        self.secret_box = secret_box
        self.capabilities = ModelCapabilityStore(
            db, config, call_probe=self._probe_capabilities)
        if self.secret_box is None and config is not None and getattr(config, "data_dir", None):
            from .security import SecretBox
            self.secret_box = SecretBox(config.data_dir)

    # ---------- 用户上下文 ----------
    def _uid(self, user_id: int | None = None) -> int:
        if user_id is not None:
            return int(user_id)
        return int(getattr(self._local, "user_id", 0) or 0)

    def set_user(self, user_id: int | None) -> int:
        """设置当前线程的模型归属用户，返回旧值用于恢复。"""
        previous = int(getattr(self._local, "user_id", 0) or 0)
        self._local.user_id = int(user_id or 0)
        return previous

    @contextmanager
    def user_context(self, user_id: int | None):
        previous = self.set_user(user_id)
        try:
            yield
        finally:
            self.set_user(previous)

    def set_usage_context(self, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """设置当前线程的用量归属（任务 / 工坊 / 助手 / 技能）。"""
        previous = dict(getattr(self._local, "usage_meta", {}) or {})
        self._local.usage_meta = dict(meta or {})
        return previous

    @contextmanager
    def usage_context(self, user_id: int | None, **meta: Any):
        previous_user = self.set_user(user_id)
        previous_meta = self.set_usage_context(meta)
        try:
            yield
        finally:
            self.set_usage_context(previous_meta)
            self.set_user(previous_user)

    def _emit_usage(self, item: dict[str, Any], model: dict[str, Any],
                    purpose: str) -> None:
        if self.on_usage is None:
            return
        meta = dict(getattr(self._local, "usage_meta", {}) or {})
        event = {
            "user_id": self._uid(),
            "task_id": str(meta.get("task_id") or ""),
            "task_kind": str(meta.get("task_kind") or "direct"),
            "skill_name": str(meta.get("skill_name") or ""),
            "model_id": str(model.get("id") or ""),
            "model_name": str(model.get("name") or model.get("model") or ""),
            "provider": str(model.get("provider") or ""),
            "model": str(model.get("model") or ""),
            "scope": str(model.get("source") or "admin"),
            "purpose": str(purpose or ""),
            "stage": str(item.get("stage") or ""),
            "status": str(item.get("status") or "success"),
            "input_tokens": int(item.get("input_tokens") or 0),
            "output_tokens": int(item.get("output_tokens") or 0),
            "total_tokens": int(item.get("total_tokens") or 0),
            "estimated_input_tokens": int(item.get("estimated_input_tokens") or 0),
            "latency_ms": int(item.get("latency_ms") or 0),
            "error": str(item.get("error") or ""),
        }
        try:
            self.on_usage(event)
        except Exception:
            # 统计落库失败不能打断诊断 / 工坊主链路。
            pass

    # ---------- 模型选择 ----------
    def _global_models(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM model_configs WHERE enabled=1 ORDER BY priority ASC, id ASC")
        for row in rows:
            try:
                row["roles"] = json.loads(row.get("roles") or "[]")
            except json.JSONDecodeError:
                row["roles"] = []
            try:
                row["extra"] = json.loads(row.get("extra") or "{}")
            except json.JSONDecodeError:
                row["extra"] = {}
            if self.secret_box and row.get("api_key"):
                row["api_key"] = self.secret_box.decrypt(row["api_key"])
            row["source"] = "admin"
            row["personal"] = False
            row["allow_private"] = (
                bool(self.config.get("model_discovery_allow_private", False))
                if self.config is not None else True)
        return rows

    def _personal_model(self, user_id: int | None = None) -> dict[str, Any] | None:
        uid = self._uid(user_id)
        if not uid:
            return None
        row = self.db.one(
            "SELECT c.*, u.username FROM user_model_configs c JOIN users u ON u.id=c.user_id "
            "WHERE c.user_id=? AND c.enabled=1", (uid,))
        if not row:
            return None
        model = dict(row)
        model.update({
            "id": f"personal:{uid}",
            "name": f"个人 MaaS（{row.get('username') or uid}）",
            "roles": ["default", "planner", "router", "arbiter", "writer", "workshop", "assistant"],
            "extra": {},
            "source": "personal",
            "personal": True,
            "allow_private": (
                bool(self.config.get("model_discovery_allow_private", False))
                if self.config is not None else True),
        })
        try:
            model["api_key"] = self.secret_box.decrypt(row["api_key"]) if self.secret_box else row["api_key"]
            model["key_error"] = ""
        except ValueError as exc:
            model["api_key"] = ""
            model["key_error"] = str(exc)
        return model

    def _models(self, user_id: int | None = None) -> list[dict[str, Any]]:
        personal = self._personal_model(user_id)
        return ([personal] if personal else []) + self._global_models()

    def choose(self, role: str, override_id: int | None = None,
               user_id: int | None = None) -> list[dict[str, Any]]:
        models = self._models(user_id)
        if override_id:
            chosen = [m for m in models if not m.get("personal") and m["id"] == override_id]
            if chosen:
                return chosen
            raise NoModelError(f"指定的模型配置不存在或已停用: {override_id}")
        exact = [m for m in models if role in m["roles"] and not m.get("key_error")]
        rest = [m for m in models if role not in m["roles"]
                and (not m["roles"] or "default" in m["roles"]) and not m.get("key_error")]
        ordered = exact + [m for m in rest if m not in exact]
        return [m for m in ordered if not self.breaker.is_open(f"{m['id']}:{m['name']}")]

    def available(self, role: str = "default", user_id: int | None = None) -> bool:
        try:
            return bool(self.choose(role, user_id=user_id))
        except NoModelError:
            return False

    def has_real_model(self, role: str = "default", user_id: int | None = None) -> bool:
        try:
            return any(m["provider"] != "mock" for m in self.choose(role, user_id=user_id))
        except NoModelError:
            return False

    def configured(self, role: str = "default", user_id: int | None = None) -> bool:
        """是否存在该角色的真实模型配置（忽略熔断状态）。"""
        models = self._models(user_id)
        exact = [m for m in models if role in m["roles"]]
        rest = [m for m in models if role not in m["roles"] and (not m["roles"] or "default" in m["roles"])]
        return any(m["provider"] != "mock" for m in exact + rest)

    def model_status(self, user_id: int | None = None) -> dict[str, Any]:
        """当前用户的模型可用性；不返回 Key 明文。"""
        uid = self._uid(user_id)
        personal = self._personal_model(uid)
        globals_ = self._global_models()
        personal_ok = bool(personal and not personal.get("key_error")
                           and not self.breaker.is_open(f"{personal['id']}:{personal['name']}"))
        global_rows = [m for m in globals_ if (not m["roles"] or "default" in m["roles"])]
        global_ok = [m for m in global_rows
                     if not self.breaker.is_open(f"{m['id']}:{m['name']}")]
        usable = personal_ok or bool(global_ok)
        url = str(self.config.get("personal_api_key_url") or "")
        notice = None
        if not usable:
            if personal:
                notice = {"code": "personal_key_error", "action": "configure_personal_key",
                          "title": "个人 MaaS Key 暂不可用",
                          "message": "个人模型调用失败或 Key 无法解密，请更新个人 API Key 后重试。",
                          "url": url}
            elif not global_rows:
                notice = {"code": "personal_key_required", "action": "configure_personal_key",
                          "title": "尚未配置可用模型",
                          "message": "管理员尚未配置默认模型，请配置个人 MaaS API Key。",
                          "url": url}
            else:
                notice = {"code": "default_model_error", "action": "configure_personal_key",
                          "title": "管理员默认模型暂时不可用",
                          "message": "默认模型连续失败，建议配置个人 MaaS API Key 后继续诊断。",
                          "url": url}
        return {
            "user_id": uid,
            "personal": {
                "configured": bool(personal), "model": (personal or {}).get("model") or "",
                "available": personal_ok,
                "error": (personal or {}).get("key_error") or "",
            },
            "default": {
                "configured": bool(global_rows),
                "available": bool(global_ok),
                "count": len(global_rows),
                "models": [{"name": m.get("name"), "model": m.get("model"),
                            "provider": m.get("provider")} for m in global_rows[:5]],
            },
            "usable": usable,
            "source": "personal" if personal_ok else ("admin" if global_ok else ""),
            "notice": notice,
            "maas_url": url,
        }

    # ---------- 主入口 ----------
    # ---------- 健康与指标 ----------
    def _note_model(self, model: dict[str, Any], ok: bool,
                    latency_ms: int = 0, error: str = "") -> None:
        key = f"{model.get('name') or model.get('model') or model.get('id')}"
        with self._metrics_lock:
            self._metrics["calls"] += 1
            if not ok:
                self._metrics["failures"] += 1
            self._metrics["recent"].append({
                "ts": round(time.time(), 3), "model": key, "ok": ok,
                "latency_ms": latency_ms, "error": (error or "")[:200],
            })
            self._metrics["recent"] = self._metrics["recent"][-self._health_window:]
            item = self._metrics["by_model"].setdefault(
                key, {"calls": 0, "failures": 0, "latency_ms": []})
            item["calls"] += 1
            if not ok:
                item["failures"] += 1
            if latency_ms:
                item["latency_ms"].append(latency_ms)
                item["latency_ms"] = item["latency_ms"][-self._health_window:]

    def health(self) -> dict[str, Any]:
        with self._metrics_lock:
            recent = list(self._metrics["recent"])
            models = {}
            for name, item in self._metrics["by_model"].items():
                calls = item["calls"] or 1
                lat = sorted(item["latency_ms"])
                models[name] = {
                    "calls": item["calls"],
                    "failures": item["failures"],
                    "success_rate": round(1 - item["failures"] / calls, 3),
                    "p95_latency_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0,
                }
        success = sum(1 for r in recent if r["ok"])
        return {
            "calls": self._metrics["calls"],
            "failures": self._metrics["failures"],
            "recent_calls": len(recent),
            "recent_success_rate": round(success / len(recent), 3) if recent else None,
            "models": models,
            "circuit_open": self.breaker.open_keys() if hasattr(self.breaker, "open_keys") else [],
        }

    def _probe_capabilities(self, model: dict[str, Any]) -> dict[str, Any]:
        """低成本探测 JSON Mode/usage；不发业务数据，不写入任务预算。"""
        provider = str(model.get("provider") or "")
        extra = model.get("extra") if isinstance(model.get("extra"), dict) else {}
        base = {"json_mode": provider == "anthropic", "strict_schema": False,
                "usage": True,
                "context_window": int(extra.get("context_window") or 32768),
                "max_output": int(extra.get("max_output") or model.get("max_tokens") or 4096),
                "degraded": False, "probe_error": ""}
        if provider == "anthropic":
            return base
        if provider != "openai":
            base.update({"json_mode": False, "degraded": True,
                         "probe_error": f"未知 provider {provider}"})
            return base
        headers = {"Authorization": f"Bearer {model['api_key']}"}
        headers.update(_headers_extra(model))
        payload = {
            "model": model["model"],
            "messages": [{"role": "user", "content": "只返回 JSON：{\"ok\":true}"}],
            "max_tokens": 16,
            "temperature": 0.0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        try:
            data, _ = _post_json(_openai_url(model["base_url"]), payload, headers,
                                 min(30, int(model.get("timeout_sec") or 30)),
                                 allow_private=bool(model.get("allow_private", False)))
            base["json_mode"] = True
            base["usage"] = bool(data.get("usage"))
            return base
        except AIError as exc:
            text = str(exc).lower()
            if "response_format" in text or "json_object" in text or "unsupported" in text:
                base.update({"json_mode": False, "degraded": True,
                             "probe_error": str(exc)[:300]})
                return base
            raise

    def complete(self, role: str, system: str, user: str, *,
                 json_schema: dict[str, Any] | None = None,
                 max_tokens: int | None = None,
                 temperature: float | None = None,
                 purpose: str = "",
                 budget: Budget | None = None,
                 model_override: int | None = None,
                 retries_per_model: int | None = None,
                 stage: str = "",
                 stream: bool = False,
                 attempt_sink: Callable[[dict[str, Any]], None] | None = None,
                 trace: Any = None) -> AIResult:
        models = self.choose(role, model_override, user_id=self._uid())
        if not models:
            status = self.model_status()
            notice = status.get("notice") or {}
            raise NoModelError(notice.get("message")
                               or "没有可用的模型配置（请到“模型接入”添加并启用）")
        if retries_per_model is None:
            retries_per_model = max(0, min(3, int(
                self.config.get("llm_retry_attempts", 2) if self.config else 1)))
        backoff = max(0.0, float(
            self.config.get("llm_retry_backoff_sec", 2.0) if self.config else 1.2))
        attempt_stage = stage or purpose or "model"

        def note_attempt(model: dict[str, Any], status: str, latency_ms: int = 0,
                         usage: Usage | None = None, error: str = "",
                         input_estimate: int = 0,
                         stream_used: bool | None = None) -> None:
            item: dict[str, Any] = {
                "stage": attempt_stage,
                "model": str(model.get("name") or model.get("model") or model.get("id") or ""),
                "latency_ms": max(0, int(latency_ms)),
                "input_tokens": int(usage.input_tokens) if usage else 0,
                "output_tokens": int(usage.output_tokens) if usage else 0,
                "status": status,
                "stream": bool(stream if stream_used is None else stream_used),
            }
            if usage:
                item["total_tokens"] = int(usage.total_tokens or
                                               usage.input_tokens + usage.output_tokens)
            elif input_estimate:
                item["estimated_input_tokens"] = max(1, int(input_estimate))
            if error:
                item["error"] = str(error)[:300]
                match = re.search(r"\bHTTP\s+(\d{3})\b", str(error))
                if match:
                    item["http_status"] = int(match.group(1))
            if attempt_sink is not None:
                attempt_sink(item)
            self._emit_usage(item, model, purpose)

        errors: list[str] = []
        for model in models:
            key = f"{model['id']}:{model['name']}"
            for attempt in range(retries_per_model + 1):
                reservation: dict[str, int] | None = None
                call_started = time.monotonic()
                try:
                    if budget:
                        est = max(1, (len(system) + len(user)) // 3)
                        reservation = budget.reserve(est)
                    schema_mode = str(self.config.get("model_schema_mode", "auto")
                                      if self.config else "auto").lower()
                    capabilities = self.capabilities.get(
                        model, probe=(schema_mode in ("auto", "probe")))
                    model_schema = json_schema
                    prompt_user = user
                    if json_schema and provider_name(model) != "mock":
                        if schema_mode == "off" or not capabilities.get("json_mode"):
                            model_schema = None
                            prompt_user = (user + "\n\n请只输出严格 JSON 对象，"
                                           "不要 Markdown 代码块或解释。字段和类型必须符合要求。")
                        elif not capabilities.get("strict_schema"):
                            # 普通 OpenAI 兼容端点通常只支持 json_object。强制
                            # json_schema/strict 会让可选字段变必填，也会触发部分
                            # 网关的 400/503；业务校验仍用原始 schema 在本地完成。
                            model_schema = {"type": "json_object"}
                            prompt_user = (user + "\n\n请只输出严格 JSON 对象，"
                                           "不要 Markdown 代码块或解释；字段和类型必须符合上面的 JSON Schema。")
                    if trace:
                        try:
                            trace.model_request(
                                role=role, model=model, provider=provider_name(model),
                                purpose=purpose, stage=attempt_stage, attempt=attempt + 1,
                                system=system, user=prompt_user, json_schema=model_schema,
                                temperature=(model.get("temperature") if temperature is None else temperature),
                                max_tokens=max_tokens, stream=stream, phase=attempt_stage)
                        except Exception:
                            pass
                    try:
                        result = self._call(model, system, prompt_user, model_schema,
                                            max_tokens, temperature, purpose, stream)
                    except Exception:
                        if budget and reservation:
                            budget.release(reservation)
                        reservation = None
                        raise
                    self._note_model(model, True, result.latency_ms)
                    if budget and reservation:
                        budget.settle(reservation, result.usage)
                    reservation = None
                    self.breaker.ok(key)
                    parsed = extract_json(result.text) if json_schema else None
                    if trace:
                        try:
                            trace.model_response(
                                role=role, model=model, provider=provider_name(model),
                                purpose=purpose, stage=attempt_stage, attempt=attempt + 1,
                                text=result.text, parsed=parsed, usage=result.usage,
                                latency_ms=result.latency_ms, stream=result.stream_used,
                                phase=attempt_stage)
                        except Exception:
                            pass
                    if json_schema:
                        data = parsed
                        problems = (["模型未返回合法 JSON"] if data is None
                                    else validate_schema(data, json_schema))
                        if not problems:
                            result.data = data
                            note_attempt(model, "success", result.latency_ms, result.usage,
                                         stream_used=result.stream_used)
                            return result
                        note_attempt(model, "error", result.latency_ms, result.usage,
                                     "JSON 校验失败: " + "；".join(problems[:3]))
                        if trace:
                            try:
                                trace.validation(phase=attempt_stage, name="model_json_schema",
                                                 ok=False, errors=problems[:20],
                                                 details={"text": result.text})
                            except Exception:
                                pass
                        # 只修复一次，修复失败直接换下一个模型/交给调用方降级
                        if attempt == 0:
                            repair_user = (
                                f"{user}\n\n[上一次输出不合法，问题如下]\n"
                                + "\n".join(f"- {e}" for e in problems[:12])
                                + "\n请只输出严格符合 JSON Schema 的 JSON，不要解释。"
                            )
                            repair_reservation: dict[str, int] | None = None
                            if budget:
                                repair_reservation = budget.reserve(
                                    max(1, (len(system) + len(repair_user)) // 3))
                            if trace:
                                try:
                                    trace.model_request(
                                        role=role, model=model, provider=provider_name(model),
                                        purpose=purpose + ":repair", stage=attempt_stage,
                                        attempt=attempt + 1, system=system, user=repair_user,
                                        json_schema=model_schema,
                                        temperature=(model.get("temperature") if temperature is None else temperature),
                                        max_tokens=max_tokens, stream=stream, repair=True,
                                        phase=attempt_stage)
                                except Exception:
                                    pass
                            try:
                                repair = self._call(model, system, repair_user, model_schema,
                                                    max_tokens, temperature,
                                                    purpose + ":repair", stream)
                            except Exception:
                                if budget and repair_reservation:
                                    budget.release(repair_reservation)
                                repair_reservation = None
                                raise
                            self._note_model(model, True, repair.latency_ms)
                            if budget and repair_reservation:
                                budget.settle(repair_reservation, repair.usage)
                            repair_reservation = None
                            repaired = extract_json(repair.text)
                            repair_problems = (["模型未返回合法 JSON"] if repaired is None
                                               else validate_schema(repaired, json_schema))
                            if trace:
                                try:
                                    trace.model_response(
                                        role=role, model=model, provider=provider_name(model),
                                        purpose=purpose + ":repair", stage=attempt_stage,
                                        attempt=attempt + 1, text=repair.text, parsed=repaired,
                                        usage=repair.usage, latency_ms=repair.latency_ms,
                                        stream=repair.stream_used, repair=True,
                                        phase=attempt_stage)
                                    trace.validation(
                                        phase=attempt_stage, name="model_json_repair",
                                        ok=not repair_problems,
                                        errors=repair_problems[:20],
                                        details={"text": repair.text})
                                except Exception:
                                    pass
                            if repaired is not None and not repair_problems:
                                repair.data = repaired
                                self.breaker.ok(key)
                                note_attempt(model, "success", repair.latency_ms, repair.usage,
                                             stream_used=repair.stream_used)
                                return repair
                            note_attempt(model, "error", repair.latency_ms, repair.usage,
                                         "JSON 校验失败: " + "；".join(repair_problems[:3]))
                            errors.append(f"{model['name']}: JSON 校验失败 {repair_problems[:3]}")
                            self._note_model(model, False, error="JSON 校验失败")
                            break
                        errors.append(f"{model['name']}: JSON 校验失败 {problems[:3]}")
                        self._note_model(model, False, error="JSON 校验失败")
                        break
                    note_attempt(model, "success", result.latency_ms, result.usage,
                                 stream_used=result.stream_used)
                    return result
                except BudgetExceeded:
                    raise
                except (AIError, urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                    latency_ms = int((time.monotonic() - call_started) * 1000)
                    note_attempt(model, "error", latency_ms,
                                 error=str(exc),
                                 input_estimate=max(1, (len(system) + len(prompt_user)) // 3))
                    if trace:
                        try:
                            trace.model_error(
                                role=role, model=model, provider=provider_name(model),
                                purpose=purpose, stage=attempt_stage, attempt=attempt + 1,
                                exc=exc, latency_ms=latency_ms,
                                http_status=int(getattr(exc, "http_status", 0) or 0),
                                response_body=str(getattr(exc, "response_body", "") or ""),
                                retryable=bool(getattr(exc, "retryable", True)),
                                phase=attempt_stage)
                        except Exception:
                            pass
                    errors.append(f"{model['name']}: {exc}")
                    self._note_model(model, False, error=str(exc))
                    self.breaker.fail(key)
                    if attempt < retries_per_model:
                        time.sleep(min(8.0, backoff * (2 ** attempt)))
        hint = ""
        if self._personal_model(self._uid()):
            hint = "；个人 MaaS Key 调用失败，请到个人中心更新 Key 后重试"
        else:
            url = str(self.config.get("personal_api_key_url") or "") if self.config else ""
            hint = ("；管理员默认模型异常，可配置个人 MaaS API Key"
                    + (f"：{url}" if url else ""))
        raise AIError("所有模型均调用失败：" + "；".join(errors[-4:]) + hint)

    # ---------- 单次调用 ----------
    def _call(self, model: dict[str, Any], system: str, user: str,
              json_schema: dict[str, Any] | None, max_tokens: int | None,
              temperature: float | None, purpose: str,
              stream: bool = False) -> AIResult:
        provider = model["provider"]
        started = time.time()
        stream_used = False
        if provider == "openai":
            text, usage, stream_used = _call_openai(model, system, user, json_schema,
                                                    max_tokens, temperature, stream)
        elif provider == "anthropic":
            text, usage = _call_anthropic(model, system, user, json_schema,
                                          max_tokens, temperature)
        elif provider == "mock":
            text, usage = _call_mock(model, system, user, json_schema, purpose)
        else:
            raise AIError(f"未知 provider: {provider}")
        latency = int((time.time() - started) * 1000)
        if self.on_event:
            self.on_event("model_call", f"{model['name']} 调用完成（{purpose or '通用'}）", {
                "model": model["name"], "provider": provider, "purpose": purpose,
                "latency_ms": latency,
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            })
        return AIResult(text=text, model_name=model["name"], provider=provider,
                        model=model["model"], usage=usage, latency_ms=latency,
                        purpose=purpose, stream_used=stream_used)

    # ---------- 连通性测试 ----------
    def test_model(self, model: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._call(model, "你是连通性测试助手。", "只回复 OK 两个字母。",
                                None, 32, 0.0, "test")
            return {"ok": True, "reply": result.text.strip()[:50],
                    "latency_ms": result.latency_ms,
                    "usage": result.usage.input_tokens + result.usage.output_tokens}
        except Exception as exc:  # noqa: BLE001 - 测试接口需原样返回错误
            return {"ok": False, "error": str(exc)[:600]}


def provider_name(model: dict[str, Any]) -> str:
    return str(model.get("provider") or "")


def _headers_extra(model: dict[str, Any]) -> dict[str, str]:
    extra = model.get("extra") or {}
    headers = extra.get("headers") if isinstance(extra.get("headers"), dict) else {}
    return {str(k): str(v) for k, v in headers.items()}


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: int, *, allow_private: bool = False,
               max_bytes: int | None = None) -> tuple[dict[str, Any], int]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    raw = b""
    policy = OutboundPolicy(allow_private=allow_private,
                            max_bytes=max_bytes or 1024 * 1024)
    try:
        with open_url(req, timeout=timeout, policy=policy) as resp:
            require_json_content_type(resp)
            raw = read_limited(resp, policy.max_bytes)
            return json.loads(raw.decode("utf-8", errors="replace")), resp.status
    except urllib.error.HTTPError as exc:
        raw_text = error_body(exc, min(policy.max_bytes, 4096))
        raise AIError(f"HTTP {exc.code}: {raw_text[:500]}", http_status=exc.code,
                      response_body=raw_text, retryable=exc.code >= 500 or exc.code == 429) from exc
    except OutboundPolicyError as exc:
        raise AIError(str(exc), http_status=403, retryable=False) from exc
    except OutboundResponseTooLarge as exc:
        raise AIError(str(exc), http_status=413, retryable=False) from exc
    except OutboundContentTypeError as exc:
        raise AIError(str(exc), retryable=False) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise AIError("模型服务响应超时", retryable=True) from exc
    except urllib.error.URLError as exc:
        raise AIError(f"网络错误: {exc.reason}", retryable=True) from exc
    except (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionError) as exc:
        raise AIError(f"模型响应中断: {type(exc).__name__}", retryable=True) from exc
    except json.JSONDecodeError as exc:
        text = raw.decode("utf-8", errors="replace") if raw else ""
        raise AIError(f"响应不是合法 JSON: {exc}", response_body=text[:2000],
                      retryable=False) from exc


def _strict_output_schema(schema: dict[str, Any], name: str = "bbu_structured") -> dict[str, Any]:
    """生成 OpenAI 原生 json_schema/strict 参数；对象节点补齐 required 和禁额外字段。"""
    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            item = {k: walk(v) for k, v in value.items()}
            if item.get("type") == "object" or "properties" in item:
                properties = item.get("properties") or {}
                if isinstance(properties, dict):
                    item["additionalProperties"] = False
                    item["required"] = list(properties)
            return item
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return {"type": "json_schema",
            "json_schema": {"name": name[:48] or "bbu_structured",
                            "schema": walk(schema), "strict": True}}


def _openai_url(base: str) -> str:
    base = (base or "https://api.openai.com/v1").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _usage_from_openai(usage_raw: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(usage_raw.get("prompt_tokens") or 0),
        output_tokens=int(usage_raw.get("completion_tokens") or 0),
        total_tokens=int(usage_raw.get("total_tokens") or 0) or
        int(usage_raw.get("prompt_tokens") or 0) + int(usage_raw.get("completion_tokens") or 0),
    )


def _parse_openai_payload(data: dict[str, Any]) -> tuple[str, Usage]:
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError(f"响应结构异常: {str(data)[:300]}") from exc
    return text, _usage_from_openai(data.get("usage") or {})


def _models_url(model: dict[str, Any]) -> str:
    base = str(model.get("base_url") or "").rstrip("/")
    provider = str(model.get("provider") or "")
    if provider == "anthropic":
        base = base or "https://api.anthropic.com"
        if base.endswith("/v1/messages"):
            base = base[:-len("/v1/messages")]
        if base.endswith("/v1"):
            return base + "/models"
        return base + "/v1/models"
    base = base or "https://api.openai.com/v1"
    if base.endswith("/chat/completions"):
        base = base[:-len("/chat/completions")]
    return base + "/models"


def _get_json(url: str, headers: dict[str, str], timeout: int, *,
              allow_private: bool = False, max_bytes: int | None = None) -> Any:
    policy = OutboundPolicy(allow_private=allow_private,
                            max_bytes=max_bytes or 1024 * 1024)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    raw = b""
    try:
        with open_url(req, timeout=timeout, policy=policy) as resp:
            require_json_content_type(resp)
            raw = read_limited(resp, policy.max_bytes)
            return json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raw_text = error_body(exc, min(policy.max_bytes, 4096))
        raise AIError(f"HTTP {exc.code}: {raw_text[:500]}", http_status=exc.code,
                      response_body=raw_text, retryable=False) from exc
    except OutboundPolicyError as exc:
        raise AIError(str(exc), http_status=403, retryable=False) from exc
    except OutboundResponseTooLarge as exc:
        raise AIError(str(exc), http_status=413, retryable=False) from exc
    except OutboundContentTypeError as exc:
        raise AIError(str(exc), retryable=False) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise AIError("模型列表响应超时", retryable=False) from exc
    except urllib.error.URLError as exc:
        raise AIError(f"网络错误: {exc.reason}", retryable=False) from exc
    except (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionError) as exc:
        raise AIError(f"模型列表响应中断: {type(exc).__name__}", retryable=False) from exc
    except json.JSONDecodeError as exc:
        raw_text = raw.decode("utf-8", errors="replace") if raw else ""
        raise AIError(f"模型列表不是合法 JSON: {exc}", response_body=raw_text[:2000],
                      retryable=False) from exc


def list_models(model: dict[str, Any], timeout: int = 15) -> list[str]:
    """读取 OpenAI/Anthropic 兼容的模型列表；只返回模型名，不触发推理。"""
    provider = str(model.get("provider") or "")
    if provider == "mock":
        return ["mock"]
    if provider not in ("openai", "anthropic"):
        raise AIError(f"未知 provider: {provider}", retryable=False)
    headers = {}
    if provider == "openai":
        headers["Authorization"] = f"Bearer {model.get('api_key') or ''}"
    else:
        headers["x-api-key"] = str(model.get("api_key") or "")
        headers["anthropic-version"] = str(
            (model.get("extra") or {}).get("anthropic_version") or "2023-06-01")
    headers.update(_headers_extra(model))
    data = _get_json(_models_url(model), headers, max(5, min(int(timeout or 15), 30)),
                     allow_private=bool(model.get('allow_private', False)))
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    else:
        items = []
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in seen:
            seen.add(name)
            names.append(name)
        if len(names) >= 200:
            break
    if not names:
        raise AIError("接口未返回模型列表（请确认 Base URL 和 API Key）", retryable=False)
    return names

def _post_openai_stream(url: str, payload: dict[str, Any], headers: dict[str, str],
                        timeout: int, *, allow_private: bool = False,
                        max_bytes: int | None = None) -> tuple[str, Usage]:
    """OpenAI SSE 流式读取；长输出不再依赖单个 30 秒空闲连接。

    部分兼容网关会忽略 stream 仍返回普通 JSON，这里按 Content-Type 兼容两种情况。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for key, value in headers.items():
        req.add_header(key, value)
    raw = b""
    policy = OutboundPolicy(allow_private=allow_private,
                            max_bytes=max_bytes or 1024 * 1024)
    try:
        with open_url(req, timeout=timeout, policy=policy) as resp:
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in ctype:
                require_json_content_type(resp)
                raw = read_limited(resp, policy.max_bytes)
                return _parse_openai_payload(json.loads(raw.decode("utf-8", "replace")))
            parts: list[str] = []
            usage_raw: dict[str, Any] = {}
            for raw in iter_limited(resp, policy.max_bytes):
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    item = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if item.get("usage"):
                    usage_raw = item["usage"]
                choices = item.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                piece = delta.get("content") if isinstance(delta, dict) else None
                if (not isinstance(piece, str) or not piece) and not parts:
                    # 少数兼容网关以 SSE 返回完整 message，而不是增量 delta。
                    message = choice.get("message") or {}
                    piece = message.get("content") if isinstance(message, dict) else None
                if (not isinstance(piece, str) or not piece) and not parts:
                    piece = choice.get("text")
                if isinstance(piece, str) and piece:
                    parts.append(piece)
            text = "".join(parts)
            if not text:
                raise AIError("流式响应无文本")
            return text, _usage_from_openai(usage_raw)
    except urllib.error.HTTPError as exc:
        raw_text = error_body(exc, min(policy.max_bytes, 4096))
        raise AIError(f"HTTP {exc.code}: {raw_text[:500]}", http_status=exc.code,
                      response_body=raw_text, retryable=exc.code >= 500 or exc.code == 429) from exc
    except OutboundPolicyError as exc:
        raise AIError(str(exc), http_status=403, retryable=False) from exc
    except OutboundResponseTooLarge as exc:
        raise AIError(str(exc), http_status=413, retryable=False) from exc
    except OutboundContentTypeError as exc:
        raise AIError(str(exc), retryable=False) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise AIError("模型流式响应超时", retryable=True) from exc
    except urllib.error.URLError as exc:
        raise AIError(f"网络错误: {exc.reason}", retryable=True) from exc
    except (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionError) as exc:
        raise AIError(f"模型流式响应中断: {type(exc).__name__}", retryable=True) from exc
    except json.JSONDecodeError as exc:
        text = raw.decode("utf-8", "replace") if raw else ""
        raise AIError(f"响应不是合法 JSON: {exc}", response_body=text[:2000],
                      retryable=False) from exc


def _call_openai(model: dict[str, Any], system: str, user: str,
                 json_schema: dict[str, Any] | None, max_tokens: int | None,
                 temperature: float | None, stream: bool = False
                 ) -> tuple[str, Usage, bool]:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload: dict[str, Any] = {
        "model": model["model"],
        "messages": messages,
        "temperature": model["temperature"] if temperature is None else temperature,
        "max_tokens": max_tokens or model["max_tokens"],
        "stream": bool(stream),
    }
    if json_schema:
        if json_schema.get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["response_format"] = _strict_output_schema(json_schema)
    headers = {"Authorization": f"Bearer {model['api_key']}"}
    headers.update(_headers_extra(model))
    url = _openai_url(model["base_url"])

    if stream:
        try:
            text, usage = _post_openai_stream(url, payload, headers, model["timeout_sec"],
                                              allow_private=bool(model.get("allow_private", False)))
            if usage.total_tokens == 0:
                # 部分网关不返回 usage；给预算一个保守估算，避免完全不计成本。
                usage = Usage(
                    input_tokens=max(1, (len(system) + len(user)) // 3),
                    output_tokens=max(1, len(text) // 3),
                    total_tokens=max(2, (len(system) + len(user) + len(text)) // 3),
                )
            return text, usage, True
        except AIError as exc:
            # 只有明确不支持流式时才回退非流式；连接中断交给外层按流式重试。
            reason = str(exc).lower()
            unsupported = (
                "http 400", "http 404", "http 405", "http 422",
                "stream not supported", "stream is not supported",
                "does not support stream", "unsupported stream",
                "streaming not supported", "unknown parameter: stream",
                "不支持流式", "流式不支持",
            )
            if not any(token in reason for token in unsupported):
                raise
            payload["stream"] = False

    try:
        data, _ = _post_json(url, payload, headers, model["timeout_sec"],
                              allow_private=bool(model.get("allow_private", False)))
    except AIError as exc:
        # 部分兼容网关不支持原生 json_schema，降级为 json_object 后重试一次。
        if json_schema and "response_format" in str(exc).lower():
            payload["response_format"] = {"type": "json_object"}
            try:
                data, _ = _post_json(url, payload, headers, model["timeout_sec"],
                              allow_private=bool(model.get("allow_private", False)))
            except AIError as exc2:
                if "response_format" not in str(exc2).lower():
                    raise
                payload.pop("response_format", None)
                data, _ = _post_json(url, payload, headers, model["timeout_sec"],
                              allow_private=bool(model.get("allow_private", False)))
        else:
            raise
    text, usage = _parse_openai_payload(data)
    return text, usage, False


def _anthropic_url(base: str) -> str:
    base = (base or "https://api.anthropic.com").rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _call_anthropic(model: dict[str, Any], system: str, user: str,
                    json_schema: dict[str, Any] | None, max_tokens: int | None,
                    temperature: float | None) -> tuple[str, Usage]:
    if json_schema:
        user = user + "\n\n只输出严格符合要求的 JSON，不要任何解释或 Markdown 代码块标记。"
    payload: dict[str, Any] = {
        "model": model["model"],
        "max_tokens": max_tokens or model["max_tokens"],
        "temperature": model["temperature"] if temperature is None else temperature,
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        payload["system"] = system
    headers = {
        "x-api-key": model["api_key"],
        "anthropic-version": str((model.get("extra") or {}).get("anthropic_version") or "2023-06-01"),
    }
    headers.update(_headers_extra(model))
    data, _ = _post_json(_anthropic_url(model["base_url"]), payload, headers,
                         model["timeout_sec"],
                         allow_private=bool(model.get("allow_private", False)))
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    if not text:
        raise AIError(f"Anthropic 响应无文本: {str(data)[:300]}")
    usage_raw = data.get("usage") or {}
    usage = Usage(
        input_tokens=int(usage_raw.get("input_tokens") or 0),
        output_tokens=int(usage_raw.get("output_tokens") or 0),
        total_tokens=int(usage_raw.get("input_tokens") or 0) + int(usage_raw.get("output_tokens") or 0),
    )
    return text, usage


def _call_mock(model: dict[str, Any], system: str, user: str,
               json_schema: dict[str, Any] | None, purpose: str) -> tuple[str, Usage]:
    """确定性 Mock：只用于演示与自动化测试，不参与真实诊断。"""
    if json_schema:
        if purpose.startswith("route"):
            payload = {"candidates": [], "note": "mock"}
        elif purpose.startswith("adjudicate"):
            payload = {"decision": "insufficient", "reason": "mock"}
        elif purpose.startswith("report"):
            payload = {"narrative": "（Mock 模型未参与实际研判，结论以证据链为准。）"}
        elif purpose.startswith("workshop"):
            payload = {"yaml": "", "notes": ["mock 模型不生成 profile"]}
        else:
            payload = {}
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = "OK"
    usage = Usage(input_tokens=max(1, len(user) // 3), output_tokens=max(1, len(text) // 3),
                  total_tokens=max(2, (len(user) + len(text)) // 3))
    return text, usage
