#!/usr/bin/env python3
"""1.31.0 故障知识库 UI 冒烟：导航拆分、库切换、跨库、导入批次、审核标签与库管理。"""
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


def wait_job(client: Client, job_id: str, timeout: int = 30) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        _, payload = client.json("GET", f"/api/kb/import/jobs/{job_id}")
        job = payload["job"]
        if job["status"] in ("completed", "partial", "failed", "cancelled"):
            return job
        time.sleep(0.3)
    raise RuntimeError(f"导入任务未完成：{job}")


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

        _, created = admin.json("POST", "/api/kb/libraries", {
            "name": "专家经验库", "kind": "expert", "description": "UI smoke",
        }, expected=201)
        expert_id = int(created["library"]["id"])
        expert_csv = work / "专家导入.csv"
        expert_csv.write_text('问题,答案\n"专家库专用案例","【原因分析】专家库专用原因'
                              '\n【处理方案】专家库专用方案"\n', encoding="utf-8")
        _, expert_job = admin.multipart(
            "/api/kb/import", {"mode": "direct", "kb_id": str(expert_id)},
            [("files", expert_csv.name, expert_csv.read_bytes(), "text/csv")], expected=202)
        assert wait_job(admin, expert_job["job"]["id"])["status"] == "completed"
        # 同一文件再导入到同一个专家库：应整文件跳过；再导入到默认库：应允许。
        _, dup_job = admin.multipart(
            "/api/kb/import", {"mode": "direct", "kb_id": str(expert_id)},
            [("files", expert_csv.name, expert_csv.read_bytes(), "text/csv")], expected=202)
        dup = wait_job(admin, dup_job["job"]["id"])
        assert dup["stats"].get("skipped_files", 0) >= 1, dup
        _, default_job = admin.multipart(
            "/api/kb/import", {"mode": "direct", "kb_id": "1"},
            [("files", "默认库同名.csv", expert_csv.read_bytes(), "text/csv")], expected=202)
        assert wait_job(admin, default_job["job"]["id"])["stats"].get("documents", 0) == 1

        expert_docs = admin.json("GET", f"/api/kb/docs?kb={expert_id}&status=active")[1]
        expert_doc = expert_docs["items"][0]
        expert_detail = admin.json("GET", f"/api/kb/docs/{expert_doc['id']}")[1]
        expert_case = expert_detail["doc"]["cases"][0]["id"]
        admin.json("POST", "/api/kb/reviews", {
            "target_type": "case", "target_id": expert_case, "stage": "manual",
        }, expected=201)
        # 再生成一条专家库待审草稿
        review_csv = work / "专家待审.csv"
        review_csv.write_text('问题,答案\n"专家待审案例","【原因分析】待审原因'
                              '\n【处理方案】待审方案"\n', encoding="utf-8")
        _, review_job = admin.multipart(
            "/api/kb/import", {"mode": "review", "kb_id": str(expert_id)},
            [("files", review_csv.name, review_csv.read_bytes(), "text/csv")], expected=202)
        wait_job(admin, review_job["job"]["id"])
        drafts = admin.json("GET", f"/api/kb/drafts?kb={expert_id}")[1]
        assert any(x["title"] == "专家待审案例" for x in drafts["items"]), drafts

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
            page.wait_for_selector("#kb-query", timeout=15000)
            page.wait_for_timeout(500)
            nav = page.evaluate("""() => {
              const links = [...document.querySelectorAll('#nav a')];
              return links.map((a) => ({text: a.textContent.trim(), href: a.getAttribute('href'),
                section: (() => { let s = a.previousElementSibling;
                  while (s && !s.classList.contains('nav-section')) s = s.previousElementSibling;
                  return s ? s.textContent.trim() : ''; })()}));
            }""")
            by_href = {x["href"]: x for x in nav}
            for href in ("#/kb", "#/kb/docs?tab=overview"):
                if href not in by_href:
                    errors.append(f"导航缺少 {href}")
                elif by_href[href]["section"] != "诊断":
                    errors.append(f"导航 {href} 不在诊断分组")
            if by_href.get("#/kb", {}).get("text") != "故障问答":
                errors.append("问答入口名称不是故障问答")
            if by_href.get("#/kb/docs?tab=overview", {}).get("text") != "知识库管理":
                errors.append("管理入口名称不是知识库管理")
            if page.locator(".kb-main-tab").count() != 0:
                errors.append("问答页仍有旧双主标签")
            if page.locator("#kb-library-select").count() != 1:
                errors.append("问答页缺少知识库选择")
            if page.locator("#kb-cross").count() != 1:
                errors.append("问答页缺少跨库检索开关")
            page.click("#kb-cross")
            page.wait_for_selector("#kb-cross-scope", state="visible", timeout=5000)
            if page.locator(".kb-cross-option").count() < 2:
                errors.append("跨库范围没有列出多个库")
            page.locator(f'.kb-cross-option[value="{expert_id}"]').check()
            page.fill("#kb-query", "专家库专用")
            page.click('.kb-filters [data-act="search"]')
            page.wait_for_selector(".kb-case-card", timeout=20000)
            cross_body = page.locator("#view").inner_text()
            if "检索范围 2 个库" not in cross_body or "专家经验库" not in cross_body:
                errors.append("跨库检索没有显示范围/来源库")
            page.fill("#kb-query", "看门狗复位")
            page.click('[data-act="ask"]')
            page.wait_for_selector(".kb-answer", timeout=20000)
            body = page.locator("#view").inner_text()
            for token in ("知识库回答", "可能根因", "引用案例"):
                if token not in body:
                    errors.append(f"问答页缺少：{token}")
            if page.locator("a.kb-cite").count() == 0:
                errors.append("问答页没有可点击引用")
            page.screenshot(path=str(out / "kb-ask.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs?tab=overview&kb={expert_id}",
                      wait_until="domcontentloaded")
            page.wait_for_selector(".kb-tabbar", timeout=15000)
            page.wait_for_timeout(500)
            over = page.locator("#view").inner_text()
            if "专家经验库" not in over:
                errors.append("概览没有切换到专家库")
            if "专家导入.csv" not in over:
                errors.append("概览最近导入没有显示文件名")
            if "需要关注" not in over:
                errors.append("概览缺少需要关注")
            if page.locator(".kb-main-tab").count() != 0:
                errors.append("管理页仍有旧双主标签")
            page.screenshot(path=str(out / "kb-overview.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs?tab=docs&kb={expert_id}",
                      wait_until="domcontentloaded")
            page.wait_for_selector("table.data", timeout=15000)
            docs_body = page.locator("#view").inner_text()
            if "专家库专用案例" not in docs_body:
                errors.append("专家库文档页没有显示本库文档")
            if "看门狗复位" in docs_body:
                errors.append("专家库文档页串入了默认库文档")
            page.screenshot(path=str(out / "kb-docs.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs?tab=review&view=reviews&kb={expert_id}",
                      wait_until="domcontentloaded")
            page.wait_for_selector(".kb-tabbar", timeout=15000)
            page.wait_for_timeout(400)
            review_body = page.locator("#view").inner_text()
            if "审核队列" not in review_body or "专家库专用案例" not in review_body:
                errors.append("审核标签没有显示专家库审核对象")
            for token in ("批量驳回", "批量重试", "处理排队中的 AI 预审"):
                if token not in review_body:
                    errors.append(f"审核标签缺少：{token}")
            if page.locator(".kb-tabbar").count() < 2:
                errors.append("审核区没有草稿/审核二级标签")
            for tab in page.locator(".kb-tabbar").nth(1).locator("a").all():
                if f"kb={expert_id}" not in (tab.get_attribute("href") or ""):
                    errors.append("审核标签切换丢失知识库参数")
                    break
            page.screenshot(path=str(out / "kb-review.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs?tab=review&view=drafts&kb={expert_id}",
                      wait_until="domcontentloaded")
            page.wait_for_selector(".kb-tabbar", timeout=15000)
            page.wait_for_timeout(300)
            draft_body = page.locator("#view").inner_text()
            for token in ("待入库草稿", "专家待审案例", "批量批准", "批量退回"):
                if token not in draft_body:
                    errors.append(f"草稿标签缺少：{token}")
            page.screenshot(path=str(out / "kb-drafts.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs?tab=import&kb={expert_id}",
                      wait_until="domcontentloaded")
            page.wait_for_selector("#kb-import-form", timeout=15000)
            imp = page.locator("#view").inner_text()
            for token in ("导入文档", "目标知识库", "回退所选", "重试失败", "删除记录"):
                if token not in imp:
                    errors.append(f"导入页缺少：{token}")
            if page.locator('[name="kb_id"]').input_value() != str(expert_id):
                errors.append("导入页目标知识库没有跟随当前库")
            page.screenshot(path=str(out / "kb-import.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs?tab=libraries", wait_until="domcontentloaded")
            page.wait_for_selector("#kb-lib-name", timeout=15000)
            lib_body = page.locator("#view").inner_text()
            for token in ("知识库列表", "专家经验库", "新建 / 恢复", "备份", "重命名",
                          "设为默认", "合并到"):
                if token not in lib_body:
                    errors.append(f"库管理缺少：{token}")
            page.screenshot(path=str(out / "kb-libraries.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/kb/docs?tab=audit&kb={expert_id}",
                      wait_until="domcontentloaded")
            page.wait_for_selector("table.data", timeout=15000)
            audit_body = page.locator("#view").inner_text()
            if "专家库专用案例" not in audit_body:
                errors.append("审计对象没有显示案例名称")
            page.screenshot(path=str(out / "kb-audit.png"), full_page=True)

            page.goto(f"http://{host}:{port}/#/tasks?tab=kb&kb={expert_id}",
                      wait_until="domcontentloaded")
            page.wait_for_selector("#kb-op-kb", timeout=15000)
            if page.locator("#kb-op-kb").input_value() != str(expert_id):
                errors.append("操作记录没有按知识库过滤")

            page.goto(f"http://{host}:{port}/#/traces", wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            traces_body = page.locator("#view").inner_text()
            if "故障知识库" not in traces_body:
                errors.append("执行轨迹没有知识库类型")
            page.screenshot(path=str(out / "traces.png"), full_page=True)

            page.set_viewport_size({"width": 390, "height": 844})
            for path, selector in (("#/kb", "#kb-query"),
                                   (f"#/kb/docs?tab=overview&kb={expert_id}", ".kb-tabbar")):
                page.goto(f"http://{host}:{port}/{path}", wait_until="domcontentloaded")
                page.wait_for_selector(selector, timeout=15000)
                width = page.evaluate("() => ({scroll: document.documentElement.scrollWidth, "
                                      "inner: window.innerWidth})")
                if width["scroll"] > width["inner"] + 8:
                    errors.append(f"移动端横向溢出 {path}: {width}")
            page.screenshot(path=str(out / "kb-mobile.png"), full_page=True)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    if errors:
        print("[FAIL] UI smoke")
        for err in errors:
            print(" -", err)
        return 1
    print("[PASS] KB UI smoke：导航拆分 / 库切换 / 跨库入口 / 导入批次 / 审核标签 / 库管理 / 轨迹 / 移动端")
    print(f"[PASS] 截图目录：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
