#!/usr/bin/env python3
"""迁移 UI 冒烟：覆盖恢复危险区、迁移包选择、记录空态与移动端布局。"""
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


def open_migration(page, host: str, port: int) -> None:
    page.goto(f"http://{host}:{port}/#/system", wait_until="networkidle")
    page.click('[data-system-tab="migration"]')
    page.wait_for_selector(".migration-danger")


def main() -> int:
    host, port = "127.0.0.1", free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-migration-ui-"))
    data_dir = work / "data"
    out = Path("/tmp/bbu-migration-ui")
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
        sample = work / "sample.bbumig"
        sample.write_bytes(b"demo")
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
            open_migration(page, host, port)
            assert page.locator(".migration-danger-aside").count() == 1
            assert page.locator(".migration-danger-form").count() == 1
            assert page.locator(".migration-dropzone").count() == 1
            assert page.locator(".migration-empty").count() == 1
            assert page.locator(".migration-records-head").count() == 1
            assert page.evaluate(
                "() => getComputedStyle(document.querySelector('.migration-file-input')).opacity") == "0"
            page.evaluate("() => { document.documentElement.style.height='auto'; document.body.style.height='auto'; }")
            page.screenshot(path=str(out / "migration-empty-1440.png"), full_page=True)
            with page.expect_file_chooser() as chooser_info:
                page.locator(".migration-dropzone").click()
            chooser_info.value.set_files(str(sample))
            page.wait_for_timeout(150)
            assert page.locator("#migration-import-file-name").inner_text() == sample.name
            assert "0.00 MB" in page.locator("#migration-import-file-meta").inner_text()
            assert "已选择迁移包" in page.locator("#migration-import-state").inner_text()
            page.evaluate("() => { document.documentElement.style.height='auto'; document.body.style.height='auto'; }")
            page.screenshot(path=str(out / "migration-1440.png"), full_page=True)

            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="networkidle")
            page.click('[data-system-tab="migration"]')
            page.wait_for_selector(".migration-danger")
            page.wait_for_timeout(200)
            page.evaluate("() => { document.documentElement.style.height='auto'; document.body.style.height='auto'; }")
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 1, f"390px 横向溢出 {overflow}px"
            assert page.locator(".migration-danger-aside").count() == 1
            assert page.locator(".migration-dropzone").count() == 1
            page.screenshot(path=str(out / "migration-390.png"), full_page=True)
            assert not errors, f"页面脚本错误: {errors[:3]}"
            browser.close()
        print("[PASS] 迁移 UI: 覆盖恢复分区 / 自定义迁移包选择 / 记录空态 / 390px 无横向溢出")
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
