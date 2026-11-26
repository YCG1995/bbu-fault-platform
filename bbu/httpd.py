"""标准库 HTTP 服务：路由、流式 multipart、SSE、静态资源、安全头。

不引入 FastAPI/uvicorn，保证离线部署零 pip 依赖。
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from .util import safe_filename, within_directory


class HTTPError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


class StreamReader:
    """带缓冲的流读取器，支持按分隔符流式拷贝，避免大文件进内存。"""

    def __init__(self, rfile, limit: int = 0) -> None:
        self.rfile = rfile
        self.buf = b""
        self.limit = limit
        self.total = 0

    def _fill(self, size: int = 65536) -> bool:
        if self.limit:
            remaining = self.limit - self.total
            if remaining <= 0:
                return False
            size = min(size, remaining)
        chunk = self.rfile.read(size)
        if not chunk:
            return False
        self.total += len(chunk)
        if self.limit and self.total > self.limit:
            raise HTTPError(413, f"请求体超过上限 {self.limit // 1024 // 1024} MB")
        self.buf += chunk
        return True

    def readline(self, max_len: int = 8192) -> bytes:
        while b"\n" not in self.buf:
            if len(self.buf) > max_len:
                raise HTTPError(400, "请求头过长")
            if not self._fill():
                break
        idx = self.buf.find(b"\n")
        if idx < 0:
            line, self.buf = self.buf, b""
            return line
        line = self.buf[:idx]
        self.buf = self.buf[idx + 1:]
        return line

    def read(self, size: int) -> bytes:
        while len(self.buf) < size and self._fill(size - len(self.buf)):
            pass
        data, self.buf = self.buf[:size], self.buf[size:]
        return data

    def copy_until(self, delim: bytes, out, max_bytes: int = 0) -> int:
        written = 0
        keep = len(delim) - 1
        while True:
            idx = self.buf.find(delim)
            if idx >= 0:
                out.write(self.buf[:idx])
                written += idx
                self.buf = self.buf[idx + len(delim):]
                return written
            if len(self.buf) > keep:
                out.write(self.buf[:-keep] if keep else self.buf)
                written += len(self.buf) - keep
                self.buf = self.buf[-keep:] if keep else b""
            if max_bytes and written > max_bytes:
                raise HTTPError(413, f"上传文件超过上限 {max_bytes // 1024 // 1024} MB")
            if not self._fill():
                raise HTTPError(400, "multipart 请求体不完整")


@dataclass
class UploadedFile:
    field: str
    filename: str
    path: Path
    size: int
    content_type: str = ""


class Request:
    def __init__(self, handler: "Handler", app: Any) -> None:
        self.handler = handler
        self.app = app
        parsed = urlparse(handler.path)
        self.path = unquote(parsed.path)
        self.query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.headers = handler.headers
        self.method = handler.command
        self.body: bytes | None = None
        self.user: dict[str, Any] | None = None
        self.session: dict[str, Any] | None = None
        self.ip = handler.client_address[0] if handler.client_address else ""
        self._json: Any = None
        self._files: list[UploadedFile] | None = None
        self._fields: dict[str, str] | None = None

    # ---------- body ----------
    def read_body(self, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        if self.body is not None:
            return self.body
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_bytes:
            raise HTTPError(413, f"请求体超过上限 {max_bytes // 1024 // 1024} MB")
        self.body = self.handler.rfile.read(length) if length else b""
        return self.body

    def json(self, max_bytes: int = 8 * 1024 * 1024) -> Any:
        if self._json is None:
            raw = self.read_body(max_bytes)
            if not raw:
                self._json = {}
            else:
                try:
                    self._json = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HTTPError(400, f"JSON 解析失败: {exc}") from exc
        return self._json

    def parse_multipart(self, max_bytes: int, max_file_bytes: int = 0) -> tuple[dict[str, str], list[UploadedFile]]:
        ctype = self.headers.get("Content-Type", "")
        match = re.search(r'boundary="?([^";]+)"?', ctype, re.I)
        if "multipart/form-data" not in ctype.lower() or not match:
            raise HTTPError(400, "需要 multipart/form-data")
        boundary = match.group(1).encode("latin-1")
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_bytes:
            raise HTTPError(413, f"上传总大小超过上限 {max_bytes // 1024 // 1024} MB")
        tmp_dir = Path(self.app.config.data_dir) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        reader = StreamReader(self.handler.rfile, limit=length)
        fields: dict[str, str] = {}
        files: list[UploadedFile] = []
        # 读首边界
        line = reader.readline()
        while line and line.strip() != b"--" + boundary:
            if line.strip() == b"--" + boundary + b"--":
                return fields, files
            line = reader.readline()
        while True:
            headers_blob = b""
            while True:
                line = reader.readline(16384)
                if not line:
                    raise HTTPError(400, "multipart 头不完整")
                if not line.strip():
                    break
                headers_blob += line
            disposition = ""
            content_type = ""
            for header_line in headers_blob.decode("utf-8", errors="replace").split("\r\n"):
                if header_line.lower().startswith("content-disposition:"):
                    disposition = header_line
                elif header_line.lower().startswith("content-type:"):
                    content_type = header_line.split(":", 1)[1].strip()
            name_match = re.search(r'name="([^"]*)"', disposition)
            filename_match = re.search(r'filename="([^"]*)"', disposition)
            name = name_match.group(1) if name_match else ""
            filename = filename_match.group(1) if filename_match else ""
            if filename:
                filename = unquote(filename)
                tmp_path = tmp_dir / f"upload-{uuid.uuid4().hex}.part"
                with open(tmp_path, "wb") as out:
                    size = reader.copy_until(b"\r\n--" + boundary, out, max_file_bytes)
                files.append(UploadedFile(field=name, filename=filename, path=tmp_path,
                                          size=size, content_type=content_type))
            else:
                import io
                buf = io.BytesIO()
                reader.copy_until(b"\r\n--" + boundary, buf, 1024 * 1024)
                fields[name] = buf.getvalue().decode("utf-8", errors="replace")
            # 边界后：-- 表示结束，否则 CRLF 表示下一段
            tail = reader.read(2)
            if tail == b"--":
                # 消费结尾 CRLF
                reader.readline()
                break
            if tail != b"\r\n":
                # 兼容 \n
                if tail[:1] == b"\n":
                    continue
                raise HTTPError(400, "multipart 边界异常")
        self._fields = fields
        self._files = files
        return fields, files

    def cleanup_files(self) -> None:
        for f in self._files or []:
            try:
                f.path.unlink(missing_ok=True)
            except OSError:
                pass


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, func: Callable[..., Any]) -> None:
        self.routes.append((method.upper(), re.compile("^" + pattern + "$"), func))

    def match(self, method: str, path: str) -> tuple[Callable[..., Any], dict[str, str]] | None:
        for m, regex, func in self.routes:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                return func, match.groupdict()
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BBUFaultPlatform"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        app = self.server.app  # type: ignore[attr-defined]
        req = Request(self, app)
        try:
            gate = getattr(app, "migration_gate", None)
            if method in ("POST", "PUT", "PATCH", "DELETE") and gate and gate.active:
                raise HTTPError(503, "系统正在执行工作目录切换或数据迁移，请稍后重试")
            route = app.router.match(method, req.path)
            if not route:
                if method in ("GET", "HEAD") and not req.path.startswith("/api/"):
                    return self._serve_static(req)
                raise HTTPError(404, "接口不存在")
            func, params = route
            result = func(app, req, **params)
            if result is None:
                return
            status, headers, body = result
            if isinstance(body, FileBody):
                self._respond_file(status, headers, body, head=(method == "HEAD"))
            else:
                self._respond(status, headers, body, head=(method == "HEAD"))
        except HTTPError as exc:
            payload = {"error": exc.message}
            if exc.detail is not None:
                payload["detail"] = exc.detail
            self._json_response(req, payload, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json_response(req, {"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            req.cleanup_files()

    # ---------- 响应 ----------
    def _security_headers(self) -> dict[str, str]:
        return {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                       "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                                       "connect-src 'self'; frame-src 'self'; object-src 'none'",
        }

    def _respond(self, status: int, headers: dict[str, str], body: bytes,
                 head: bool = False) -> None:
        self.send_response(status)
        merged = self._security_headers()
        merged.update(headers)
        for key, value in merged.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _respond_file(self, status: int, headers: dict[str, str], body: "FileBody",
                      head: bool = False) -> None:
        path = body.path
        if not path.is_file():
            raise HTTPError(404, "文件不存在")
        self.send_response(status)
        merged = self._security_headers()
        merged.update(headers)
        for key, value in merged.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        if head:
            return
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _json_response(self, req: Request, payload: Any, status: int = 200,
                       extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
        if extra:
            headers.update(extra)
        self._respond(status, headers, body)

    def _serve_static(self, req: Request) -> None:
        app = self.server.app  # type: ignore[attr-defined]
        web_dir = Path(app.config.get("_root")) / "web"
        rel = req.path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        path = (web_dir / rel).resolve()
        if not within_directory(web_dir, path) or not path.is_file():
            # 只有前端路由才回退 index.html；缺失的静态资源必须返回 404，
            # 否则 /favicon.ico、/assets/*.js 会拿到 HTML，浏览器继续使用旧图标。
            if rel.startswith("assets/") or Path(rel).suffix or rel == "favicon.ico":
                raise HTTPError(404, "静态资源不存在")
            path = web_dir / "index.html"
            if not path.is_file():
                raise HTTPError(404, "前端资源不存在")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        body = path.read_bytes()
        headers = {"Content-Type": ctype, "Cache-Control": "no-cache"}
        self._respond(200, headers, body, head=(req.method == "HEAD"))

    def _sse(self, req: Request, task_id: str, last_id: int) -> None:
        """SSE：轮询数据库事件表，天然支持断线重连与多进程。"""
        self.send_response(200)
        headers = {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        merged = self._security_headers()
        merged.update(headers)
        for k, v in merged.items():
            self.send_header(k, v)
        self.end_headers()
        last_heartbeat = time.time()
        try:
            while True:
                rows = req.app.db.all(
                    "SELECT seq, ts, level, stage, message, data_json FROM task_events "
                    "WHERE task_id=? AND seq>? ORDER BY seq ASC LIMIT 200",
                    (task_id, last_id))
                if rows:
                    for row in rows:
                        last_id = row["seq"]
                        payload = json.dumps({
                            "seq": row["seq"], "ts": row["ts"], "level": row["level"],
                            "stage": row["stage"], "message": row["message"],
                            "data": json.loads(row["data_json"] or "{}"),
                        }, ensure_ascii=False, default=str)
                        self.wfile.write(f"id: {row['seq']}\nevent: task\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    last_heartbeat = time.time()
                task = req.app.db.one("SELECT status FROM tasks WHERE id=?", (task_id,))
                if not task:
                    break
                if task["status"] in ("done", "failed", "cancelled", "needs_data") and not rows:
                    break
                if time.time() - last_heartbeat > 15:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.time()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.close_connection = True

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/assets/"):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], app: Any) -> None:
        self.app = app
        super().__init__(address, Handler)


@dataclass
class FileBody:
    path: Path


def response_json(data: Any, status: int = 200,
                  headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
    if headers:
        h.update(headers)
    return status, h, body


def response_file(path: Path, content_type: str, download_name: str | None = None,
                  inline: bool = True) -> tuple[int, dict[str, str], "FileBody"]:
    if not path.is_file():
        raise HTTPError(404, "文件不存在")
    headers = {"Content-Type": content_type, "Cache-Control": "no-store"}
    if download_name:
        disposition = "inline" if inline else "attachment"
        headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(download_name)}"
    return 200, headers, FileBody(path)
