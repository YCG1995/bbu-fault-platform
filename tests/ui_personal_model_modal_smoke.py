#!/usr/bin/env python3
"""个人模型弹窗冒烟：连点防叠层、关闭一次生效、X/遮罩/Esc 均可关闭。"""
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
    work = Path(tempfile.mkdtemp(prefix="bbu-model-modal-"))
    data_dir = work / "data"
    out = Path("/tmp/bbu-model-modal-ui")
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
            ctx = browser.new_context(viewport={"width": 938, "height": 664})
            ctx.add_cookies([{"name": "bbu_session", "value": cookie,
                              "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                              "secure": False, "sameSite": "Strict"}])
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(f"http://{host}:{port}/#/dashboard", wait_until="networkidle")
            trigger = page.locator('#view [data-act="model-key"]')
            assert trigger.count() == 1, "总览页应有个人 Key 配置入口"

            # 同步连点三次：接口返回前不能创建三个相同弹窗。
            page.evaluate("""() => {
              const b = document.querySelector('#view [data-act="model-key"]');
              b.click(); b.click(); b.click();
            }""")
            page.wait_for_selector('.modal-mask[data-modal="personal-model"]')
            page.wait_for_timeout(400)
            assert page.locator(".modal-mask").count() == 1, "连点后出现了叠层弹窗"
            page.screenshot(path=str(out / "personal-model-modal-938.png"))

            # 底部“关闭”一次生效。
            page.locator('.modal-mask [data-act="close"]').click()
            page.wait_for_timeout(200)
            assert page.locator(".modal-mask").count() == 0, "底部关闭未一次生效"

            # 右上角 X 一次生效。
            trigger.click(); page.wait_for_selector('.modal-mask[data-modal="personal-model"]')
            page.locator('.modal-mask [data-act="close-modal"]').click()
            page.wait_for_timeout(200)
            assert page.locator(".modal-mask").count() == 0, "X 关闭未一次生效"

            # 点击遮罩一次生效。
            trigger.click(); page.wait_for_selector('.modal-mask[data-modal="personal-model"]')
            page.mouse.click(10, 300)
            page.wait_for_timeout(200)
            assert page.locator(".modal-mask").count() == 0, "遮罩关闭未一次生效"

            # Esc 一次生效。
            trigger.click(); page.wait_for_selector('.modal-mask[data-modal="personal-model"]')
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            assert page.locator(".modal-mask").count() == 0, "Esc 关闭未一次生效"
            assert not errors, f"页面脚本错误: {errors[:3]}"
            browser.close()
        print("[PASS] 个人模型弹窗: 连点不叠层 / 关闭一次生效 / X、遮罩、Esc 均一次关闭")
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
