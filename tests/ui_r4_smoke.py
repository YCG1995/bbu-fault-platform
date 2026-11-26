#!/usr/bin/env python3
"""R4 UI 冒烟：系统页上传升级包、预检、二次密码、状态轮询与回滚入口。"""
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

from api_full_smoke import Client, free_port, make_update_package  # noqa: E402
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


def main() -> int:
    host = "127.0.0.1"
    port = free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-r4-ui-"))
    data_dir = work / "data"
    fake_helper = work / "fake_upgrade_helper.py"
    fake_helper.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"state = Path({str(work / 'data').__repr__()}).parent\n"
        "job = sys.argv[1]\n"
        "job_dir = state / 'updates' / job\n"
        "request = json.loads((job_dir / 'request.json').read_text())\n"
        "status = json.loads((job_dir / 'status.json').read_text())\n"
        "if request.get('action') == 'upgrade':\n"
        "    backup = state / 'backups' / ('fake-' + job)\n"
        "    backup.mkdir(parents=True, exist_ok=True)\n"
        "    (backup / 'backup.json').write_text(json.dumps({'old_version': '1.0.0', 'new_version': request.get('target_version')}))\n"
        "    status.update({'phase': 'succeeded', 'progress': 100, 'message': 'fake upgrade succeeded',\n"
        "                   'backup_dir': str(backup), 'error': ''})\n"
        "else:\n"
        "    status.update({'phase': 'rolled_back', 'progress': 100, 'message': 'fake rollback succeeded', 'error': ''})\n"
        "(job_dir / 'status.json').write_text(json.dumps(status))\n",
        encoding="utf-8")
    env = os.environ.copy()
    env.update({"BBU_DATA_DIR": str(data_dir), "BBU_SKILLS_DIR": str(work / "skills"),
                "BBU_CASES_DIR": str(work / "cases"), "BBU_MAX_WORKERS": "1",
                "PYTHONUNBUFFERED": "1",
                "BBU_UPGRADE_HELPER_CMD": f"{sys.executable} {fake_helper}"})
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
        package = make_update_package(work, "9.9.9")

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
            page.goto(f"http://{host}:{port}/#/system", wait_until="networkidle")
            # 系统页默认只渲染概览；“离线升级”在 update tab 内，先切 tab。
            page.click('[data-system-tab="update"]')
            page.wait_for_selector('[data-system-panel="update"]:not([hidden])')
            page.wait_for_selector("text=离线升级")
            page.set_input_files("#update-file", str(package))
            page.click("#update-upload-btn")
            page.wait_for_selector("text=目标 v9.9.9", timeout=30000)
            assert page.locator("text=执行升级").count() >= 1
            page.screenshot(path="/tmp/r4-update-ready.png", full_page=True)

            page.click("button:has-text('执行升级')")
            page.wait_for_selector("#apply-password")
            page.fill("#apply-password", NEW_ADMIN_PASSWORD)
            page.click("button:has-text('确认升级')")
            page.wait_for_selector("text=升级成功", timeout=30000)
            page.screenshot(path="/tmp/r4-update-success.png", full_page=True)

            page.click("button:has-text('回滚到快照')")
            page.wait_for_selector("#rollback-password")
            page.fill("#rollback-password", NEW_ADMIN_PASSWORD)
            page.click("button:has-text('确认回滚')")
            page.wait_for_selector("text=已回滚", timeout=30000)
            page.screenshot(path="/tmp/r4-update-rolledback.png", full_page=True)
            ctx.close()
            browser.close()
        if errors:
            print("UI console/page errors:")
            for item in errors:
                print(" -", item)
            return 1
        print("[PASS] R4 UI：上传/预检/二次密码/升级状态/回滚入口")
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
