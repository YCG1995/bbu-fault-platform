#!/usr/bin/env python3
"""R3 UI 冒烟：草稿/上传、时间线、通知、全局搜索、reviewer 待办、移动端与键盘。"""
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
    work = Path(tempfile.mkdtemp(prefix="bbu-r3-ui-"))
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

        invite = admin.json("POST", "/api/users/invites", {
            "username": "ui_r3_reviewer", "display_name": "R3 复核专家",
            "role": "reviewer", "hours": 24}, expected=201)[1]
        reviewer = Client(host, port)
        reviewer.json("POST", "/api/auth/activate", {
            "code": invite["code"], "password": "Reviewer!2026x"}, expected=201)
        reviewer.login("ui_r3_reviewer", "Reviewer!2026x")
        task_id = admin.task_upload("R3 UI 时钟任务",
                                    "NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
                                    ROOT / "tests/fixtures/logs/clock-demo.zip")
        task = wait_task(admin, task_id)
        assert task["status"] == "done"
        users = admin.json("GET", "/api/users?q=ui_r3_reviewer")[1]
        reviewer_id = next(u["id"] for u in users["items"] if u["username"] == "ui_r3_reviewer")
        admin.json("POST", f"/api/tasks/{task_id}/assign-reviewer", {"reviewer_id": reviewer_id})
        admin_cookie = admin.cookie.split("=", 1)[1]
        reviewer_cookie = reviewer.cookie.split("=", 1)[1]
        base = f"http://{host}:{port}"

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
            page.goto(f"{base}/#/dashboard", wait_until="networkidle")
            page.wait_for_selector("text=开始使用")
            assert page.locator("text=配置一个模型").count() >= 1

            # 全局搜索：输入、结果、Esc 关闭与焦点回收
            page.click("button[data-act='search']")
            page.wait_for_selector("#global-search")
            assert page.locator("#global-search").evaluate("el => document.activeElement === el")
            page.fill("#global-search", "时钟")
            page.wait_for_selector(".search-hit")
            assert page.locator(".search-group-title", has_text="任务").count() >= 1
            page.screenshot(path="/tmp/r3-search.png", full_page=True)
            page.keyboard.press("Escape")
            page.wait_for_function("() => !document.querySelector('.modal-mask')")
            assert page.locator("button[data-act='search']").evaluate("el => document.activeElement === el")

            # 站内通知
            page.click("button[data-act='notifications']")
            page.wait_for_selector("text=站内通知")
            page.wait_for_selector(".notif-item")
            assert page.locator("text=诊断完成").count() >= 1
            page.screenshot(path="/tmp/r3-notifications.png", full_page=True)
            page.keyboard.press("Escape")

            # 新建任务：草稿自动保存 + 文件上传 + 提交
            page.goto(f"{base}/#/tasks/new", wait_until="networkidle")
            # 标题已改为盘点后自动生成，页面不再提供 #f-title；测试改为断言描述草稿。
            sentinel = "R3 浏览器验收：CLK032 时钟失锁，验证草稿自动保存与文件上传。"
            page.fill("#f-desc", sentinel)
            page.wait_for_timeout(1200)
            draft = admin.json("GET", "/api/task-drafts/current")[1]["draft"]
            assert draft and draft["description"] == sentinel, draft
            assert draft.get("title") in ("", None), draft
            page.set_input_files("#file-input", str(ROOT / "tests/fixtures/logs/clock-demo.zip"))
            page.wait_for_selector("text=已上传", timeout=30000)
            assert page.locator("#upload-summary").is_visible()
            page.screenshot(path="/tmp/r3-new-task.png", full_page=True)
            page.click("#submit-btn")
            page.wait_for_function("() => location.hash.startsWith('#/tasks/T')", timeout=30000)
            ui_task_id = page.evaluate("() => location.hash.split('/').pop()")
            ui_task = wait_task(admin, ui_task_id)
            # 标题由盘点结果自动生成（可能是 collectinfo 相关故障诊断），不再是描述首行。
            assert ui_task["title"] and ui_task["title_source"] == "deterministic", ui_task
            assert ui_task["description"] == sentinel, ui_task
            assert admin.json("GET", "/api/task-drafts/current")[1]["draft"] is None

            # 阶段时间线与结论理由
            page.goto(f"{base}/#/tasks/{task_id}", wait_until="networkidle")
            # 完成态默认打开“诊断报告”；结论在“诊断概览”，时间线在“运行详情”。
            page.click('[data-detail-tab="overview"]')
            page.wait_for_selector("text=为什么得出这个结论")
            assert page.locator("text=下一步").count() >= 1
            page.click('[data-detail-tab="runtime"]')
            page.wait_for_selector("text=阶段时间线")
            page.screenshot(path="/tmp/r3-task-detail.png", full_page=True)

            # 375px：无整页横向滚动，宽表格转卡片
            mctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True,
                                       has_touch=True)
            mctx.add_cookies([{"name": "bbu_session", "value": admin_cookie,
                               "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                               "secure": False, "sameSite": "Strict"}])
            mp = mctx.new_page()
            mp.on("pageerror", lambda e: errors.append(f"mobile pageerror: {e}"))
            mp.goto(f"{base}/#/tasks", wait_until="networkidle")
            mp.wait_for_selector("table.data.responsive")
            width = mp.evaluate("() => document.documentElement.scrollWidth")
            assert width <= 376, f"375px 任务页横向溢出 {width}"
            assert mp.evaluate("() => getComputedStyle(document.querySelector('.data.responsive thead')).display") == "none"
            mp.screenshot(path="/tmp/r3-mobile-tasks.png", full_page=True)
            mp.goto(f"{base}/#/users", wait_until="networkidle")
            mp.wait_for_selector("table.data.responsive")
            width = mp.evaluate("() => document.documentElement.scrollWidth")
            assert width <= 376, f"375px 用户页横向溢出 {width}"
            mp.screenshot(path="/tmp/r3-mobile-users.png", full_page=True)
            mctx.close()
            ctx.close()

            # reviewer：待复核、复核草稿恢复
            rctx = browser.new_context(viewport={"width": 1440, "height": 1000})
            rctx.add_cookies([{"name": "bbu_session", "value": reviewer_cookie,
                               "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                               "secure": False, "sameSite": "Strict"}])
            rp = rctx.new_page()
            rp.on("pageerror", lambda e: errors.append(f"reviewer pageerror: {e}"))
            rp.goto(f"{base}/#/dashboard", wait_until="networkidle")
            rp.wait_for_selector("text=待复核")
            rp.goto(f"{base}/#/tasks", wait_until="networkidle")
            rp.click("button[data-status='pending']")
            rp.wait_for_selector(f"text={task_id}")
            rp.goto(f"{base}/#/tasks/{task_id}", wait_until="networkidle")
            # 完成态默认在报告分区；复核按钮在诊断概览。
            rp.click('[data-detail-tab="overview"]')
            rp.wait_for_selector("text=专家复核")
            rp.click("button[data-decision='confirm']")
            rp.wait_for_selector("#review-form")
            rp.fill("textarea[name='comment']", "现场已更换 GPS 天线，确认时钟恢复。")
            rp.wait_for_timeout(500)
            rp.keyboard.press("Escape")
            rp.wait_for_function("() => !document.querySelector('.modal-mask')")
            rp.click("button[data-decision='confirm']")
            rp.wait_for_selector("#review-form")
            assert rp.input_value("textarea[name='comment']") == "现场已更换 GPS 天线，确认时钟恢复。"
            rp.screenshot(path="/tmp/r3-reviewer-draft.png", full_page=True)
            rp.keyboard.press("Escape")
            rctx.close()
            browser.close()
        if errors:
            print("UI console/page errors:")
            for item in errors:
                print(" -", item)
            return 1
        print("[PASS] R3 UI：搜索/通知/草稿上传/时间线/375px 卡片化/reviewer 待办与草稿恢复")
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
