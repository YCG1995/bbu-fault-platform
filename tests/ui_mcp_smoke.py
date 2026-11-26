#!/usr/bin/env python3
"""MCP UI 冒烟：客户端配置分区、代码面板、复制按钮与移动端无横向溢出。"""
from __future__ import annotations

import json
import os
import shutil
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


def main() -> int:
    host, port = "127.0.0.1", free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-mcp-ui-"))
    data_dir = work / "data"
    out = Path("/tmp/bbu-mcp-ui")
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
            ctx.grant_permissions(["clipboard-read", "clipboard-write"])
            ctx.add_cookies([{"name": "bbu_session", "value": cookie,
                              "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                              "secure": False, "sameSite": "Strict"}])
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(f"http://{host}:{port}/#/system", wait_until="networkidle")
            page.click('[data-system-tab="mcp"]')
            page.wait_for_selector("#mcp-config-example")
            page.wait_for_timeout(200)
            assert page.locator(".mcp-client-layout").count() == 1
            assert page.locator(".mcp-client-notes").count() == 1
            assert page.locator(".mcp-steps li").count() == 3
            assert page.locator(".mcp-code-panel").count() == 1
            assert page.locator(".mcp-client-card textarea").count() == 0
            assert page.evaluate(
                "() => document.querySelector('#mcp-config-example').tagName") == "CODE"
            code = page.locator("#mcp-config-example").inner_text()
            assert '"mcpServers"' in code and "<YOUR_TOKEN>" in code
            page.evaluate("() => { document.documentElement.style.height='auto'; document.body.style.height='auto'; }")
            page.screenshot(path=str(out / "mcp-1440.png"), full_page=True)
            page.click("#mcp-copy-config")
            page.wait_for_timeout(150)
            clip = page.evaluate("() => navigator.clipboard.readText()")
            assert '"mcpServers"' in clip and "<YOUR_TOKEN>" in clip

            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            page.click('[data-system-tab="mcp"]')
            page.wait_for_selector("#mcp-config-example")
            page.wait_for_timeout(200)
            page.evaluate("() => { document.documentElement.style.height='auto'; document.body.style.height='auto'; }")
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 1, f"390px 横向溢出 {overflow}px"
            assert page.locator(".mcp-client-layout").count() == 1
            assert page.locator(".mcp-code-panel").count() == 1
            page.screenshot(path=str(out / "mcp-390.png"), full_page=True)
            assert not errors, f"页面脚本错误: {errors[:3]}"
            browser.close()
        print("[PASS] MCP UI: 配置分区 / 代码面板 / 复制 JSON / 390px 无横向溢出")
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
