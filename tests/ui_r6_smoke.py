#!/usr/bin/env python3
"""R6 UI 冒烟：个人 MaaS Key 配置卡、保存、掩码与默认模型异常提示。"""
from __future__ import annotations

import json
import os
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
from bbu.db import Database  # noqa: E402

NEW_ADMIN_PASSWORD = "Platform!2026x"
KEY = "maas-ui-test-key-2026"


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
    work = Path(tempfile.mkdtemp(prefix="bbu-r6-ui-"))
    data_dir = work / "data"
    env = os.environ.copy()
    env.update({"BBU_DATA_DIR": str(data_dir), "BBU_SKILLS_DIR": str(work / "skills"),
                "BBU_CASES_DIR": str(work / "cases"), "BBU_MAX_WORKERS": "1",
                "BBU_PERSONAL_MODEL_BASE_URL": "http://127.0.0.1:1/v1",
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
        now = "2026-11-25T01:50:00Z"
        with sqlite3.connect(data_dir / "bbu.db") as conn:
            conn.execute(
                "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,error,"
                "created_at,finish_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("T-R6-MODEL-KEY", login["user"]["id"], "模型异常提示验证",
                 "验证默认模型异常时任务详情的个人 Key 提示。", "failed", "failed", 8,
                 "NoModelError: 管理员尚未配置默认模型，请配置个人 MaaS API Key。",
                 now, 1))

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

            page.goto(f"http://{host}:{port}/#/tasks/T-R6-MODEL-KEY", wait_until="networkidle")
            page.wait_for_selector("text=执行失败：")
            page.wait_for_selector("text=尚未配置可用模型")
            page.locator('[data-act="model-key"]').first.click()
            page.wait_for_selector("text=个人模型配置（企业内网 MaaS）")
            assert page.locator("text=deepseek-v4-flash").count() >= 1
            page.fill('input[name="api_key"]', KEY)
            page.screenshot(path="/tmp/r6-personal-key.png", full_page=True)
            page.locator('[data-act="save-key"]').click()
            page.wait_for_selector("text=个人模型配置已保存")
            page.wait_for_timeout(300)
            page.goto(f"http://{host}:{port}/#/dashboard", wait_until="networkidle")
            assert page.locator("text=尚未配置可用模型").count() == 0

            page.locator('[data-act="profile"]').click()
            page.wait_for_selector("text=企业内网模型（个人 Key）")
            assert page.locator("text=已配置 deepseek-v4-flash").count() >= 1
            page.locator('[data-act="close"]').first.click()

            # 375px 下个人 Key 弹窗仍可操作，无整页横向滚动
            page.set_viewport_size({"width": 375, "height": 812})
            page.goto(f"http://{host}:{port}/#/dashboard", wait_until="networkidle")
            page.locator('[data-act="profile"]').click()
            page.wait_for_selector("text=企业内网模型（个人 Key）")
            page.locator('[data-act="model-key"]').first.click()
            page.wait_for_selector("text=个人模型配置（企业内网 MaaS）")
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
            browser.close()

        assert not errors, "\n".join(errors)
        row = Database(data_dir / "bbu.db").one("SELECT api_key FROM user_model_configs")
        assert row and KEY not in row["api_key"] and row["api_key"].startswith("enc:v1:")
        _, key_view = admin.json("GET", "/api/profile/model-key")
        assert key_view["configured"] and KEY not in json.dumps(key_view, ensure_ascii=False)
        print("[PASS] R6 UI：任务详情默认模型异常提示 / 个人 Key 保存 / 掩码 / 375px")
        print("[PASS] 截图: /tmp/r6-personal-key.png")
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
