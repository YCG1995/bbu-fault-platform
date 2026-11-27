"""执行轨迹：一次诊断/工坊任务一个 append-only JSONL 文件。

轨迹不是普通运行日志：它需要能回答“当时发给模型什么、模型回了什么、
工具怎么执行、校验在哪一步失败”。SQLite 只保存 run 索引，完整内容写 JSONL，
避免把大段 prompt/response 放进数据库。
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .db import Database
from .util import iso_now, new_id


class TraceRecorder:
    """单个 run 的线程安全 JSONL 写入器。"""

    def __init__(self, store: "TraceStore", run: dict[str, Any], path: Path,
                 enabled: bool = True) -> None:
        self.store = store
        self.run_id = str(run["id"])
        self.kind = str(run["kind"])
        self.ref_id = str(run["ref_id"])
        self.user_id = int(run.get("user_id") or 0)
        self.attempt_no = int(run.get("attempt_no") or 1)
        self.path = path
        self.enabled = enabled
        self.status = "running"
        self._seq = 0
        self._lock = threading.Lock()
        self._closed = False
        self._fh = None
        self._size = 0
        self._model_calls = 0
        self._tool_calls = 0
        self._max_event_bytes = max(1024, int(store.max_event_mb) * 1024 * 1024)
        self._max_run_bytes = max(1024, int(store.max_run_mb) * 1024 * 1024)
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
            self._fh = path.open("a", encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self.append("run_start", status="running", meta=run.get("meta") or {})

    @property
    def trace_id(self) -> str:
        return self.run_id

    def append(self, event_type: str, *, phase: str = "", step_id: str = "",
               status: str = "", **payload: Any) -> dict[str, Any]:
        if not self.enabled or self._closed:
            return {}
        with self._lock:
            self._seq += 1
            event: dict[str, Any] = {
                "v": 1,
                "seq": self._seq,
                "ts": iso_now(),
                "type": event_type,
                "run_id": self.run_id,
                "kind": self.kind,
                "ref_id": self.ref_id,
                "attempt_no": self.attempt_no,
                "phase": phase,
                "step_id": step_id,
                "status": status,
            }
            if payload:
                event.update(payload)
            event = self._trim_event(event)
            line = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
            if event_type == "model_request":
                self._model_calls += 1
            elif event_type == "tool_call":
                self._tool_calls += 1
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
                self._size += len(line.encode("utf-8")) + 1
            except OSError:
                # 轨迹写失败不能覆盖诊断/工坊结果；关闭文件并保留 DB 中的 run 记录。
                self.enabled = False
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            self._sync_db()
            return event

    def _trim_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event = _trim_strings(event, self.store.max_string_bytes)
        try:
            encoded = json.dumps(event, ensure_ascii=False, default=str,
                                separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            event = {k: str(v) if not isinstance(v, (dict, list, bool, int, float, type(None))) else v
                     for k, v in event.items()}
            encoded = json.dumps(event, ensure_ascii=False, default=str,
                                separators=(",", ":")).encode("utf-8")
        if len(encoded) > self._max_event_bytes:
            event = _trim_event_to_fit(event, self._max_event_bytes)
            event["_truncated"] = True
            event["_original_bytes"] = len(encoded)
        if self._size + len(encoded) > self._max_run_bytes:
            event = {
                "v": 1, "seq": event.get("seq"), "ts": event.get("ts"),
                "type": "trace_truncated", "run_id": self.run_id,
                "kind": self.kind, "ref_id": self.ref_id,
                "phase": event.get("phase") or "", "step_id": event.get("step_id") or "",
                "status": "warning",
                "message": "单条轨迹达到体积上限，后续大字段内容不再写入；错误和状态事件仍保留。",
                "_original_bytes": len(encoded),
            }
        return event

    def _sync_db(self) -> None:
        try:
            self.store.db.execute(
                "UPDATE trace_runs SET event_count=?, model_calls=?, tool_calls=?, "
                "size_bytes=?, status=?, updated_at=? WHERE id=?",
                (self._seq, self._model_calls, self._tool_calls, self._size,
                 self.status, iso_now(), self.run_id))
        except Exception:
            pass

    @contextmanager
    def step(self, name: str, phase: str = "", step_id: str = "") -> Iterator[None]:
        sid = step_id or new_id("S", with_time=False)
        started = datetime.now(timezone.utc)
        self.append("step_start", phase=phase, step_id=sid, name=name)
        try:
            yield
        except Exception as exc:
            self.append("step_end", phase=phase, step_id=sid, name=name,
                        status="failed", duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                        error={"type": type(exc).__name__, "message": str(exc)[:4000]})
            raise
        else:
            self.append("step_end", phase=phase, step_id=sid, name=name,
                        status="done",
                        duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000))

    def model_request(self, *, role: str, model: dict[str, Any], provider: str,
                      purpose: str, stage: str, attempt: int, system: str,
                      user: str, json_schema: dict[str, Any] | None,
                      temperature: float | None, max_tokens: int | None,
                      stream: bool, repair: bool = False, phase: str = "",
                      step_id: str = "") -> None:
        request = {
            "system": system or "", "user": user or "",
            "json_schema": json_schema, "temperature": temperature,
            "max_tokens": max_tokens, "stream": bool(stream),
            "purpose": purpose, "stage": stage, "attempt": int(attempt),
            "repair": bool(repair),
        }
        self.append("model_request", phase=phase or stage, step_id=step_id,
                    role=role,
                    model={"id": model.get("id"), "name": model.get("name"),
                           "provider": provider, "model": model.get("model"),
                           "base_url": _redact_url(model.get("base_url") or ""),
                           "temperature": model.get("temperature"),
                           "timeout_sec": model.get("timeout_sec")},
                    request=request, status="requested")

    def model_response(self, *, role: str, model: dict[str, Any], provider: str,
                       purpose: str, stage: str, attempt: int, text: str,
                       parsed: Any = None, usage: Any = None, latency_ms: int = 0,
                       http_status: int = 200, stream: bool = False,
                       repair: bool = False, phase: str = "",
                       step_id: str = "") -> None:
        self.append("model_response", phase=phase or stage, step_id=step_id,
                    role=role,
                    model={"id": model.get("id"), "name": model.get("name"),
                           "provider": provider, "model": model.get("model")},
                    response={"text": text, "parsed": parsed,
                              "usage": _usage_dict(usage),
                              "latency_ms": int(latency_ms),
                              "http_status": int(http_status),
                              "stream": bool(stream)},
                    purpose=purpose, stage=stage, attempt=int(attempt),
                    repair=bool(repair), status="success")

    def model_error(self, *, role: str, model: dict[str, Any], provider: str,
                    purpose: str, stage: str, attempt: int, exc: Exception,
                    latency_ms: int = 0, http_status: int = 0,
                    response_body: str = "", retryable: bool = True,
                    repair: bool = False, phase: str = "",
                    step_id: str = "") -> None:
        self.append("model_error", phase=phase or stage, step_id=step_id,
                    role=role,
                    model={"id": model.get("id"), "name": model.get("name"),
                           "provider": provider, "model": model.get("model")},
                    purpose=purpose, stage=stage, attempt=int(attempt),
                    repair=bool(repair),
                    error={"type": type(exc).__name__, "message": str(exc),
                           "http_status": int(http_status or getattr(exc, "http_status", 0) or 0),
                           "response_body": response_body or getattr(exc, "response_body", ""),
                           "retryable": bool(retryable)},
                    latency_ms=int(latency_ms), status="failed")

    def validation(self, *, phase: str, step_id: str = "", name: str,
                   ok: bool, errors: Any = None, details: Any = None) -> None:
        self.append("validation", phase=phase, step_id=step_id,
                    name=name, ok=bool(ok), errors=errors or [], details=details,
                    status="passed" if ok else "failed")

    def tool_call(self, *, phase: str, tool: str, version: str = "",
                  arguments: Any = None, decision: Any = None,
                  tool_call_id: str = "", sandbox: Any = None,
                  step_id: str = "") -> None:
        self.append("tool_call", phase=phase, step_id=step_id,
                    tool=tool, version=version,
                    tool_call_id=tool_call_id, arguments=arguments,
                    decision=decision, sandbox=sandbox, status="running")

    def tool_result(self, *, phase: str, tool: str, version: str = "",
                    tool_call_id: str = "", result: Any = None,
                    exit_code: int | None = None, duration_ms: int = 0,
                    stdout: str = "", stderr: str = "", error: str = "",
                    artifacts: Any = None, status: str = "done",
                    step_id: str = "") -> None:
        self.append("tool_result", phase=phase, step_id=step_id,
                    tool=tool, version=version, tool_call_id=tool_call_id,
                    result=result, exit_code=exit_code, duration_ms=int(duration_ms),
                    stdout=stdout, stderr=stderr, error=error,
                    artifacts=artifacts, status=status)

    def error(self, *, phase: str, code: str = "", message: str,
              exc: Exception | None = None, step_id: str = "",
              retryable: bool = False, details: Any = None) -> None:
        self.append("error", phase=phase, step_id=step_id,
                    name=code or "error", code=code or type(exc).__name__ if exc else "error",
                    message=message, error_type=type(exc).__name__ if exc else "",
                    stack=(_format_exception(exc) if exc else ""),
                    retryable=bool(retryable), details=details, status="failed")

    def finish(self, status: str = "success", *, error_code: str = "",
               error_summary: str = "", meta: Any = None) -> None:
        if not self.enabled or self._closed:
            return
        self.status = status
        self.append("run_end", status=status, error_code=error_code,
                    error_summary=error_summary, meta=meta or {})
        with self._lock:
            self._closed = True
            if self._fh:
                try:
                    os.fsync(self._fh.fileno())
                except OSError:
                    pass
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
        try:
            self.store.db.execute(
                "UPDATE trace_runs SET status=?, finished_at=?, event_count=?, "
                "model_calls=?, tool_calls=?, error_code=?, error_summary=?, "
                "size_bytes=?, updated_at=? WHERE id=?",
                (status, iso_now(), self._seq, self._model_calls, self._tool_calls,
                 error_code[:200], error_summary[:4000], self._size, iso_now(),
                 self.run_id))
        except Exception:
            pass


class TraceStore:
    def __init__(self, db: Database, config: Any) -> None:
        self.db = db
        self.config = config
        self.root = Path(config.data_dir) / "traces"
        self.max_event_mb = max(1, int(config.get("trace_max_event_mb", 4)))
        self.max_run_mb = max(1, int(config.get("trace_max_run_mb", 64)))
        self.max_string_bytes = 256 * 1024
        self._recover()

    def _recover(self) -> None:
        """进程重启后没有 run_end 的轨迹标记为中断，不能继续显示 running。"""
        try:
            rows = self.db.all(
                "SELECT id FROM trace_runs WHERE status='running' "
                "ORDER BY started_at DESC LIMIT 200")
            for row in rows:
                self.db.execute(
                    "UPDATE trace_runs SET status='interrupted', finished_at=?, "
                    "error_code='interrupted', error_summary=?, updated_at=? WHERE id=?",
                    (iso_now(), "服务重启导致轨迹中断，未生成 run_end",
                     iso_now(), row["id"]))
        except Exception:
            pass

    def start(self, *, kind: str, ref_id: str, user_id: int,
              meta: dict[str, Any] | None = None) -> TraceRecorder:
        enabled = bool(self.config.get("trace_enabled", True))
        attempt_no = 1
        try:
            attempt_no = int(self.db.one(
                "SELECT COUNT(*) AS n FROM trace_runs WHERE kind=? AND ref_id=?",
                (kind, str(ref_id)))["n"]) + 1
        except Exception:
            pass
        run_id = new_id("TR", with_time=True)
        now = datetime.now(timezone.utc)
        rel = Path("traces") / now.strftime("%Y") / now.strftime("%m") / f"{run_id}.jsonl"
        path = Path(self.config.data_dir) / rel
        row = {
            "id": run_id, "kind": kind, "ref_id": str(ref_id),
            "user_id": int(user_id or 0), "attempt_no": attempt_no,
            "status": "running" if enabled else "disabled",
            "started_at": iso_now(), "trace_path": str(rel), "meta": meta or {},
        }
        try:
            self.db.execute(
                "INSERT INTO trace_runs(id,kind,ref_id,user_id,attempt_no,status,"
                "started_at,event_count,model_calls,tool_calls,error_code,error_summary,"
                "trace_path,size_bytes,meta_json) VALUES(?,?,?,?,?,?,?,0,0,0,'','',?,0,?)",
                (run_id, kind, str(ref_id), int(user_id or 0), attempt_no,
                 "running" if enabled else "disabled", iso_now(), str(rel),
                 json.dumps(meta or {}, ensure_ascii=False, default=str)))
        except Exception:
            pass
        return TraceRecorder(self, row, path, enabled=enabled)

    def cleanup(self) -> int:
        """按 trace_retention_days 清理过期轨迹文件和索引；返回删除条数。"""
        try:
            days = max(1, int(self.config.get("trace_retention_days", 90)))
        except (TypeError, ValueError):
            days = 90
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        removed = 0
        try:
            rows = self.db.all(
                "SELECT id,trace_path FROM trace_runs WHERE started_at<?",
                (cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        except Exception:
            return 0
        for row in rows:
            path = (Path(self.config.data_dir) / str(row.get("trace_path") or "")).resolve()
            try:
                if path.is_file() and self.root.resolve() in path.parents:
                    path.unlink()
            except OSError:
                pass
            try:
                self.db.execute("DELETE FROM trace_runs WHERE id=?", (row["id"],))
                removed += 1
            except Exception:
                pass
        return removed

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM trace_runs WHERE id=?", (run_id,))
        if not row:
            return None
        row["meta"] = _load_json(row.pop("meta_json", "{}"), {})
        row["trace_path_abs"] = str(Path(self.config.data_dir) / str(row.get("trace_path") or ""))
        return row

    def list(self, *, kind: str = "", ref_id: str = "", status: str = "",
              limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trace_runs"
        args: list[Any] = []
        where: list[str] = []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if ref_id:
            where.append("ref_id=?")
            args.append(str(ref_id))
        if status:
            where.append("status=?")
            args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        rows = self.db.all(sql, tuple(args))
        for row in rows:
            row["meta"] = _load_json(row.pop("meta_json", "{}"), {})
        return rows

    def events(self, run_id: str, *, after: int = 0, limit: int = 300,
               type_filter: str = "", phase: str = "", only_failed: bool = False) -> dict[str, Any]:
        run = self.get(run_id)
        if not run:
            raise KeyError("轨迹不存在")
        path = Path(run["trace_path_abs"])
        items: list[dict[str, Any]] = []
        total = 0
        if not path.is_file():
            return {"run": run, "items": [], "total": 0, "next": 0}
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                total += 1
                seq = int(event.get("seq") or 0)
                if seq <= after:
                    continue
                if type_filter and event.get("type") != type_filter:
                    continue
                if phase and event.get("phase") != phase:
                    continue
                if only_failed:
                    failed = (event.get("status") == "failed"
                              or event.get("type") in ("error", "model_error")
                              or (event.get("type") == "validation" and not event.get("ok")))
                    if not failed:
                        continue
                if len(items) < max(1, min(int(limit), 1000)):
                    items.append(event)
        next_seq = int(items[-1].get("seq") or after) if items else after
        return {"run": run, "items": items, "total": total, "next": next_seq}

    def summary(self, run_id: str) -> dict[str, Any]:
        run = self.get(run_id)
        if not run:
            raise KeyError("轨迹不存在")
        events = self.events(run_id, limit=100000)["items"]
        errors = [e for e in events
                  if e.get("type") in ("error", "model_error")
                  or e.get("status") == "failed"
                  or (e.get("type") == "validation" and not e.get("ok"))]
        first_error = errors[0] if errors else None
        last_model = next((e for e in reversed(events)
                           if e.get("type") == "model_response"), None)
        return {
            "run": run,
            "event_count": len(events),
            "errors": errors[:20],
            "first_error": first_error,
            "last_model_response": last_model,
            "run_end": next((e for e in reversed(events) if e.get("type") == "run_end"), None),
        }


def _trim_strings(value: Any, max_bytes: int) -> Any:
    if isinstance(value, str):
        if len(value.encode("utf-8")) <= max_bytes:
            return value
        return value[:max_bytes] + f"\n...[truncated {len(value) - max_bytes} chars]"
    if isinstance(value, list):
        return [_trim_strings(v, max_bytes) for v in value]
    if isinstance(value, dict):
        return {str(k): _trim_strings(v, max_bytes) for k, v in value.items()}
    return value


def _trim_event_to_fit(event: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """超长事件保留结构、错误和状态，优先压缩大文本字段。"""
    out = dict(event)
    for key in ("request", "response", "stdout", "stderr", "stack", "result", "details"):
        if key not in out:
            continue
        value = out[key]
        if isinstance(value, dict):
            out[key] = {k: (_truncate_value(v, 32 * 1024) if isinstance(v, str) else v)
                        for k, v in value.items()}
        elif isinstance(value, str):
            out[key] = _truncate_value(value, 32 * 1024)
    while len(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8")) > max_bytes:
        # 极端情况下只保留定位字段，页面仍可看到失败点。
        out = {k: v for k, v in out.items()
               if k in ("v", "seq", "ts", "type", "run_id", "kind", "ref_id", "attempt_no",
                        "phase", "step_id", "status", "role", "model", "error", "name",
                        "message", "code")}
        if len(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8")) <= max_bytes:
            break
        out.pop("model", None)
    return out


def _truncate_value(value: str, max_bytes: int) -> str:
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    return value[:max_bytes] + f"\n...[truncated {len(value) - max_bytes} chars]"


def _load_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return default


def _usage_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    return {"input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0)}


def _format_exception(exc: Exception | None) -> str:
    if exc is None:
        return ""
    import traceback
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-20000:]


def _redact_url(url: str) -> str:
    if not url:
        return ""
    # 只保留协议/主机/路径，查询参数可能包含临时签名或 key。
    return url.split("?", 1)[0][:500]
