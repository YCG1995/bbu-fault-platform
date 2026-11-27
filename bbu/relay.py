"""浏览器代理模型中继：语义在服务端，传输在浏览器。

浏览器只领取一条已经由服务端序列化好的 HTTP 请求，执行一次 ``fetch``，
然后把原始 ``status + body_text`` 回传。模型 Key 只存在于 ``GET
/api/relay/calls`` 的单次响应和浏览器内存中，不进入轨迹、日志或持久化
队列。

队列是单进程内存结构：当前部署是 ``ThreadingHTTPServer``，同一进程内的
worker 与浏览器窗口通过 ``threading.Condition`` 会合。服务重启后队列自然
清空；这是计划中明确接受的非目标（不做跨进程中继）。
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from .util import iso_now

# 浏览器侧错误（不在线、认领超时、网络/abort/超时）不代表模型端点故障，
# 不参与传输熔断和 Schema 熔断。真实 HTTP 401/429/5xx 仍走原规则。
RELAY_CLIENT_KINDS = ("relay_absent", "relay_exec_failed")

_WINDOW_RE = re.compile(r"[A-Za-z0-9_-]{2,64}")
_LOCAL_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost"}


class RelayError(RuntimeError):
    """中继错误；``kind`` 供 AIGateway 决定是否熔断。"""

    def __init__(self, message: str, *, kind: str = "relay_absent",
                 status: int = 0, code: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = int(status or 0)
        self.code = code or kind
        self.retryable = bool(retryable)

    @property
    def circuit_break(self) -> bool:
        return self.kind not in RELAY_CLIENT_KINDS


class RelayAbsentError(RelayError):
    def __init__(self, message: str = "浏览器代理未响应", *, code: str = "relay_absent") -> None:
        super().__init__(message, kind="relay_absent", code=code, retryable=False)


class RelayExecError(RelayError):
    def __init__(self, message: str = "浏览器代理执行失败", *, status: int = 0,
                 code: str = "relay_exec_failed") -> None:
        super().__init__(message, kind="relay_exec_failed", status=status,
                         code=code, retryable=False)


class RelayDisabledError(RelayError):
    def __init__(self, message: str = "浏览器代理已关闭") -> None:
        super().__init__(message, kind="relay_absent", status=403,
                         code="relay_disabled", retryable=False)


class RelayWindowEvictedError(RelayError):
    def __init__(self, message: str = "另一个窗口已接管浏览器代理") -> None:
        super().__init__(message, kind="relay_absent", status=409,
                         code="relay_window_evicted", retryable=False)


class RelayCallConsumedError(RelayError):
    def __init__(self, message: str = "调用已领取或已完成") -> None:
        super().__init__(message, kind="relay_absent", status=409,
                         code="relay_call_consumed", retryable=False)


class RelayCallExpiredError(RelayError):
    def __init__(self, message: str = "调用已过期") -> None:
        super().__init__(message, kind="relay_absent", status=410,
                         code="relay_call_expired", retryable=False)


class RelayCrossUserError(RelayError):
    def __init__(self, message: str = "不能访问其他用户的浏览器调用") -> None:
        super().__init__(message, kind="relay_absent", status=403,
                         code="relay_cross_user", retryable=False)


class RelayOversizeError(RelayError):
    def __init__(self, message: str = "浏览器回传体超过上限") -> None:
        super().__init__(message, kind="relay_exec_failed", status=413,
                         code="relay_oversize", retryable=False)


class RelayNotFoundError(RelayError):
    def __init__(self, message: str = "浏览器调用不存在") -> None:
        super().__init__(message, kind="relay_absent", status=404,
                         code="relay_not_found", retryable=False)


@dataclass
class RelaySpec:
    """一条已经序列化好的请求；不在任何地方落盘。"""

    url: str
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    auth: dict[str, str] = field(default_factory=dict)
    timeout_sec: int = 90
    max_bytes: int = 1048576

    def __post_init__(self) -> None:
        self.method = str(self.method or "POST").upper()
        self.headers = {str(k): str(v) for k, v in (self.headers or {}).items()}
        self.auth = {str(k): str(v) for k, v in (self.auth or {}).items()}
        # auth 与 headers 分离：即使调用方误把 Key 放进 headers，也不允许
        # 重复下发。浏览器只会把 auth 拼到请求头。
        auth_header = str(self.auth.get("header") or "").lower()
        for key in list(self.headers):
            if key.lower() in ("authorization", "x-api-key", "api-key"):
                self.headers.pop(key, None)
        if auth_header:
            for key in list(self.headers):
                if key.lower() == auth_header:
                    self.headers.pop(key, None)
        self.body = str(self.body or "")
        self.timeout_sec = max(1, min(300, int(self.timeout_sec or 90)))
        self.max_bytes = max(1024, min(16 * 1024 * 1024, int(self.max_bytes or 1048576)))

    def public(self, *, expires_at: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": self.url,
            "method": self.method,
            "headers": dict(self.headers),
            "body": self.body,
            "timeout_sec": self.timeout_sec,
            "max_bytes": self.max_bytes,
        }
        if self.auth:
            # 这是 Key 生命周期中唯一允许下发的位置。HTTP 响应必须 no-store。
            payload["auth"] = dict(self.auth)
        if expires_at:
            payload["expires_at"] = expires_at
        return payload


@dataclass
class RelayResult:
    text: str
    status: int = 200
    latency_ms: int = 0
    usage: Any = None


@dataclass
class RelayWindow:
    user_id: int
    window_id: str
    last_seen: float = field(default_factory=time.monotonic)
    active_calls: int = 0


@dataclass
class RelayCall:
    call_id: str
    user_id: int
    model_id: int
    model_name: str
    spec: RelaySpec
    purpose: str
    state: str = "pending"  # pending -> claimed -> done | failed | expired | cancelled
    created_at: str = field(default_factory=iso_now)
    created_mono: float = field(default_factory=time.monotonic)
    claim_deadline: float = 0.0
    claimed_at: str = ""
    claimed_mono: float = 0.0
    deadline: float = 0.0
    claimed_by: str = ""
    exec_timeout: int = 90
    result: RelayResult | None = None
    error: str = ""
    error_kind: str = ""
    http_status: int = 0


def normalize_window_id(value: Any) -> str:
    text = str(value or "").strip()
    if _WINDOW_RE.fullmatch(text):
        return text
    return "w-" + secrets.token_hex(8)


def _host_is_forbidden(host: str) -> bool:
    value = (host or "").strip().strip("[]").lower()
    if not value or value in _LOCAL_HOSTS or value.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def validate_browser_url(url: str, *, allow_http: bool = False) -> bool:
    """服务端第一道 URL 安全检查；浏览器 relay.js 还会再检查一次。"""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("https", "http"):
        return False
    if parsed.scheme == "http" and not allow_http:
        return False
    if not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is not None and not (0 < port < 65536):
        return False
    return not _host_is_forbidden(parsed.hostname)


class RelayHub:
    """内存中的浏览器窗口与调用队列。"""

    def __init__(self, db: Any, config: Any, *, start_cleaner: bool = True) -> None:
        self.db = db
        self.config = config
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._windows: dict[int, dict[str, RelayWindow]] = {}
        self._evicted: dict[tuple[int, str], float] = {}
        self._calls: dict[str, RelayCall] = {}
        self._pending: dict[int, deque[str]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cleanup = 0.0
        if start_cleaner:
            self.start()

    # ---------- 配置 ----------
    def _int_cfg(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int((self.config.get(key, default) if self.config is not None else default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("relay_enabled", True) if self.config is not None else True)

    @property
    def allow_http(self) -> bool:
        return bool(self.config.get("relay_allow_insecure_http", False)
                    if self.config is not None else False)

    @property
    def max_response_bytes(self) -> int:
        return self._int_cfg("relay_max_response_bytes", 1048576, 1024, 16 * 1024 * 1024)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._cleaner, name="relay-cleaner",
                                            daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    # ---------- 窗口租约 ----------
    def _user_role(self, user_id: int) -> str:
        try:
            row = self.db.one("SELECT role FROM users WHERE id=?", (int(user_id),))
        except Exception:
            row = None
        return str((row or {}).get("role") or "")

    def _max_windows(self) -> int:
        return self._int_cfg("relay_max_windows_per_user", 2, 1, 16)

    def _lease_ttl(self) -> float:
        return float(self._int_cfg("relay_lease_ttl_sec", 120, 10, 3600))

    def _refresh_windows_locked(self) -> None:
        now = time.monotonic()
        ttl = self._lease_ttl()
        gone: list[tuple[int, RelayWindow]] = []
        for uid, group in list(self._windows.items()):
            for wid, win in list(group.items()):
                if now - win.last_seen > ttl:
                    group.pop(wid, None)
                    self._evicted[(uid, wid)] = now
                    gone.append((uid, win))
            if not group:
                self._windows.pop(uid, None)
        for uid, win in gone:
            for call in list(self._calls.values()):
                if call.user_id == uid and call.claimed_by == win.window_id \
                        and call.state == "claimed":
                    self._fail_call_locked(call, "relay_absent",
                                           "浏览器窗口心跳超时，调用已失效")
            if not self.online_locked(uid):
                self._fail_user_pending_locked(uid, "relay_absent",
                                               "浏览器窗口已离线，调用已失效")
        # 过期驱逐记录允许窗口重新注册；旧页面若在 TTL 内回来仍会收到 409。
        for key, ts in list(self._evicted.items()):
            if now - ts > ttl * 2:
                self._evicted.pop(key, None)

    def register_window(self, user_id: int, window_id: str) -> dict[str, Any]:
        uid = int(user_id or 0)
        if not self.enabled:
            raise RelayDisabledError()
        if not uid:
            raise RelayAbsentError("缺少用户身份")
        wid = normalize_window_id(window_id)
        now = time.monotonic()
        with self._cv:
            self._refresh_windows_locked()
            evicted = self._evicted.get((uid, wid))
            if evicted is not None and now - evicted <= self._lease_ttl():
                raise RelayWindowEvictedError()
            group = self._windows.setdefault(uid, {})
            existing = group.get(wid)
            if existing:
                existing.last_seen = now
                return self._window_public(uid, existing, len(group))
            limit = self._max_windows()
            while len(group) >= limit:
                oldest_id, oldest = min(group.items(), key=lambda item: item[1].last_seen)
                group.pop(oldest_id, None)
                self._evicted[(uid, oldest_id)] = now
                for call in list(self._calls.values()):
                    if call.claimed_by == oldest_id and call.state == "claimed":
                        self._fail_call_locked(call, "relay_absent",
                                               "浏览器窗口被新窗口顶替，调用已失效")
            win = RelayWindow(user_id=uid, window_id=wid, last_seen=now)
            group[wid] = win
            self._cv.notify_all()
            return self._window_public(uid, win, len(group))

    def _window_public(self, uid: int, win: RelayWindow, count: int) -> dict[str, Any]:
        return {"window_id": win.window_id, "ttl_sec": int(self._lease_ttl()),
                "windows": int(count)}

    def touch_window(self, user_id: int, window_id: str) -> RelayWindow:
        uid = int(user_id or 0)
        wid = normalize_window_id(window_id)
        now = time.monotonic()
        with self._lock:
            if not self.enabled:
                raise RelayDisabledError()
            evicted = self._evicted.get((uid, wid))
            if evicted is not None and now - evicted <= self._lease_ttl():
                raise RelayWindowEvictedError()
            win = self._windows.get(uid, {}).get(wid)
            if not win:
                raise RelayWindowEvictedError("浏览器窗口已失效，请刷新页面")
            win.last_seen = now
            return win

    def release_window(self, user_id: int, window_id: str) -> bool:
        uid = int(user_id or 0)
        wid = normalize_window_id(window_id)
        with self._cv:
            group = self._windows.get(uid, {})
            win = group.pop(wid, None)
            if not group:
                self._windows.pop(uid, None)
            if win:
                for call in list(self._calls.values()):
                    if call.claimed_by == wid and call.state == "claimed":
                        self._fail_call_locked(call, "relay_absent",
                                               "浏览器窗口已关闭，调用已失效")
                if not self.online_locked(uid):
                    self._fail_user_pending_locked(uid, "relay_absent",
                                                   "浏览器窗口已关闭，调用已失效")
            self._cv.notify_all()
            return bool(win)

    def online_locked(self, user_id: int) -> bool:
        now = time.monotonic()
        ttl = self._lease_ttl()
        return any(now - win.last_seen <= ttl
                   for win in self._windows.get(int(user_id or 0), {}).values())

    def online(self, user_id: int) -> bool:
        with self._lock:
            self._refresh_windows_locked()
            return self.online_locked(int(user_id or 0))

    def window_count(self, user_id: int) -> int:
        with self._lock:
            self._refresh_windows_locked()
            return len(self._windows.get(int(user_id or 0), {}))

    # ---------- 模型可见性 ----------
    @staticmethod
    def _extra(model: dict[str, Any]) -> dict[str, Any]:
        extra = model.get("extra")
        if isinstance(extra, dict):
            return extra
        return {}

    def role_allowed(self, model: dict[str, Any], user_id: int) -> bool:
        extra = self._extra(model)
        relay_cfg = extra.get("browser_relay") if isinstance(extra.get("browser_relay"), dict) else {}
        allowed = relay_cfg.get("allow_roles") or extra.get("allow_roles") or []
        if isinstance(allowed, str):
            allowed = [x.strip() for x in allowed.split(",") if x.strip()]
        if not isinstance(allowed, (list, tuple)) or not allowed:
            return True
        role = self._user_role(int(user_id or 0))
        return bool(role) and role in {str(x) for x in allowed}

    def browser_models(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM model_configs WHERE enabled=1 ORDER BY priority ASC, id ASC")
        models = []
        for row in rows:
            import json
            try:
                extra = json.loads(row.get("extra") or "{}")
            except Exception:
                extra = {}
            if not isinstance(extra, dict):
                continue
            if str(extra.get("transport") or "server") != "browser":
                continue
            model = dict(row)
            model["extra"] = extra
            try:
                model["roles"] = json.loads(row.get("roles") or "[]")
            except Exception:
                model["roles"] = []
            models.append(model)
        return models

    def available_models(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_windows_locked()
            online = self.online_locked(int(user_id or 0))
        if not self.enabled or not online:
            return []
        items = []
        for model in self.browser_models():
            if not self.role_allowed(model, user_id):
                continue
            extra = model.get("extra") or {}
            relay_cfg = extra.get("browser_relay") if isinstance(extra.get("browser_relay"), dict) else {}
            items.append({
                "id": model.get("id"),
                "name": model.get("name") or model.get("model") or "",
                "model": model.get("model") or "",
                "roles": model.get("roles") or [],
                "priority": model.get("priority") or 0,
                "max_concurrent": max(1, min(4, int(relay_cfg.get("max_concurrent") or 2))),
                "transport": "browser",
            })
        return items

    def status_payload(self, user_id: int, window_id: str) -> dict[str, Any]:
        window = self.register_window(user_id, window_id)
        with self._lock:
            self._refresh_windows_locked()
            pending = sum(1 for call in self._calls.values()
                          if call.user_id == int(user_id) and call.state == "pending")
        return {
            "enabled": bool(self.enabled),
            "window": window,
            "poll_sec": self._int_cfg("relay_status_poll_sec", 5, 1, 60),
            "pending": pending,
            "models": self.available_models(user_id),
            "allow_http": bool(self.allow_http),
            "max_response_bytes": self.max_response_bytes,
        }

    def pending_count(self, user_id: int) -> int:
        with self._lock:
            return sum(1 for call in self._calls.values()
                       if call.user_id == int(user_id) and call.state == "pending")

    # ---------- 排队与领取 ----------
    def _fail_call_locked(self, call: RelayCall, kind: str, message: str,
                          status: int = 0, *, expired: bool = False) -> None:
        if call.state in ("done", "failed", "expired", "cancelled"):
            return
        previous = call.state
        call.state = "expired" if expired else "failed"
        call.error = str(message or "")
        call.error_kind = str(kind or "")
        call.http_status = int(status or 0)
        call.result = None
        if call.claimed_by:
            win = self._windows.get(call.user_id, {}).get(call.claimed_by)
            if win and win.active_calls > 0:
                win.active_calls -= 1
        self._cv.notify_all()
        # 只记录异常事实，不写 prompt、Key 或请求体。pending 超时属于现场
        # 最需要排查的“浏览器没在轮询”信号，写一条即可。
        try:
            action = ("relay_claim_timeout" if previous == "pending"
                      else "relay_exec_timeout")
            self.db.audit(call.user_id, "relay", action, call.call_id,
                          {"model_id": call.model_id, "purpose": call.purpose,
                           "bytes": len(call.spec.body.encode("utf-8")),
                           "error_kind": kind}, "")
        except Exception:
            pass

    def _fail_user_pending_locked(self, uid: int, kind: str, message: str) -> None:
        for call in list(self._calls.values()):
            if call.user_id == uid and call.state == "pending":
                self._fail_call_locked(call, kind, message)

    def _enqueue(self, user_id: int, model: dict[str, Any], spec: RelaySpec,
                 purpose: str, claim_timeout: float, exec_timeout: float) -> RelayCall:
        uid = int(user_id or 0)
        now = time.monotonic()
        call_id = "c-" + secrets.token_hex(8)
        call = RelayCall(
            call_id=call_id, user_id=uid, model_id=int(model.get("id") or 0),
            model_name=str(model.get("name") or model.get("model") or ""),
            spec=spec, purpose=str(purpose or "model"),
            claim_deadline=now + max(0.05, float(claim_timeout)),
            exec_timeout=max(1, int(exec_timeout or 90)),
            deadline=now + max(0.05, float(claim_timeout)) + max(1, int(exec_timeout or 90)),
        )
        with self._cv:
            self._calls[call_id] = call
            self._pending.setdefault(uid, deque()).append(call_id)
            self._cv.notify_all()
        return call

    def submit(self, user_id: int, model: dict[str, Any], spec: RelaySpec, *,
               purpose: str = "model", claim_timeout: float | None = None,
               exec_timeout: float | None = None) -> RelayResult:
        if not self.enabled:
            raise RelayDisabledError()
        if not validate_browser_url(spec.url, allow_http=self.allow_http):
            raise RelayAbsentError("浏览器代理地址被安全策略拒绝", code="relay_url_blocked")
        if not self.online(user_id):
            raise RelayAbsentError("浏览器代理不在线")
        timeout_claim = (float(claim_timeout) if claim_timeout is not None
                         else float(self._int_cfg("relay_claim_timeout_sec", 10, 5, 60)))
        timeout_exec = (float(exec_timeout) if exec_timeout is not None
                        else float(self._int_cfg("relay_exec_timeout_sec", 90, 30, 120)))
        timeout_claim = max(0.05, timeout_claim)
        timeout_exec = max(1.0, timeout_exec)
        call = self._enqueue(int(user_id or 0), model, spec, purpose,
                             timeout_claim, timeout_exec)
        started = time.monotonic()
        hard_deadline = started + timeout_claim + timeout_exec + 1.0
        while True:
            with self._cv:
                self._refresh_windows_locked()
                if call.state == "done" and call.result is not None:
                    return call.result
                if call.state in ("failed", "expired", "cancelled"):
                    if call.error_kind == "relay_absent":
                        raise RelayAbsentError(call.error or "浏览器代理未响应")
                    raise RelayExecError(call.error or "浏览器代理执行失败",
                                         status=call.http_status or 0)
                now = time.monotonic()
                if hard_deadline <= now:
                    self._fail_call_locked(call, "relay_absent",
                                           "浏览器代理等待超时，已切换模型")
                    continue
                if call.state == "pending" and now >= call.claim_deadline:
                    self._fail_call_locked(call, "relay_absent",
                                           "浏览器代理未认领该调用，已切换模型")
                    continue
                if call.state == "claimed" and now >= call.deadline:
                    self._fail_call_locked(call, "relay_absent",
                                           "浏览器代理执行超时，已切换模型",
                                           expired=True)
                    continue
                wait = min(0.5, max(0.02, hard_deadline - now))
                self._cv.wait(wait)

    def _next_pending_locked(self, uid: int, now: float) -> RelayCall | None:
        queue = self._pending.get(uid)
        if not queue:
            return None
        while queue:
            cid = queue[0]
            call = self._calls.get(cid)
            if not call or call.state != "pending":
                queue.popleft()
                continue
            if now >= call.claim_deadline:
                queue.popleft()
                self._fail_call_locked(call, "relay_absent",
                                       "浏览器代理未及时认领，调用已失效")
                continue
            return call
        return None

    def claim(self, user_id: int, window_id: str, wait_sec: float = 10.0,
              *, call_id: str | None = None) -> RelayCall | None:
        uid = int(user_id or 0)
        wid = normalize_window_id(window_id)
        wait_sec = max(0.0, min(30.0, float(wait_sec or 10)))
        deadline = time.monotonic() + wait_sec
        with self._cv:
            if not self.enabled:
                raise RelayDisabledError()
            self._refresh_windows_locked()
            self._touch_locked(uid, wid)
            while True:
                win = self._windows.get(uid, {}).get(wid)
                if not win:
                    raise RelayWindowEvictedError("浏览器窗口已失效，请刷新页面")
                if call_id:
                    call = self._calls.get(str(call_id))
                    if not call:
                        raise RelayNotFoundError()
                    if call.user_id != uid:
                        raise RelayCrossUserError()
                    if call.state == "claimed" and call.claimed_by == wid:
                        raise RelayCallConsumedError()
                    if call.state != "pending":
                        if call.state == "done" or call.state == "failed":
                            raise RelayCallConsumedError()
                        raise RelayCallExpiredError()
                    if win.active_calls < self._window_max_concurrent(uid, wid):
                        self._mark_claimed_locked(call, win)
                        return call
                elif win.active_calls < self._window_max_concurrent(uid, wid):
                    call = self._next_pending_locked(uid, time.monotonic())
                    if call is not None:
                        self._mark_claimed_locked(call, win)
                        return call
                now = time.monotonic()
                if now >= deadline:
                    return None
                self._cv.wait(min(deadline - now, 1.0))

    def _touch_locked(self, uid: int, wid: str) -> None:
        evicted = self._evicted.get((uid, wid))
        if evicted is not None and time.monotonic() - evicted <= self._lease_ttl():
            raise RelayWindowEvictedError()
        win = self._windows.get(uid, {}).get(wid)
        if not win:
            raise RelayWindowEvictedError("浏览器窗口已失效，请刷新页面")
        win.last_seen = time.monotonic()

    def _window_max_concurrent(self, uid: int, wid: str) -> int:
        win = self._windows.get(uid, {}).get(wid)
        if not win:
            return 1
        # window 对应的模型并发在调用级别由服务端提交时确定；浏览器端
        # relay.js 另有限制。这里用该用户可见 browser 模型的最大值作为
        # 保守上限，避免单窗口无限领取。
        values = []
        for model in self.browser_models():
            if self.role_allowed(model, uid):
                extra = model.get("extra") or {}
                cfg = extra.get("browser_relay") if isinstance(extra.get("browser_relay"), dict) else {}
                values.append(max(1, min(4, int(cfg.get("max_concurrent") or 2))))
        return max(values or [2])

    def _mark_claimed_locked(self, call: RelayCall, win: RelayWindow) -> None:
        now = time.monotonic()
        call.state = "claimed"
        call.claimed_at = iso_now()
        call.claimed_mono = now
        call.deadline = now + max(1, int(call.exec_timeout or 90))
        call.claimed_by = win.window_id
        win.active_calls += 1
        win.last_seen = now
        self._cv.notify_all()

    def complete(self, user_id: int, call_id: str, status: int, body_text: str,
                 latency_ms: int = 0, error_kind: str = "",
                 truncated: bool = False) -> None:
        uid = int(user_id or 0)
        text = str(body_text or "")
        with self._cv:
            call = self._calls.get(str(call_id))
            if not call:
                raise RelayNotFoundError()
            if call.user_id != uid:
                raise RelayCrossUserError()
            if call.state in ("done", "failed", "expired", "cancelled"):
                if call.state == "expired":
                    raise RelayCallExpiredError()
                raise RelayCallConsumedError()
            if call.state != "claimed":
                raise RelayCallConsumedError("调用尚未领取")
            if truncated or len(text.encode("utf-8")) > call.spec.max_bytes:
                self._fail_call_locked(call, "relay_exec_failed",
                                       "浏览器回传体超过上限", 413)
                raise RelayOversizeError()
            kind = str(error_kind or "").strip().lower()
            if kind and kind not in ("network", "abort", "timeout", "http"):
                kind = ""
            try:
                code = int(status or 0)
            except (TypeError, ValueError):
                code = 0
            if kind in ("network", "abort", "timeout"):
                self._fail_call_locked(call, "relay_exec_failed",
                                       f"浏览器执行失败（{kind}）", code)
                raise RelayExecError(f"浏览器执行失败（{kind}）", status=code)
            if code <= 0:
                self._fail_call_locked(call, "relay_exec_failed",
                                       "浏览器未返回 HTTP 状态")
                raise RelayExecError("浏览器未返回 HTTP 状态")
            call.result = RelayResult(text=text, status=code,
                                      latency_ms=max(0, int(latency_ms or 0)))
            call.state = "done"
            call.http_status = code
            if call.claimed_by:
                win = self._windows.get(call.user_id, {}).get(call.claimed_by)
                if win and win.active_calls > 0:
                    win.active_calls -= 1
            self._cv.notify_all()

    def cancel_user(self, user_id: int) -> int:
        uid = int(user_id or 0)
        removed = 0
        with self._cv:
            for wid in list(self._windows.get(uid, {})):
                self._windows[uid].pop(wid, None)
            self._windows.pop(uid, None)
            for call in list(self._calls.values()):
                if call.user_id == uid and call.state in ("pending", "claimed"):
                    if call.claimed_by:
                        win = self._windows.get(uid, {}).get(call.claimed_by)
                        if win and win.active_calls > 0:
                            win.active_calls -= 1
                    call.state = "cancelled"
                    call.error = "用户已退出登录，调用已取消"
                    call.error_kind = "relay_absent"
                    removed += 1
            self._cv.notify_all()
        return removed

    # ---------- 查询/清理 ----------
    def call_owner(self, call_id: str) -> int | None:
        with self._lock:
            call = self._calls.get(str(call_id))
            return call.user_id if call else None

    def consume_auth(self, call_id: str) -> None:
        """调用 payload 已写出后立即擦除 Key；重复领取不会再看到 auth。"""
        with self._lock:
            call = self._calls.get(str(call_id))
            if call and call.state == "claimed" and call.spec.auth:
                call.spec.auth = {}

    def call_state(self, call_id: str) -> str:
        with self._lock:
            call = self._calls.get(str(call_id))
            return call.state if call else ""

    def _cleaner(self) -> None:
        while not self._stop.is_set():
            try:
                with self._cv:
                    self._refresh_windows_locked()
                    self._expire_calls_locked()
                    self._last_cleanup = time.monotonic()
                    self._trim_calls_locked()
            except Exception:
                pass
            self._stop.wait(5.0)

    def _expire_calls_locked(self) -> None:
        now = time.monotonic()
        for call in list(self._calls.values()):
            if call.state == "pending" and now >= call.claim_deadline:
                self._fail_call_locked(call, "relay_absent",
                                       "浏览器代理未及时认领，调用已失效")
            elif call.state == "claimed" and now >= call.deadline:
                self._fail_call_locked(call, "relay_absent",
                                       "浏览器代理执行超时，调用已失效",
                                       expired=True)

    def _trim_calls_locked(self) -> None:
        if len(self._calls) <= 2000:
            return
        finished = [(call.created_mono, cid) for cid, call in self._calls.items()
                    if call.state not in ("pending", "claimed")]
        finished.sort()
        for _, cid in finished[:max(0, len(self._calls) - 1500)]:
            self._calls.pop(cid, None)
        for uid, queue in list(self._pending.items()):
            live = deque(cid for cid in queue if cid in self._calls)
            if live:
                self._pending[uid] = live
            else:
                self._pending.pop(uid, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": bool(self.enabled),
                "windows": sum(len(v) for v in self._windows.values()),
                "pending": sum(1 for c in self._calls.values() if c.state == "pending"),
                "claimed": sum(1 for c in self._calls.values() if c.state == "claimed"),
                "calls": len(self._calls),
            }


def key_fingerprint(value: str) -> str:
    """审计只留 Key 指纹，永远不写明文。"""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:8]


def build_auth(headers: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """把敏感认证头从普通 headers 中拆出，返回 (headers, auth)。"""
    clean = {str(k): str(v) for k, v in (headers or {}).items()}
    auth: dict[str, str] = {}
    for key in list(clean):
        lower = key.lower()
        if lower in ("authorization", "x-api-key", "api-key"):
            if not auth:
                auth = {"header": key, "value": clean.pop(key)}
            else:
                clean.pop(key)
    if not auth:
        # 兼容 Anthropic 的认证头被调用方放在普通 headers 中。
        for key in list(clean):
            if key.lower() == "x-api-key":
                auth = {"header": key, "value": clean.pop(key)}
                break
    return clean, auth
