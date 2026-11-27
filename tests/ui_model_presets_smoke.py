#!/usr/bin/env python3
"""模型配置预设冒烟：平台下拉、Base URL/模型下拉、获取模型、max_tokens 自动带出与保存。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT / "tests"))

from api_full_smoke import Client, free_port  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402


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


class _ModelList(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            body = {"data": [{"id": "test-model"}, {"id": "test-model-2"}]}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)

    def log_message(self, *_args):
        return


def main() -> int:
    fake = ThreadingHTTPServer(("127.0.0.1", 0), _ModelList)
    fake_port = fake.server_address[1]
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    host, port = "127.0.0.1", free_port()
    work = Path(tempfile.mkdtemp(prefix="bbu-model-presets-"))
    data_dir = work / "data"
    out = Path("/tmp/bbu-model-presets-ui")
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
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            ctx.add_cookies([{"name": "bbu_session", "value": cookie,
                              "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                              "secure": False, "sameSite": "Strict"}])
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(f"http://{host}:{port}/#/models", wait_until="networkidle")
            page.locator('#view [data-act="new-model"]').click()
            page.wait_for_selector('.modal-mask')

            platform = page.locator("#model-platform")
            assert platform.locator("option").count() == 13, "应有 12 个平台预设 + 自定义"
            assert page.locator("#base-url-options option").count() >= 12, "Base URL 下拉缺失"
            platform.select_option("openai")
            assert page.locator("#base-url").input_value() == "https://api.openai.com/v1"
            assert page.locator("#model-config-name").input_value() == "OpenAI"
            assert page.locator("#model-name").input_value() == "gpt-4o"
            assert page.locator("#max-tokens").input_value() == "16384"

            page.locator("#model-name").fill("gpt-4o-mini")
            page.locator("#model-name").dispatch_event("change")
            assert page.locator("#max-tokens").input_value() == "16384"

            platform.select_option("anthropic")
            assert page.locator("#provider-select").input_value() == "anthropic"
            assert page.locator("#base-url").input_value() == "https://api.anthropic.com"
            assert page.locator("#model-name").input_value().startswith("claude-")

            platform.select_option("custom")
            page.locator("#provider-select").select_option("openai")
            page.locator("#base-url").fill(f"http://127.0.0.1:{fake_port}/v1")
            page.locator("#model-name").fill("test-model")
            page.locator('input[name="api_key"]').fill("test-key")
            page.locator("#max-tokens").fill("4096")
            page.locator("#model-discover").click()
            expect(page.locator("#model-options option")).to_have_count(2, timeout=10000)
            discovered = page.eval_on_selector_all(
                "#model-options option", "els => els.map(e => e.value)")
            assert discovered == ["test-model", "test-model-2"], discovered
            expect(page.locator("#model-select option")).to_have_count(3)
            page.select_option("#model-select", "test-model-2")
            assert page.locator("#model-name").input_value() == "test-model-2"
            page.wait_for_timeout(4200)
            page.screenshot(path=str(out / "model-presets-1280.png"), full_page=True)

            page.locator('input[name="name"]').fill("测试模型")
            page.locator('[data-act="save"]').click()
            page.wait_for_selector('.modal-mask', state="detached")
            expect(page.locator("#view")).to_contain_text("test-model", timeout=5000)

            # 编辑已有配置时，datalist 会按当前值过滤；真实 select 必须仍能
            # 展开并选择刚获取到的其他模型。
            page.locator('[data-act="edit"]').first.click()
            page.wait_for_selector(".modal-mask")
            page.locator("#model-discover").click()
            expect(page.locator("#model-options option")).to_have_count(2, timeout=10000)
            expect(page.locator("#model-select option")).to_have_count(3)
            page.select_option("#model-select", "test-model")
            assert page.locator("#model-name").input_value() == "test-model"
            page.locator('[data-act="cancel"]').click()
            page.wait_for_selector(".modal-mask", state="detached")

            page.set_viewport_size({"width": 390, "height": 844})
            page.locator('#page-actions [data-act="new-model"]').click()
            page.wait_for_selector(".modal-mask")
            page.locator("#model-platform").select_option("deepseek")
            page.wait_for_timeout(300)
            box = page.locator(".modal").bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= 391, box
            page.locator(".modal").screenshot(path=str(out / "model-presets-390.png"))
            page.locator('[data-act="cancel"]').click()
            page.wait_for_selector(".modal-mask", state="detached")
            assert not errors, f"页面脚本错误: {errors[:3]}"
            browser.close()
        print("[PASS] 模型预设: 平台/Base URL/模型下拉、获取列表、max_tokens 自动带出、保存成功")
        print("[PASS] 截图:", out / "model-presets-1280.png")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        fake.shutdown()
        fake.server_close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
