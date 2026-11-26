#!/usr/bin/env python3
"""用量统计 UI 冒烟：图表、表格、用户筛选与移动端无横向溢出。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
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


def seed(data_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    with sqlite3.connect(data_dir / "bbu.db") as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
        eng = conn.execute("SELECT id FROM users WHERE username='usage-eng'").fetchone()
        if not eng:
            from bbu.security import hash_password
            eng_id = conn.execute(
                "INSERT INTO users(username,display_name,role,password_hash,must_change,active,"
                "created_at,status,domain,notification_prefs) VALUES(?,?,?,?,0,1,?, 'active','','{}')",
                ("usage-eng", "现场工程师", "engineer", hash_password("Usage!2026x"),
                 now.strftime("%Y-%m-%dT%H:%M:%SZ"))).lastrowid
        else:
            eng_id = eng[0]
        models = [
            ("1", "DeepSeek-V3", "admin", "planning"),
            ("2", "Qwen2.5-72B", "admin", "report"),
            ("personal:1", "个人 GPT-4o", "personal", "assistant"),
        ]
        for day in range(30):
            for i, (mid, name, scope, purpose) in enumerate(models):
                calls = 1 + (day + i) % 4
                stamp = (now - timedelta(days=day, minutes=i * 3)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for k in range(calls):
                    uid = admin_id if day % 3 != 0 else eng_id
                    conn.execute(
                        "INSERT INTO llm_usage_events(user_id,task_id,task_kind,skill_name,model_id,"
                        "model_name,provider,model,scope,purpose,stage,status,input_tokens,"
                        "output_tokens,total_tokens,estimated_input_tokens,latency_ms,error,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
                        (uid, f"T-UI-{day:02d}", "diagnosis", "demo-bbu-clock-loss" if i == 0 else "",
                         mid, name, "mock", name, scope, purpose, purpose, "success",
                         120 + i * 40, 60 + k * 8, 180 + i * 40 + k * 8,
                         900 + i * 100, "", stamp))
        for day in range(4):
            stamp = (now - timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT INTO tasks(id,user_id,title,description,status,mode,input_json,stats_json,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"T-UI-{day:02d}", admin_id if day % 2 else eng_id, f"用量演示任务 {day + 1}",
                 "用于验证检查", "done", "fast", '[{"name":"a.zip","size":1024}]',
                 json.dumps({"llm_calls": 8, "input_tokens": 900, "output_tokens": 300,
                             "total_tokens": 1200}), stamp))
            conn.execute(
                "INSERT INTO task_skills(task_id,skill_name,skill_version,status,score,stage,"
                "result_json,stats_json,finished_at,invalidated) VALUES(?,?,?,?,?,?,?,?,?,0)",
                (f"T-UI-{day:02d}", "demo-bbu-clock-loss", "1.0.0", "done", 80, "done",
                 json.dumps({"evidence": [{"rule_id": "CLK032"}] * 3}),
                 json.dumps({"duration_ms": 90000 + day * 5000,
                             "tokens": {"total_tokens": 600}}), stamp))
        conn.commit()


def main() -> int:
    host, port = "127.0.0.1", free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-usage-ui-"))
    data_dir = work / "data"
    out = Path("/tmp/bbu-usage-ui"); out.mkdir(parents=True, exist_ok=True)
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
        login = admin.login("admin", initial)
        admin.json("POST", "/api/auth/password", {
            "old_password": initial, "new_password": "Platform!2026x"})
        seed(data_dir)
        admin_cookie = admin.cookie.split("=", 1)[1]
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path="/usr/local/bin/chromium",
                                        args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 1100})
            ctx.add_cookies([{"name": "bbu_session", "value": admin_cookie,
                              "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                              "secure": False, "sameSite": "Strict"}])
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(f"http://{host}:{port}/#/usage", wait_until="networkidle")
            page.wait_for_selector(".usage-chart")
            page.wait_for_selector("text=技能消耗")
            page.wait_for_selector("text=任务 Token")
            page.wait_for_selector("text=用户每日用量")
            assert page.locator(".usage-subdetails").count() == 1
            page.screenshot(path=str(out / "usage-1440.png"), full_page=True)
            assert page.locator(".usage-legend-item").count() >= 1
            assert page.locator(".usage-kpis .stat").count() == 4
            page.select_option("select[data-change='user']", str(
                page.locator("select[data-change='user'] option").nth(1).get_attribute("value")))
            page.wait_for_timeout(700)
            page.screenshot(path=str(out / "usage-filtered.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            page.wait_for_selector(".usage-chart")
            metrics = page.evaluate("""() => ({
              scrollH: document.documentElement.scrollHeight,
              chart: document.querySelector('.usage-chart-card')?.getBoundingClientRect().height,
              skills: document.querySelectorAll('.usage-table').length,
              kpis: getComputedStyle(document.querySelector('.usage-kpis')).gridTemplateColumns,
            })""")
            print("[mobile]", metrics)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 1, f"390px 横向溢出 {overflow}px"
            # 截图专用：解除 100% 高度钳制，让 full_page 覆盖完整内容；不改产品样式。
            page.evaluate("() => { document.documentElement.style.height='auto'; document.body.style.height='auto'; }")
            page.wait_for_timeout(250)
            page.screenshot(path=str(out / "usage-390.png"), full_page=True)
            assert not errors, f"页面脚本错误: {errors[:3]}"

            eng_client = Client(host, port)
            eng_client.login("usage-eng", "Usage!2026x")
            eng_cookie = eng_client.cookie.split("=", 1)[1]
            ctx2 = browser.new_context(viewport={"width": 1280, "height": 900})
            ctx2.add_cookies([{"name": "bbu_session", "value": eng_cookie,
                               "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                               "secure": False, "sameSite": "Strict"}])
            page2 = ctx2.new_page()
            page2.on("pageerror", lambda e: errors.append(f"engineer pageerror: {e}"))
            page2.goto(f"http://{host}:{port}/#/usage", wait_until="networkidle")
            page2.wait_for_selector(".usage-chart")
            assert page2.locator("select[data-change='user']").count() == 0
            assert page2.locator("text=仅显示我的调用记录").count() == 1
            page2.wait_for_selector("text=用户每日用量")
            page2.screenshot(path=str(out / "usage-user.png"), full_page=True)
            assert not errors, f"页面脚本错误: {errors[:3]}"
            browser.close()
        print("[PASS] 用量统计 UI: 1440px / 用户筛选 / 390px 无横向溢出")
        print("[PASS] 截图:", out)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil_ignore(work)


def shutil_ignore(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
