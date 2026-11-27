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
import random
import re
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .db import Database
from .model_capabilities import ModelCapabilityStore, endpoint_key
from .relay import (
    RelayAbsentError,
    RelayExecError,
    RelayError,
    RelaySpec,
    build_auth,
)
from .netguard import (
    OutboundContentTypeError,
    OutboundPolicy,
    OutboundPolicyError,
    OutboundResponseTooLarge,
    endpoint_target,
    error_body,
    iter_limited,
    open_url,
    read_limited,
    require_json_content_type,
)
from .schema_validate import validate_schema
from .util import utc_now


class AIError(RuntimeError):
    """模型错误保留结构化 HTTP 信息，轨迹页需要原始响应体定位网关问题。"""

    def __init__(self, message: str, *, http_status: int = 0,
                 response_body: str = "", retryable: bool = True,
                 circuit_break: bool = True, retry_after: float = 0.0,
                 kind: str = "") -> None:
        super().__init__(message)
        self.http_status = int(http_status or 0)
        self.response_body = str(response_body or "")
        self.retryable = bool(retryable)
        self.circuit_break = bool(circuit_break)
        self.retry_after = max(0.0, float(retry_after or 0))
        self.kind = str(kind or "")


def parse_retry_after(value: Any) -> float:
    """Parse an HTTP ``Retry-After`` value (seconds or HTTP-date)."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


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
    context_trimmed_chars: int = 0
    context_window: int = 0


_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_CONTEXT_TRIM_MARKER = "\n\n…[已按模型上下文窗口裁剪]…\n\n"


def estimate_tokens(text: str) -> int:
    """Conservative token estimate without third-party tokenizers.

    CJK characters are counted as one token each (the common tokenizer
    behaviour); the remaining ASCII/Unicode text is counted at four characters
    per token.  This deliberately over-estimates Chinese prompts compared with
    the old ``len(text)//3`` heuristic, so budget and context checks no longer
    silently undercount.
    """
    text = str(text or "")
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    return max(1, cjk + (rest + 3) // 4)


def _take_prefix_tokens(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def _take_suffix_tokens(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[len(text) - mid:]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[len(text) - low:] if low else ""


def trim_text_to_tokens(text: str, budget: int) -> str:
    """Trim the middle of a prompt, preserving its head and schema-bearing tail."""
    text = str(text or "")
    budget = max(0, int(budget or 0))
    if estimate_tokens(text) <= budget:
        return text
    marker_tokens = estimate_tokens(_CONTEXT_TRIM_MARKER)
    if budget <= marker_tokens + 8:
        return _take_prefix_tokens(text, budget)
    remaining = budget - marker_tokens
    head_budget = max(1, int(remaining * 0.55))
    tail_budget = max(1, remaining - head_budget)
    return (_take_prefix_tokens(text, head_budget) + _CONTEXT_TRIM_MARKER
            + _take_suffix_tokens(text, tail_budget))


def fit_prompt_to_context(system: str, user: str, *,
                          context_window: int | None,
                          max_output: int | None,
                          requested_max_tokens: int | None = None,
                          max_output_declared: bool = True,
                          margin_tokens: int = 256) -> dict[str, Any]:
    """Fit a prompt + output request into the model's declared context.

    Returns a dict with the possibly-trimmed prompt, the effective output cap
    and the number of prompt characters removed.  The caller still owns budget
    reservation and must use ``max_tokens`` from this result for the request.
    """
    context = int(context_window or 32768)
    if context <= 0:
        context = 32768
    margin = max(0, min(int(margin_tokens or 0), max(0, context - 2)))
    output_limit = max(1, int(max_output or 4096))
    # DB ``max_tokens`` is a default, while workshop/assistant callers may
    # deliberately request a larger output.  Only an explicitly declared
    # ``extra.capabilities.max_output`` is a hard cap.
    if requested_max_tokens is None:
        requested = output_limit
        cap_by_declared = True
    else:
        requested = max(1, int(requested_max_tokens))
        cap_by_declared = bool(max_output_declared)
    output_cap = output_limit if cap_by_declared else requested
    # Never ask for more output than the context can hold while leaving at
    # least one token for the prompt.  Do not floor the window: a declared
    # 512-token model must remain a 512-token budget.
    output = min(requested, output_cap, max(1, context - margin - 1))
    input_budget = max(1, context - margin - output)
    original_system, original_user = str(system or ""), str(user or "")
    current_system = original_system
    system_budget = min(input_budget, max(32, input_budget // 3))
    system_budget = min(system_budget, input_budget - 1) if input_budget > 1 else 0
    if estimate_tokens(current_system) > system_budget:
        current_system = trim_text_to_tokens(current_system, system_budget)
    user_budget = max(1, input_budget - estimate_tokens(current_system))
    current_user = trim_text_to_tokens(original_user, user_budget)
    return {
        "system": current_system,
        "user": current_user,
        "max_tokens": output,
        "context_window": context,
        "input_tokens": estimate_tokens(current_system) + estimate_tokens(current_user),
        "trimmed_chars": (len(original_system) - len(current_system)
                          + len(original_user) - len(current_user)),
    }


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
        self.over_budget = bool(
            max_tokens > 0 and self.usage.total_tokens > max_tokens)

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
            if self.max_tokens > 0 and self.usage.total_tokens > self.max_tokens:
                self.over_budget = True
            if self.on_event:
                self.on_event("model_usage", "模型调用完成", {
                    "calls": self.calls,
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                    "total_tokens": self.usage.total_tokens,
                    "over_budget": self.over_budget,
                })

    def settle_failed(self, reservation: dict[str, int]) -> None:
        """Count a failed provider attempt against the call budget.

        Failed attempts were previously released without incrementing ``calls``,
        which let an unreachable/401 endpoint run forever despite
        ``max_llm_calls``.  They consume a call but no tokens because no usage
        was reported.
        """
        with self._lock:
            est = int((reservation or {}).get("estimated_tokens") or 0)
            self._pending_calls = max(0, self._pending_calls - 1)
            self._pending_tokens = max(0, self._pending_tokens - est)
            self.calls += 1
            if self.on_event:
                self.on_event("model_usage", "模型调用失败", {
                    "calls": self.calls,
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                    "total_tokens": self.usage.total_tokens,
                    "status": "error",
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
            if self.max_tokens > 0 and self.usage.total_tokens > self.max_tokens:
                self.over_budget = True
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
            "over_budget": self.over_budget,
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


class EndpointGate:
    """Per-endpoint concurrency + bounded queue.

    The semaphore protects the upstream provider from a thundering herd.  The
    queue counter bounds the number of waiters so a slow endpoint cannot grow an
    unbounded thread pile; refusal is non-retryable so ``complete`` immediately
    falls through to the next model instead of sleeping on the same endpoint.
    """

    def __init__(self, concurrency: int = 4, queue_limit: int = 32,
                 queue_timeout_sec: float = 30.0) -> None:
        self.concurrency = max(1, int(concurrency or 1))
        self.queue_limit = max(1, int(queue_limit or 1))
        self.queue_timeout_sec = max(0.1, float(queue_timeout_sec or 30.0))
        self._sem = threading.BoundedSemaphore(self.concurrency)
        self._lock = threading.Lock()
        self._waiting = 0

    @property
    def waiting(self) -> int:
        with self._lock:
            return self._waiting

    def acquire(self) -> None:
        with self._lock:
            if self._waiting >= self.queue_limit:
                raise AIError(
                    f"模型端点排队已满（{self.queue_limit}），本次请求转下一个模型",
                    retryable=False, circuit_break=False)
            self._waiting += 1
        try:
            acquired = self._sem.acquire(timeout=self.queue_timeout_sec)
        finally:
            with self._lock:
                self._waiting = max(0, self._waiting - 1)
        if not acquired:
            raise AIError(
                f"模型端点繁忙，排队超过 {self.queue_timeout_sec:g} 秒，本次请求转下一个模型",
                retryable=False, circuit_break=False)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass


def endpoint_limit_key(model: dict[str, Any]) -> str:
    """Limit by provider + base URL, shared across model aliases on one host."""
    return (f"{str(model.get('provider') or '')}|"
            f"{str(model.get('base_url') or '').rstrip('/')}")


class EndpointLimiter:
    def __init__(self, concurrency: int = 4, queue_limit: int = 32,
                 queue_timeout_sec: float = 30.0) -> None:
        self.concurrency = max(1, int(concurrency or 1))
        self.queue_limit = max(1, int(queue_limit or 1))
        self.queue_timeout_sec = max(0.1, float(queue_timeout_sec or 30.0))
        self._lock = threading.Lock()
        self._gates: dict[str, EndpointGate] = {}

    def gate(self, key: str) -> EndpointGate:
        with self._lock:
            gate = self._gates.get(key)
            if gate is None:
                gate = EndpointGate(self.concurrency, self.queue_limit,
                                    self.queue_timeout_sec)
                self._gates[key] = gate
            return gate

    @contextmanager
    def slot(self, key: str):
        gate = self.gate(key)
        gate.acquire()
        try:
            yield
        finally:
            gate.release()


class AIGateway:
    def __init__(self, db: Database, config: Any,
                 on_event: Callable[[str, str, dict], None] | None = None,
                 secret_box: Any | None = None,
                 relay: Any | None = None) -> None:
        self.db = db
        self.config = config
        self.on_event = on_event
        self.relay = relay
        self.on_usage: Callable[[dict[str, Any]], None] | None = None
        self.breaker = CircuitBreaker()
        self.schema_breaker = CircuitBreaker(
            threshold=max(1, int(config.get("llm_schema_fail_threshold", 2)
                                if config is not None else 2)),
            cooldown=max(10, int(config.get("llm_schema_cooldown_sec", 300)
                               if config is not None else 300)))
        self.endpoint_limiter = EndpointLimiter(
            concurrency=max(1, int(config.get("llm_endpoint_concurrency", 4)
                                  if config is not None else 4)),
            queue_limit=max(1, int(config.get("llm_endpoint_queue_limit", 32)
                                  if config is not None else 32)),
            queue_timeout_sec=max(0.1, float(
                config.get("llm_endpoint_queue_timeout_sec", 30)
                if config is not None else 30)))
        self._cache: dict[int, dict[str, Any]] = {}
        self._rr_lock = threading.Lock()
        self._rr_index: dict[str, int] = {}
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
        host, path = endpoint_target(str(model.get("base_url") or ""))
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
            "endpoint_host": host,
            "endpoint_path": path[:300],
            "transport": str(model.get("_transport_used") or model.get("transport") or "server"),
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
            row["transport"] = str((row.get("extra") or {}).get("transport") or "server")
            if row["transport"] not in ("server", "browser"):
                row["transport"] = "server"
            if self.secret_box and row.get("api_key"):
                try:
                    row["api_key"] = self.secret_box.decrypt(row["api_key"])
                except ValueError as exc:
                    row["api_key"] = ""
                    row["key_error"] = str(exc)
            row["source"] = "admin"
            row["personal"] = False
            row["allow_private"] = (
                bool(self.config.get("model_discovery_allow_private", True))
                if self.config is not None else True)
            row["private_allowlist"] = (
                self.config.get("model_private_allowlist", "")
                if self.config is not None else "")
            row["_relay_max_bytes"] = (
                int(self.config.get("relay_max_response_bytes", 1048576))
                if self.config is not None else 1048576)
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
            "transport": "server",
            "source": "personal",
            "personal": True,
            "allow_private": (
                bool(self.config.get("model_discovery_allow_private", True))
                if self.config is not None else True),
            "private_allowlist": (
                self.config.get("model_private_allowlist", "")
                if self.config is not None else ""),
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
        rows = ([personal] if personal else []) + self._global_models()
        uid = self._uid(user_id)
        return [m for m in rows if self._usable_here(m, uid)]

    def _usable_here(self, model: dict[str, Any], user_id: int | None = None) -> bool:
        """浏览器模型只在有在线窗口且角色被允许时参与调度。

        离线窗口不是端点故障，所以这里直接剔除，不写失败计数，也不让
        任务把 10 秒浪费在必然失败的等待上。
        """
        if str(model.get("transport") or "server") != "browser":
            return True
        if self.relay is None:
            return False
        enabled = getattr(self.relay, "enabled", False)
        if not enabled:
            return False
        try:
            if not self.relay.online(self._uid(user_id)):
                return False
            return bool(self.relay.role_allowed(model, self._uid(user_id)))
        except Exception:
            return False

    @staticmethod
    def _model_breaker_key(model: dict[str, Any]) -> str:
        return f"{model['id']}:{model['name']}"

    def _model_skipped(self, model: dict[str, Any]) -> bool:
        key = self._model_breaker_key(model)
        return self.breaker.is_open(key) or self.schema_breaker.is_open(key)

    def _round_robin_priority(self, rows: list[dict[str, Any]],
                              group: str) -> list[dict[str, Any]]:
        """Round-robin inside each equal-priority group.

        Priority remains the primary ordering signal; only ties rotate, so
        several identical-priority deployments share load instead of always
        hitting the lowest id.
        """
        if len(rows) <= 1:
            return list(rows)
        grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for model in rows:
            try:
                priority = int(model.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            # 同 priority 内服务端模型先于浏览器模型，避免用户无感地把
            # 任务压到自己的标签页上。浏览器模型之间仍按同档轮转。
            transport_rank = 1 if str(model.get("transport") or "server") == "browser" else 0
            grouped.setdefault((priority, transport_rank), []).append(model)
        ordered: list[dict[str, Any]] = []
        for priority_key in sorted(grouped):
            items = grouped[priority_key]
            if len(items) == 1:
                ordered.extend(items)
                continue
            with self._rr_lock:
                start = self._rr_index.get(group, 0) % len(items)
                self._rr_index[group] = start + 1
            ordered.extend(items[start:] + items[:start])
        return ordered

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
        personal = [m for m in exact if m.get("personal")]
        exact_global = [m for m in exact if not m.get("personal")]
        rest_unique = [m for m in rest if m not in exact]
        rest_personal = [m for m in rest_unique if m.get("personal")]
        rest_global = [m for m in rest_unique if not m.get("personal")]
        # 诊断/助手等通用链路保留“个人模型优先”语义：个人 Key 有独立额度，
        # 不应被管理员默认模型抢占。技能工坊是例外：它的自动选择必须服从
        # 管理员在“模型配置”里设置的 priority，否则个人模型会永远压住
        # 刚调高优先级的全局模型，重跑工坊也不会切换到目标模型。个人模型
        # 在工坊里仍保留为最后一个可回退候选，而不是被删除。
        if role == "workshop":
            ordered = (self._round_robin_priority(exact_global, f"{role}:exact")
                       + self._round_robin_priority(rest_global, f"{role}:fallback")
                       + personal
                       + rest_personal)
        else:
            ordered = (personal
                       + self._round_robin_priority(exact_global, f"{role}:exact")
                       + rest_personal
                       + self._round_robin_priority(rest_global, f"{role}:fallback"))
        return [m for m in ordered if not self._model_skipped(m)]

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
                           and not self._model_skipped(personal))
        # ``default`` status must mirror ``choose("default")``: only models
        # with the default role (or an empty role list) are default-capable.
        # Keep key-error rows in ``global_rows`` so the notice can distinguish
        # "未配置" from "已配置但 Key 解密失败".
        global_rows = [m for m in globals_
                       if not m.get("roles") or "default" in m.get("roles")]
        global_ok = [m for m in global_rows
                     if not m.get("key_error") and not self._model_skipped(m)
                     and self._usable_here(m, uid)]
        browser_rows = [m for m in global_rows if m.get("transport") == "browser"]
        browser_online = bool(self.relay and self.relay.online(uid)) if self.relay else False
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
                            "provider": m.get("provider"),
                            "transport": m.get("transport") or "server"}
                           for m in global_rows[:5]],
            },
            "browser": {
                "configured": bool(browser_rows),
                "online": browser_online,
                "count": len(browser_rows),
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
            "schema_circuit_open": (self.schema_breaker.open_keys()
                                    if hasattr(self.schema_breaker, "open_keys") else []),
        }

    def _probe_capabilities(self, model: dict[str, Any]) -> dict[str, Any]:
        """低成本探测 JSON Mode/usage；不发业务数据，不写入任务预算。"""
        provider = str(model.get("provider") or "")
        extra = model.get("extra") if isinstance(model.get("extra"), dict) else {}
        base = {"json_mode": provider == "anthropic", "strict_schema": False,
                "usage": True,
                "context_window": int(extra.get("context_window") or 32768),
                "max_output": int(extra.get("max_output") or model.get("max_tokens") or 4096),
                "max_output_declared": bool(extra.get("max_output")),
                "degraded": False, "probe_error": ""}
        if str(extra.get("transport") or model.get("transport") or "server") == "browser":
            # 浏览器代理模型不能由服务端直连探测；能力按网关默认值处理，
            # 真正的 400/response_format 降级仍在浏览器回传后按原逻辑触发。
            base.update({"json_mode": True, "degraded": False, "probe_error": ""})
            return base
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
            with self.endpoint_limiter.slot(endpoint_limit_key(model)):
                data, _ = _post_json(
                    _openai_url(model["base_url"]), payload, headers,
                    min(30, int(model.get("timeout_sec") or 30)),
                    allow_private=bool(model.get("allow_private", False)),
                    private_allowlist=model.get("private_allowlist") or ())
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
        jitter = max(0.0, float(
            self.config.get("llm_retry_jitter_sec", 0.5) if self.config else 0.0))
        rate_limit_backoff = max(0.0, float(
            self.config.get("llm_rate_limit_backoff_sec", 30) if self.config else 30))
        margin_tokens = max(0, int(
            self.config.get("llm_context_margin_tokens", 256) if self.config else 256))
        attempt_stage = stage or purpose or "model"

        def note_attempt(model: dict[str, Any], status: str, latency_ms: int = 0,
                         usage: Usage | None = None, error: str = "",
                         input_estimate: int = 0,
                         stream_used: bool | None = None,
                         context_trimmed_chars: int = 0,
                         context_window: int = 0,
                         request_max_tokens: int = 0) -> None:
            item: dict[str, Any] = {
                "stage": attempt_stage,
                "model": str(model.get("name") or model.get("model") or model.get("id") or ""),
                "latency_ms": max(0, int(latency_ms)),
                "input_tokens": int(usage.input_tokens) if usage else 0,
                "output_tokens": int(usage.output_tokens) if usage else 0,
                "status": status,
                "stream": bool(stream if stream_used is None else stream_used),
                "context_trimmed_chars": max(0, int(context_trimmed_chars or 0)),
                "context_window": max(0, int(context_window or 0)),
                "request_max_tokens": max(0, int(request_max_tokens or 0)),
            }
            host, path = endpoint_target(str(model.get("base_url") or ""))
            item["endpoint_host"] = host
            item["endpoint_path"] = path[:300]
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
        rate_limited = False
        for model_index, model in enumerate(models):
            key = self._model_breaker_key(model)
            has_alternative = model_index < len(models) - 1
            for attempt in range(retries_per_model + 1):
                reservation: dict[str, int] | None = None
                repair_in_flight = False
                prompt_user = user
                system_prompt = system
                call_started = time.monotonic()
                try:
                    schema_mode = str(self.config.get("model_schema_mode", "auto")
                                      if self.config else "auto").lower()
                    capabilities = self.capabilities.get(
                        model, probe=(schema_mode in ("auto", "probe")))
                    model_schema = json_schema
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
                    # 上下文窗口裁剪必须发生在预算预留和请求构造之前，否则长
                    # prompt 会先 400，预算也已按虚高长度占用。
                    fitted = fit_prompt_to_context(
                        system_prompt, prompt_user,
                        context_window=capabilities.get("context_window"),
                        max_output=capabilities.get("max_output"),
                        requested_max_tokens=max_tokens,
                        max_output_declared=bool(
                            capabilities.get("max_output_declared", True)),
                        margin_tokens=margin_tokens)
                    system_prompt = fitted["system"]
                    prompt_user = fitted["user"]
                    effective_max_tokens = int(fitted["max_tokens"])
                    trimmed_chars = int(fitted["trimmed_chars"])
                    input_est = (estimate_tokens(system_prompt)
                                 + estimate_tokens(prompt_user))
                    if budget:
                        # 预留输入 + 最大输出，结算时按实际 usage 校正；这样
                        # max_total_tokens 在请求前就是硬上限，而不是事后才超。
                        reservation = budget.reserve(input_est + effective_max_tokens)
                    if trace:
                        try:
                            trace.model_request(
                                role=role, model=model, provider=provider_name(model),
                                purpose=purpose, stage=attempt_stage, attempt=attempt + 1,
                                system=system_prompt, user=prompt_user,
                                json_schema=model_schema,
                                temperature=(model.get("temperature") if temperature is None else temperature),
                                max_tokens=effective_max_tokens, stream=stream,
                                phase=attempt_stage)
                        except Exception:
                            pass
                    try:
                        with self.endpoint_limiter.slot(endpoint_limit_key(model)):
                            result = self._call(model, system_prompt, prompt_user,
                                                model_schema, effective_max_tokens,
                                                temperature, purpose, stream)
                    except Exception:
                        if budget and reservation:
                            budget.settle_failed(reservation)
                        reservation = None
                        raise
                    if budget and reservation:
                        budget.settle(reservation, result.usage)
                    reservation = None
                    result.context_trimmed_chars = trimmed_chars
                    result.context_window = int(fitted["context_window"])
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
                            self._note_model(model, True, result.latency_ms)
                            self.breaker.ok(key)
                            self.schema_breaker.ok(key)
                            result.data = data
                            note_attempt(model, "success", result.latency_ms, result.usage,
                                         stream_used=result.stream_used,
                                         context_trimmed_chars=trimmed_chars,
                                         context_window=result.context_window,
                                         request_max_tokens=effective_max_tokens)
                            return result
                        note_attempt(model, "error", result.latency_ms, result.usage,
                                     "JSON 校验失败: " + "；".join(problems[:3]),
                                     context_trimmed_chars=trimmed_chars,
                                     context_window=result.context_window,
                                     request_max_tokens=effective_max_tokens)
                        if trace:
                            try:
                                trace.validation(phase=attempt_stage, name="model_json_schema",
                                                 ok=False, errors=problems[:20],
                                                 details={"text": result.text})
                            except Exception:
                                pass
                        # 只修复一次；修复失败则熔断该端点并换下一个模型。
                        if attempt == 0:
                            repair_user = (
                                f"{prompt_user}\n\n[上一次输出不合法，问题如下]\n"
                                + "\n".join(f"- {e}" for e in problems[:12])
                                + "\n请只输出严格符合 JSON Schema 的 JSON，不要解释。"
                            )
                            repair_fit = fit_prompt_to_context(
                                system_prompt, repair_user,
                                context_window=result.context_window,
                                max_output=capabilities.get("max_output"),
                                requested_max_tokens=effective_max_tokens,
                                max_output_declared=bool(
                                    capabilities.get("max_output_declared", True)),
                                margin_tokens=margin_tokens)
                            repair_system = repair_fit["system"]
                            repair_text = repair_fit["user"]
                            repair_reservation: dict[str, int] | None = None
                            if budget:
                                repair_reservation = budget.reserve(
                                    estimate_tokens(repair_system)
                                    + estimate_tokens(repair_text)
                                    + int(repair_fit["max_tokens"]))
                            if trace:
                                try:
                                    trace.model_request(
                                        role=role, model=model, provider=provider_name(model),
                                        purpose=purpose + ":repair", stage=attempt_stage,
                                        attempt=attempt + 1, system=repair_system,
                                        user=repair_text, json_schema=model_schema,
                                        temperature=(model.get("temperature") if temperature is None else temperature),
                                        max_tokens=int(repair_fit["max_tokens"]),
                                        stream=stream, repair=True, phase=attempt_stage)
                                except Exception:
                                    pass
                            repair_in_flight = True
                            try:
                                with self.endpoint_limiter.slot(endpoint_limit_key(model)):
                                    repair_result = self._call(
                                        model, repair_system, repair_text, model_schema,
                                        int(repair_fit["max_tokens"]),
                                        temperature, purpose + ":repair", stream)
                            except Exception:
                                if budget and repair_reservation:
                                    budget.settle_failed(repair_reservation)
                                repair_reservation = None
                                raise
                            if budget and repair_reservation:
                                budget.settle(repair_reservation, repair_result.usage)
                            repair_reservation = None
                            repaired = extract_json(repair_result.text)
                            repair_problems = (["模型未返回合法 JSON"] if repaired is None
                                               else validate_schema(repaired, json_schema))
                            if trace:
                                try:
                                    trace.model_response(
                                        role=role, model=model, provider=provider_name(model),
                                        purpose=purpose + ":repair", stage=attempt_stage,
                                        attempt=attempt + 1, text=repair_result.text,
                                        parsed=repaired, usage=repair_result.usage,
                                        latency_ms=repair_result.latency_ms,
                                        stream=repair_result.stream_used, repair=True,
                                        phase=attempt_stage)
                                    trace.validation(
                                        phase=attempt_stage, name="model_json_repair",
                                        ok=not repair_problems,
                                        errors=repair_problems[:20],
                                        details={"text": repair_result.text})
                                except Exception:
                                    pass
                            if repaired is not None and not repair_problems:
                                repair_result.data = repaired
                                repair_result.context_trimmed_chars = (
                                    trimmed_chars + int(repair_fit["trimmed_chars"]))
                                repair_result.context_window = int(repair_fit["context_window"])
                                self._note_model(model, True, repair_result.latency_ms)
                                self.breaker.ok(key)
                                self.schema_breaker.ok(key)
                                note_attempt(model, "success", repair_result.latency_ms,
                                             repair_result.usage,
                                             stream_used=repair_result.stream_used,
                                             context_trimmed_chars=repair_result.context_trimmed_chars,
                                             context_window=repair_result.context_window,
                                             request_max_tokens=int(repair_fit["max_tokens"]))
                                return repair_result
                            reason = "JSON 校验失败: " + "；".join(repair_problems[:3])
                            note_attempt(model, "error", repair_result.latency_ms,
                                         repair_result.usage, reason,
                                         context_trimmed_chars=int(repair_fit["trimmed_chars"]),
                                         context_window=int(repair_fit["context_window"]),
                                         request_max_tokens=int(repair_fit["max_tokens"]))
                            errors.append(f"{model['name']}: {reason}")
                            self._note_model(model, False, error=reason)
                            self.schema_breaker.fail(key)
                            try:
                                self.capabilities.mark_degraded(model, reason, caps=capabilities)
                            except Exception:
                                pass
                            break
                        reason = "JSON 校验失败: " + "；".join(problems[:3])
                        errors.append(f"{model['name']}: {reason}")
                        self._note_model(model, False, error=reason)
                        self.schema_breaker.fail(key)
                        try:
                            self.capabilities.mark_degraded(model, reason, caps=capabilities)
                        except Exception:
                            pass
                        break
                    self._note_model(model, True, result.latency_ms)
                    self.breaker.ok(key)
                    note_attempt(model, "success", result.latency_ms, result.usage,
                                 stream_used=result.stream_used,
                                 context_trimmed_chars=trimmed_chars,
                                 context_window=result.context_window,
                                 request_max_tokens=effective_max_tokens)
                    return result
                except BudgetExceeded:
                    raise
                except (AIError, urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                    latency_ms = int((time.monotonic() - call_started) * 1000)
                    note_attempt(model, "error", latency_ms,
                                 error=str(exc),
                                 input_estimate=max(1, estimate_tokens(system_prompt)
                                                    + estimate_tokens(prompt_user)),
                                 context_trimmed_chars=locals().get("trimmed_chars", 0),
                                 context_window=locals().get("fitted", {}).get("context_window", 0),
                                 request_max_tokens=locals().get("effective_max_tokens", 0))
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
                    status = int(getattr(exc, "http_status", 0) or 0)
                    if status == 429:
                        rate_limited = True
                    if getattr(exc, "circuit_break", True):
                        # 队列/本地并发拒绝不是端点故障，不能计入模型健康失败。
                        self._note_model(model, False, error=str(exc))
                        self.breaker.fail(key)
                    # 只有可重试错误才在同一模型上退避重试；401/400/JSON
                    # Content-Type 等确定性错误立即换下一个模型。429 表示
                    # 当前 Key/端点配额已满，若还有备用模型就立即切换，避免
                    # 三次快速重试继续撞同一个限流窗口。
                    retryable = bool(getattr(exc, "retryable", True)) and not repair_in_flight
                    if retryable and attempt < retries_per_model:
                        if status == 429 and has_alternative:
                            break
                        delay = min(8.0, backoff * (2 ** attempt))
                        if status == 429:
                            retry_after = max(0.0, float(getattr(exc, "retry_after", 0) or 0))
                            delay = max(delay, rate_limit_backoff, retry_after)
                            delay = min(300.0, delay)
                        if jitter:
                            delay += random.uniform(0, jitter)
                        if delay > 0:
                            time.sleep(delay)
                        continue
                    break
        hint = ""
        if rate_limited:
            hint = "；模型服务限流（HTTP 429），已尝试可用模型；请稍后重试，或切换模型/Key"
        elif self._personal_model(self._uid()):
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
            text, usage, stream_used = _call_openai(
                model, system, user, json_schema, max_tokens, temperature, stream,
                relay=self.relay, user_id=self._uid(), purpose=purpose)
        elif provider == "anthropic":
            text, usage = _call_anthropic(
                model, system, user, json_schema, max_tokens, temperature,
                relay=self.relay, user_id=self._uid(), purpose=purpose)
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
        if str(model.get("transport") or (model.get("extra") or {}).get("transport")) == "browser":
            return {"ok": False, "kind": "relay_test_required",
                    "error": "浏览器代理模型请使用模型列表中的“浏览器内自检”"}
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


def model_transport(model: dict[str, Any]) -> str:
    value = model.get("transport")
    if value is None:
        value = (model.get("extra") or {}).get("transport")
    return str(value or "server")


def browser_relay_config(model: dict[str, Any]) -> dict[str, Any]:
    extra = model.get("extra") or {}
    cfg = extra.get("browser_relay") if isinstance(extra.get("browser_relay"), dict) else {}
    return dict(cfg or {})


def browser_relay_server_fallback(model: dict[str, Any]) -> bool:
    """浏览器 fetch 被 CORS/代理阻断时，是否允许用同一配置由 Python 直连。

    默认开启：浏览器代理是“优先走浏览器”，不是排他的执行位置。现场服务端
    无法直连、只有浏览器能通时，可在模型配置里关闭；成功路径不会先走服务端，
    因此默认开启不改变正常的浏览器代理时延。
    """
    value = browser_relay_config(model).get("server_fallback", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def browser_relay_timeouts(model: dict[str, Any]) -> tuple[int, int]:
    """返回 (认领超时, 执行超时)，按计划约束夹到 5–60 / 30–120。"""
    cfg = browser_relay_config(model)
    try:
        claim = int(cfg.get("claim_timeout_sec") or 10)
    except (TypeError, ValueError):
        claim = 10
    try:
        exec_ = int(cfg.get("exec_timeout_sec") or 90)
    except (TypeError, ValueError):
        exec_ = 90
    return max(5, min(60, claim)), max(30, min(120, exec_))


def build_browser_relay_spec(model: dict[str, Any], url: str, payload: dict[str, Any],
                             headers: dict[str, str], *,
                             max_bytes: int = 1048576,
                             timeout_sec: int | None = None) -> RelaySpec:
    """构造浏览器只执行、不拼装的请求；Key 永远只放在 auth.value。"""
    clean_headers, auth = build_auth(headers)
    _, exec_timeout = browser_relay_timeouts(model)
    return RelaySpec(
        url=str(url), method="POST", headers=clean_headers,
        body=json.dumps(payload, ensure_ascii=False),
        auth=auth, timeout_sec=int(timeout_sec or exec_timeout),
        max_bytes=max(1024, int(max_bytes or 1048576)))


def _post_relay_json(relay: Any, user_id: int, model: dict[str, Any], url: str,
                     payload: dict[str, Any], headers: dict[str, str],
                     purpose: str) -> tuple[dict[str, Any], int]:
    """把服务端已经构造好的 JSON 请求交给浏览器执行并解析原始响应。

    浏览器 fetch 受同源策略约束：目标网关没有放行平台 Origin 时，请求会在
    浏览器内直接失败。此时若现场的服务端 Python 可以直连同一地址，就对同一
    请求做一次服务端回退；这是浏览器代理的降级路径，不改变正常成功路径。
    """
    if relay is None:
        raise AIError("浏览器代理未初始化", kind="relay_absent", retryable=False,
                      circuit_break=False)
    model["_transport_used"] = "browser"
    spec = build_browser_relay_spec(
        model, url, payload, headers,
        max_bytes=int(model.get("_relay_max_bytes") or 1048576))
    claim_timeout, exec_timeout = browser_relay_timeouts(model)
    try:
        result = relay.submit(user_id, model, spec, purpose=purpose,
                              claim_timeout=claim_timeout, exec_timeout=exec_timeout)
    except RelayExecError as exc:
        # network 是 CORS/混合内容/代理/DNS 这类“请求未发出或未完成”的失败；
        # timeout/abort 可能已在网关侧产生推理，盲目回退会重复计费，只按原
        # 重试规则处理。
        failure = str(exc).lower()
        if browser_relay_server_fallback(model) and "timeout" not in failure and "abort" not in failure:
            model["_transport_used"] = "server"
            try:
                return _post_json(
                    url, payload, headers, int(model.get("timeout_sec") or 180),
                    allow_private=bool(model.get("allow_private", False)),
                    private_allowlist=model.get("private_allowlist") or (),
                    max_bytes=int(model.get("_relay_max_bytes") or 1048576))
            except AIError as server_exc:
                raise AIError(
                    f"浏览器代理失败（{exc}）；服务端直连回退也失败：{server_exc}",
                    http_status=int(getattr(server_exc, "http_status", 0) or 0),
                    response_body=str(getattr(server_exc, "response_body", "") or ""),
                    retryable=bool(getattr(server_exc, "retryable", True)),
                    circuit_break=bool(getattr(server_exc, "circuit_break", True)),
                    kind="relay_server_fallback_failed") from server_exc
        raise AIError(str(exc), kind="relay_exec_failed",
                      http_status=int(getattr(exc, "http_status", 0) or 0),
                      retryable=False, circuit_break=False) from exc
    except RelayAbsentError as exc:
        raise AIError(str(exc), kind="relay_absent", retryable=False,
                      circuit_break=False) from exc
    except RelayError as exc:
        raise AIError(str(exc), kind=getattr(exc, "kind", "relay_absent"),
                      http_status=int(getattr(exc, "http_status", 0) or 0),
                      retryable=False, circuit_break=False) from exc
    status = int(result.status or 0)
    raw_text = str(result.text or "")
    if status >= 400 or not (200 <= status < 300):
        raise AIError(f"HTTP {status}: {raw_text[:500]}", http_status=status,
                      response_body=raw_text[:4000],
                      retryable=status >= 500 or status == 429)
    try:
        return json.loads(raw_text), status
    except json.JSONDecodeError as exc:
        raise AIError(f"响应不是合法 JSON: {exc}", response_body=raw_text[:2000],
                      retryable=False) from exc


def _openai_models_url(base: str) -> str:
    base = (base or "https://api.openai.com/v1").rstrip("/")
    if base.endswith("/chat/completions"):
        return base[:-len("/chat/completions")] + "/models"
    if base.endswith("/models"):
        return base
    return base + "/models"


def browser_models_spec(model: dict[str, Any]) -> RelaySpec:
    """浏览器执行“获取模型”的 GET 请求，服务端只解析结果。"""
    provider = str(model.get("provider") or "openai")
    if provider == "anthropic":
        base = str(model.get("base_url") or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/messages"):
            base = base[:-len("/messages")]
        url = base + ("/models" if base.endswith("/v1") else "/v1/models")
        headers = {"x-api-key": str(model.get("api_key") or ""),
                   "anthropic-version": str((model.get("extra") or {}).get("anthropic_version")
                                            or "2023-06-01")}
    else:
        url = _openai_models_url(str(model.get("base_url") or ""))
        headers = {"Authorization": f"Bearer {model.get('api_key') or ''}"}
    headers.update(_headers_extra(model))
    clean, auth = build_auth(headers)
    return RelaySpec(url=url, method="GET", headers=clean, body="", auth=auth,
                     timeout_sec=browser_relay_timeouts(model)[1],
                     max_bytes=int(model.get("_relay_max_bytes") or 1048576))


def browser_test_spec(model: dict[str, Any]) -> RelaySpec:
    """管理员“浏览器内自检”的最小推理请求。"""
    provider = str(model.get("provider") or "openai")
    if provider == "anthropic":
        url = _anthropic_url(model["base_url"])
        payload = {"model": model["model"], "max_tokens": 32, "temperature": 0,
                   "messages": [{"role": "user", "content": "只回复 OK 两个字母。"}]}
        headers = {"x-api-key": str(model.get("api_key") or ""),
                   "anthropic-version": str((model.get("extra") or {}).get("anthropic_version")
                                            or "2023-06-01")}
    else:
        url = _openai_url(model["base_url"])
        payload = {"model": model["model"], "max_tokens": 32, "temperature": 0,
                   "stream": False,
                   "messages": [{"role": "user", "content": "只回复 OK 两个字母。"}]}
        headers = {"Authorization": f"Bearer {model.get('api_key') or ''}"}
    headers.update(_headers_extra(model))
    return build_browser_relay_spec(
        model, url, payload, headers,
        max_bytes=int(model.get("_relay_max_bytes") or 1048576))


def parse_model_names(data: Any, provider: str = "openai") -> list[str]:
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(data, dict) and not isinstance(items, list):
        items = data.get("models")
    if not isinstance(items, list):
        raise ValueError("模型列表响应缺少 data 数组")
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
        else:
            name = ""
        if name and str(name) not in names:
            names.append(str(name))
    return names


def parse_test_reply(data: Any, provider: str = "openai") -> str:
    if provider == "anthropic":
        blocks = data.get("content") if isinstance(data, dict) else []
        text = "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
    else:
        choices = data.get("choices") if isinstance(data, dict) else []
        text = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            text = message.get("content") if isinstance(message, dict) else ""
            if not text:
                text = choices[0].get("text") or ""
    return str(text or "").strip()[:200]


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: int, *, allow_private: bool = False,
               private_allowlist: tuple[str, ...] | str | None = None,
               max_bytes: int | None = None) -> tuple[dict[str, Any], int]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    raw = b""
    policy = OutboundPolicy(allow_private=allow_private,
                            private_allowlist=private_allowlist or (),
                            max_bytes=max_bytes or 1024 * 1024)
    try:
        with open_url(req, timeout=timeout, policy=policy) as resp:
            require_json_content_type(resp)
            raw = read_limited(resp, policy.max_bytes)
            return json.loads(raw.decode("utf-8", errors="replace")), resp.status
    except urllib.error.HTTPError as exc:
        raw_text = error_body(exc, min(policy.max_bytes, 4096))
        raise AIError(f"HTTP {exc.code}: {raw_text[:500]}", http_status=exc.code,
                      response_body=raw_text, retryable=exc.code >= 500 or exc.code == 429,
                      retry_after=parse_retry_after(exc.headers.get("Retry-After"))) from exc
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
              allow_private: bool = False,
              private_allowlist: tuple[str, ...] | str | None = None,
              max_bytes: int | None = None) -> Any:
    policy = OutboundPolicy(allow_private=allow_private,
                            private_allowlist=private_allowlist or (),
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
                     allow_private=bool(model.get('allow_private', False)),
                     private_allowlist=model.get("private_allowlist") or ())
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
                        private_allowlist: tuple[str, ...] | str | None = None,
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
                            private_allowlist=private_allowlist or (),
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
                      response_body=raw_text, retryable=exc.code >= 500 or exc.code == 429,
                      retry_after=parse_retry_after(exc.headers.get("Retry-After"))) from exc
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
                 temperature: float | None, stream: bool = False, *,
                 relay: Any | None = None, user_id: int = 0,
                 purpose: str = ""
                 ) -> tuple[str, Usage, bool]:
    browser = model_transport(model) == "browser"
    if browser:
        # 浏览器每次只 fetch 一次，不能把流式响应交给服务端边读边解析。
        stream = False
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

    def post(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if browser:
            return _post_relay_json(relay, user_id, model, url, body, headers, purpose)
        return _post_json(
            url, body, headers, model["timeout_sec"],
            allow_private=bool(model.get("allow_private", False)),
            private_allowlist=model.get("private_allowlist") or ())

    if stream:
        try:
            text, usage = _post_openai_stream(
                url, payload, headers, model["timeout_sec"],
                allow_private=bool(model.get("allow_private", False)),
                private_allowlist=model.get("private_allowlist") or ())
            if usage.total_tokens == 0:
                # 部分网关不返回 usage；给预算一个保守估算，避免完全不计成本。
                usage = Usage(
                    input_tokens=max(1, estimate_tokens(system) + estimate_tokens(user)),
                    output_tokens=max(1, estimate_tokens(text)),
                    total_tokens=max(2, estimate_tokens(system) + estimate_tokens(user)
                                     + estimate_tokens(text)),
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
        data, _ = post(payload)
    except AIError as exc:
        # 部分兼容网关不支持原生 json_schema，降级为 json_object 后重试一次。
        if json_schema and "response_format" in str(exc).lower():
            payload["response_format"] = {"type": "json_object"}
            try:
                data, _ = post(payload)
            except AIError as exc2:
                if "response_format" not in str(exc2).lower():
                    raise
                payload.pop("response_format", None)
                data, _ = post(payload)
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
                    temperature: float | None, *,
                    relay: Any | None = None, user_id: int = 0,
                    purpose: str = "") -> tuple[str, Usage]:
    browser = model_transport(model) == "browser"
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
    url = _anthropic_url(model["base_url"])
    if browser:
        data, _ = _post_relay_json(relay, user_id, model, url, payload, headers, purpose)
    else:
        data, _ = _post_json(
            url, payload, headers, model["timeout_sec"],
            allow_private=bool(model.get("allow_private", False)),
            private_allowlist=model.get("private_allowlist") or ())
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
