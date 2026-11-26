#!/usr/bin/env python3
"""验证平台级消费：注册带推理数据的技能，跑一条真实任务，检查报告出现研判路径。"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

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
        self.host, self.port, self.cookie, self.csrf = host, port, "", ""

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
        h = {"Cookie": self.cookie} if self.cookie else {}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc and sc.startswith("bbu_session="):
            self.cookie = sc.split(";", 1)[0]
        status = resp.status
        conn.close()
        return status, data

    def json(self, method, path, payload=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if method in ("POST", "PUT", "PATCH", "DELETE") and self.csrf:
            headers["X-CSRF"] = self.csrf
        status, data = self.request(method, path, body, headers)
        if status >= 400:
            raise RuntimeError(f"{method} {path} -> {status}: {data[:300]!r}")
        return json.loads(data or b"{}")

    def upload_task(self, title, description, archive: Path) -> str:
        boundary = "----bbu" + uuid.uuid4().hex
        parts = []
        for name, value in (("title", title), ("description", description), ("mode", "fast")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                         f"{value}\r\n".encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                     f'filename="{archive.name}"\r\nContent-Type: application/zip\r\n\r\n'.encode()
                     + archive.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        status, data = self.request("POST", "/api/tasks", b"".join(parts), {
            "Content-Type": f"multipart/form-data; boundary={boundary}", "X-CSRF": self.csrf})
        if status >= 400:
            raise RuntimeError(f"上传失败 {status}: {data[:300]!r}")
        return json.loads(data)["id"]


def wait_health(host, port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=2)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError("服务未就绪")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bbu-reasoning-e2e-"))
    case_dir = ROOT / "cases/bbu-fault-vsw-watchdog/pos-caterr-0x634-10"
    archive = tmp / "watchdog.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in (case_dir / "input").rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(case_dir / "input"))
    port = free_port()
    env = os.environ.copy()
    env.update({"BBU_DATA_DIR": str(tmp / "data"),
                "BBU_SKILLS_DIR": str(ROOT / "dist/workshop-reasoning/skills"),
                "BBU_MAX_WORKERS": "2", "PYTHONUNBUFFERED": "1"})
    log = open(tmp / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "run.py", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_health("127.0.0.1", port)
        password = next(line.split("密码:", 1)[1].strip()
                        for line in (tmp / "data/初始管理员密码.txt").read_text(encoding="utf-8").splitlines()
                        if line.startswith("密码:"))
        c = Client("127.0.0.1", port)
        c.csrf = c.json("POST", "/api/auth/login", {"username": "admin", "password": password})["csrf"]
        c.csrf = c.json("POST", "/api/auth/password", {
            "old_password": password, "new_password": "Platform!2026x"})["csrf"]
        desc = (case_dir / "case.yaml").read_text(encoding="utf-8")
        import yaml
        desc = yaml.safe_load(desc)["description"]
        task_id = c.upload_task("推理路径平台验收", desc, archive)
        deadline = time.time() + 90
        while time.time() < deadline:
            task = c.json("GET", f"/api/tasks/{task_id}")
            if task["status"] in ("done", "failed", "needs_data", "cancelled"):
                break
            time.sleep(0.4)
        assert task["status"] == "done", task
        assert task["verdict"]["status"] == "matched", task["verdict"]
        assert task["verdict"].get("root_cause_name") == "CPU 中断异常导致看门狗复位", task["verdict"]
        assert task["result"]["plan"].get("experience_refs"), "未检索到种子专家剧本"
        status, md = c.request("GET", f"/api/tasks/{task_id}/report?format=md")
        text = md.decode("utf-8")
        assert status == 200 and "## 三、研判路径" in text, text[:1200]
        assert "总体策略" in text and "关键鉴别点" in text and "条件不成立，排除" in text
        print("[PASS] 任务:", task_id, "root=", task["verdict"]["root_cause_name"])
        print("[PASS] 报告推理章节:", len(text), "字符")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
