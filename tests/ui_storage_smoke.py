#!/usr/bin/env python3
"""任务空间 UI 冒烟：工具栏、技能诊断条、统计卡与空态/任务清单。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
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


def seed_tasks(work: Path, data_dir: Path) -> None:
    upload = data_dir / "uploads"
    upload.mkdir(parents=True, exist_ok=True)
    raw = upload / "clock-login-2026-11-20.zip"
    raw.write_bytes(b"x" * 1250000)
    extracted = data_dir / "tasks" / "T-STORAGE-A" / "skills" / "demo-clock" / "unpacked"
    extracted.mkdir(parents=True, exist_ok=True)
    (extracted / "clock.log").write_bytes(b"y" * 3200000)
    (data_dir / "tasks" / "T-STORAGE-A" / "report.html").write_bytes(b"z" * 60000)
    with sqlite3.connect(data_dir / "bbu.db") as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        conn.execute(
            "INSERT INTO tasks(id,user_id,title,description,mode,status,inputs_state,"
            "input_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("T-STORAGE-A", admin_id, "BBU 时钟失锁复盘（2026-11-20）", "UI 演示",
             "demo", "success", "available",
             json.dumps([{"name": raw.name, "stored_path": str(raw)}], ensure_ascii=False),
             "2026-11-20T08:12:00+08:00"))
        conn.execute(
            "INSERT INTO tasks(id,user_id,title,description,mode,status,inputs_state,"
            "input_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("T-STORAGE-B", admin_id, "GNSS 卫星搜星异常排查（2026-11-21）", "UI 演示",
             "demo", "success", "available", "[]", "2026-11-21T21:40:00+08:00"))
        conn.commit()


def open_storage(page, host: str, port: int) -> None:
    page.goto(f"http://{host}:{port}/#/system", wait_until="networkidle")
    page.click('[data-system-tab="storage"]')


def main() -> int:
    host, port = "127.0.0.1", free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-storage-ui-"))
    data_dir = work / "data"
    out = Path("/tmp/bbu-storage-ui")
    out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"BBU_DATA_DIR": str(data_dir), "BBU_SKILLS_DIR": str(work / "skills"),
                "BBU_CASES_DIR": str(work / "cases"), "BBU_MAX_WORKERS": "1",
                "PYTHONUNBUFFERED": "1"})
    log = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "run.py", "--host", host, "--port", str(port)],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    errors: list[str] = []
    try:
        wait_health(host, port)
        admin = Client(host, port)
        initial = next(line.split("密码:", 1)[1].strip() for line in
                       (data_dir / "初始管理员密码.txt").read_text(encoding="utf-8").splitlines()
                       if line.startswith("密码:"))
        admin.login("admin", initial)
        admin.json("POST", "/api/auth/password", {
            "old_password": initial, "new_password": "Platform!2026x"})
        cookie = admin.cookie.split("=", 1)[1]
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path="/usr/local/bin/chromium",
                                        args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
            ctx.add_cookies([{"name": "bbu_session", "value": cookie,
                              "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                              "secure": False, "sameSite": "Strict"}])
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            open_storage(page, host, port)
            page.wait_for_selector(".storage-empty")
            assert page.locator(".storage-table").count() == 0
            assert page.locator(".storage-head .storage-toolbar").count() == 1
            page.evaluate("() => { document.documentElement.style.height='auto'; document.body.style.height='auto'; }")
            page.screenshot(path=str(out / "storage-empty-1440.png"), full_page=True)

            seed_tasks(work, data_dir)
            page.reload(wait_until="networkidle")
            page.click('[data-system-tab="storage"]')
            page.wait_for_selector(".storage-table")
            page.wait_for_timeout(200)
            assert page.locator(".storage-table tbody tr").count() == 2
            assert page.locator(".storage-empty").count() == 0
            assert page.locator(".storage-stats > .storage-stat").count() == 4
            assert page.locator(".storage-stats .card").count() == 0
            skill_box = page.locator(".storage-skill-actions .select").bounding_box()
            button_box = page.locator("#storage-skill-diagnose").bounding_box()
            assert skill_box and button_box
            assert abs((skill_box["y"] + skill_box["height"] / 2)
                       - (button_box["y"] + button_box["height"] / 2)) <= 2, "技能诊断控件未在同一行"
            header = page.locator(".storage-head").bounding_box()
            toolbar = page.locator(".storage-toolbar").bounding_box()
            assert header and toolbar and toolbar["x"] > header["x"] + header["width"] / 2, "工具栏未靠右"
            page.screenshot(path=str(out / "storage-rows-1440.png"), full_page=True)

            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            page.click('[data-system-tab="storage"]')
            page.wait_for_selector(".storage-table")
            page.wait_for_timeout(200)
            page.evaluate("() => { document.documentElement.style.height='auto'; document.body.style.height='auto'; }")
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 1, f"390px 横向溢出 {overflow}px"
            assert page.evaluate(
                "() => getComputedStyle(document.querySelector('.storage-table')).display") == "block"
            assert page.locator(".storage-table tbody tr").count() == 2
            page.screenshot(path=str(out / "storage-rows-390.png"), full_page=True)
            assert not errors, f"页面脚本错误: {errors[:3]}"
            browser.close()
        print("[PASS] 任务空间 UI: 工具栏靠右 / 技能诊断单行 / 空态 / 390px 卡片化")
        print("[PASS] 截图:", out)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
