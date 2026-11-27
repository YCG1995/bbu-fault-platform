#!/usr/bin/env python3
"""全量 REST 接口冒烟：逐条覆盖 bbu/api.py 注册的所有路由。

启动一个临时实例，使用本地假模型 endpoint，不污染主 data/。覆盖：
认证/权限/CSRF、任务全生命周期、报告与 SSE、技能库、工坊、模型、用户、审计、系统信息。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
CURRENT_VERSION = re.search(
    r'__version__\s*=\s*"([^"]+)"',
    (ROOT / "bbu" / "__init__.py").read_text(encoding="utf-8")).group(1)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

EXPECTED_ROUTES = {
    "GET /api/health",
    "GET /api/public/config",
    "POST /api/auth/login",
    "POST /api/auth/logout",
    "GET /api/auth/me",
    "POST /api/auth/password",
    "POST /api/auth/activate",
    "POST /api/auth/register",
    "POST /api/auth/refresh",
    "GET /api/auth/sessions",
    "POST /api/auth/sessions/revoke-others",
    "DELETE /api/auth/sessions/{id}",
    "GET /api/stats",
    "GET /api/notifications",
    "POST /api/notifications/{id}/read",
    "POST /api/notifications/read-all",
    "GET /api/search",
    "GET /api/metrics",
    "GET /api/metrics/prometheus",
    "GET /api/usage/overview",
    "GET /api/usage/tasks",
    "GET /api/cases",
    "GET /api/cases/{id}",
    "POST /api/cases/{id}/label",
    "POST /api/cases/{id}/assign-reviewer",
    "GET /api/cases/{id}/history",
    "GET /api/evaluations",
    "POST /api/evaluations/run",
    "POST /api/tasks/{id}/review",
    "GET /api/quality",
    "GET /api/task-drafts/current",
    "POST /api/task-drafts",
    "PUT /api/task-drafts/{id}",
    "DELETE /api/task-drafts/{id}",
    "POST /api/task-drafts/{id}/files",
    "DELETE /api/task-drafts/{id}/files/{file}",
    "GET /api/tasks",
    "POST /api/tasks",
    "GET /api/tasks/{id}",
    "GET /api/tasks/{id}/events",
    "GET /api/tasks/{id}/questions",
    "GET /api/tasks/{id}/session",
    "GET /api/tasks/{id}/evidence",
    "GET /api/tasks/{id}/agent-trace",
    "GET /api/tasks/{id}/recovery-runs",
    "POST /api/tasks/{id}/recovery-runs",
    "POST /api/recovery-runs/{id}/observe",
    "POST /api/recovery-runs/{id}/review",
    "POST /api/tasks/{id}/tool-approvals",
    "POST /api/tasks/{id}/inputs",
    "POST /api/tasks/{id}/continue",
    "GET /api/tasks/{id}/stream",
    "POST /api/tasks/{id}/cancel",
    "POST /api/tasks/{id}/retry",
    "POST /api/tasks/{id}/release",
    "GET /api/tasks/{id}/storage-jobs",
    "GET /api/storage/overview",
    "GET /api/backups",
    "DELETE /api/backups/{name}",
    "GET /api/storage/jobs/{id}",
    "POST /api/tasks/{id}/assign-reviewer",
    "GET /api/tasks/{id}/report",
    "GET /api/tasks/{id}/skills/{skill}/report",
    "GET /api/skills",
    "GET /api/skills/{name}",
    "POST /api/skills/scan",
    "POST /api/skills/{name}/disable",
    "POST /api/skills/{name}/enable",
    "POST /api/skills/import",
    "GET /api/skills/{name}/export",
    "GET /api/skills/{name}/manifest",
    "POST /api/skills/{name}/manifest/sign",
    "POST /api/skills/{name}/rollback",
    "POST /api/skills/{name}/channel",
    "GET /api/workshop/recipes",
    "GET /api/workshop/drafts",
    "POST /api/workshop/drafts",
    "GET /api/workshop/drafts/{id}",
    "GET /api/workshop/drafts/{id}/provenance",
    "POST /api/workshop/drafts/{id}/provenance/lock",
    "PUT /api/workshop/drafts/{id}",
    "DELETE /api/workshop/drafts/{id}",
    "POST /api/workshop/drafts/{id}/logs",
    "POST /api/workshop/drafts/{id}/analyze",
    "POST /api/workshop/drafts/{id}/refine",
    "POST /api/workshop/drafts/{id}/check",
    "POST /api/workshop/drafts/{id}/verify-rules",
    "POST /api/workshop/drafts/{id}/rules/try",
    "POST /api/workshop/drafts/{id}/build",
    "POST /api/workshop/drafts/{id}/publish",
    "POST /api/workshop/drafts/{id}/case",
    "GET /api/workshop/drafts/{id}/cases",
    "GET /api/workshop/drafts/{id}/jobs",
    "GET /api/workshop/jobs/{id}",
    "GET /api/calibration",
    "POST /api/calibration/fit",
    "GET /api/case-matrix",
    "POST /api/case-matrix/rebuild",
    "GET /api/experience-candidates",
    "POST /api/experience-candidates/{id}/decide",
    "GET /api/skill-versions",
    "GET /api/models",
    "POST /api/models",
    "PUT /api/models/{id}",
    "DELETE /api/models/{id}",
    "POST /api/models/{id}/test",
    "POST /api/models/{id}/capabilities",
    "POST /api/models/{id}/relay-test",
    "GET /api/models/{id}/relay-test/{test_id}",
    "GET /api/relay/status",
    "GET /api/relay/calls",
    "POST /api/relay/calls/{call_id}/result",
    "POST /api/relay/release",
    "POST /api/models/discover",
    "POST /api/models/test",
    "GET /api/users",
    "POST /api/users",
    "GET /api/users/{id}",
    "PUT /api/users/{id}",
    "GET /api/users/invites",
    "POST /api/users/invites",
    "DELETE /api/users/invites/{id}",
    "POST /api/users/batch-disable",
    "POST /api/users/{id}/force-logout",
    "GET /api/profile",
    "PUT /api/profile",
    "GET /api/profile/model-key",
    "PUT /api/profile/model-key",
    "DELETE /api/profile/model-key",
    "POST /api/profile/model-key/test",
    "GET /api/audit",
    "GET /api/audit/export",
    "GET /api/traces",
    "GET /api/traces/{run_id}/summary",
    "GET /api/traces/{run_id}/events",
    "GET /api/traces/{run_id}/download",
    "GET /api/tool-calls",
    "GET /api/tool-approvals",
    "POST /api/tool-approvals/{id}/decide",
    "GET /api/skills/{name}/tool",
    "GET /api/system/info",
    "GET /api/system/runtime-config",
    "PUT /api/system/runtime-config",
    "POST /api/system/restart",
    "POST /api/system/updates/upload",
    "GET /api/system/updates",
    "GET /api/system/updates/{id}",
    "POST /api/system/updates/{id}/verify",
    "POST /api/system/updates/{id}/apply",
    "POST /api/system/updates/rollback",
    "GET /api/system/migrations",
    "POST /api/system/migrations/workspace/preflight",
    "POST /api/system/migrations/workspace",
    "POST /api/system/migrations/export",
    "POST /api/system/migrations/import",
    "GET /api/system/migrations/{migration_id}",
    "GET /api/system/migrations/{migration_id}/download",
    "DELETE /api/system/migrations/{migration_id}",
    "GET /mcp",
    "POST /mcp",
    "GET /api/assistant/status",
    "GET /api/assistant/suggestions",
    "POST /api/assistant/chat",
    "POST /api/assistant/actions/{id}/confirm",
    "POST /api/assistant/actions/{id}/cancel",
    "GET /api/kb/status",
    "GET /api/kb/facets",
    "GET /api/kb/docs",
    "POST /api/kb/docs",
    "GET /api/kb/docs/{id}",
    "POST /api/kb/docs/{id}/restore",
    "GET /api/kb/cases/{case_id}",
    "PUT /api/kb/cases/{case_id}",
    "POST /api/kb/cases/{case_id}/status",
    "GET /api/kb/cases/{case_id}/revisions",
    "GET /api/kb/cases/{case_id}/revisions/{revision}",
    "POST /api/kb/cases/{case_id}/revisions/{revision}/restore",
    "GET /api/kb/cases/{case_id}/revisions/verify",
    "PUT /api/kb/docs/{id}",
    "POST /api/kb/docs/{id}/status",
    "DELETE /api/kb/docs/{id}",
    "GET /api/kb/docs/{id}/versions",
    "POST /api/kb/docs/{id}/versions/{version_id}/restore",
    "GET /api/kb/files/{id}",
    "POST /api/kb/import",
    "GET /api/kb/import/jobs",
    "GET /api/kb/import/jobs/{id}",
    "POST /api/kb/import/jobs/{id}/cancel",
    "GET /api/kb/drafts",
    "GET /api/kb/drafts/{id}",
    "PUT /api/kb/drafts/{id}",
    "POST /api/kb/drafts/{id}/approve",
    "POST /api/kb/drafts/{id}/reject",
    "POST /api/kb/chat",
    "POST /api/kb/feedback",
    "POST /api/kb/reindex",
    "GET /api/kb/queries",
    "POST /api/kb/sample",
    "GET /api/kb/audit",
    "GET /api/kb/stats",
    "GET /api/kb/reviews",
    "GET /api/kb/reviews/{review_id}",
    "POST /api/kb/reviews",
    "POST /api/kb/reviews/run",
    "POST /api/kb/reviews/{review_id}/decision",
    "POST /api/kb/reviews/{review_id}/retry",
    "GET /api/kb/search",
    "GET /api/mcp/info",
    "POST /api/mcp/token",
    "DELETE /api/mcp/token",
}


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Client:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.cookie = ""
        self.csrf = ""

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None,
                timeout: int = 30) -> tuple[int, bytes, dict[str, str]]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        all_headers = {"Cookie": self.cookie} if self.cookie else {}
        all_headers.update(headers or {})
        conn.request(method, path, body=body, headers=all_headers)
        resp = conn.getresponse()
        raw = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        set_cookie = hdrs.get("set-cookie")
        if set_cookie and set_cookie.startswith("bbu_session="):
            self.cookie = set_cookie.split(";", 1)[0]
        status = resp.status
        conn.close()
        return status, raw, hdrs

    def json(self, method: str, path: str, payload: dict | None = None,
             expected: int = 200) -> tuple[int, dict]:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if method in ("POST", "PUT", "PATCH", "DELETE") and self.csrf:
            headers["X-CSRF"] = self.csrf
        status, raw, _ = self.request(method, path, body, headers)
        assert status == expected, f"{method} {path} 期望 {expected}，实际 {status}: {raw[:300]!r}"
        return status, json.loads(raw or b"{}")

    def multipart(self, path: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]],
                  expected: int = 200) -> tuple[int, dict]:
        boundary = "----bbu-api-" + os.urandom(8).hex()
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode())
        for field, filename, content, ctype in files:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.csrf:
            headers["X-CSRF"] = self.csrf
        status, raw, _ = self.request("POST", path, body, headers)
        assert status == expected, f"POST {path} 期望 {expected}，实际 {status}: {raw[:300]!r}"
        return status, json.loads(raw or b"{}")

    def login(self, username: str, password: str, expected: int = 200) -> dict:
        status, data = self.json("POST", "/api/auth/login",
                                 {"username": username, "password": password}, expected)
        if status == 200:
            self.csrf = data["csrf"]
        return data

    def task_upload(self, title: str, description: str, log_path: Path) -> str:
        content = log_path.read_bytes()
        _, data = self.multipart("/api/tasks", {
            "title": title, "description": description, "mode": "fast",
        }, [("files", log_path.name, content, "application/zip")], expected=201)
        return data["id"]


class _OpenAIMock(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send({"data": [{"id": "test-model"}, {"id": "test-model-2"}]})
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = {"choices": [{"message": {"content": "API-TEST-OK"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
        self._send(body)

    def _send(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class _PlannerMock(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        user = "\n".join(str(m.get("content") or "") for m in payload.get("messages") or [])
        description = user.split("## 用户故障描述", 1)[-1].split("## 日志盘点", 1)[0]
        if "## 已执行技能与证据结果" in user:
            plan = {"in_scope": True, "summary": "证据已足够，不补规划",
                    "hypothesis": [], "steps": [], "missing_info": []}
        elif "光模块" in description:
            plan = {
                "in_scope": True, "summary": "时钟与光模块并行首验",
                "hypothesis": ["时钟参考源失锁", "光模块收光链路异常"],
                "steps": [{"id": "P1", "title": "双方向并行首验",
                           "objective": "用两个独立技能同时验证",
                           "skills": ["demo-bbu-clock-loss", "demo-bbu-optical-rx"],
                           "gate": "always", "reason": "CLK032 与 RX_LOSS 同时出现"}],
                "missing_info": [],
            }
        else:
            plan = {
                "in_scope": True, "summary": "先验时钟直接证据",
                "hypothesis": ["参考时钟源失锁"],
                "steps": [{"id": "P1", "title": "时钟首验",
                           "objective": "确认 CLK032 / PLL unlock",
                           "skills": ["demo-bbu-clock-loss"],
                           "gate": "always", "reason": "描述出现时钟告警"}],
                "missing_info": [],
            }
        raw = json.dumps({"choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}],
                          "usage": {"prompt_tokens": 30, "completion_tokens": 10,
                                    "total_tokens": 40}}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


def start_mock(handler) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def wait_health(host: str, port: int) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                conn.close()
                return
            conn.close()
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError("服务未在 30 秒内就绪")


def wait_task(client: Client, task_id: str, timeout: float = 90) -> dict:
    deadline = time.time() + timeout
    task = {}
    while time.time() < deadline:
        _, task = client.json("GET", f"/api/tasks/{task_id}")
        if task["status"] in ("done", "failed", "needs_data", "cancelled"):
            return task
        time.sleep(0.4)
    raise AssertionError(f"任务 {task_id} 超时未结束: {task.get('status')}")


def make_update_package(tmp: Path, version: str) -> Path:
    """构造满足预检结构的最小离线包（不会被 fake helper 真正安装）。"""
    root = tmp / "update-package" / "bbu-fault-platform"
    for rel in ("run.py", "vendor/yaml/__init__.py", "web/index.html",
                "deploy/upgrade_offline.sh", "deploy/rollback_offline.sh", "docs"):
        src = ROOT / rel
        dst = root / rel
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    (root / "bbu").mkdir(parents=True, exist_ok=True)
    text = (ROOT / "bbu/__init__.py").read_text(encoding="utf-8")
    text = re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"', text)
    (root / "bbu/__init__.py").write_text(text, encoding="utf-8")
    (root / "docs" / f"版本说明-{version}.md").write_text("# fake update\n", encoding="utf-8")
    out = tmp / f"bbu-fault-platform-{version}-offline.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(root, arcname="bbu-fault-platform")
    return out


def main() -> int:
    hits: set[str] = set()

    def hit(name: str) -> None:
        hits.add(name)

    port = free_port()
    host = "127.0.0.1"
    work = Path(tempfile.mkdtemp(prefix="bbu-api-full-"))
    data_dir = work / "data"
    skills_dir = work / "skills"
    db_path = data_dir / "bbu.db"
    fake_helper = work / "fake_upgrade_helper.py"
    fake_helper.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        f"state = Path({str(work / 'data').__repr__()}).parent\n"
        "job = sys.argv[1]\n"
        "job_dir = state / 'updates' / job\n"
        "request = json.loads((job_dir / 'request.json').read_text())\n"
        "status_path = job_dir / 'status.json'\n"
        "status = json.loads(status_path.read_text())\n"
        "if request.get('action') == 'upgrade':\n"
        "    backup = state / 'backups' / ('fake-' + job)\n"
        "    backup.mkdir(parents=True, exist_ok=True)\n"
        "    (backup / 'backup.json').write_text(json.dumps({'old_version': __CURRENT_VERSION__, 'new_version': request.get('target_version')}))\n"
        "    status.update({'phase': 'succeeded', 'progress': 100, 'message': 'fake upgrade succeeded',\n"
        "                   'backup_dir': str(backup), 'error': ''})\n"
        "else:\n"
        "    status.update({'phase': 'rolled_back', 'progress': 100, 'message': 'fake rollback succeeded', 'error': ''})\n"
        "status['updated_at'] = '2026-01-01T00:00:00Z'\n"
        "(job_dir / 'status.json').write_text(json.dumps(status))\n".replace(
            "__CURRENT_VERSION__", repr(CURRENT_VERSION)),
        encoding="utf-8")
    env = os.environ.copy()
    env.update({"BBU_DATA_DIR": str(data_dir), "BBU_SKILLS_DIR": str(skills_dir),
                "BBU_CASES_DIR": str(work / "cases"),
                "BBU_MAX_WORKERS": "1", "PYTHONUNBUFFERED": "1",
                "BBU_CONFIG_FILE": str(work / "config.json"),
                "BBU_PERSONAL_MODEL_BASE_URL": "http://127.0.0.1:1/v1",
                "BBU_MODEL_DISCOVERY_ALLOW_PRIVATE": "1",
                "BBU_UPGRADE_HELPER_CMD": f"{sys.executable} {fake_helper}"})
    log_file = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "run.py", "--host", host, "--port", str(port)],
                            cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT)
    openai_server = planner_server = None
    case_name = ""
    try:
        wait_health(host, port)
        admin_client = Client(host, port)

        # 公共接口 + 未登录保护
        status, raw, _ = admin_client.request("GET", "/api/health")
        assert status == 200 and json.loads(raw)["ok"]
        hit("GET /api/health")
        status, raw, _ = admin_client.request("GET", "/api/public/config")
        assert status == 200 and "brand" in json.loads(raw)
        hit("GET /api/public/config")
        status, _, _ = admin_client.request("GET", "/api/tasks")
        assert status == 401

        initial_password = next(
            line.split("密码:", 1)[1].strip()
            for line in (data_dir / "初始管理员密码.txt").read_text(encoding="utf-8").splitlines()
            if line.startswith("密码:"))
        admin_client.login("admin", "wrong-password", expected=401)
        hit("POST /api/auth/login")
        admin_client.login("admin", initial_password)
        _, me = admin_client.json("GET", "/api/auth/me")
        admin_id = me["user"]["id"]
        hit("GET /api/auth/me")
        status, raw, _ = admin_client.request("GET", "/api/tasks")
        assert status == 200
        assert json.loads(raw)["items"] == []
        hit("GET /api/tasks")  # 首次登录不再强制改密，可直接进入工作台
        _, changed = admin_client.json("POST", "/api/auth/password",
                                       {"old_password": initial_password,
                                        "new_password": "Platform!2026x"})
        assert changed["ok"]
        assert changed["other_sessions_revoked"] >= 0
        admin_client.csrf = changed["csrf"]
        hit("POST /api/auth/password")
        assert not (data_dir / "初始管理员密码.txt").exists()
        _, logout = admin_client.json("POST", "/api/auth/logout")
        assert logout["ok"]
        hit("POST /api/auth/logout")
        status, _, _ = admin_client.request("GET", "/api/auth/me")
        assert status == 401
        admin_client.login("admin", "Platform!2026x")
        _, me = admin_client.json("GET", "/api/auth/me")
        admin_id = me["user"]["id"]
        hit("GET /api/auth/me")
        status, raw, _ = admin_client.request("POST", "/api/tasks", b"{}",
                                              {"Content-Type": "application/json",
                                               "X-CSRF": admin_client.csrf})
        assert status == 400
        hit("POST /api/tasks")  # 缺描述错误路径

        _, sessions = admin_client.json("GET", "/api/auth/sessions")
        assert sessions["items"] and sessions["items"][0]["current"] is True
        hit("GET /api/auth/sessions")
        _, refreshed = admin_client.json("POST", "/api/auth/refresh")
        assert refreshed["ok"] and refreshed["expires_at"]
        hit("POST /api/auth/refresh")
        _, revoked = admin_client.json("POST", "/api/auth/sessions/revoke-others")
        assert revoked["ok"]
        hit("POST /api/auth/sessions/revoke-others")
        status, _, _ = admin_client.request("DELETE", "/api/auth/sessions/000000000000",
                                            headers={"X-CSRF": admin_client.csrf})
        assert status == 404
        hit("DELETE /api/auth/sessions/{id}")

        # 模型：OpenAI 兼容 CRUD + test；planner 保留给任务
        openai_server, openai_port = start_mock(_OpenAIMock)
        _, created = admin_client.json("POST", "/api/models", {
            "name": "api-openai", "provider": "openai",
            "base_url": f"http://127.0.0.1:{openai_port}/v1",
            "api_key": "api-test-key", "model": "test-model", "roles": ["default"],
        }, expected=201)
        model_id = created["model"]["id"]
        hit("POST /api/models")
        _, tested = admin_client.json("POST", f"/api/models/{model_id}/test")
        assert tested["ok"], tested
        hit("POST /api/models/{id}/test")
        _, caps = admin_client.json("POST", f"/api/models/{model_id}/capabilities")
        assert caps["capabilities"]["json_mode"] is True, caps
        hit("POST /api/models/{id}/capabilities")
        _, draft_tested = admin_client.json("POST", "/api/models/test", {
            "name": "draft-test", "provider": "openai",
            "base_url": f"http://127.0.0.1:{openai_port}/v1",
            "api_key": "api-test-key", "model": "test-model", "roles": ["default"],
        })
        assert draft_tested["ok"], draft_tested
        hit("POST /api/models/test")
        _, discovered = admin_client.json("POST", "/api/models/discover", {
            "provider": "openai", "base_url": f"http://127.0.0.1:{openai_port}/v1",
            "api_key": "api-test-key",
        })
        assert discovered["models"] == ["test-model", "test-model-2"], discovered
        _, discovered_existing = admin_client.json("POST", "/api/models/discover", {
            "model_id": model_id, "provider": "openai",
            "base_url": f"http://127.0.0.1:{openai_port}/v1",
        })
        assert discovered_existing["models"] == ["test-model", "test-model-2"], discovered_existing
        hit("POST /api/models/discover")
        _, updated = admin_client.json("PUT", f"/api/models/{model_id}",
                                       {"name": "api-openai-updated", "priority": 1})
        assert updated["model"]["name"] == "api-openai-updated"
        hit("PUT /api/models/{id}")
        admin_client.json("DELETE", f"/api/models/{model_id}")
        hit("DELETE /api/models/{id}")

        # 浏览器代理：状态/长轮询/回传/释放/管理员自检
        _, relay_status = admin_client.json("GET", "/api/relay/status")
        assert relay_status["enabled"] is True and relay_status["window"]["window_id"]
        relay_window = relay_status["window"]["window_id"]
        hit("GET /api/relay/status")
        status, _, _ = admin_client.request(
            "GET", f"/api/relay/calls?window_id={relay_window}&wait=0")
        assert status == 204, status
        hit("GET /api/relay/calls")
        admin_client.json("POST", "/api/relay/release",
                          {"window_id": relay_window})
        hit("POST /api/relay/release")
        admin_client.json("POST", "/api/relay/calls/not-a-call/result",
                          {"body_text": "", "status": 0}, expected=404)
        hit("POST /api/relay/calls/{call_id}/result")
        _, browser_model = admin_client.json("POST", "/api/models", {
            "name": "api-browser-relay", "provider": "openai",
            "base_url": "https://maas.example.com/v1",
            "api_key": "browser-test-key", "model": "browser-model",
            "roles": ["default"],
            "extra": {"transport": "browser",
                      "browser_relay": {"max_concurrent": 1,
                                        "claim_timeout_sec": 5,
                                        "exec_timeout_sec": 30,
                                        "allow_roles": []}},
        }, expected=201)
        browser_id = browser_model["model"]["id"]
        _, relay_test = admin_client.json(
            "POST", f"/api/models/{browser_id}/relay-test", expected=202)
        assert relay_test["state"] == "pending" and relay_test["test_id"]
        hit("POST /api/models/{id}/relay-test")
        relay_test_id = relay_test["test_id"]
        for _ in range(20):
            _, relay_test_detail = admin_client.json(
                "GET", f"/api/models/{browser_id}/relay-test/{relay_test_id}")
            if relay_test_detail["state"] != "pending":
                break
            time.sleep(0.1)
        assert relay_test_detail["state"] in ("failed", "done"), relay_test_detail
        hit("GET /api/models/{id}/relay-test/{test_id}")
        admin_client.json("DELETE", f"/api/models/{browser_id}")

        planner_server, planner_port = start_mock(_PlannerMock)
        _, created = admin_client.json("POST", "/api/models", {
            "name": "api-planner", "provider": "openai",
            "base_url": f"http://127.0.0.1:{planner_port}/v1",
            "api_key": "planner-key", "model": "planner-model", "roles": ["planner"],
        }, expected=201)
        planner_id = created["model"]["id"]
        admin_client.json("POST", f"/api/models/{planner_id}/test")
        _, models = admin_client.json("GET", "/api/models")
        assert "planner" in models["roles"]
        hit("GET /api/models")

        # 任务：单技能、混合并行、取消/重跑、报告/SSE
        clock = admin_client.task_upload("API 时钟任务", "NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
                                         ROOT / "tests/fixtures/logs/clock-demo.zip")
        hit("POST /api/tasks")
        _, tasks = admin_client.json("GET", "/api/tasks?page=1&page_size=10&q=API")
        assert tasks["total"] >= 1
        hit("GET /api/tasks")
        clock_task = wait_task(admin_client, clock)
        assert clock_task["status"] == "done" and clock_task["verdict"]["status"] == "matched"
        _, calls = admin_client.json("GET", f"/api/tool-calls?task_id={clock}")
        assert any(x["tool_name"] == "demo-bbu-clock-loss" for x in calls["items"]), calls
        hit("GET /api/tool-calls")
        _, approvals = admin_client.json("GET", "/api/tool-approvals")
        assert isinstance(approvals["items"], list)
        hit("GET /api/tool-approvals")
        _, approval = admin_client.json("POST", f"/api/tasks/{clock}/tool-approvals", {
            "skill": "demo-bbu-clock-loss", "reason": "API 冒烟审批"})
        approval_id = approval["approval"]["id"]
        assert approval["approval"]["status"] == "pending"
        hit("POST /api/tasks/{id}/tool-approvals")
        _, decided = admin_client.json("POST", f"/api/tool-approvals/{approval_id}/decide",
                                       {"approved": True, "reason": "API 冒烟通过"})
        assert decided["approval"]["status"] == "approved"
        hit("POST /api/tool-approvals/{id}/decide")
        _, trace = admin_client.json("GET", f"/api/tasks/{clock}/agent-trace")
        assert trace["trace"]["task_id"] == clock
        hit("GET /api/tasks/{id}/agent-trace")
        _, cal = admin_client.json("GET", "/api/calibration")
        assert "calibration" in cal
        hit("GET /api/calibration")
        _, cal_fit = admin_client.json("POST", "/api/calibration/fit", {"split": "dev"})
        assert cal_fit["calibration"]["status"] in ("uncalibrated", "calibrated")
        hit("POST /api/calibration/fit")
        _, matrix = admin_client.json("POST", "/api/case-matrix/rebuild",
                                      {"skill": "demo-bbu-clock-loss", "build_sha": "smoke"})
        assert matrix["matrix"]["summary"]["cells"] >= 1, matrix
        hit("POST /api/case-matrix/rebuild")
        _, matrix_get = admin_client.json("GET", "/api/case-matrix?skill=demo-bbu-clock-loss")
        assert matrix_get["verify"]["ok"], matrix_get
        hit("GET /api/case-matrix")
        admin_client.json("POST", f"/api/tasks/{clock}/review", {
            "decision": "correct", "corrected_root": "参考时钟源抖动",
            "action_result": "unknown", "comment": "冒烟：验证增量候选"})
        _, candidates = admin_client.json("GET", "/api/experience-candidates?status=pending")
        assert candidates["candidates"], candidates
        candidate_id = candidates["candidates"][0]["id"]
        hit("GET /api/experience-candidates")
        _, decided_candidate = admin_client.json(
            "POST", f"/api/experience-candidates/{candidate_id}/decide",
            {"status": "approved", "note": "冒烟通过"})
        assert decided_candidate["candidate"]["status"] == "approved"
        hit("POST /api/experience-candidates/{id}/decide")
        _, recovery = admin_client.json("POST", f"/api/tasks/{clock}/recovery-runs",
                                        {"source": "readonly_receipt"}, expected=201)
        run_id = recovery["run"]["id"]
        assert recovery["run"]["before"], recovery
        hit("POST /api/tasks/{id}/recovery-runs")
        _, observed = admin_client.multipart(f"/api/recovery-runs/{run_id}/observe", {
            "after": json.dumps({"verdict": {"status": "matched", "root_cause_name": ""},
                                 "evidence": [{"rule_id": "E1", "file": "after-clock.log",
                                               "line_no": 3}]}, ensure_ascii=False),
        }, [("file", "after-clock.log", b"clock stable\nPLL locked\n", "text/plain")],
            expected=200)
        assert observed["run"]["comparison"]["status"] == "recovered", observed
        uploaded = observed["run"]["after"]["evidence"][0]
        assert uploaded["uploaded"] is True and uploaded["sha256"]
        assert uploaded["file"] == "after-clock.log" and uploaded["size"] > 0
        assert "clock stable" in uploaded["text"]
        hit("POST /api/recovery-runs/{id}/observe")
        _, recovery_review = admin_client.json("POST", f"/api/recovery-runs/{run_id}/review",
                                               {"decision": "confirmed", "note": "现场已恢复"})
        assert recovery_review["run"]["learning_gate"]["eligible"] is True, recovery_review
        hit("POST /api/recovery-runs/{id}/review")
        _, runs = admin_client.json("GET", f"/api/tasks/{clock}/recovery-runs")
        assert runs["runs"], runs
        hit("GET /api/tasks/{id}/recovery-runs")
        assert clock_task["result"]["plan"]["source"] == "ai"
        hit("GET /api/tasks/{id}")
        _, questions = admin_client.json("GET", f"/api/tasks/{clock}/questions")
        assert questions["task_id"] == clock
        hit("GET /api/tasks/{id}/questions")
        _, session = admin_client.json("GET", f"/api/tasks/{clock}/session")
        assert session["task_id"] == clock
        hit("GET /api/tasks/{id}/session")
        _, evidence = admin_client.json("GET", f"/api/tasks/{clock}/evidence")
        assert evidence["total"] >= 1
        hit("GET /api/tasks/{id}/evidence")
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
                     "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("TAPI-SUPP-1", admin_id, "补证测试", "帮助需要补证的任务",
                      "needs_data", "done", 100, "fast", "[]", "{}", utc_now()))
        conn.commit()
        conn.close()
        _, supp_session = admin_client.json("GET", "/api/tasks/TAPI-SUPP-1/session")
        _, saved = admin_client.json("POST", "/api/tasks/TAPI-SUPP-1/inputs", {
            "expected_revision": supp_session["revision"],
            "answers": {"Q1": "补充 CLK032 现场信息"},
            "idempotency_key": "api-smoke-supp-1",
        }, expected=201)
        assert saved["revision"] == supp_session["revision"] + 1
        hit("POST /api/tasks/{id}/inputs")
        _, continued = admin_client.json("POST", "/api/tasks/TAPI-SUPP-1/continue", {
            "expected_revision": saved["revision"],
        }, expected=202)
        assert continued["task"]["id"] == "TAPI-SUPP-1"
        hit("POST /api/tasks/{id}/continue")
        # 复核、案例回流、质量门禁、指标与新技能治理接口
        clock_root = clock_task["verdict"].get("root_cause_name") or ""
        _, reviewed = admin_client.json("POST", f"/api/tasks/{clock}/review", {
            "decision": "confirm", "action_result": "effective",
            "comment": "API smoke 复核", "promote_to_case": True,
        })
        assert reviewed["review"]["decision"] == "confirm"
        assert reviewed["review"]["real_case_id"]
        hit("POST /api/tasks/{id}/review")
        _, cases = admin_client.json("GET", "/api/cases")
        assert any(c["id"] == reviewed["review"]["real_case_id"] for c in cases["items"])
        hit("GET /api/cases")
        case_id = reviewed["review"]["real_case_id"]
        _, case = admin_client.json("GET", f"/api/cases/{case_id}")
        assert case["case"]["status"] == "labeled"
        hit("GET /api/cases/{id}")
        _, me = admin_client.json("GET", "/api/auth/me")
        _, assigned_case = admin_client.json("POST", f"/api/cases/{case_id}/assign-reviewer", {
            "reviewer_id": me["user"]["id"], "note": "API smoke 分配",
        })
        assert assigned_case["case"]["review_status"] == "pending"
        hit("POST /api/cases/{id}/assign-reviewer")
        _, case_history = admin_client.json("GET", f"/api/cases/{case_id}/history")
        assert case_history["items"][0]["action"] == "assign"
        hit("GET /api/cases/{id}/history")
        _, labeled = admin_client.json("POST", f"/api/cases/{case_id}/label", {
            "split": "test", "expected_roots": [clock_root], "action": "approve",
            "decision_note": "api smoke label",
            "labels": {"evidence_window": {"start": "2025-01-01 00:00", "end": "2025-01-01 00:10"},
                       "recovery_result": "已恢复"},
        })
        assert labeled["case"]["split"] == "test" and labeled["case"]["review_status"] == "confirmed"
        hit("POST /api/cases/{id}/label")
        _, run = admin_client.json("POST", "/api/evaluations/run", {
            "split": "test", "limit": 1, "name": "api-smoke-r5",
        })
        assert run["summary"]["cases"] == 1 and run["report"]
        hit("POST /api/evaluations/run")
        admin_client.json("POST", "/api/evaluations/run", {
            "split": "test", "limit": 1, "llm_mode": "bogus",
        }, expected=400)
        _, evals = admin_client.json("GET", "/api/evaluations")
        assert isinstance(evals["items"], list)
        hit("GET /api/evaluations")
        _, quality = admin_client.json("GET", "/api/quality")
        assert quality["maturity_gate"]
        hit("GET /api/quality")
        _, metrics = admin_client.json("GET", "/api/metrics")
        assert metrics["queue"]["max_queue"] >= 1 and "tasks" in metrics
        hit("GET /api/metrics")
        status, raw, headers = admin_client.request("GET", "/api/metrics/prometheus")
        assert status == 200 and b"bbu_queue_depth" in raw and "text/plain" in headers.get("content-type", "")
        hit("GET /api/metrics/prometheus")
        _, usage = admin_client.json("GET", "/api/usage/overview?days=7")
        assert "totals" in usage and isinstance(usage["daily"], list)
        assert len(usage["daily"]) == 7 and "directory" in usage
        assert isinstance(usage["user_daily"], list)
        hit("GET /api/usage/overview")
        _, usage_tasks = admin_client.json("GET", "/api/usage/tasks?days=7&page=1&page_size=10")
        assert usage_tasks["page"] == 1 and isinstance(usage_tasks["items"], list)
        hit("GET /api/usage/tasks")
        _, events = admin_client.json("GET", f"/api/tasks/{clock}/events?after=0")
        assert events["items"]
        hit("GET /api/tasks/{id}/events")
        status, raw, headers = admin_client.request(
            "GET", f"/api/tasks/{clock}/stream", headers={"Accept": "text/event-stream"}, timeout=10)
        assert status == 200 and b"event: task" in raw and "text/event-stream" in headers.get("content-type", "")
        hit("GET /api/tasks/{id}/stream")
        _, report_json = admin_client.json("GET", f"/api/tasks/{clock}/report?format=json")
        assert report_json["plan"]["source"] == "ai"
        status, raw, _ = admin_client.request("GET", f"/api/tasks/{clock}/report?format=md&download=1")
        assert status == 200 and b"BBU" in raw
        status, raw, _ = admin_client.request("GET", f"/api/tasks/{clock}/report?format=html")
        assert status == 200 and "AI 诊断计划".encode() in raw
        hit("GET /api/tasks/{id}/report")
        status, raw, _ = admin_client.request(
            "GET", f"/api/tasks/{clock}/skills/demo-bbu-clock-loss/report?format=html")
        assert status == 200 and raw
        hit("GET /api/tasks/{id}/skills/{skill}/report")
        status, _, _ = admin_client.request("POST", f"/api/tasks/{clock}/cancel",
                                             b"{}", {"Content-Type": "application/json",
                                                     "X-CSRF": admin_client.csrf})
        assert status == 400
        hit("POST /api/tasks/{id}/cancel")  # 已结束错误路径
        # 直接插入一个 queued 任务，验证取消成功路径
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
                     "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("TAPI-CANCEL-1", admin_id, "取消测试", "取消接口", "queued", "queued", 0,
                      "fast", "[]", "{}", utc_now()))
        conn.commit()
        conn.close()
        _, cancel = admin_client.json("POST", "/api/tasks/TAPI-CANCEL-1/cancel")
        assert cancel["ok"]
        hit("POST /api/tasks/{id}/cancel")
        _, retry = admin_client.json("POST", f"/api/tasks/{clock}/retry", expected=201)
        assert retry["id"] != clock
        hit("POST /api/tasks/{id}/retry")
        wait_task(admin_client, retry["id"])

        mixed = admin_client.task_upload(
            "API 混合任务",
            "NE_A 时钟失锁 CLK032；NE_B 光模块收光异常，RX_LOSS 告警。",
            ROOT / "tests/fixtures/logs/mixed-demo.zip")
        mixed_task = wait_task(admin_client, mixed)
        assert mixed_task["status"] == "done" and len(mixed_task["skills"]) >= 2

        # 技能库：列表/详情/扫描/停用启用/导出
        _, skills = admin_client.json("GET", "/api/skills")
        assert any(s["name"] == "demo-bbu-clock-loss" for s in skills["items"])
        hit("GET /api/skills")
        _, detail = admin_client.json("GET", "/api/skills/demo-bbu-clock-loss")
        assert detail["name"] == "demo-bbu-clock-loss"
        hit("GET /api/skills/{name}")
        _, manifest = admin_client.json("GET", "/api/skills/demo-bbu-clock-loss/manifest")
        assert manifest["status"] in ("missing", "verified", "unsigned"), manifest
        hit("GET /api/skills/{name}/manifest")
        _, signed = admin_client.json("POST", "/api/skills/demo-bbu-clock-loss/manifest/sign")
        assert signed["manifest"]["content_sha256"]
        hit("POST /api/skills/{name}/manifest/sign")
        _, tool = admin_client.json("GET", "/api/skills/demo-bbu-clock-loss/tool")
        assert tool["tool_name"] == "demo-bbu-clock-loss"
        hit("GET /api/skills/{name}/tool")
        _, scan = admin_client.json("POST", "/api/skills/scan")
        assert scan["ok"]
        hit("POST /api/skills/scan")
        admin_client.json("POST", "/api/skills/demo-bbu-optical-rx/disable")
        hit("POST /api/skills/{name}/disable")
        admin_client.json("POST", "/api/skills/demo-bbu-optical-rx/enable")
        hit("POST /api/skills/{name}/enable")
        status, exported, _ = admin_client.request("GET", "/api/skills/demo-bbu-clock-loss/export")
        assert status == 200 and exported[:2] == b"PK"
        hit("GET /api/skills/{name}/export")
        _, imported = admin_client.multipart("/api/skills/import", {"overwrite": "1"},
                                             [("files", "demo-bbu-clock-loss.zip", exported,
                                               "application/zip")])
        assert imported["skill"]["name"] == "demo-bbu-clock-loss"
        hit("POST /api/skills/import")

        # 工坊：列表/详情/更新/日志/分析/校验/规则确认/构建/发布/案例（错误路径）
        _, drafts = admin_client.json("GET", "/api/workshop/drafts")
        hit("GET /api/workshop/drafts")
        auto_drafts = [d for d in drafts["items"] if d.get("category") == "自学习"]
        assert auto_drafts, "未生成自学习草稿"
        auto_id = auto_drafts[0]["id"]
        _, auto_detail = admin_client.json("GET", f"/api/workshop/drafts/{auto_id}")
        hit("GET /api/workshop/drafts/{id}")
        _, updated = admin_client.json("PUT", f"/api/workshop/drafts/{auto_id}",
                                       {"description": "API 全量测试草稿"})
        assert updated["draft"]["description"] == "API 全量测试草稿"
        hit("PUT /api/workshop/drafts/{id}")
        admin_client.multipart(f"/api/workshop/drafts/{auto_id}/logs", {},
                               [("files", "clock-again.zip",
                                 (ROOT / "tests/fixtures/logs/clock-demo.zip").read_bytes(),
                                 "application/zip")])
        hit("POST /api/workshop/drafts/{id}/logs")
        admin_client.json("POST", f"/api/workshop/drafts/{auto_id}/analyze")
        hit("POST /api/workshop/drafts/{id}/analyze")
        _, tried_auto = admin_client.json("POST", f"/api/workshop/drafts/{auto_id}/rules/try")
        assert tried_auto["ok"], tried_auto
        hit("POST /api/workshop/drafts/{id}/rules/try")
        admin_client.json("POST", f"/api/workshop/drafts/{auto_id}/check", {})
        hit("POST /api/workshop/drafts/{id}/check")
        _, verified = admin_client.json("POST", f"/api/workshop/drafts/{auto_id}/verify-rules",
                                        {"rule_ids": ["E1"]})
        assert verified.get("ok", True)
        hit("POST /api/workshop/drafts/{id}/verify-rules")
        case_name = auto_detail["name"]
        _, case_result = admin_client.multipart(
            f"/api/workshop/drafts/{auto_id}/case",
            {"case_id": "case-api-01", "kind": "positive", "description": "API 案例",
             "root_cause": "参考时钟源失锁", "hit_rules": "E1", "device": "BBU",
             "tags": "api"},
            [("files", "clock-case.zip",
              (ROOT / "tests/fixtures/logs/clock-demo.zip").read_bytes(), "application/zip")])
        assert case_result["ok"] and case_result["case_id"] == "case-api-01"
        hit("POST /api/workshop/drafts/{id}/case")
        _, case_list = admin_client.json("GET", f"/api/workshop/drafts/{auto_id}/cases")
        assert any(c["case_id"] == "case-api-01" for c in case_list["items"])
        hit("GET /api/workshop/drafts/{id}/cases")
        # 异步分析：提交后台任务并轮询真实阶段进度
        status, async_started, _ = admin_client.request(
            "POST", f"/api/workshop/drafts/{auto_id}/analyze?async=1",
            b"{}", {"Content-Type": "application/json", "X-CSRF": admin_client.csrf})
        assert status == 202, async_started[:300]
        job_id = json.loads(async_started)["job"]["id"]
        deadline = time.time() + 60
        job = {}
        while time.time() < deadline:
            _, job = admin_client.json("GET", f"/api/workshop/jobs/{job_id}")
            hit("GET /api/workshop/jobs/{id}")
            if job["status"] in ("done", "failed", "interrupted"):
                break
            time.sleep(0.25)
        assert job["status"] == "done", job
        _, jobs = admin_client.json("GET", f"/api/workshop/drafts/{auto_id}/jobs")
        assert jobs["items"][0]["id"] == job_id
        hit("GET /api/workshop/drafts/{id}/jobs")

        # 创建一个可构建/发布的完整草稿
        _, new_draft = admin_client.json("POST", "/api/workshop/drafts", {
            "name": "bbu-fault-api-demo", "title": "API 演示技能",
            "fault_name": "API 演示故障", "category": "测试", "description": "API 全量测试",
            "keywords": ["API演示"],
        }, expected=201)
        valid_id = new_draft["draft"]["id"]
        hit("POST /api/workshop/drafts")
        _, trash_draft = admin_client.json("POST", "/api/workshop/drafts", {
            "name": "bbu-fault-api-trash", "title": "API 删除演示",
            "fault_name": "API 删除演示", "category": "测试",
        }, expected=201)
        admin_client.json("DELETE", f"/api/workshop/drafts/{trash_draft['draft']['id']}")
        hit("DELETE /api/workshop/drafts/{id}")
        # 整段文本建草稿 + 自由文本补充确认
        _, recipes = admin_client.json("GET", "/api/workshop/recipes")
        assert any(r["id"] == "vsw-ssd" and r["verified"] is False for r in recipes["items"])
        hit("GET /api/workshop/recipes")
        intake_text = (ROOT / "tests/fixtures/intake/vsw-ssd.txt").read_text(encoding="utf-8")
        _, intake_draft = admin_client.json("POST", "/api/workshop/drafts", {
            "source_text": intake_text, "name": "bbu-fault-api-intake", "recipe_id": "vsw-ssd",
        }, expected=201)
        intake_id = intake_draft["draft"]["id"]
        assert intake_draft["draft"]["recipe_id"] == "vsw-ssd"
        assert intake_draft["draft"]["analysis"]["intake"]["recipe"]["id"] == "vsw-ssd"
        assert intake_draft["draft"]["analysis"]["intake"]["recipe"]["verified"] is False
        assert intake_draft["draft"]["analysis"]["quality"]["counts"]["rules"] == 5
        _, refined = admin_client.json("POST", f"/api/workshop/drafts/{intake_id}/refine", {
            "text": "补充确认：C1 为正常反例，SSD 寿命阈值 30 保持不变。",
        })
        assert refined["draft"]["analysis"]["quality"]["counts"]["root_causes"] == 4
        hit("POST /api/workshop/drafts/{id}/refine")
        _, prov = admin_client.json("GET", f"/api/workshop/drafts/{intake_id}/provenance")
        assert prov["provenance"], prov
        hit("GET /api/workshop/drafts/{id}/provenance")
        _, locked = admin_client.json(
            "POST", f"/api/workshop/drafts/{intake_id}/provenance/lock",
            {"paths": ["/fault_name"], "source_revision": prov["source_revision"]})
        assert locked["audit"]["locked"], locked
        hit("POST /api/workshop/drafts/{id}/provenance/lock")
        profile = (ROOT / "examples/profiles/demo-optical-rx.yaml").read_text(encoding="utf-8")
        profile = profile.replace("name: demo-bbu-optical-rx", "name: bbu-fault-api-demo", 1)
        profile = profile.replace("title: \"[示例] 光模块接收光功率异常诊断\"", "title: \"API 演示技能\"", 1)
        profile = profile.replace("version: \"1.0.0\"", "version: \"0.1.0\"", 1)
        admin_client.json("PUT", f"/api/workshop/drafts/{valid_id}",
                          {"profile_yaml": profile, "title": "API 演示技能"})
        status, raw, _ = admin_client.request("POST", f"/api/workshop/drafts/{valid_id}/check",
                                               json.dumps({"profile_yaml": profile}).encode(),
                                               {"Content-Type": "application/json",
                                                "X-CSRF": admin_client.csrf})
        assert status == 200 and json.loads(raw)["ok"], raw[:300]
        _, built = admin_client.json("POST", f"/api/workshop/drafts/{valid_id}/build")
        assert built["ok"] and not built["verified"], built
        hit("POST /api/workshop/drafts/{id}/build")
        # 未跑真实案例回归，发布必须被拒绝
        admin_client.json("POST", f"/api/workshop/drafts/{valid_id}/publish", expected=400)
        # 写入正例和反例，重新构建后才允许发布
        admin_client.multipart(
            f"/api/workshop/drafts/{valid_id}/case",
            {"case_id": "case-positive", "kind": "positive", "description": "收光异常正例",
             "root_cause": "R1", "hit_rules": "E1"},
            [("files", "optical-case.zip", (ROOT / "tests/fixtures/logs/optical-demo.zip").read_bytes(), "application/zip")])
        admin_client.multipart(
            f"/api/workshop/drafts/{valid_id}/case",
            {"case_id": "case-negative", "kind": "negative", "description": "正常反例",
             "root_cause": "NONE", "not_hit_rules": "E1"},
            [("files", "normal-case.zip", (ROOT / "tests/fixtures/logs/normal-demo.zip").read_bytes(), "application/zip")])
        _, built_verified = admin_client.json("POST", f"/api/workshop/drafts/{valid_id}/build")
        assert built_verified["ok"] and built_verified["verified"], built_verified
        _, published = admin_client.json("POST", f"/api/workshop/drafts/{valid_id}/publish")
        assert published["ok"]
        hit("POST /api/workshop/drafts/{id}/publish")
        _, versions = admin_client.json("GET", "/api/skill-versions?skill=bbu-fault-api-demo")
        assert versions["items"]
        hit("GET /api/skill-versions")
        _, rollback = admin_client.json("POST", "/api/skills/bbu-fault-api-demo/rollback",
                                        {"version": "0.1.0"})
        assert rollback["ok"]
        hit("POST /api/skills/{name}/rollback")
        _, channel = admin_client.json("POST", "/api/skills/bbu-fault-api-demo/channel",
                                       {"channel": "canary"})
        assert channel["channel"] == "canary"
        _, skill_detail = admin_client.json("GET", "/api/skills/bbu-fault-api-demo")
        assert skill_detail["channel"] == "canary"
        admin_client.json("POST", "/api/skills/bbu-fault-api-demo/channel",
                          {"channel": "stable"})
        hit("POST /api/skills/{name}/channel")

        # 用户、审计、系统信息、工程师 RBAC
        _, users = admin_client.json("GET", "/api/users")
        hit("GET /api/users")
        _, created_user = admin_client.json("POST", "/api/users", {
            "username": "api_engineer", "password": "Engineer!2026",
            "display_name": "API 工程师", "role": "engineer",
        }, expected=201)
        engineer_id = created_user["id"]
        hit("POST /api/users")
        admin_client.json("PUT", f"/api/users/{engineer_id}",
                          {"display_name": "API 工程师改", "role": "engineer"})
        hit("PUT /api/users/{id}")
        _, paged_users = admin_client.json(
            "GET", "/api/users?page=1&page_size=1&q=api_engineer&role=engineer")
        assert paged_users["total"] == 1 and len(paged_users["items"]) == 1

        # 1000 个账号的列表必须服务端分页，而不是把全部记录塞进一个响应。
        now = utc_now()
        with sqlite3.connect(str(db_path)) as conn:
            conn.executemany(
                "INSERT INTO users(username,display_name,role,password_hash,must_change,"
                "active,created_at,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [(f"bulk_api_{i:04d}", f"批量账号{i:04d}", "engineer", "x", 0, 1,
                  now, "active", now) for i in range(1000)])
        _, bulk_page = admin_client.json(
            "GET", "/api/users?q=bulk_api_&status=active&page=1&page_size=20")
        assert bulk_page["total"] == 1000 and len(bulk_page["items"]) == 20
        _, bulk_page2 = admin_client.json(
            "GET", "/api/users?q=bulk_api_&status=active&page=50&page_size=20")
        assert len(bulk_page2["items"]) == 20
        _, user_detail = admin_client.json("GET", f"/api/users/{engineer_id}")
        assert user_detail["user"]["username"] == "api_engineer"
        hit("GET /api/users/{id}")
        _, profile0 = admin_client.json("GET", "/api/profile")
        assert profile0["user"]["role"] == "admin"
        hit("GET /api/profile")
        _, profile1 = admin_client.json("PUT", "/api/profile", {
            "display_name": "平台管理员", "notification_prefs": {"task_done": True},
        })
        assert profile1["user"]["display_name"] == "平台管理员"
        hit("PUT /api/profile")

        _, key0 = admin_client.json("GET", "/api/profile/model-key")
        assert key0["configured"] is False and key0["status"]["personal"]["configured"] is False
        assert "personal-api-key" in key0["maas_url"]
        hit("GET /api/profile/model-key")
        _, key1 = admin_client.json("PUT", "/api/profile/model-key", {
            "api_key": "maas-test-secret-key", "model": "deepseek-v4-flash",
        })
        assert key1["configured"] is True
        assert "maas-test-secret-key" not in json.dumps(key1, ensure_ascii=False)
        assert key1["api_key_masked"].startswith("maas")
        hit("PUT /api/profile/model-key")
        _, key_test = admin_client.json("POST", "/api/profile/model-key/test")
        assert key_test["ok"] is False and key_test["error"]
        hit("POST /api/profile/model-key/test")
        _, key2 = admin_client.json("GET", "/api/profile/model-key")
        assert key2["configured"] is True and key2["model"] == "deepseek-v4-flash"
        _, deleted_key = admin_client.json("DELETE", "/api/profile/model-key")
        assert deleted_key["ok"] is True
        hit("DELETE /api/profile/model-key")

        _, invites0 = admin_client.json("GET", "/api/users/invites")
        assert "items" in invites0
        hit("GET /api/users/invites")
        _, invite = admin_client.json("POST", "/api/users/invites", {
            "username": "api_reviewer", "display_name": "API 复核专家",
            "role": "reviewer", "hours": 24,
        }, expected=201)
        reviewer_code = invite["code"]
        hit("POST /api/users/invites")
        _, activated = Client(host, port).json("POST", "/api/auth/activate", {
            "code": reviewer_code, "password": "Reviewer!2026x",
        }, expected=201)
        reviewer_id = activated["user"]["id"]
        hit("POST /api/auth/activate")
        Client(host, port).json(
            "POST", "/api/auth/activate", {"code": reviewer_code, "password": "Reviewer!2026x"},
            expected=400)
        _, assigned = admin_client.json("POST", f"/api/tasks/{clock}/assign-reviewer",
                                        {"reviewer_id": reviewer_id})
        assert assigned["reviewer_id"] == reviewer_id
        hit("POST /api/tasks/{id}/assign-reviewer")
        reviewer = Client(host, port)
        reviewer.login("api_reviewer", "Reviewer!2026x")
        _, reviewer_tasks = reviewer.json("GET", "/api/tasks")
        assert any(item["id"] == clock for item in reviewer_tasks["items"])
        reviewer.json("GET", "/api/traces", expected=403)
        _, reviewer_detail = reviewer.json("GET", f"/api/tasks/{clock}")
        assert reviewer_detail["reviewer_id"] == reviewer_id
        reviewer.json("GET", "/api/users", expected=403)
        reviewer.json("GET", "/api/audit", expected=403)
        reviewer.json("GET", "/api/models", expected=403)
        _, reviewer_system = reviewer.json("GET", "/api/system/info")
        assert reviewer_system["version"]
        reviewer.json("GET", "/api/system/updates", expected=403)
        _, reviewer_profile = reviewer.json("GET", "/api/profile")
        assert reviewer_profile["user"]["role"] == "reviewer"
        _, reviewer_search = reviewer.json("GET", "/api/search?q=API")
        assert reviewer_search["users"] == []
        assert isinstance(reviewer_search["skills"], list)
        assert isinstance(reviewer_search["cases"], list)
        assert all(item["id"] == clock for item in reviewer_search["tasks"])

        _, bulk_user = admin_client.json("POST", "/api/users", {
            "username": "api_bulk", "password": "BulkUser!2026", "role": "engineer",
        }, expected=201)
        _, batch = admin_client.json("POST", "/api/users/batch-disable",
                                     {"ids": [bulk_user["id"]]})
        assert batch["disabled"] == 1
        hit("POST /api/users/batch-disable")
        Client(host, port).login("api_bulk", "BulkUser!2026", expected=401)

        _, revoke_invite = admin_client.json("POST", "/api/users/invites", {
            "username": "api_revoke", "role": "engineer", "hours": 1,
        }, expected=201)
        _, revoked = admin_client.json(
            "DELETE", f"/api/users/invites/{revoke_invite['invite']['id']}")
        assert revoked["ok"]
        hit("DELETE /api/users/invites/{id}")

        _, expired_invite = admin_client.json("POST", "/api/users/invites", {
            "username": "api_expired", "role": "engineer", "hours": 1,
        }, expected=201)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE user_invites SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                         (expired_invite["invite"]["id"],))
        Client(host, port).json("POST", "/api/auth/activate", {
            "code": expired_invite["code"], "password": "Expired!2026x",
        }, expected=400)

        admin_client.json("POST", f"/api/users/{reviewer_id}/force-logout")
        hit("POST /api/users/{id}/force-logout")
        status, _, _ = reviewer.request("GET", "/api/auth/me")
        assert status == 401

        # R3 草稿：创建、自动保存、上传文件、恢复当前草稿、删除文件、从草稿提交。
        _, draft1 = admin_client.json("POST", "/api/task-drafts", {
            "title": "草稿任务", "description": "草稿描述", "mode": "fast",
        }, expected=201)
        hit("POST /api/task-drafts")
        draft_id = draft1["draft"]["id"]
        admin_client.json("PUT", f"/api/task-drafts/{draft_id}", {
            "title": "草稿任务改", "description": "草稿描述改", "mode": "deep",
        })
        hit("PUT /api/task-drafts/{id}")
        _, uploaded = admin_client.multipart(
            f"/api/task-drafts/{draft_id}/files", {},
            [("file", "clock-demo.zip",
              (ROOT / "tests/fixtures/logs/clock-demo.zip").read_bytes(), "application/zip")],
            expected=201)
        hit("POST /api/task-drafts/{id}/files")
        _, current = admin_client.json("GET", "/api/task-drafts/current")
        assert current["draft"]["id"] == draft_id and len(current["draft"]["files"]) == 1
        hit("GET /api/task-drafts/current")
        admin_client.json(
            "DELETE", f"/api/task-drafts/{draft_id}/files/{uploaded['file']['id']}")
        hit("DELETE /api/task-drafts/{id}/files/{file}")
        admin_client.multipart(
            f"/api/task-drafts/{draft_id}/files", {},
            [("file", "clock-demo.zip",
              (ROOT / "tests/fixtures/logs/clock-demo.zip").read_bytes(), "application/zip")],
            expected=201)
        _, draft_task = admin_client.multipart("/api/tasks", {
            "title": "草稿提交任务", "description": "从草稿提交", "mode": "fast",
            "draft_id": draft_id,
        }, [], expected=201)
        _, draft_task_detail = admin_client.json("GET", f"/api/tasks/{draft_task['id']}")
        assert len(draft_task_detail["inputs"]) == 1
        _, draft2 = admin_client.json("POST", "/api/task-drafts", {
            "title": "待删除草稿", "description": "x",
        }, expected=201)
        admin_client.json("DELETE", f"/api/task-drafts/{draft2['draft']['id']}")
        hit("DELETE /api/task-drafts/{id}")

        _, traces = admin_client.json(
            "GET", f"/api/traces?kind=diagnosis&ref_id={clock}&limit=10")
        assert traces["items"], "诊断任务完成后没有执行轨迹"
        trace_run = traces["items"][0]
        assert trace_run["status"] in ("success", "needs_data", "failed"), trace_run
        assert trace_run["trace_path"].endswith(".jsonl")
        hit("GET /api/traces")
        _, trace_summary = admin_client.json(
            "GET", f"/api/traces/{trace_run['id']}/summary")
        assert trace_summary["run"]["id"] == trace_run["id"]
        assert "first_error" in trace_summary
        hit("GET /api/traces/{run_id}/summary")
        _, trace_events = admin_client.json(
            "GET", f"/api/traces/{trace_run['id']}/events?limit=50")
        assert trace_events["items"], "轨迹没有事件"
        assert any(e["type"] == "model_request" for e in trace_events["items"])
        assert any(e["type"] == "model_response" for e in trace_events["items"])
        hit("GET /api/traces/{run_id}/events")
        status, raw, headers = admin_client.request(
            "GET", f"/api/traces/{trace_run['id']}/download")
        assert status == 200 and "ndjson" in headers.get("content-type", "")
        assert raw and b'"run_id"' in raw
        hit("GET /api/traces/{run_id}/download")
        admin_client.json("GET", "/api/traces/NOTATRACE/summary", expected=404)

        _, audit = admin_client.json("GET", "/api/audit?page=1&page_size=10")
        assert audit["total"] > 0
        hit("GET /api/audit")
        _, audit_filtered = admin_client.json(
            "GET", "/api/audit?action=user.create&username=&page=1&page_size=5")
        assert audit_filtered["total"] >= 1
        status, raw, headers = admin_client.request(
            "GET", "/api/audit/export?action=user.create")
        assert status == 200 and "text/csv" in headers.get("content-type", "")
        hit("GET /api/audit/export")
        _, system = admin_client.json("GET", "/api/system/info")
        assert system["config"]["planner_enabled"] is True
        hit("GET /api/system/info")
        _, stats = admin_client.json("GET", "/api/stats")
        assert stats["today"] >= 1 and "pending_reviews" in stats
        hit("GET /api/stats")
        _, notes = admin_client.json("GET", "/api/notifications")
        assert "items" in notes and "unread" in notes
        hit("GET /api/notifications")
        assert notes["items"], "任务完成后未生成站内通知"
        note_id = notes["items"][0]["id"]
        _, read_one = admin_client.json("POST", f"/api/notifications/{note_id}/read")
        assert read_one["ok"]
        hit("POST /api/notifications/{id}/read")
        _, read_all = admin_client.json("POST", "/api/notifications/read-all")
        assert read_all["ok"]
        hit("POST /api/notifications/read-all")
        _, found = admin_client.json("GET", "/api/search?q=%E6%97%B6%E9%92%9F")
        assert "tasks" in found and "cases" in found and "skills" in found
        hit("GET /api/search")

        # 网页离线升级：预检、二次密码、fake root 助手、历史与回滚
        package = make_update_package(work, "9.9.9")
        status, uploaded = admin_client.multipart(
            "/api/system/updates/upload", {},
            [("file", package.name, package.read_bytes(), "application/gzip")], expected=201)
        update_id = uploaded["update"]["id"]
        assert uploaded["update"]["phase"] == "ready"
        assert uploaded["update"]["target_version"] == "9.9.9"
        hit("POST /api/system/updates/upload")
        _, updates = admin_client.json("GET", "/api/system/updates")
        assert updates["items"] and updates["current_version"] == CURRENT_VERSION
        hit("GET /api/system/updates")
        _, detail = admin_client.json("GET", f"/api/system/updates/{update_id}")
        assert detail["update"]["package_sha256"] == uploaded["update"]["package_sha256"]
        hit("GET /api/system/updates/{id}")
        status, raw, _ = admin_client.request(
            "POST", f"/api/system/updates/{update_id}/verify", b"{}",
            {"Content-Type": "application/json"})
        assert status == 403 and b"CSRF" in raw
        _, verified = admin_client.json("POST", f"/api/system/updates/{update_id}/verify", {})
        assert verified["update"]["phase"] == "ready"
        hit("POST /api/system/updates/{id}/verify")
        # 升级默认阻止排队/运行中的诊断任务，先等它们结束。
        for _ in range(120):
            _, running = admin_client.json("GET", "/api/tasks?status=running&page_size=1")
            _, queued = admin_client.json("GET", "/api/tasks?status=queued&page_size=1")
            if not running["items"] and not queued["items"]:
                break
            time.sleep(0.25)
        admin_client.json("POST", f"/api/system/updates/{update_id}/apply",
                          {"password": "wrong-password"}, expected=403)
        _, applied = admin_client.json("POST", f"/api/system/updates/{update_id}/apply",
                                       {"password": "Platform!2026x"}, expected=202)
        assert applied["update"]["phase"] in ("queued", "succeeded")
        for _ in range(20):
            _, applied = admin_client.json("GET", f"/api/system/updates/{update_id}")
            if applied["update"]["phase"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)
        assert applied["update"]["phase"] == "succeeded", applied
        hit("POST /api/system/updates/{id}/apply")
        _, rolled = admin_client.json("POST", "/api/system/updates/rollback",
                                      {"password": "Platform!2026x", "job_id": update_id},
                                      expected=202)
        for _ in range(20):
            _, rolled = admin_client.json("GET", f"/api/system/updates/{rolled['update']['id']}")
            if rolled["update"]["phase"] in ("rolled_back", "failed"):
                break
            time.sleep(0.1)
        assert rolled["update"]["phase"] == "rolled_back", rolled
        hit("POST /api/system/updates/rollback")

        # 历史备份：扫描升级快照并安全删除
        _, backups = admin_client.json("GET", "/api/backups")
        assert backups["items"], "升级助手未生成备份快照"
        hit("GET /api/backups")
        target_backup = next((x for x in backups["items"] if not x["in_use"]), None)
        assert target_backup, backups
        _, deleted_backup = admin_client.json(
            "DELETE", f"/api/backups/{target_backup['name']}")
        assert deleted_backup["ok"] and deleted_backup["size_bytes"] >= 0
        hit("DELETE /api/backups/{name}")

        # 任务空间：统计、共享引用释放、报告保留
        _, storage_before = admin_client.json("GET", "/api/storage/overview")
        assert storage_before["summary"]["task_count"] >= 2
        hit("GET /api/storage/overview")
        release_job = {}
        for source in (clock, retry["id"]):
            _, release = admin_client.json("POST", f"/api/tasks/{source}/release", expected=202)
            hit("POST /api/tasks/{id}/release")
            deadline = time.time() + 15
            while time.time() < deadline:
                _, release_job = admin_client.json("GET", f"/api/storage/jobs/{release['job']['id']}")
                hit("GET /api/storage/jobs/{id}")
                if release_job["status"] in ("done", "failed", "interrupted"):
                    break
                time.sleep(0.2)
            assert release_job["status"] == "done", release_job
        _, storage_jobs = admin_client.json("GET", f"/api/tasks/{clock}/storage-jobs")
        assert storage_jobs["items"][0]["status"] == "done"
        hit("GET /api/tasks/{id}/storage-jobs")
        status, _, _ = admin_client.request("GET", f"/api/tasks/{clock}/report?format=md")
        assert status == 200, "释放原始输入后报告必须仍可读"

        engineer = Client(host, port)
        engineer.login("api_engineer", "Engineer!2026")
        status, _, _ = engineer.request("GET", "/api/tasks")
        assert status == 200
        _, engineer_changed = engineer.json("POST", "/api/auth/password", {
            "old_password": "Engineer!2026", "new_password": "EngineerApi!2026",
        })
        engineer.csrf = engineer_changed["csrf"]
        status, _, _ = engineer.request("GET", "/api/users")
        assert status == 403
        status, _, _ = engineer.request("POST", "/api/skills/scan", b"{}",
                                        {"Content-Type": "application/json", "X-CSRF": engineer.csrf})
        assert status == 403
        status, _, _ = engineer.request("GET", "/api/models")
        assert status == 200
        status, _, _ = engineer.request("GET", "/api/system/updates")
        assert status == 403
        status, _, _ = engineer.request("GET", "/api/system/info")
        assert status == 200
        status, _, _ = engineer.request("GET", "/api/stats")
        assert status == 200
        status, _, _ = engineer.request("GET", "/api/audit")
        assert status == 403
        status, _, _ = engineer.request("GET", "/api/tasks")
        assert status == 200
        status, _, _ = engineer.request("POST", "/api/auth/logout")
        assert status == 200

        # 负向矩阵：未登录、CSRF、越权、404/409/400
        anon = Client(host, port)
        status, _, _ = anon.request("GET", "/api/tasks")
        assert status == 401
        status, _, _ = anon.request("GET", "/api/system/info")
        assert status == 401
        anon.login("api_engineer", "EngineerApi!2026")
        anon.csrf = ""
        anon.multipart("/api/tasks", {"title": "无 CSRF", "description": "应被拒绝",
                                      "mode": "fast"},
                       [("files", "clock.zip", (ROOT / "tests/fixtures/logs/clock-demo.zip").read_bytes(),
                         "application/zip")], expected=403)
        admin_client.json("PUT", f"/api/users/{admin_id}", {"active": 0}, expected=400)
        admin_client.json("POST", "/api/users", {
            "username": "api_engineer", "password": "Engineer!2026", "role": "engineer",
        }, expected=409)
        admin_client.json("POST", "/api/models", {
            "name": "bad-provider", "provider": "nope", "model": "x", "api_key": "x",
        }, expected=400)
        admin_client.json("POST", "/api/models", {
            "name": "api-planner", "provider": "openai",
            "base_url": f"http://127.0.0.1:{planner_port}/v1", "api_key": "x",
            "model": "planner-model", "roles": ["planner"],
        }, expected=409)
        admin_client.json("POST", "/api/models/999999/test", expected=404)
        admin_client.json("GET", "/api/tasks/T-NOT-EXIST", expected=404)
        admin_client.json("GET", "/api/skills/no-such-skill", expected=404)
        admin_client.json("GET", "/api/skills/no-such-skill/export", expected=404)
        admin_client.json("POST", "/api/skills/no-such-skill/disable", expected=404)
        admin_client.json("GET", "/api/workshop/drafts/W-NOT-EXIST", expected=404)
        admin_client.json("PUT", "/api/workshop/drafts/W-NOT-EXIST", {"title": "x"}, expected=404)
        admin_client.json("POST", "/api/workshop/drafts", {
            "name": "bbu-fault-api-demo", "title": "重复草稿",
        }, expected=400)
        admin_client.multipart("/api/skills/import", {}, [], expected=400)
        bad_zip = BytesIO()
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("README.txt", "not a skill")
        admin_client.multipart("/api/skills/import",
                               {"overwrite": "1"},
                               [("files", "bad.zip", bad_zip.getvalue(), "application/zip")],
                               expected=422)
        admin_client.multipart("/api/skills/import", {},
                               [("files", "demo-bbu-clock-loss.zip", exported, "application/zip")],
                               expected=409)
        admin_client.json("POST", "/api/skills/bbu-fault-api-demo/rollback",
                          {"version": "9.9.9"}, expected=400)

        engineer = Client(host, port)
        engineer.login("api_engineer", "EngineerApi!2026")
        engineer.json("GET", f"/api/tasks/{clock}", expected=404)
        engineer.json("POST", f"/api/tasks/{clock}/cancel", {}, expected=404)
        engineer.json("POST", f"/api/workshop/drafts/{valid_id}/publish", {}, expected=403)
        engineer.json("POST", "/api/models", {
            "name": "engineer-model", "provider": "mock", "model": "mock", "roles": ["default"],
        }, expected=403)
        engineer.json("POST", "/api/skills/demo-bbu-clock-loss/disable", {}, expected=403)
        engineer.json("GET", "/api/users", expected=403)
        engineer.json("GET", "/api/audit", expected=403)
        engineer.json("POST", "/api/skills/scan", {}, expected=403)
        engineer.json("GET", "/api/tasks", expected=200)
        engineer.json("GET", "/api/stats", expected=200)
        engineer.json("GET", "/api/system/info", expected=200)
        engineer.json("GET", "/api/models", expected=200)

        # 自助注册：默认关闭，开启后固定创建工程师账号
        _, closed = Client(host, port).json("POST", "/api/auth/register", {
            "username": "selfreg_off", "password": "SelfReg!2026x",
        }, expected=403)
        assert closed["error"]
        _, saved_register = admin_client.json("PUT", "/api/system/runtime-config", {
            "values": {"allow_self_register": True},
        })
        assert saved_register["restart_required"] is False
        _, public_cfg = Client(host, port).json("GET", "/api/public/config")
        assert public_cfg["allow_self_register"] is True
        _, registered = Client(host, port).json("POST", "/api/auth/register", {
            "username": "selfreg_api", "display_name": "自助注册",
            "domain": "时钟同步", "password": "SelfReg!2026x",
        }, expected=201)
        assert registered["user"]["role"] == "engineer"
        assert registered["user"]["domain"] == "时钟同步"
        hit("POST /api/auth/register")

        # 运行参数保存、环境变量提示和网页重启后生效
        _, runtime = admin_client.json("GET", "/api/system/runtime-config")
        assert any(x["key"] == "max_llm_calls" for x in runtime["items"])
        assert any(x["key"] == "max_workers" and x["env_override"] for x in runtime["items"])
        hit("GET /api/system/runtime-config")
        _, saved = admin_client.json("PUT", "/api/system/runtime-config", {
            "values": {"max_llm_calls": 13},
        })
        assert saved["restart_required"] and saved["saved"][0]["value"] == 13
        hit("PUT /api/system/runtime-config")
        status, raw, _ = admin_client.request(
            "POST", "/api/system/restart", b"{}",
            {"Content-Type": "application/json", "X-CSRF": admin_client.csrf})
        assert status == 202, raw[:300]
        hit("POST /api/system/restart")
        deadline = time.time() + 35
        effective = False
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                _, info = admin_client.json("GET", "/api/system/info")
            except (OSError, AssertionError):
                continue
            if info.get("config", {}).get("max_llm_calls") == 13 and info.get("uptime_sec", 99) < 25:
                effective = True
                break
        assert effective, "网页重启后新配置未生效"

        # 悬浮助手与 MCP：状态、降级问答、待确认动作、Token 与本机 HTTP
        _, assistant = admin_client.json("GET", "/api/assistant/status")
        assert assistant["status"]["enabled"] is True
        assert assistant["status"]["tools"] >= 10
        hit("GET /api/assistant/status")
        _, suggestions = admin_client.json("GET", "/api/assistant/suggestions")
        assert suggestions["items"]
        hit("GET /api/assistant/suggestions")
        _, chat = admin_client.json("POST", "/api/assistant/chat", {
            "messages": [{"role": "user", "content": "平台怎么新建诊断任务？"}],
        })
        assert chat["message"]
        hit("POST /api/assistant/chat")
        admin_client.json("POST", "/api/assistant/actions/not-a-real-action/confirm",
                          {}, expected=404)
        hit("POST /api/assistant/actions/{id}/confirm")
        _, cancelled = admin_client.json("POST", "/api/assistant/actions/not-a-real-action/cancel",
                                         {})
        assert cancelled["cancelled"]
        hit("POST /api/assistant/actions/{id}/cancel")
        _, mcp_info = admin_client.json("GET", "/api/mcp/info")
        assert mcp_info["mcp"]["configured"] is False
        hit("GET /api/mcp/info")
        _, created_token = admin_client.json("POST", "/api/mcp/token", {"mode": "read"},
                                             expected=201)
        token = created_token["mcp"]["token"]
        assert token.startswith("bbu_mcp_")
        hit("POST /api/mcp/token")
        status, raw, _ = admin_client.request("GET", "/mcp")
        assert status == 405, raw[:200]
        hit("GET /mcp")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "api-smoke", "version": "1"}}})
        status, raw, _ = admin_client.request("POST", "/mcp", body.encode(),
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {token}"})
        assert status == 200, raw[:300]
        payload = json.loads(raw)
        assert payload["result"]["serverInfo"]["name"] == "bbu-faultscope"
        hit("POST /mcp")
        body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        status, raw, _ = admin_client.request("POST", "/mcp", body.encode(),
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {token}"})
        assert status == 200 and len(json.loads(raw)["result"]["tools"]) >= 10
        _, revoked = admin_client.json("DELETE", "/api/mcp/token")
        assert revoked["mcp"]["configured"] is False
        hit("DELETE /api/mcp/token")

        # 故障知识库：状态、导入、检索、问答、审核、版本和文件下载
        _, kb_status = admin_client.json("GET", "/api/kb/status")
        assert kb_status["status"]["enabled"] is True
        hit("GET /api/kb/status")
        _, kb_facets = admin_client.json("GET", "/api/kb/facets")
        assert "boards" in kb_facets and "domains" in kb_facets
        hit("GET /api/kb/facets")
        _, sample = admin_client.json("POST", "/api/kb/sample", {})
        assert sample["documents"] >= 1
        hit("POST /api/kb/sample")
        _, kb_docs = admin_client.json("GET", "/api/kb/docs?size=10&status=all")
        assert kb_docs["items"]
        doc_id = kb_docs["items"][0]["id"]
        hit("GET /api/kb/docs")
        _, kb_detail = admin_client.json("GET", f"/api/kb/docs/{doc_id}")
        assert kb_detail["doc"]["cases"]
        hit("GET /api/kb/docs/{id}")
        _, kb_search = admin_client.json("GET", "/api/kb/search?q=" + quote("看门狗复位"))
        assert kb_search["items"]
        hit("GET /api/kb/search")
        _, kb_chat = admin_client.json("POST", "/api/kb/chat", {"query": "看门狗复位"})
        assert kb_chat["query_id"]
        hit("POST /api/kb/chat")
        admin_client.json("POST", "/api/kb/feedback", {
            "query_id": kb_chat["query_id"], "helpful": True, "reason": "api-smoke",
        })
        hit("POST /api/kb/feedback")
        admin_client.json("POST", "/api/kb/reindex", {})
        hit("POST /api/kb/reindex")
        admin_client.json("GET", "/api/kb/queries")
        hit("GET /api/kb/queries")

        _, created_doc = admin_client.json("POST", "/api/kb/docs", {
            "title": "API 粘贴案例",
            "text": "【详细描述:单板复位】\n【原因分析】喂狗异常\n【处理方案】升级逻辑",
        }, expected=201)
        doc_created_id = created_doc["doc_id"]
        case_created_id = created_doc["cases"][0]["id"]
        hit("POST /api/kb/docs")
        admin_client.json("PUT", f"/api/kb/docs/{doc_created_id}",
                          {"title": "API 粘贴案例-改名"})
        hit("PUT /api/kb/docs/{id}")
        _, versions = admin_client.json("GET", f"/api/kb/docs/{doc_created_id}/versions")
        assert versions["items"]
        hit("GET /api/kb/docs/{id}/versions")
        admin_client.json("POST",
                          f"/api/kb/docs/{doc_created_id}/versions/{versions['items'][0]['id']}/restore",
                          {})
        hit("POST /api/kb/docs/{id}/versions/{version_id}/restore")
        admin_client.json("POST", f"/api/kb/docs/{doc_created_id}/restore",
                          {"version_id": versions["items"][0]["id"]})
        hit("POST /api/kb/docs/{id}/restore")
        admin_client.json("POST", f"/api/kb/docs/{doc_created_id}/status", {"status": "disabled"})
        admin_client.json("POST", f"/api/kb/docs/{doc_created_id}/status", {"status": "active"})
        hit("POST /api/kb/docs/{id}/status")

        # 案例级修订、哈希链、恢复与审计
        _, case_detail = admin_client.json("GET", f"/api/kb/cases/{case_created_id}")
        assert case_detail["case"]["id"] == case_created_id
        hit("GET /api/kb/cases/{case_id}")
        _, case_updated = admin_client.json("PUT", f"/api/kb/cases/{case_created_id}", {
            "solution": "升级逻辑并复盘", "reason": "api-smoke 修订",
        })
        assert case_updated["case"]["revision"] >= 2
        hit("PUT /api/kb/cases/{case_id}")
        _, revisions = admin_client.json("GET", f"/api/kb/cases/{case_created_id}/revisions")
        assert revisions["items"] and revisions["total"] >= 2
        hit("GET /api/kb/cases/{case_id}/revisions")
        admin_client.json("GET", f"/api/kb/cases/{case_created_id}/revisions/1")
        hit("GET /api/kb/cases/{case_id}/revisions/{revision}")
        _, verified = admin_client.json("GET", f"/api/kb/cases/{case_created_id}/revisions/verify")
        assert verified["ok"] is True and verified["checked"] >= 1
        hit("GET /api/kb/cases/{case_id}/revisions/verify")
        admin_client.json("POST", f"/api/kb/cases/{case_created_id}/revisions/1/restore",
                          {"reason": "api-smoke 恢复"})
        hit("POST /api/kb/cases/{case_id}/revisions/{revision}/restore")
        admin_client.json("POST", f"/api/kb/cases/{case_created_id}/status",
                          {"status": "disabled", "reason": "api-smoke"})
        admin_client.json("POST", f"/api/kb/cases/{case_created_id}/status",
                          {"status": "active", "reason": "api-smoke"})
        hit("POST /api/kb/cases/{case_id}/status")

        _, stats = admin_client.json("GET", "/api/kb/stats?days=30")
        assert "inventory" in stats and "usage" in stats
        hit("GET /api/kb/stats")
        _, audit = admin_client.json("GET", "/api/kb/audit?limit=20")
        assert audit["items"]
        hit("GET /api/kb/audit")
        status, body, headers = admin_client.request("GET", "/api/kb/audit?format=csv")
        assert status == 200 and b"created_at" in body
        status, body, headers = admin_client.request("GET", "/api/kb/stats?format=csv")
        assert status == 200 and b"group,metric" in body

        _, review = admin_client.json("POST", "/api/kb/reviews", {
            "target_type": "case", "target_id": case_created_id, "stage": "manual", "run": True,
        }, expected=201)
        review_id = review["review"]["id"]
        hit("POST /api/kb/reviews")
        _, reviews = admin_client.json("GET", "/api/kb/reviews?limit=10")
        assert reviews["items"]
        hit("GET /api/kb/reviews")
        admin_client.json("GET", f"/api/kb/reviews/{review_id}")
        hit("GET /api/kb/reviews/{review_id}")
        admin_client.json("POST", f"/api/kb/reviews/{review_id}/retry", {})
        hit("POST /api/kb/reviews/{review_id}/retry")
        admin_client.json("POST", f"/api/kb/reviews/{review_id}/decision",
                          {"decision": "reject", "note": "api-smoke 终审"})
        hit("POST /api/kb/reviews/{review_id}/decision")
        admin_client.json("POST", "/api/kb/reviews/run", {"limit": 5})
        hit("POST /api/kb/reviews/run")
        admin_client.json("POST", f"/api/kb/cases/{case_created_id}/status",
                          {"status": "active", "reason": "api-smoke 复核通过"})

        admin_client.json("DELETE", f"/api/kb/docs/{doc_created_id}")
        hit("DELETE /api/kb/docs/{id}")

        # 工程师粘贴进入审核；管理员改标题、批准或退回。
        _, eng_doc = engineer.json("POST", "/api/kb/docs", {
            "title": "工程师待审案例",
            "text": "【详细描述:SSD 只读】\n【原因分析】系统盘写保护\n【处理方案】更换 SSD",
        }, expected=202)
        draft_id = eng_doc["draft_id"]
        _, drafts = admin_client.json("GET", "/api/kb/drafts")
        assert drafts["items"]
        hit("GET /api/kb/drafts")
        admin_client.json("GET", f"/api/kb/drafts/{draft_id}")
        hit("GET /api/kb/drafts/{id}")
        admin_client.json("PUT", f"/api/kb/drafts/{draft_id}",
                          {"title": "工程师待审案例-已改"})
        hit("PUT /api/kb/drafts/{id}")
        admin_client.json("POST", f"/api/kb/drafts/{draft_id}/approve", {})
        hit("POST /api/kb/drafts/{id}/approve")
        _, eng_doc2 = engineer.json("POST", "/api/kb/docs", {
            "title": "工程师待退回案例",
            "text": "【原因分析】待补充\n【处理方案】待补充",
        }, expected=202)
        admin_client.json("POST", f"/api/kb/drafts/{eng_doc2['draft_id']}/reject",
                          {"reason": "资料不足"})
        hit("POST /api/kb/drafts/{id}/reject")

        # 导入任务 + 文件下载 + 取消
        csv_bytes = ("问题,答案\n" + '"VBPE2 复位","【原因分析】逻辑异常' + '\n' +
                     '【处理方案】升级逻辑"\n').encode("utf-8")
        _, imported = admin_client.multipart(
            "/api/kb/import", {"mode": "direct"},
            [("files", "api-kb.csv", csv_bytes, "text/csv")], expected=202)
        job_id = imported["job"]["id"]
        hit("POST /api/kb/import")
        _, jobs = admin_client.json("GET", "/api/kb/import/jobs")
        assert any(x["id"] == job_id for x in jobs["items"])
        hit("GET /api/kb/import/jobs")
        deadline = time.time() + 20
        job = imported["job"]
        while time.time() < deadline:
            _, job = admin_client.json("GET", f"/api/kb/import/jobs/{job_id}")
            if job["job"]["status"] in ("completed", "partial", "failed"):
                break
            time.sleep(0.3)
        hit("GET /api/kb/import/jobs/{id}")
        assert job["job"]["files"]
        file_id = job["job"]["files"][0]["id"]
        status, _, _ = admin_client.request("GET", f"/api/kb/files/{file_id}")
        assert status == 200
        hit("GET /api/kb/files/{id}")
        admin_client.json("POST", f"/api/kb/import/jobs/{job_id}/cancel", {})
        hit("POST /api/kb/import/jobs/{id}/cancel")

        # 迁移：列表、预检、导出/详情/下载/删除，导入错误路径
        _, migrations = admin_client.json("GET", "/api/system/migrations")
        assert "items" in migrations and "allowed_roots" in migrations
        hit("GET /api/system/migrations")
        target_root = f"/var/lib/bbu-smoke-{port}-target"
        _, preflight = admin_client.json("POST", "/api/system/migrations/workspace/preflight",
                                         {"target_root": target_root})
        assert preflight["ok"] is True
        hit("POST /api/system/migrations/workspace/preflight")
        admin_client.json("POST", "/api/system/migrations/workspace",
                          {"target_root": "", "password": "Platform!2026x"}, expected=400)
        hit("POST /api/system/migrations/workspace")
        _, exported = admin_client.json("POST", "/api/system/migrations/export",
                                        {"password": "Platform!2026x",
                                         "passphrase": "Migration!2026x"}, expected=202)
        migration_id = exported["migration"]["id"]
        hit("POST /api/system/migrations/export")
        detail = {}
        for _ in range(30):
            _, detail = admin_client.json("GET", f"/api/system/migrations/{migration_id}")
            if detail["migration"].get("phase") in ("ready", "failed"):
                break
            time.sleep(0.2)
        assert detail["migration"]["phase"] == "ready", detail
        hit("GET /api/system/migrations/{migration_id}")
        status, raw, _ = admin_client.request("GET",
            f"/api/system/migrations/{migration_id}/download")
        assert status == 200 and raw, raw[:200]
        hit("GET /api/system/migrations/{migration_id}/download")
        _, deleted_migration = admin_client.json(
            "DELETE", f"/api/system/migrations/{migration_id}",
            {"password": "Platform!2026x"})
        assert deleted_migration["ok"]
        hit("DELETE /api/system/migrations/{migration_id}")
        admin_client.json("POST", "/api/system/migrations/import", {},
                          expected=400)
        hit("POST /api/system/migrations/import")

        missing = EXPECTED_ROUTES - hits
        assert not missing, f"未覆盖路由: {sorted(missing)}"
        print(f"[PASS] REST 接口覆盖 {len(hits)}/{len(EXPECTED_ROUTES)}")
        for route in sorted(hits):
            print("  -", route)
        print("[PASS] 负向矩阵: 401 未登录 / 403 无 CSRF 或越权 / 404 不存在或非 owner / "
              "409 重复配置或重复导入 / 400 非法参数或非法状态")
        print(f"[PASS] 临时数据目录: {work}")
        print(f"[PASS] 主服务未受影响")
        return 0
    finally:
        for s in (openai_server, planner_server):
            if s is not None:
                s.shutdown()
                s.server_close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        if case_name:
            shutil.rmtree(ROOT / "cases" / case_name, ignore_errors=True)
        if os.environ.get("BBU_API_KEEP") == "1":
            print("[保留] 临时目录:", work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
