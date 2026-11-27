#!/usr/bin/env python3
"""故障知识库 UI 冒烟：双主标签、检索进度、文档库、独立导入、总览、任务记录与移动端布局。

运行：python3 tests/ui_kb_smoke.py
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
    work = Path(tempfile.mkdtemp(prefix="bbu-kb-ui-"))
    data_dir = work / "data"
    out = Path("/tmp/bbu-kb-ui")
    out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "BBU_DATA_DIR": str(data_dir),
        "BBU_SKILLS_DIR": str(work / "skills"),
        "BBU_CASES_DIR": str(work / "cases"),
        "BBU_MAX_WORKERS": "1",
        "PYTHONUNBUFFERED": "1",
        "BBU_EXAMPLES_DIR": str(ROOT / "examples"),
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
        _, sample = admin.json("POST", "/api/kb/sample", {})
        assert sample["documents"] >= 1, sample
        _, docs = admin.json("GET", "/api/kb/docs?size=5&status=active")
        doc_id = docs["items"][0]["id"]
        _, detail0 = admin.json("GET", f"/api/kb/docs/{doc_id}")
        case_id = detail0["doc"]["cases"][0]["id"]
        _, review0 = admin.json("POST", "/api/kb/reviews", {
            "target_type": "case", "target_id": case_id, "stage": "manual",
        }, expected=201)
        cookie = admin.cookie.split("=", 1)[1]

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, executable_path="/usr/local/bin/chromium",
                args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
            ctx.add_cookies([{
                "name": "bbu_session", "value": cookie, "domain": "127.0.0.1",
                "path": "/", "httpOnly": True, "secure": False, "sameSite": "Strict",
            }])
            page = ctx.new_page()
            page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}")
                    if msg.type == "error" else None)
            page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
            page.goto(f"http://{host}:{port}/#/kb", wait_until="domcontentloaded")
            try:
                page.wait_for_selector("#kb-query", timeout=15000)
            except Exception:
                print("[DEBUG] URL:", page.url)
                print("[DEBUG] BODY:", page.locator("body").inner_text()[:2000])
                print("[DEBUG] ERRORS:", errors)
                page.screenshot(path=str(out / "kb-debug.png"), full_page=True)
                raise
            page.wait_for_timeout(800)
            page.fill("#kb-query", "看门狗复位")
            page.click('[data-act="ask"]')
            page.wait_for_selector(".kb-answer", timeout=20000)
            body = page.locator("#view").inner_text()
            if "故障问答" not in body or "知识库管理" not in body:
                errors.append("问答页缺少双主标签")
            if page.locator(".kb-main-tab").count() < 2:
                errors.append("问答页主标签数量不足")
            if page.locator(".kb-record-note").count() == 0:
                errors.append("问答结果缺少任务记录入口")
            for token in ("知识库回答", "可能根因", "引用案例", "K1"):
                if token not in body:
                    errors.append(f"问答页缺少：{token}")
            if page.locator("a.kb-cite").count() == 0:
                errors.append("问答页没有可点击引用")
            page.screenshot(path=str(out / "kb-ask.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs", wait_until="domcontentloaded")
            page.wait_for_selector(".kb-main-tab.active", timeout=15000)
            page.wait_for_selector("table.data", timeout=15000)
            if page.locator("#kb-import-form").count() != 0:
                errors.append("文档库不应该再包含导入表单")
            doc_body = page.locator("#view").inner_text()
            for token in ("文档库", "文档", "知识库管理"):
                if token not in doc_body:
                    errors.append(f"文档页缺少：{token}")
            page.screenshot(path=str(out / "kb-docs.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs?tab=import", wait_until="domcontentloaded")
            page.wait_for_selector("#kb-import-form", timeout=15000)
            import_body = page.locator("#view").inner_text()
            for token in ("导入文档", "选择文件", "最近导入任务"):
                if token not in import_body:
                    errors.append(f"导入页缺少：{token}")
            # 通过页面真实上传一份 CSV，验证 FormData 和多文件入口。
            csv_path = work / "ui-import.csv"
            csv_path.write_text('问题,答案\n"UI 导入案例","【原因分析】页面导入测试'
                                '\n【处理方案】确认页面导入"\n', encoding="utf-8")
            page.set_input_files('#kb-import-form input[type="file"]', str(csv_path))
            page.click('[data-act="import"]')
            deadline = time.time() + 20
            job_text = ""
            while time.time() < deadline:
                page.wait_for_timeout(500)
                try:
                    job_text = page.locator("#kb-job-status").inner_text()
                except Exception:
                    job_text = ""
                if "已完成" in job_text or "部分完成" in job_text or "失败" in job_text:
                    break
            if "已完成" not in job_text and "部分完成" not in job_text:
                errors.append(f"页面导入未完成：{job_text}")
            page.screenshot(path=str(out / "kb-import.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs/{doc_id}", wait_until="domcontentloaded")
            page.wait_for_selector("#kb-doc-edit", timeout=15000)
            page.wait_for_selector(".kb-doc-case", timeout=15000)
            page.wait_for_timeout(300)
            detail = page.locator("#view").inner_text()
            for token in ("现象", "原因", "处理方案", "案例"):
                if token not in detail:
                    errors.append(f"详情页缺少：{token}")
            page.screenshot(path=str(out / "kb-detail.png"), full_page=True)
            page.click('[data-act="case-rev"] >> nth=0')
            page.wait_for_selector(".kb-rev-row", timeout=15000)
            page.screenshot(path=str(out / "kb-revisions.png"), full_page=True)
            page.click('.modal [data-act="close"] >> nth=0')

            # 管理端九个分区：概览、统计、审核、审计至少要有真实数据。
            for tab, selector, token in (
                ("overview", ".kb-tabbar", "需要关注"),
                ("stats", ".kb-trend", "30 天查询趋势"),
                ("audit", "table.data", "变更与审计"),
                ("review", "table.data", "AI / 人工审核"),
            ):
                page.goto(f"http://{host}:{port}/#/kb/docs?tab={tab}", wait_until="domcontentloaded")
                page.wait_for_selector(selector, timeout=15000)
                page.wait_for_timeout(300)
                tab_body = page.locator("#view").inner_text()
                if token not in tab_body:
                    errors.append(f"管理分区 {tab} 缺少：{token}")
                page.screenshot(path=str(out / f"kb-admin-{tab}.png"), full_page=True)
                if tab == "review":
                    page.click('[data-act="review-view"] >> nth=0')
                    page.wait_for_selector(".modal", timeout=10000)
                    modal_body = page.locator(".modal").inner_text()
                    if "审核" not in modal_body:
                        errors.append("审核详情弹窗缺少标题")
                    page.click('.modal [data-act="close-modal"]')
                if tab == "stats":
                    page.locator(".kb-range a").nth(0).click()
                    page.wait_for_selector(".kb-trend", timeout=15000)
                    page.wait_for_timeout(300)
                    seven_body = page.locator("#view").inner_text()
                    if "7 天查询趋势" not in seven_body:
                        errors.append("统计 7 天范围没有生效")
                    page.screenshot(path=str(out / "kb-admin-stats-7.png"), full_page=True)

            # 总览必须提供知识库入口，任务记录必须能看见检索操作。
            page.goto(f"http://{host}:{port}/#/dashboard", wait_until="domcontentloaded")
            page.wait_for_selector(".kb-dashboard-card", timeout=15000)
            dash = page.locator("#view").inner_text()
            for token in ("故障知识库", "生效文档", "操作记录"):
                if token not in dash:
                    errors.append(f"总览页缺少：{token}")
            nav = page.evaluate("""() => {
              const links = [...document.querySelectorAll('#nav a')].map((a) => a.getAttribute('href'));
              const system = document.querySelector('#nav a[href="#/system"]');
              let section = system?.previousElementSibling;
              while (section && !section.classList.contains('nav-section')) section = section.previousElementSibling;
              return { links, section: section ? section.textContent.trim() : '' };
            }""")
            if nav["section"] != "管理":
                errors.append(f"系统信息未放入管理分区：{nav['section']}")
            if nav["links"].index("#/system") < nav["links"].index("#/traces"):
                errors.append("系统信息没有排在执行轨迹后面")
            if nav["links"].index("#/kb") < nav["links"].index("#/workshop"):
                errors.append("故障知识库没有排在技能工坊后面")
            page.screenshot(path=str(out / "dashboard-kb.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/system", wait_until="domcontentloaded")
            page.wait_for_selector(".system-resource-card", timeout=15000)
            sys_body = page.locator("#view").inner_text()
            for token in ("系统资源", "CPU 使用率", "当前应用内存", "系统内存", "数据盘"):
                if token not in sys_body:
                    errors.append(f"系统信息概览缺少：{token}")
            page.screenshot(path=str(out / "system-resources.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/tasks?tab=kb", wait_until="domcontentloaded")
            page.wait_for_selector("#kb-op-table", timeout=15000)
            page.wait_for_timeout(300)
            ops_body = page.locator("#view").inner_text()
            for token in ("知识库操作", "看门狗复位", "检索"):
                if token not in ops_body:
                    errors.append(f"知识库操作记录缺少：{token}")
            page.screenshot(path=str(out / "tasks-kb.png"), full_page=True)

            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"http://{host}:{port}/#/kb", wait_until="domcontentloaded")
            try:
                page.wait_for_selector("#kb-query", timeout=10000)
            except Exception:
                print("[DEBUG-MOBILE]", page.url, page.locator("body").inner_text()[:1500], errors)
                page.screenshot(path=str(out / "kb-mobile-debug.png"), full_page=True)
                raise
            mobile_width = page.evaluate("() => ({scroll: document.documentElement.scrollWidth, "
                                         "inner: window.innerWidth})")
            if mobile_width["scroll"] > mobile_width["inner"] + 8:
                errors.append(f"移动端横向溢出：{mobile_width}")
            page.screenshot(path=str(out / "kb-mobile.png"), full_page=True)
            page.goto(f"http://{host}:{port}/#/kb/docs?tab=stats", wait_until="domcontentloaded")
            page.wait_for_selector(".kb-trend", timeout=15000)
            stats_width = page.evaluate("() => ({scroll: document.documentElement.scrollWidth, "
                                         "inner: window.innerWidth})")
            if stats_width["scroll"] > stats_width["inner"] + 8:
                errors.append(f"移动端统计页横向溢出：{stats_width}")
            page.screenshot(path=str(out / "kb-mobile-stats.png"), full_page=True)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    if errors:
        print("[FAIL] 故障知识库 UI 冒烟")
        for item in errors:
            print(" -", item)
        return 1
    print("[PASS] 知识库问答/进度/双主标签/文档分页/独立导入/详情/修订/导航顺序/系统信息资源/任务记录/移动端")
    print(f"[INFO] 截图: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
