"""R1 账号安全回归：must_change、会话撤销、重置失效、限流与密码策略。"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.api import _client_ip, _set_cookie  # noqa: E402
from bbu.security import validate_password  # noqa: E402


class _FakeConfig:
    def __init__(self, proxies):
        self.proxies = proxies

    def get(self, key, default=None):
        return self.proxies if key == "trusted_proxies" else default


class _FakeApp:
    def __init__(self, proxies):
        self.config = _FakeConfig(proxies)


class _FakeReq:
    def __init__(self, ip, xff=""):
        self.ip = ip
        self.headers = {"X-Forwarded-For": xff} if xff else {}


class PasswordPolicyTest(unittest.TestCase):
    def test_password_policy(self) -> None:
        with self.assertRaises(ValueError):
            validate_password("short", "alice", 10)
        with self.assertRaises(ValueError):
            validate_password("AlicePassword!2026", "alice", 10)
        with self.assertRaises(ValueError):
            validate_password("password123", "bob", 10)
        with self.assertRaises(ValueError):
            validate_password("123456789012", "bob", 10)
        validate_password("StrongPass!2026", "alice", 10)

    def test_cookie_secure_flag(self) -> None:
        self.assertIn("; Secure", _set_cookie("token", 3600, True))
        self.assertNotIn("; Secure", _set_cookie("token", 3600, False))

    def test_forwarded_for_only_from_trusted_proxy(self) -> None:
        req = _FakeReq("10.0.0.5", "203.0.113.9")
        self.assertEqual(_client_ip(_FakeApp(["10.0.0.0/24"]), req), "203.0.113.9")
        self.assertEqual(_client_ip(_FakeApp([]), req), "10.0.0.5")
        self.assertEqual(_client_ip(_FakeApp(["192.168.0.0/16"]), req), "10.0.0.5")


class Client:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.cookie = ""
        self.csrf = ""

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        all_headers = {"Cookie": self.cookie} if self.cookie else {}
        all_headers.update(headers or {})
        conn.request(method, path, body=body, headers=all_headers)
        resp = conn.getresponse()
        raw = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith("bbu_session="):
            self.cookie = set_cookie.split(";", 1)[0]
        status = resp.status
        conn.close()
        return status, raw

    def json(self, method: str, path: str, payload: dict | None = None,
             expected: int = 200) -> tuple[int, dict]:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if method in ("POST", "PUT", "PATCH", "DELETE") and self.csrf:
            headers["X-CSRF"] = self.csrf
        status, raw = self.request(method, path, body, headers)
        assert status == expected, f"{method} {path} 期望 {expected}，实际 {status}: {raw[:300]!r}"
        return status, json.loads(raw or b"{}")

    def login(self, username: str, password: str, expected: int = 200) -> dict:
        _, data = self.json("POST", "/api/auth/login",
                            {"username": username, "password": password}, expected)
        if data.get("csrf"):
            self.csrf = data["csrf"]
        return data


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


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
        time.sleep(0.2)
    raise RuntimeError("测试服务未在限定时间内启动")


class AuthHardeningTest(unittest.TestCase):
    def test_account_security_flow(self) -> None:
        port = free_port()
        host = "127.0.0.1"
        work = Path(tempfile.mkdtemp(prefix="bbu-auth-"))
        data_dir = work / "data"
        env = os.environ.copy()
        env.update({
            "BBU_DATA_DIR": str(data_dir),
            "BBU_SKILLS_DIR": str(work / "skills"),
            "BBU_CASES_DIR": str(work / "cases"),
            "BBU_MAX_WORKERS": "1",
            "PYTHONUNBUFFERED": "1",
        })
        log_file = open(work / "server.log", "w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "run.py", "--host", host, "--port", str(port)],
                                cwd=str(ROOT), env=env,
                                stdout=log_file, stderr=subprocess.STDOUT)
        try:
            wait_health(host, port)
            initial = next(line.split("密码:", 1)[1].strip()
                           for line in (data_dir / "初始管理员密码.txt").read_text(
                               encoding="utf-8").splitlines() if line.startswith("密码:"))
            admin = Client(host, port)
            admin.login("admin", initial)
            changed = admin.json("POST", "/api/auth/password", {
                "old_password": initial, "new_password": "Platform!2026x"})[1]
            admin.csrf = changed["csrf"]
            self.assertFalse((data_dir / "初始管理员密码.txt").exists())

            created = admin.json("POST", "/api/users", {
                "username": "auth_user", "password": "InitialUser!2026",
                "display_name": "认证测试", "role": "engineer"}, expected=201)[1]
            uid = created["id"]

            a = Client(host, port)
            b = Client(host, port)
            a.login("auth_user", "InitialUser!2026")
            b.login("auth_user", "InitialUser!2026")
            status, raw = a.request("GET", "/api/tasks")
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(raw)["detail"]["code"], "password_change_required")

            a_changed = a.json("POST", "/api/auth/password", {
                "old_password": "InitialUser!2026",
                "new_password": "UserNew!2026x"})[1]
            a.csrf = a_changed["csrf"]
            self.assertGreaterEqual(a_changed["other_sessions_revoked"], 1)
            self.assertEqual(b.request("GET", "/api/auth/me")[0], 401)
            self.assertEqual(a.request("GET", "/api/auth/me")[0], 200)

            # 管理员重置密码后，旧会话立即失效，且新密码登录后仍必须先改密
            reset = admin.json("PUT", f"/api/users/{uid}", {
                "password": "ResetUser!2026"})[1]
            self.assertGreaterEqual(reset["sessions_revoked"], 1)
            self.assertEqual(a.request("GET", "/api/auth/me")[0], 401)
            e = Client(host, port)
            login = e.login("auth_user", "ResetUser!2026")
            self.assertEqual(login["user"]["must_change"], 1)
            self.assertEqual(e.request("GET", "/api/tasks")[0], 403)

            e_changed = e.json("POST", "/api/auth/password", {
                "old_password": "ResetUser!2026",
                "new_password": "UserFinal!2026x"})[1]
            e.csrf = e_changed["csrf"]
            d = Client(host, port)
            d.login("auth_user", "UserFinal!2026x")
            _, sessions = e.json("GET", "/api/auth/sessions")
            self.assertEqual(len(sessions["items"]), 2)
            _, revoked = e.json("POST", "/api/auth/sessions/revoke-others")
            self.assertEqual(revoked["revoked"], 1)
            self.assertEqual(d.request("GET", "/api/auth/me")[0], 401)
            self.assertEqual(len(e.json("GET", "/api/auth/sessions")[1]["items"]), 1)

            # 会话续期
            old_expiry = e.json("GET", "/api/auth/me")[1]["session_expires"]
            time.sleep(1.1)
            _, refresh = e.json("POST", "/api/auth/refresh")
            self.assertGreater(refresh["expires_at"], old_expiry)

            # 第 10 次失败触发账号级限流；同 IP 的其他账号仍可登录
            _, rate_user = admin.json("POST", "/api/users", {
                "username": "rate_user", "password": "RateUser!2026",
                "role": "engineer"}, expected=201)
            for _ in range(10):
                admin.json("POST", "/api/auth/login", {
                    "username": "rate_user", "password": "wrong-password"}, expected=401)
            status, raw = admin.request("POST", "/api/auth/login", json.dumps({
                "username": "rate_user", "password": "RateUser!2026"}).encode(), {
                    "Content-Type": "application/json"})
            self.assertEqual(status, 429)
            detail = json.loads(raw)["detail"]
            self.assertEqual(detail["code"], "login_rate_limited")
            self.assertGreater(detail["retry_after"], 0)
            other = Client(host, port)
            other.login("admin", "Platform!2026x")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log_file.close()


if __name__ == "__main__":
    unittest.main()
