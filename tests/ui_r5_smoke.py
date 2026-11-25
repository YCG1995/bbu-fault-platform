#!/usr/bin/env python3
"""R5 UI 冒烟：真实案例列表、分配复核人、标签/证据时间窗、版本历史与移动端布局。"""
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
from bbu.db import Database  # noqa: E402
from bbu.evaluation import RealCaseStore  # noqa: E402

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


def main() -> int:
    host = "127.0.0.1"
    port = free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-r5-ui-"))
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
        admin_cookie = admin.cookie.split("=", 1)[1]

        store = RealCaseStore(Database(data_dir / "bbu.db"), data_dir)
        case = store.ingest(
            [ROOT / "tests/fixtures/logs/clock-demo.zip"],
            name="review-ui-clock", title="R5 UI 待复核时钟案例",
            description="CLK032 时钟失锁，PLL 无法锁定。", site="深圳实验室",
            device_type="BBU", software_version="V1.0", split="unlabeled",
            status="unlabeled", created_by="admin", origin="real")

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

            page.goto(f"http://{host}:{port}/#/cases", wait_until="networkidle")
            page.wait_for_selector("text=案例队列")
            assert page.locator("text=R5 UI 待复核时钟案例").count() >= 1
            page.locator("text=R5 UI 待复核时钟案例").first.click()
            page.wait_for_selector("text=根因标注")
            assert page.locator("text=待复核").count() >= 1

            page.click('[data-act="assign"]')
            page.wait_for_selector("#assign-reviewer")
            admin_id = admin.json("GET", "/api/auth/me")[1]["user"]["id"]
            page.wait_for_selector(f'#assign-reviewer option[value="{admin_id}"]',
                                   state="attached", timeout=10000)
            page.select_option("#assign-reviewer", str(admin_id))
            page.fill("#assign-note", "R5 UI 分配")
            page.click('[data-act="submit"]')
            page.wait_for_selector("text=复核人已更新")
            page.wait_for_timeout(500)
            page.fill('input[name="expected_roots"]', "时钟源失锁")
            page.fill('input[name="excluded_roots"]', "光模块故障")
            page.fill('input[name="window_start"]', "2025-01-01T00:00")
            page.fill('input[name="window_end"]', "2025-01-01T00:10")
            page.select_option('select[name="recovery_result"]', "已恢复")
            page.select_option('select[name="status"]', "golden")
            page.select_option('select[name="split"]', "test")
            page.fill('textarea[name="decision_note"]', "现场确认为参考源切换，双故障排除光模块")
            page.click('[data-act="approve"]')
            page.wait_for_selector('[data-act="ok"]')
            page.click('[data-act="ok"]')
            page.wait_for_selector("text=标签已保存并写入历史")
            page.wait_for_timeout(600)
            assert page.locator("text=已复核").count() >= 1
            assert page.locator("text=R5 UI 分配").count() >= 1

            page.set_viewport_size({"width": 375, "height": 812})
            page.goto(f"http://{host}:{port}/#/cases", wait_until="networkidle")
            page.wait_for_selector("text=案例队列")
            width_overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert width_overflow <= 2, f"375px 横向溢出 {width_overflow}px"
            page.screenshot(path="/tmp/r5-cases.png", full_page=True)
            browser.close()

        if errors:
            raise AssertionError("前端控制台错误: " + " | ".join(errors[:5]))
        print("[PASS] R5 UI：案例列表/分配/标注/确认复核/历史/375px 卡片化")
        print("[PASS] 截图: /tmp/r5-cases.png")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
