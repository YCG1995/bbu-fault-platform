#!/usr/bin/env python3
"""端到端冒烟：临时目录启动独立实例，验证登录、上传、单技能诊断、多技能并行与报告。"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Client:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.cookie = ""
        self.csrf = ""

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
        all_headers = {"Cookie": self.cookie} if self.cookie else {}
        all_headers.update(headers or {})
        conn.request(method, path, body=body, headers=all_headers)
        resp = conn.getresponse()
        data = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith("bbu_session="):
            self.cookie = set_cookie.split(";", 1)[0]
        status = resp.status
        conn.close()
        return status, data

    def json(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if method in ("POST", "PUT", "PATCH", "DELETE") and self.csrf:
            headers["X-CSRF"] = self.csrf
        status, data = self.request(method, path, body, headers)
        if status >= 400:
            raise RuntimeError(f"{method} {path} -> {status}: {data[:300]!r}")
        return json.loads(data or b"{}")

    def upload_task(self, title: str, description: str, mode: str, log_path: Path) -> str:
        boundary = "----bbu" + uuid.uuid4().hex
        parts: list[bytes] = []
        for name, value in (("title", title), ("description", description), ("mode", mode)):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode())
        content = log_path.read_bytes()
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{log_path.name}"\r\n'
            f"Content-Type: application/zip\r\n\r\n".encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        status, data = self.request("POST", "/api/tasks", body, {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-CSRF": self.csrf,
        })
        if status >= 400:
            raise RuntimeError(f"上传任务失败 {status}: {data[:300]!r}")
        return json.loads(data)["id"]


def wait_health(host: str, port: int, timeout: float = 30) -> None:
    deadline = time.time() + timeout
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


class _OpenAIMock(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = {"choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


class _PlanMock(BaseHTTPRequestHandler):
    """OpenAI 兼容的假 planner：按描述返回结构化计划。"""
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        user = "\n".join(str(m.get("content") or "") for m in payload.get("messages") or [])
        description = user.split("## 用户故障描述", 1)[-1].split("## 日志盘点", 1)[0]
        if "## 已执行技能与证据结果" in user:
            if "仅先验证时钟" in description:
                plan = {
                    "in_scope": True,
                    "summary": "首波时钟已命中但仍有光路疑点，补验光模块",
                    "hypothesis": ["光模块收光链路异常"],
                    "steps": [{"id": "P2", "title": "补验光路",
                               "objective": "用独立的光模块证据解释剩余疑点",
                               "skills": ["demo-bbu-optical-rx"],
                               "gate": "always", "reason": "补规划只追加一个未执行技能"}],
                    "missing_info": [],
                }
            else:
                plan = {"in_scope": True, "summary": "证据已足够，不补规划",
                        "hypothesis": [], "steps": [], "missing_info": []}
        elif "光模块" in description or "RX_LOSS" in description:
            plan = {
                "in_scope": True,
                "summary": "时钟与光模块是两个独立现象，并行验证；不做额外扩展",
                "hypothesis": ["时钟参考源失锁", "光模块收光链路异常"],
                "steps": [{"id": "P1", "title": "双方向并行首验",
                           "objective": "用两个互相独立的直接证据技能同时验证两类告警",
                           "skills": ["demo-bbu-clock-loss", "demo-bbu-optical-rx"],
                           "gate": "always", "reason": "日志同时出现 CLK032 与 RX_LOSS"}],
                "missing_info": [],
            }
        else:
            plan = {
                "in_scope": True,
                "summary": "先验证时钟失锁的直接证据，命中即停止",
                "hypothesis": ["参考时钟源失锁"],
                "steps": [{"id": "P1", "title": "时钟直接证据首验",
                           "objective": "检索 CLK032 与 PLL unlock，确认是否为参考源失锁",
                           "skills": ["demo-bbu-clock-loss"],
                           "gate": "always", "reason": "描述明确出现 CLK032/PLL 失锁"}],
                "missing_info": [],
            }
        content = json.dumps(plan, ensure_ascii=False)
        body = {"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 80,
                          "total_tokens": len(user) // 4 + 80}}
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return


def start_mock_ai(handler=_OpenAIMock) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main() -> int:
    port = free_port()
    host = "127.0.0.1"
    work = Path(tempfile.mkdtemp(prefix="bbu-e2e-"))
    data_dir = work / "data"
    skills_dir = work / "skills"
    env = os.environ.copy()
    env.update({
        "BBU_DATA_DIR": str(data_dir),
        "BBU_SKILLS_DIR": str(skills_dir),
        "BBU_MAX_WORKERS": "2",
        "PYTHONUNBUFFERED": "1",
    })
    log_file = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--host", host, "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT)
    mock_server = None
    plan_server = None
    try:
        wait_health(host, port)
        password_file = data_dir / "初始管理员密码.txt"
        password = next(
            line.split("密码:", 1)[1].strip()
            for line in password_file.read_text(encoding="utf-8").splitlines()
            if line.startswith("密码:"))
        client = Client(host, port)
        login = client.json("POST", "/api/auth/login", {"username": "admin", "password": password})
        client.csrf = login["csrf"]
        changed = client.json("POST", "/api/auth/password", {
            "old_password": password, "new_password": "Platform!2026x"})
        client.csrf = changed["csrf"]

        # API 层验证模型 Key 加密落库、测试连通时解密、协议调用
        mock_server, mock_port = start_mock_ai()
        model = client.json("POST", "/api/models", {
            "name": "e2e-openai-mock", "provider": "openai",
            "base_url": f"http://127.0.0.1:{mock_port}/v1",
            "api_key": "test-key", "model": "test-model", "roles": ["default"],
        })["model"]
        assert model["api_key_masked"] and "test-key" not in json.dumps(model, ensure_ascii=False)
        tested = client.json("POST", f"/api/models/{model['id']}/test")
        assert tested["ok"], tested
        client.json("DELETE", f"/api/models/{model['id']}")
        print("[PASS] 模型接入: OpenAI 兼容测试连通 ->", tested["reply"])

        # 真实 planner 协议：假 OpenAI 兼容 endpoint 返回结构化计划
        plan_server, plan_port = start_mock_ai(_PlanMock)
        planner = client.json("POST", "/api/models", {
            "name": "e2e-planner", "provider": "openai",
            "base_url": f"http://127.0.0.1:{plan_port}/v1",
            "api_key": "planner-key", "model": "planner-model", "roles": ["planner"],
        })["model"]
        assert "planner" in planner["roles"]

        clock = client.upload_task(
            "E2E 单技能诊断", "NE_A 上报 CLK032 时钟失锁，PLL 无法锁定，GPS 参考源异常。",
            "fast", ROOT / "tests/fixtures/logs/clock-demo.zip")
        deadline = time.time() + 60
        while time.time() < deadline:
            task = client.json("GET", f"/api/tasks/{clock}")
            if task["status"] in ("done", "failed", "needs_data", "cancelled"):
                break
            time.sleep(0.4)
        assert task["status"] == "done", f"单技能任务状态异常: {task['status']} {task.get('error')}"
        assert task["verdict"]["status"] == "matched", task["verdict"]
        assert task["verdict"].get("root_cause_name"), task["verdict"]
        assert task.get("has_report"), "缺少 HTML 报告"
        assert task["result"]["plan"]["source"] == "ai", task["result"].get("plan")
        assert task["result"]["plan"]["steps"][0]["skills"] == ["demo-bbu-clock-loss"]
        # 未复核经验默认不进入检索；先由专家确认，第二次相似任务才应命中
        reviewed = client.json("POST", f"/api/tasks/{clock}/review", {
            "decision": "confirm", "action_result": "effective",
            "comment": "E2E 复核：确认时钟结论"})
        assert reviewed["review"]["decision"] == "confirm"

        mixed = client.upload_task(
            "E2E 并行扇出",
            "NE_A 时钟失锁 CLK032，PLL 无法锁定；同时 NE_B 光模块收光异常，接收光功率过低，RX_LOSS 告警。",
            "fast", ROOT / "tests/fixtures/logs/mixed-demo.zip")
        deadline = time.time() + 90
        while time.time() < deadline:
            task2 = client.json("GET", f"/api/tasks/{mixed}")
            if task2["status"] in ("done", "failed", "needs_data", "cancelled"):
                break
            time.sleep(0.5)
        assert task2["status"] == "done", f"并行任务状态异常: {task2['status']} {task2.get('error')}"
        assert len(task2["skills"]) >= 2, f"未发生多技能扇出: {task2['skills']}"
        assert all(s["status"] in ("matched", "no_evidence", "error", "skipped") for s in task2["skills"])
        assert task2["result"]["plan"]["source"] == "ai", task2["result"].get("plan")
        assert set(task2["result"]["plan"]["steps"][0]["skills"]) == {
            "demo-bbu-clock-loss", "demo-bbu-optical-rx"}
        assert task2["result"]["plan"]["experience_refs"], "第二次相似任务未检索到历史经验"

        hybrid = client.upload_task(
            "E2E 一次补规划",
            "NE_A 时钟失锁 CLK032，仅先验证时钟。",
            "fast", ROOT / "tests/fixtures/logs/mixed-demo.zip")
        deadline = time.time() + 90
        while time.time() < deadline:
            task3 = client.json("GET", f"/api/tasks/{hybrid}")
            if task3["status"] in ("done", "failed", "needs_data", "cancelled"):
                break
            time.sleep(0.5)
        assert task3["status"] in ("done", "needs_data"), f"补规划任务状态异常: {task3}"
        replan = task3["result"]["plan"].get("replan") or {}
        assert replan.get("status") == "executed", replan
        assert "demo-bbu-optical-rx" in {
            x for s in (replan.get("steps") or []) for x in (s.get("skills") or [])
        }, replan
        assert len({s["skill_name"] for s in task3["skills"]}) >= 2, task3["skills"]

        deadline = time.time() + 15
        self_drafts = []
        while time.time() < deadline:
            drafts = client.json("GET", "/api/workshop/drafts")["items"]
            self_drafts = [d for d in drafts if d.get("category") == "自学习"]
            if self_drafts:
                break
            time.sleep(0.5)
        assert self_drafts, "成功任务未自动沉淀工坊草稿"
        assert len(self_drafts) == 1, f"同根因自动沉淀未去重: {len(self_drafts)}"

        status, report = client.request("GET", f"/api/tasks/{mixed}/report?format=html")
        assert status == 200 and b"BBU" in report and len(report) > 1000, "HTML 报告内容异常"
        assert "AI 诊断计划".encode() in report, "报告缺少 AI 计划附录"
        assert "子技能原始报告".encode() in report, "报告缺少原始技能报告附录"

        print("[PASS] 单技能任务:", clock, task["verdict"])
        print("[PASS] 多技能任务:", mixed, "skills =", [s["skill_name"] for s in task2["skills"]])
        print("[PASS] 一次补规划:", hybrid, "replan =", replan.get("status"),
              "skills =", [s["skill_name"] for s in task3["skills"]])
        print("[PASS] 自动沉淀草稿:", self_drafts[0]["title"], self_drafts[0]["status"])
        print("[PASS] HTML 报告字节:", len(report))
        return 0
    finally:
        if mock_server is not None:
            mock_server.shutdown()
            mock_server.server_close()
        if plan_server is not None:
            plan_server.shutdown()
            plan_server.server_close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        if os.environ.get("BBU_E2E_KEEP") == "1":
            print("[保留] 临时目录:", work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
