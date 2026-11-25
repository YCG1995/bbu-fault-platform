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
from .util import utc_now


class AIError(RuntimeError):
    pass


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


class Budget:
    """任务级预算；线程安全。"""

    def __init__(self, max_calls: int, max_tokens: int, deadline: float | None = None,
                 on_event: Callable[[str, str, dict], None] | None = None) -> None:
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.deadline = deadline
        self.calls = 0
        self.usage = Usage()
        self._lock = threading.Lock()
        self.on_event = on_event

    def check(self, estimated_tokens: int = 0) -> None:
        with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(f"模型调用次数已达上限 {self.max_calls}")
            if self.usage.total_tokens + estimated_tokens > self.max_tokens:
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
        }

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.calls)


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


_TYPE_MAP = {
    "object": dict, "array": list, "string": str,
    "number": (int, float), "integer": int, "boolean": bool, "null": type(None),
}


def validate_schema(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """最小 JSON Schema 校验器：type/required/properties/items/enum。"""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors
    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        pytypes = tuple(_TYPE_MAP.get(t, object) for t in types)
        if not isinstance(data, pytypes):
            errors.append(f"{path}: 期望 {expected}，实际 {type(data).__name__}")
            return errors
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: 值 {data!r} 不在枚举 {schema['enum']} 中")
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path}: 缺少必填字段 {key}")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in data:
                errors.extend(validate_schema(data[key], subschema, f"{path}.{key}"))
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        for idx, item in enumerate(data):
            errors.extend(validate_schema(item, schema["items"], f"{path}[{idx}]"))
    return errors


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
        self.breaker = CircuitBreaker()
        self._cache: dict[int, dict[str, Any]] = {}
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, Any] = {"calls": 0, "failures": 0, "recent": [],
                                         "by_model": {}}
        self._health_window = max(5, int(config.get("llm_health_window", 20)) if config else 20)
        self._local = threading.local()
        self.secret_box = secret_box
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
            "roles": ["default", "planner", "router", "arbiter", "writer", "workshop"],
            "extra": {},
            "source": "personal",
            "personal": True,
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

    def complete(self, role: str, system: str, user: str, *,
                 json_schema: dict[str, Any] | None = None,
                 max_tokens: int | None = None,
                 temperature: float | None = None,
                 purpose: str = "",
                 budget: Budget | None = None,
                 model_override: int | None = None,
                 retries_per_model: int | None = None) -> AIResult:
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
        errors: list[str] = []
        for model in models:
            key = f"{model['id']}:{model['name']}"
            for attempt in range(retries_per_model + 1):
                try:
                    if budget:
                        est = max(1, (len(system) + len(user)) // 3)
                        budget.check(est)
                    result = self._call(model, system, user, json_schema,
                                        max_tokens, temperature, purpose)
                    self._note_model(model, True, result.latency_ms)
                    if budget:
                        budget.record(result.usage)
                    self.breaker.ok(key)
                    if json_schema:
                        data = extract_json(result.text)
                        problems = (["模型未返回合法 JSON"] if data is None
                                    else validate_schema(data, json_schema))
                        if not problems:
                            result.data = data
                            return result
                        # 只修复一次，修复失败直接换下一个模型/交给调用方降级
                        if attempt == 0:
                            repair_user = (
                                f"{user}\n\n[上一次输出不合法，问题如下]\n"
                                + "\n".join(f"- {e}" for e in problems[:12])
                                + "\n请只输出严格符合 JSON Schema 的 JSON，不要解释。"
                            )
                            repair = self._call(model, system, repair_user, json_schema,
                                                max_tokens, temperature, purpose + ":repair")
                            self._note_model(model, True, repair.latency_ms)
                            if budget:
                                budget.record(repair.usage)
                            repaired = extract_json(repair.text)
                            if repaired is not None and not validate_schema(repaired, json_schema):
                                repair.data = repaired
                                self.breaker.ok(key)
                                return repair
                            errors.append(f"{model['name']}: JSON 校验失败 {problems[:3]}")
                            self._note_model(model, False, error="JSON 校验失败")
                            break
                        errors.append(f"{model['name']}: JSON 校验失败 {problems[:3]}")
                        self._note_model(model, False, error="JSON 校验失败")
                        break
                    return result
                except BudgetExceeded:
                    raise
                except (AIError, urllib.error.URLError, socket.timeout, TimeoutError) as exc:
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
              temperature: float | None, purpose: str) -> AIResult:
        provider = model["provider"]
        started = time.time()
        if provider == "openai":
            text, usage = _call_openai(model, system, user, json_schema,
                                       max_tokens, temperature)
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
                        purpose=purpose)

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


def _headers_extra(model: dict[str, Any]) -> dict[str, str]:
    extra = model.get("extra") or {}
    headers = extra.get("headers") if isinstance(extra.get("headers"), dict) else {}
    return {str(k): str(v) for k, v in headers.items()}


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: int) -> tuple[dict[str, Any], int]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace")), resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise AIError(f"HTTP {exc.code}: {raw[:500]}") from exc
    except urllib.error.URLError as exc:
        raise AIError(f"网络错误: {exc.reason}") from exc
    except (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionError) as exc:
        raise AIError(f"模型响应中断: {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise AIError(f"响应不是合法 JSON: {exc}") from exc


def _openai_url(base: str) -> str:
    base = (base or "https://api.openai.com/v1").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _call_openai(model: dict[str, Any], system: str, user: str,
                 json_schema: dict[str, Any] | None, max_tokens: int | None,
                 temperature: float | None) -> tuple[str, Usage]:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload: dict[str, Any] = {
        "model": model["model"],
        "messages": messages,
        "temperature": model["temperature"] if temperature is None else temperature,
        "max_tokens": max_tokens or model["max_tokens"],
        "stream": False,
    }
    if json_schema:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {model['api_key']}"}
    headers.update(_headers_extra(model))
    url = _openai_url(model["base_url"])
    try:
        data, _ = _post_json(url, payload, headers, model["timeout_sec"])
    except AIError as exc:
        # 部分兼容网关不支持 response_format，降级后重试一次
        if json_schema and "response_format" in str(exc):
            payload.pop("response_format", None)
            data, _ = _post_json(url, payload, headers, model["timeout_sec"])
        else:
            raise
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AIError(f"响应结构异常: {str(data)[:300]}") from exc
    usage_raw = data.get("usage") or {}
    usage = Usage(
        input_tokens=int(usage_raw.get("prompt_tokens") or 0),
        output_tokens=int(usage_raw.get("completion_tokens") or 0),
        total_tokens=int(usage_raw.get("total_tokens") or 0) or
        int(usage_raw.get("prompt_tokens") or 0) + int(usage_raw.get("completion_tokens") or 0),
    )
    return text, usage


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
                         model["timeout_sec"])
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
