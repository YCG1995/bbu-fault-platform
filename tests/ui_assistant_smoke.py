#!/usr/bin/env python3
"""悬浮助手 UI 冒烟：挂载、问答、来源、管理端 MCP 面板与 Token 一次性展示。"""
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

NEW_ADMIN_PASSWORD = "Assistant!2026x"


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
    host = "127.0.0.1"
    port = free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-assistant-ui-"))
    data_dir = work / "data"
    env = os.environ.copy()
    env.update({"BBU_DATA_DIR": str(data_dir), "BBU_SKILLS_DIR": str(work / "skills"),
                "BBU_CASES_DIR": str(work / "cases"), "BBU_MAX_WORKERS": "1",
                "BBU_ASSISTANT_ENABLED": "true", "PYTHONUNBUFFERED": "1"})
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

            page.goto(f"http://{host}:{port}/#/dashboard", wait_until="networkidle")
            page.wait_for_selector("#assistant-fab")
            assert page.locator("#assistant-panel").is_hidden()
            page.click("#assistant-fab")
            page.wait_for_selector("#assistant-panel:not([hidden])")
            page.wait_for_selector("text=平台小助手")
            assert page.locator(".assistant-suggestion").count() >= 4

            page.fill("#assistant-input", "平台怎么新建诊断任务？")
            page.click("#assistant-send")
            page.wait_for_selector("text=先按平台文档给你定位到以下内容")
            assert page.locator(".assistant-source").count() >= 1
            page.screenshot(path="/tmp/assistant-ui.png", full_page=False)

            # 管理端 MCP 面板：生成 read Token 并只展示一次
            page.goto(f"http://{host}:{port}/#/system", wait_until="networkidle")
            page.click('[data-system-tab="mcp"]')
            page.wait_for_selector("#mcp-generate")
            assert page.locator("text=仅本机 loopback").count() >= 1
            page.click("#mcp-generate")
            page.wait_for_selector("#mcp-token-mode")
            page.click("[data-create-token]")
            page.wait_for_selector("#mcp-new-token")
            token = page.input_value("#mcp-new-token")
            assert token.startswith("bbu_mcp_")
            page.screenshot(path="/tmp/assistant-mcp-token.png", full_page=False)
            page.click('[data-act="close-modal"]')

            # 375px 下悬浮窗全宽且不产生整页横向滚动
            page.set_viewport_size({"width": 375, "height": 812})
            page.goto(f"http://{host}:{port}/#/dashboard", wait_until="networkidle")
            if page.locator("#assistant-panel").is_hidden():
                page.click("#assistant-fab")
            page.wait_for_selector("#assistant-panel:not([hidden])")
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
            page.screenshot(path="/tmp/assistant-ui-mobile.png", full_page=False)
            browser.close()

        assert not errors, "\n".join(errors)
        _, info = admin.json("GET", "/api/mcp/info")
        assert info["mcp"]["configured"] is True and info["mcp"]["mode"] == "read"
        assert token not in json.dumps(info, ensure_ascii=False)
        print("[PASS] 悬浮助手：挂载 / 建议问题 / 文档问答 / 来源展示 / 375px")
        print("[PASS] MCP 管理：面板 / read Token 生成 / 一次性展示 / 不回传明文")
        print("[PASS] 截图: /tmp/assistant-ui.png /tmp/assistant-mcp-token.png /tmp/assistant-ui-mobile.png")
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
