#!/usr/bin/env python3
"""R2 UI 冒烟：用户管理、审计、任务分配与 reviewer 视角（Playwright + 临时实例）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT / "tests"))

from api_full_smoke import Client, free_port  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

NEW_ADMIN_PASSWORD = "Platform!2026x"


def wait_health(host: str, port: int, timeout: int = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, raw, _ = Client(host, port).request("GET", "/api/health")
            if status == 200 and json.loads(raw)["ok"]:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("服务未在预期时间内启动")


def wait_task(client: Client, task_id: str, timeout: int = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = client.json("GET", f"/api/tasks/{task_id}")
        if data.get("status") in ("done", "failed", "needs_data"):
            return data
        time.sleep(0.5)
    raise RuntimeError(f"任务 {task_id} 未在 {timeout}s 内结束")


def main() -> int:
    host = "127.0.0.1"
    port = free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-r2-ui-"))
    data_dir = work / "data"
    env = os.environ.copy()
    env.update({"BBU_DATA_DIR": str(data_dir), "BBU_SKILLS_DIR": str(work / "skills"),
                "BBU_CASES_DIR": str(work / "cases"), "BBU_MAX_WORKERS": "1",
                "PYTHONUNBUFFERED": "1"})
    log = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "run.py", "--host", host, "--port", str(port)],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_health(host, port)
        admin = Client(host, port)
        initial = next(line.split("密码:", 1)[1].strip() for line in
                       (data_dir / "初始管理员密码.txt").read_text(encoding="utf-8").splitlines()
                       if line.startswith("密码:"))
        login = admin.login("admin", initial)
        admin.csrf = login["csrf"]
        changed = admin.json("POST", "/api/auth/password", {
            "old_password": initial, "new_password": NEW_ADMIN_PASSWORD})[1]
        admin.csrf = changed["csrf"]

        # 邀请 reviewer 并激活，随后创建一个真实任务并分配给他。
        invite = admin.json("POST", "/api/users/invites", {
            "username": "ui_reviewer", "display_name": "UI 复核专家",
            "role": "reviewer", "hours": 24}, expected=201)[1]
        reviewer = Client(host, port)
        reviewer.json("POST", "/api/auth/activate", {
            "code": invite["code"], "password": "Reviewer!2026x"}, expected=201)
        reviewer.login("ui_reviewer", "Reviewer!2026x")
        task_id = admin.task_upload("R2 UI 时钟任务",
                                    "NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
                                    ROOT / "tests/fixtures/logs/clock-demo.zip")
        task = wait_task(admin, task_id)
        assert task["status"] == "done"
        admin.json("POST", f"/api/tasks/{task_id}/assign-reviewer",
                   {"reviewer_id": invite["user"]["id"] if "user" in invite else None},
                   expected=200) if False else None
        # 邀请响应不含 user，从用户列表拿 reviewer id。
        users = admin.json("GET", "/api/users?q=ui_reviewer")[1]
        reviewer_id = next(u["id"] for u in users["items"] if u["username"] == "ui_reviewer")
        admin.json("POST", f"/api/tasks/{task_id}/assign-reviewer",
                   {"reviewer_id": reviewer_id})
        admin_cookie = admin.cookie.split("=", 1)[1]
        reviewer_cookie = reviewer.cookie.split("=", 1)[1]

        errors: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path="/usr/local/bin/chromium",
                                        args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
            ctx.add_cookies([{"name": "bbu_session", "value": admin_cookie,
                              "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                              "secure": False, "sameSite": "Strict"}])
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(f"http://{host}:{port}/#/users", wait_until="networkidle")
            page.wait_for_selector("#user-search")
            assert page.locator("tbody tr").count() >= 2
            assert page.locator("#user-search").count() == 1
            page.screenshot(path="/tmp/r2-users.png", full_page=True)
            page.goto(f"http://{host}:{port}/#/audit", wait_until="networkidle")
            page.wait_for_selector("#audit-action")
            assert page.locator("#audit-action").count() == 1
            assert page.locator("text=导出 CSV").count() == 1
            page.screenshot(path="/tmp/r2-audit.png", full_page=True)
            page.goto(f"http://{host}:{port}/#/tasks/{task_id}", wait_until="networkidle")
            page.wait_for_selector("text=/分配专家|调整专家/")
            page.screenshot(path="/tmp/r2-task-admin.png", full_page=True)
            ctx.close()

            rctx = browser.new_context(viewport={"width": 1440, "height": 1000})
            rctx.add_cookies([{"name": "bbu_session", "value": reviewer_cookie,
                               "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                               "secure": False, "sameSite": "Strict"}])
            rp = rctx.new_page()
            rp.on("pageerror", lambda e: errors.append(f"reviewer pageerror: {e}"))
            rp.goto(f"http://{host}:{port}/#/dashboard", wait_until="networkidle")
            rp.wait_for_selector("text=复核任务")
            assert rp.locator("a:has-text('新建诊断')").count() == 0
            rp.screenshot(path="/tmp/r2-reviewer-dashboard.png", full_page=True)
            rp.goto(f"http://{host}:{port}/#/tasks/{task_id}", wait_until="networkidle")
            rp.wait_for_selector("text=专家复核")
            assert rp.locator("button:has-text('取消任务')").count() == 0
            assert rp.locator("button:has-text('重跑')").count() == 0
            assert rp.locator("button:has-text('分配专家')").count() == 0
            rp.screenshot(path="/tmp/r2-task-reviewer.png", full_page=True)
            rp.goto(f"http://{host}:{port}/#/users", wait_until="networkidle")
            rp.wait_for_selector("text=无权访问")
            rctx.close()

            gctx = browser.new_context(viewport={"width": 1440, "height": 1000})
            gp = gctx.new_page()
            gp.goto(f"http://{host}:{port}/#/dashboard", wait_until="domcontentloaded")
            gp.wait_for_selector("#login-form")
            gp.wait_for_selector("#to-activate")
            gp.click("#to-activate")
            assert gp.locator("#activate-form").is_visible()
            gp.screenshot(path="/tmp/r2-activate.png", full_page=True)
            gctx.close()
            browser.close()
        if errors:
            print("UI console/page errors:")
            for item in errors:
                print(" -", item)
            return 1
        print("[PASS] R2 UI：用户管理 / 审计 / 管理员分配专家 / reviewer 任务详情与导航 / 激活表单")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
