#!/usr/bin/env python3
"""浏览器代理 UI 冒烟：顶栏徽标、管理端执行位置/自检入口、免登录自检页。

运行：python3 tests/ui_relay_smoke.py
"""
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


def wait_health(host: str, port: int, timeout: int = 25) -> None:
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
    host, port = "127.0.0.1", free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-relay-ui-"))
    data_dir = work / "data"
    out = Path("/tmp/bbu-relay-ui")
    out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "BBU_DATA_DIR": str(data_dir),
        "BBU_SKILLS_DIR": str(work / "skills"),
        "BBU_CASES_DIR": str(work / "cases"),
        "BBU_MAX_WORKERS": "1",
        "PYTHONUNBUFFERED": "1",
    })
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
        admin.login("admin", "Platform!2026x")
        admin.json("POST", "/api/models", {
            "name": "UI 浏览器代理", "provider": "openai",
            "base_url": "https://maas.example.com/v1",
            "api_key": "ui-browser-key", "model": "browser-model",
            "roles": ["default"],
            "extra": {"transport": "browser", "browser_relay": {
                "max_concurrent": 1, "claim_timeout_sec": 5,
                "exec_timeout_sec": 30, "allow_roles": []}},
        }, expected=201)
        admin_cookie = admin.cookie.split("=", 1)[1]
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, executable_path="/usr/local/bin/chromium",
                args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
            ctx.add_cookies([{
                "name": "bbu_session", "value": admin_cookie, "domain": "127.0.0.1",
                "path": "/", "httpOnly": True, "secure": False, "sameSite": "Strict",
            }])
            page = ctx.new_page()
            page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                    if msg.type == "error" else None)
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.goto(f"http://{host}:{port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#relay-pill", timeout=15000)
            page.wait_for_timeout(2500)
            pill = page.locator("#relay-pill").inner_text()
            if "浏览器代理" not in pill:
                errors.append(f"顶栏徽标文案异常: {pill}")
            page.goto(f"http://{host}:{port}/#/models", wait_until="domcontentloaded")
            page.wait_for_selector("text=UI 浏览器代理", timeout=15000)
            body = page.locator("body").inner_text()
            for token in ("执行位置", "浏览器代理", "链路自检", "可回退"):
                if token not in body:
                    errors.append(f"模型列表缺少：{token}")

            check = ctx.new_page()
            check.on("pageerror", lambda exc: errors.append(f"check-pageerror:{exc}"))
            check.goto(f"http://{host}:{port}/relay-check.html", wait_until="domcontentloaded")
            check.wait_for_selector("#check-start", timeout=10000)
            for selector in ("#check-base", "#check-model", "#check-provider", "#check-key"):
                if not check.locator(selector).count():
                    errors.append(f"自检页缺少控件：{selector}")
            page.screenshot(path=str(out / "models.png"), full_page=True)
            check.screenshot(path=str(out / "relay-check.png"), full_page=True)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    if errors:
        print("[FAIL] 浏览器代理 UI 冒烟")
        for item in errors:
            print(" -", item)
        return 1
    print("[PASS] 顶栏徽标、模型配置执行位置/自检入口、免登录自检页")
    print(f"[INFO] 截图: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
