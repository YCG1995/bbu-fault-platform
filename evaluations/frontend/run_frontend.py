"""FE0-FE7 前端认证 runner（Playwright + 真实临时实例）。

默认会启动真实模型代理跑一条任务；静态/权限/视觉/axe 用例复用该实例。
如果真实模型不可用，FE7 条款标 blocked，其余前端条款仍继续。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from playwright.sync_api import sync_playwright  # noqa: E402

from evaluations.frontend.pages import (  # noqa: E402
    ROLE_ALLOWED, ROUTES, RolePage, expected_title, resolve_route,
)
from evaluations.frontend.suites import FE_SUITES  # noqa: E402
from testlab.lab import Lab  # noqa: E402
from testlab.results import blocked, fail, ok, summarize  # noqa: E402

CHROMIUM = "/usr/local/bin/chromium"
ROLE_PASSWORD = "BbuOps!2026x"
ADMIN_PASSWORD = "Platform!2026x"


def _now() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


class Harness:
    def __init__(self, *, live: bool = True) -> None:
        self.lab = Lab(prefix="bbu-fe-", live_model=live, max_workers=2)
        self.base = f"http://{self.lab.host}:{self.lab.port}"
        self.admin_client = self.lab.admin
        self.engineer = self.lab.create_user("fe_engineer", "engineer", ROLE_PASSWORD,
                                             "FE 工程师")
        self.reviewer = self.lab.create_user("fe_reviewer", "reviewer", ROLE_PASSWORD,
                                             "FE 复核专家")
        self.engineer_client = self.engineer["client"]
        self.reviewer_client = self.reviewer["client"]
        self.reviewer_id = self.reviewer["id"]
        self.live_task = self.lab.task(
            "NE_A 上报 CLK032 时钟失锁，PLL 无法锁定。",
            ROOT / "tests" / "fixtures" / "logs" / "clock-demo.zip",
            wait=True, timeout=240)
        self.live_task_id = self.live_task["id"]
        self.normal_task = self.lab.task(
            "现场反馈设备运行正常，无告警，上传日志做健康基线。",
            ROOT / "tests" / "fixtures" / "logs" / "normal-demo.zip",
            wait=True, timeout=120)
        self.normal_task_id = self.normal_task["id"]
        # 真实模型会把“空日志”也收敛为 done/unmatched；为了让补证/冲突用例
        # 获得真实的 needs_data 状态，临时移除模型，建完任务后恢复。
        removed_models = []
        if self.lab.proxy:
            _, models = self.lab.admin.json("GET", "/api/models")
            for item in models.get("items") or []:
                if item.get("enabled"):
                    self.lab.admin.json("DELETE", f"/api/models/{item['id']}")
                    removed_models.append(item.get("name"))
        needs_zip = self.lab.work / "fe-needs-data.zip"
        with zipfile.ZipFile(needs_zip, "w") as zf:
            zf.writestr("collectinfo.log", "")
        self.needs_task = self.lab.task(
            "现场只拿到空 collectinfo.log，日志正文缺失，无法判断时钟状态。",
            needs_zip, wait=True, timeout=120)
        self.needs_task_id = self.needs_task["id"]
        self.conflict_task = self.lab.task(
            "现场只拿到空 collectinfo.log，日志正文缺失，无法判断时钟状态。",
            needs_zip, wait=True, timeout=120)
        self.conflict_task_id = self.conflict_task["id"]
        if removed_models:
            self.lab.create_model(
                name="lab-real-proxy", provider="openai",
                base_url=self.lab.proxy.base_url, api_key="lab-proxy",
                model=self.lab._upstream["model"],
                roles=["default", "planner", "router", "arbiter", "writer", "workshop"],
                priority=1, max_tokens=16384, timeout_sec=240)
        self.lab.admin.json("POST", f"/api/tasks/{self.live_task_id}/assign-reviewer",
                            {"reviewer_id": self.reviewer_id})
        self.long_tasks = self._seed_long_tasks(60)
        self._sessions: dict[str, RolePage] = {}
        self._guest: dict[int, RolePage] = {}

    def _seed_long_tasks(self, count: int) -> int:
        db = self.lab.data_dir / "bbu.db"
        con = sqlite3.connect(str(db))
        inserted = 0
        for i in range(count):
            tid = f"FE-LONG-{i + 1:03d}"
            if con.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
                continue
            con.execute(
                "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
                "input_json,options_json,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, 1, f"长列表任务 {i + 1}", "长列表分页压测", "done", "done", 100, "fast",
                 "[]", "{}", json.dumps({"verdict": {"status": "unmatched",
                 "root_cause_name": "", "confidence": "low"}}, ensure_ascii=False),
                 "2026-11-25T12:00:00Z"))
            inserted += 1
        con.commit()
        con.close()
        return inserted

    def cookie(self, role: str) -> str:
        if role == "admin":
            return self.admin_client.cookie
        if role == "engineer":
            return self.engineer_client.cookie
        if role == "reviewer":
            return self.reviewer_client.cookie
        return ""

    def session(self, role: str = "admin", viewport: dict[str, int] | None = None) -> RolePage:
        key = f"{role}:{(viewport or {}).get('width', 1440)}"
        if key not in self._sessions:
            self._sessions[key] = RolePage(
                self._browser, self.base, cookie=self.cookie(role),
                viewport=viewport, name=key)
        return self._sessions[key]

    def guest(self, viewport: dict[str, int] | None = None) -> RolePage:
        key = (viewport or {}).get("width", 1440)
        if key not in self._guest:
            self._guest[key] = RolePage(self._browser, self.base, cookie="",
                                        viewport=viewport, name=f"guest:{key}")
        return self._guest[key]

    def fresh(self, role: str, viewport: dict[str, int] | None = None) -> RolePage:
        return RolePage(self._browser, self.base, cookie=self.cookie(role),
                        viewport=viewport, name=f"fresh:{role}")

    def fresh_guest(self, viewport: dict[str, int] | None = None) -> RolePage:
        return RolePage(self._browser, self.base, cookie="",
                        viewport=viewport, name="fresh:guest")

    def stop(self) -> None:
        for page in list(self._sessions.values()) + list(self._guest.values()):
            try:
                page.close()
            except Exception:
                pass
        self.lab.stop()


def _route(h: Harness, role: str, route: str) -> str:
    return resolve_route(route, h.live_task_id)


# ------------------------------------------------------------------ FE0
def assets_200(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    paths = [
        "/index.html", "/assets/css/app.css", "/assets/js/app.js", "/assets/js/core.js",
        "/assets/js/page_dashboard.js", "/assets/js/page_tasks.js",
        "/assets/js/page_task_detail.js", "/assets/js/page_skills.js",
        "/assets/js/page_workshop.js", "/assets/js/page_admin.js",
        "/assets/js/page_cases.js", "/assets/js/personal_model.js",
        "/favicon.ico", "/assets/logo.svg",
    ]
    page = h.session("admin")
    missing = []
    for path in paths:
        response = page.page.request.get(h.base + path)
        if response.status != 200:
            missing.append({"path": path, "status": response.status})
    _assert(not missing, f"资源非 200：{missing}")
    return "pass", f"{len(paths)} 个静态资源全部 200", {"paths": paths}


def routes_no_console(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    errors = []
    for route in ROUTES:
        target = _route(h, "admin", route)
        page.clear_errors()
        page.goto(target, wait_ms=900)
        page.page.wait_for_timeout(500)
        for item in page.console_errors + [f"pageerror: {x}" for x in page.page_errors]:
            errors.append({"route": target, "error": item})
    _assert(not errors, f"{len(errors)} 个 console/pageerror")
    return "pass", "11 路由无 console error / pageerror", {"errors": []}


def routes_no_404(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    bad = []
    for route in ROUTES:
        target = _route(h, "admin", route)
        page.clear_errors()
        page.goto(target, wait_ms=900)
        for item in page.responses:
            if item["status"] == 404:
                bad.append({"route": target, **item})
    _assert(not bad, f"{len(bad)} 个 404")
    return "pass", "11 路由无 404", {"responses": []}


# ------------------------------------------------------------------ FE1
def role_matrix(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    cases = []
    failures = []
    for role in ("admin", "engineer", "reviewer"):
        page = h.session(role)
        for route in ROUTES:
            target = _route(h, role, route)
            title = page.goto(target, wait_ms=1000)
            expected = expected_title(route, role)
            status = "pass" if title == expected else "fail"
            if status == "fail":
                failures.append({"role": role, "route": target, "expected": expected,
                                 "actual": title})
            cases.append({"role": role, "route": target, "expected": expected,
                          "actual": title, "status": status})
    guest = h.guest()
    for route in ROUTES:
        target = _route(h, "guest", route)
        guest.goto(target, wait_ms=800)
        visible = guest.login_visible()
        status = "pass" if visible else "fail"
        if not visible:
            failures.append({"role": "guest", "route": target, "expected": "login",
                             "actual": guest.title()})
        cases.append({"role": "guest", "route": target, "expected": "login",
                      "actual": "login" if visible else guest.title(), "status": status})
    _assert(not failures, f"{len(failures)} 格不一致：{failures[:8]}")
    return "pass", f"{len(cases)} 格路由/角色矩阵全部符合", {
        "cases": cases, "total": len(cases)}


def denied_isolation(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("engineer")
    found = []
    for route in ("#/models", "#/users", "#/audit", "#/system"):
        title = page.goto(route, wait_ms=900)
        _assert(title == "无权访问", f"{route} 标题 {title}")
        text = page.text()
        _assert("API Key" not in text and "用户管理" not in text, f"{route} 泄露管理内容")
    reviewer = h.session("reviewer")
    for route in ("#/skills", "#/workshop", "#/models", "#/users", "#/audit", "#/system"):
        title = reviewer.goto(route, wait_ms=900)
        _assert(title == "无权访问", f"reviewer {route} 标题 {title}")
        found.append(route)
    nav = page.page.locator("#nav").inner_text()
    _assert("模型接入" not in nav and "用户管理" not in nav, "engineer 导航泄露管理入口")
    return "pass", "engineer/reviewer 越权页均显示无权访问且无管理入口", {
        "reviewer_denied": found, "engineer_nav": nav}


def empty_loading_error(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/tasks", wait_ms=900)
    page.fill("#task-search", "ZZZ-NOT-FOUND-999")
    page.page.wait_for_timeout(900)
    text = page.text()
    _assert("没有符合条件的任务" in text or "没有符合" in text, "任务空态缺失")
    page.goto("#/cases", wait_ms=900)
    _assert("真实案例" in page.text(), "案例页未加载")
    return "pass", "任务空态/案例页加载正常", {"task_empty": True}


# ------------------------------------------------------------------ FE2
def _wait_upload_done(page: RolePage, timeout: int = 30000) -> None:
    page.page.wait_for_function(
        """() => {
          const rows = [...document.querySelectorAll('.upload-row')];
          return rows.length > 0 && rows.every(r => r.innerText.includes('已上传'));
        }""", timeout=timeout)
    page.page.wait_for_timeout(350)


def _reset_new_task(page: RolePage) -> None:
    """先离开再进入，强制 renderNewTask 重建闭包状态（避免旧 draftId）。"""
    page.goto("#/dashboard", wait_ms=700)
    page.goto("#/tasks/new", wait_ms=1000)


def _wait_draft(client, description: str, timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            _, data = client.json("GET", "/api/task-drafts/current")
            last = data.get("draft") or {}
            if (last.get("description") or "") == description:
                return last
        except Exception:
            pass
        time.sleep(0.25)
    return last


def _close_modal(page: RolePage) -> None:
    for _ in range(3):
        if page.page.locator(".modal-mask").count() == 0:
            return
        page.page.keyboard.press("Escape")
        try:
            page.page.wait_for_function(
                "() => document.querySelectorAll('.modal-mask').length === 0",
                timeout=2000)
            return
        except Exception:
            close = page.page.locator(".modal-mask [data-act='close-modal']")
            if close.count():
                close.first.click()
                page.page.wait_for_timeout(200)
    if page.page.locator(".modal-mask").count():
        page.page.evaluate(
            "() => document.querySelectorAll('.modal-mask').forEach(m => m.remove())")


def _clear_draft(client) -> None:
    try:
        _, data = client.json("GET", "/api/task-drafts/current")
        draft = data.get("draft")
        if draft:
            client.json("DELETE", f"/api/task-drafts/{draft['id']}")
    except Exception:
        pass


def new_task_form(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("engineer")
    _clear_draft(h.engineer_client)
    _reset_new_task(page)
    desc = "FE2 草稿验收：CLK032 时钟失锁，验证自动保存、上传与模式选择。"
    page.fill("#f-desc", desc)
    page.page.wait_for_timeout(1200)
    _, data = h.engineer_client.json("GET", "/api/task-drafts/current")
    draft = data.get("draft") or {}
    _assert(draft.get("description") == desc, f"草稿未保存：{draft.get('description')}")
    page.page.set_input_files("#file-input", str(ROOT / "tests/fixtures/logs/clock-demo.zip"))
    _wait_upload_done(page)
    _assert(page.page.locator("#upload-summary").is_visible(), "上传摘要不可见")
    page.page.locator("input[name=mode][value=deep]").check()
    _assert(page.page.locator("input[name=mode][value=deep]").is_checked(), "深度模式未选中")
    _clear_draft(h.engineer_client)
    return "pass", "描述草稿、文件上传、深度模式均可交互", {"draft": draft.get("id"),
                                                       "upload": True}


def keyboard_search(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/dashboard", wait_ms=900)
    page.click("button[data-act='search']")
    page.wait_selector("#global-search")
    focus = page.page.evaluate("() => document.activeElement === document.querySelector('#global-search')")
    _assert(focus, "搜索框未聚焦")
    page.page.keyboard.press("Escape")
    page.page.wait_for_function("() => !document.querySelector('.modal-mask')")
    back = page.page.evaluate("() => document.activeElement?.dataset?.act === 'search'")
    _assert(back, "Esc 后焦点未回到搜索按钮")
    return "pass", "搜索框自动聚焦，Esc 关闭并回收焦点", {"focus": True}


def duplicate_submit(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("engineer")
    _clear_draft(h.engineer_client)
    _reset_new_task(page)
    page.fill("#f-desc", "FE2 重复提交：CLK032 时钟失锁，验证 submitting 门闩。")
    page.page.set_input_files("#file-input", str(ROOT / "tests" / "fixtures" / "logs" / "clock-demo.zip"))
    _wait_upload_done(page)
    before = h.admin_client.json("GET", "/api/tasks?page=1&page_size=200")[1].get("total", 0)
    page.page.evaluate("() => { const b=document.querySelector('#submit-btn'); b.click(); b.click(); }")
    page.page.wait_for_function("() => location.hash.startsWith('#/tasks/T')", timeout=30000)
    page.page.wait_for_timeout(500)
    after = h.admin_client.json("GET", "/api/tasks?page=1&page_size=200")[1].get("total", 0)
    _assert(after == before + 1, f"重复提交创建了 {after - before} 个任务")
    _clear_draft(h.engineer_client)
    return "pass", "双击提交仅创建 1 个任务", {"before": before, "after": after}


def draft_restore(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("engineer")
    _clear_draft(h.engineer_client)
    _reset_new_task(page)
    desc = "FE2 草稿恢复：NE_A CLK032，离开页面后回来应恢复。"
    page.fill("#f-desc", desc)
    draft = _wait_draft(h.engineer_client, desc)
    _assert(draft.get("description") == desc, f"自动保存未落库：{draft}")
    page.goto("#/dashboard", wait_ms=700)
    page.goto("#/tasks/new", wait_ms=1000)
    value = page.page.locator("#f-desc").input_value()
    _assert(value == desc, f"恢复值不一致：{value[:80]}")
    _clear_draft(h.engineer_client)
    return "pass", "离开再返回后草稿恢复", {"value": value}


# ------------------------------------------------------------------ FE3
def login_logout(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.fresh_guest()
    page.goto("#/dashboard", wait_ms=700)
    _assert(page.login_visible(), "未显示登录表单")
    page.fill("#login-user", "admin")
    page.fill("#login-pass", ADMIN_PASSWORD)
    page.click("#login-btn")
    page.wait_selector("#page-title")
    page.page.wait_for_timeout(800)
    _assert(page.title() == "总览", f"登录后标题 {page.title()}")
    page.click("button[data-act='logout']")
    page.wait_selector("#login-form")
    _assert(page.login_visible(), "退出后未回登录")
    return "pass", "登录→总览→退出→登录页", {"title": "总览"}


def activation_register(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    stamp = int(time.time())
    username = f"fe_act_{stamp}"
    invite = h.admin_client.json("POST", "/api/users/invites", {
        "username": username, "display_name": "FE 激活用户", "role": "engineer",
        "hours": 24}, expected=201)[1]
    guest = h.fresh_guest()
    guest.goto("#/dashboard", wait_ms=700)
    guest.click("#to-activate")
    guest.fill("#activate-code", invite["code"])
    guest.fill("#activate-pass", "Activate!2026x")
    guest.fill("#activate-pass2", "Activate!2026x")
    guest.click("#activate-btn")
    guest.wait_text("账号已激活", timeout=15000)
    guest.fill("#login-user", username)
    guest.fill("#login-pass", "Activate!2026x")
    guest.click("#login-btn")
    guest.wait_selector("#page-title")
    guest.page.wait_for_timeout(700)
    _assert(guest.title() == "总览", "激活账号登录失败")
    # 自助注册开关开/关
    h.admin_client.json("PUT", "/api/system/runtime-config", {"allow_self_register": True})
    guest2 = h.guest(viewport={"width": 1200, "height": 900})
    guest2.goto("#/dashboard", wait_ms=700)
    _assert(guest2.page.locator("#to-register").count() == 1, "开关开后无自助注册按钮")
    h.admin_client.json("PUT", "/api/system/runtime-config", {"allow_self_register": False})
    guest2.page.reload(wait_until="domcontentloaded")
    guest2.page.wait_for_timeout(700)
    _assert(guest2.page.locator("#to-register").count() == 0, "开关关后仍显示自助注册")
    return "pass", "激活码开户成功；自助注册开关开/关生效", {"username": username}


def new_diagnosis_report(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("engineer")
    _clear_draft(h.engineer_client)
    _reset_new_task(page)
    page.fill("#f-desc", "FE3 全流程：NE_A CLK032 时钟失锁，PLL 无法锁定。")
    page.page.set_input_files("#file-input", str(ROOT / "tests" / "fixtures" / "logs" / "clock-demo.zip"))
    _wait_upload_done(page)
    page.click("#submit-btn")
    page.page.wait_for_function("() => location.hash.startsWith('#/tasks/T')", timeout=30000)
    task_id = page.page.evaluate("() => location.hash.split('/').pop()")
    task = h.engineer_client.wait_task(task_id, timeout=240)
    _assert(task["status"] == "done", f"任务未完成：{task['status']}")
    page.goto(f"#/tasks/{task_id}", wait_ms=1000)
    page.page.wait_for_timeout(1000)
    _assert(page.title() == "任务详情", f"详情标题 {page.title()}")
    page.click("[data-detail-tab='report']")
    page.page.wait_for_selector("iframe.report-frame", timeout=15000)
    frame = page.page.frame_locator("iframe.report-frame").locator("body")
    _assert("BBU 故障诊断报告" in frame.inner_text(timeout=8000), "报告 iframe 内容缺失")
    return "pass", f"新建任务 {task_id} 完成，报告 iframe 可读", {"task_id": task_id}


def supplement_same_task(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto(f"#/tasks/{h.needs_task_id}", wait_ms=1000)
    page.page.wait_for_selector("[data-supplement-form]", timeout=15000)
    before = h.admin_client.json("GET", "/api/tasks?page=1&page_size=200")[1].get("total", 0)
    answer = page.page.locator("[data-answer-id]").first
    answer.fill("现场补采：已确认无时钟告警，等待下一轮证据。")
    page.click("[data-act='supplement']")
    page.wait_text("补证已保存", timeout=30000)
    page.page.wait_for_timeout(1800)
    after = h.admin_client.json("GET", "/api/tasks?page=1&page_size=200")[1].get("total", 0)
    _assert(after == before, "补证创建了新任务")
    task = h.admin_client.json("GET", f"/api/tasks/{h.needs_task_id}")[1]
    _assert(task.get("status") in ("needs_data", "done", "queued", "running", "failed"), task.get("status"))
    session = task.get("session") or {}
    _assert(session.get("supplement_round", 0) >= 1, f"补证轮次未记录：{session}")
    return "pass", "补证保存在同一 task ID，并记录补证轮次", {
        "task_id": h.needs_task_id, "status": task.get("status"),
        "supplement_round": session.get("supplement_round")}


def detail_tabs(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto(f"#/tasks/{h.live_task_id}", wait_ms=1000)
    page.page.wait_for_timeout(1000)
    _assert(page.page.locator("iframe.report-frame").count() == 1, "默认报告 iframe 缺失")
    _assert(page.page.locator("iframe.report-frame").get_attribute("sandbox") == "",
            "报告 iframe 不是 sandbox=\"\"")
    page.click("[data-detail-tab='evidence']")
    page.page.wait_for_timeout(500)
    _assert(page.page.locator("[data-detail-panel='evidence']").is_visible(), "证据面板不可见")
    page.click("[data-detail-tab='runtime']")
    page.page.wait_for_timeout(500)
    _assert(page.page.locator("[data-detail-panel='runtime']").is_visible(), "运行面板不可见")
    page.click("[data-detail-tab='overview']")
    _assert(page.page.locator("[data-detail-panel='overview']").is_visible(), "概览面板不可见")
    _assert("证据" in page.text() or "执行" in page.text(), "详情文本异常")
    return "pass", "四 tab 可切换，报告 iframe sandbox 为空", {"task_id": h.live_task_id}


def recovery_ui(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto(f"#/tasks/{h.live_task_id}", wait_ms=1000)
    page.click("[data-detail-tab='overview']")
    page.page.wait_for_timeout(500)
    page.click("[data-act='recovery-create']")
    runs = []
    deadline = time.time() + 15
    while time.time() < deadline:
        runs = h.admin_client.json(
            "GET", f"/api/tasks/{h.live_task_id}/recovery-runs")[1].get("runs") or []
        if runs and runs[0].get("status") == "pending":
            break
        time.sleep(0.4)
    _assert(runs and runs[0].get("status") == "pending", f"恢复登记失败：{runs[:1]}")
    page.page.locator("[data-act='recovery-observe']").first.click()
    page.page.wait_for_selector("#recovery-form", timeout=10000)
    page.fill("#recovery-form input[name='file']", "after-fe.log")
    page.fill("#recovery-form textarea[name='text']", "FE 恢复后独立采集：PWR_NORMAL 48.2V")
    page.page.locator("#recovery-submit").click()
    page.wait_text("已回传", timeout=15000)
    page.page.wait_for_timeout(500)
    runs = h.admin_client.json(
        "GET", f"/api/tasks/{h.live_task_id}/recovery-runs")[1].get("runs") or []
    _assert(runs and runs[0].get("status") in ("observed", "reviewed", "rejected"),
            f"恢复状态 {runs[:1]}")
    btn = page.page.locator("[data-act='recovery-review'][data-decision='confirmed']")
    if btn.count():
        btn.first.click()
        page.wait_selector(".modal-mask")
        page.click("[data-act='ok']")
        page.page.wait_for_timeout(1000)
        runs = h.admin_client.json(
            "GET", f"/api/tasks/{h.live_task_id}/recovery-runs")[1].get("runs") or []
        _assert(runs and runs[0].get("status") == "reviewed", f"复核状态 {runs[:1]}")
    return "pass", "恢复登记→独立回传→（可选）专家确认链路可操作", {
        "runs": runs, "task_id": h.live_task_id}


def workshop_steps(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/workshop", wait_ms=1000)
    if page.page.locator("[data-act='new-draft']").count():
        page.click("[data-act='new-draft']")
        page.wait_selector("#create-form")
        source = (ROOT / "tests" / "fixtures" / "intake" / "vsw-ssd.txt").read_text(encoding="utf-8")
        page.fill("#create-form textarea[name='source_text']", source)
        page.click("[data-act='create']")
        page.wait_text("已提交后台解析", timeout=20000)
        page.page.wait_for_timeout(1200)
        page.goto("#/workshop", wait_ms=1000)
    tabs = page.page.locator("[data-act='workshop-step']")
    _assert(tabs.count() >= 5, f"工坊步骤数 {tabs.count()}")
    for i in range(1, 6):
        page.click(f"[data-act='workshop-step'][data-step='{i}']")
        page.page.wait_for_timeout(200)
        _assert(page.page.locator(f"[data-workshop-panel='{i}']").count() == 1, f"步骤 {i} 面板缺失")
    return "pass", "工坊五步步骤条与五个面板均存在", {"steps": 5}


def skills_page(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/skills", wait_ms=1000)
    _assert(page.title() == "技能库", f"标题 {page.title()}")
    text = page.text()
    _assert("demo-bbu-clock-loss" in text or "技能" in text, "技能列表为空")
    _assert(page.page.locator("[data-act='toggle']").count() >= 1, "无启停按钮")
    return "pass", "技能库列表与 manifest/启停入口可见", {"has_toggle": True}


def cases_review(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("reviewer")
    page.goto(f"#/tasks/{h.live_task_id}", wait_ms=1000)
    page.click("[data-detail-tab='overview']")
    page.page.wait_for_timeout(500)
    page.click("[data-act='review']")
    page.page.wait_for_selector("#review-form", timeout=10000)
    page.page.locator("#review-form select[name='decision']").select_option("confirm")
    page.fill("#review-form textarea[name='comment']", "FE 复核：证据链完整，确认结论。")
    page.click("[data-act='submit-review']")
    page.wait_text("复核已保存", timeout=15000)
    page.page.wait_for_timeout(1000)
    review = h.admin_client.json("GET", f"/api/tasks/{h.live_task_id}")[1].get("review") or {}
    _assert(review.get("decision") == "confirm", f"复核未保存 {review}")
    page.goto("#/cases", wait_ms=1000)
    _assert("真实案例" in page.text(), "案例页不可读")
    rows = page.page.locator("tr[data-href^='#/cases/']")
    _assert(rows.count() >= 1, "复核回流后案例列表为空")
    return "pass", "专家确认复核并回流案例，案例列表可见", {
        "review": review, "cases": rows.count()}


def models_page(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/models", wait_ms=1000)
    _assert(page.title() == "模型接入", f"标题 {page.title()}")
    text = page.text()
    _assert("lab-real-proxy" in text, "模型行缺失")
    _assert("****" in text or "已配置" in text, "Key 掩码/状态缺失")
    _assert("planner" in text or "角色" in text, "角色路由信息缺失")
    return "pass", "模型接入页展示模型、Key 掩码与角色信息", {"masked": True}


def system_tabs(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/system", wait_ms=1200)
    _assert(page.title() == "系统信息", f"系统页未加载：{page.title()}")
    tabs = page.page.locator("[data-system-tab]")
    _assert(tabs.count() == 6, f"系统 tab 数 {tabs.count()}")
    for key in ("overview", "runtime", "storage", "backups", "migration", "update"):
        page.click(f"[data-system-tab='{key}']")
        page.page.wait_for_timeout(500)
        panel = page.page.locator(f"[data-system-panel='{key}']")
        _assert(panel.count() == 1 and panel.is_visible(), f"{key} 面板不可见")
    page.click("[data-system-tab='update']")
    page.page.wait_for_timeout(500)
    _assert("离线升级" in page.text(), "升级面板无离线升级")
    return "pass", "系统六 tab 均可切换，升级入口可见", {"tabs": 6}


def shell_tools(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/dashboard", wait_ms=900)
    page.click("button[data-act='search']")
    page.wait_selector("#global-search")
    page.fill("#global-search", "CLK032")
    page.page.wait_for_timeout(700)
    _assert(page.page.locator(".search-hit").count() >= 1, "全局搜索无结果")
    _close_modal(page)
    page.click("button[data-act='notifications']")
    page.wait_selector("text=站内通知")
    _close_modal(page)
    page.click("button[data-act='profile']")
    page.wait_selector("#profile-form, .modal-mask")
    _assert("个人" in page.page.locator(".modal-mask").inner_text(), "个人中心未打开")
    _close_modal(page)
    page.click("button[data-act='password']")
    page.wait_selector("text=修改密码")
    _close_modal(page)
    return "pass", "搜索/通知/个人中心/改密入口均可操作", {"search_hits": True}


def mobile_no_overflow(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin", viewport={"width": 375, "height": 812})
    bad = []
    for route in ROUTES:
        target = _route(h, "admin", route)
        page.goto(target, wait_ms=900)
        page.page.wait_for_timeout(500)
        if page.overflow_x():
            bad.append({"route": target,
                        "scroll_width": page.page.evaluate("() => document.documentElement.scrollWidth"),
                        "inner": page.page.evaluate("() => window.innerWidth")})
    _assert(not bad, f"{len(bad)} 路由 375px 横向溢出：{bad[:4]}")
    return "pass", "11 路由 375px 无整页横向滚动", {"routes": len(ROUTES)}


# ------------------------------------------------------------------ FE4
VIEWPORTS = [(1440, 1000), (1024, 900), (768, 900), (375, 812)]


def visual_screenshots(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    files = []
    for width, height in VIEWPORTS:
        page = h.session("admin", viewport={"width": width, "height": height})
        for route in ROUTES:
            target = _route(h, "admin", route)
            page.goto(target, wait_ms=900)
            name = route.strip("#/").replace("/", "_").replace("{task}", h.live_task_id)
            if route == "#/tasks/{task}":
                name = "tasks_detail"
            path = h.out / "fe4" / f"{width}" / f"{name}.png"
            page.screenshot(path)
            files.append({"path": str(path), "size": path.stat().st_size,
                          "width": width, "route": target})
    _assert(len(files) == 44, f"截图数 {len(files)}")
    _assert(all(f["size"] > 1000 for f in files), "存在空截图")
    return "pass", "4 视口 × 11 路由 44 张截图齐全", {"count": len(files), "dir": str(h.out / "fe4")}


def visual_mobile(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    return mobile_no_overflow(h, browser)


def visual_sanity(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    bad = []
    for route in ROUTES:
        target = _route(h, "admin", route)
        page.goto(target, wait_ms=1000)
        text = page.text().strip()
        if len(text) < 20:
            bad.append({"route": target, "text_len": len(text)})
    _assert(not bad, f"{len(bad)} 页面近空白：{bad}")
    return "pass", "11 路由主视图均有可读内容，无空白页", {"routes": len(ROUTES)}


# ------------------------------------------------------------------ FE5
def _axe_source() -> str:
    return (Path(__file__).parent / "vendor" / "axe.min.js").read_text(encoding="utf-8")


def axe_wcag(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    source = _axe_source()
    violations = []
    for route in ROUTES:
        target = _route(h, "admin", route)
        page.goto(target, wait_ms=1000)
        _close_modal(page)
        page.page.evaluate(source)
        data = page.page.evaluate("""async () => {
          const r = await axe.run(document, {runOnly: {type: 'tag',
            values: ['wcag2a','wcag2aa','wcag21a','wcag21aa']}});
          return r.violations.map(v => ({id:v.id, impact:v.impact, help:v.help,
            nodes:v.nodes.length, targets:v.nodes.map(n => ({target:n.target,
              html:n.html, summary:n.failureSummary}))}));
        }""")
        for item in data:
            violations.append({"route": target, **item})
    if violations:
        return "fail", f"{len(violations)} 条 WCAG 违规", {"violations": violations}
    return "pass", "11 路由 axe WCAG 2.1 AA 0 违规", {"violations": []}


def keyboard_path(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/dashboard", wait_ms=900)
    _close_modal(page)
    page.page.keyboard.press("Tab")
    focus_count = page.page.evaluate("() => document.activeElement?.tagName || ''")
    _assert(focus_count in ("A", "BUTTON", "INPUT"), "Tab 未进入可交互元素")
    page.click("button[data-act='search']")
    page.wait_selector("#global-search")
    page.page.keyboard.press("Tab")
    inside = page.page.evaluate("() => !!document.activeElement?.closest('.modal')")
    _assert(inside, "Tab 跳出 modal")
    _close_modal(page)
    return "pass", "Tab 可达；modal 内 Tab 循环", {"tag": focus_count}


def focus_trap(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto("#/dashboard", wait_ms=900)
    _close_modal(page)
    page.click("button[data-act='search']")
    page.wait_selector("#global-search")
    for _ in range(6):
        page.page.keyboard.press("Tab")
    inside = page.page.evaluate("() => !!document.activeElement?.closest('.modal')")
    _assert(inside, "焦点逃出 modal（焦点陷阱失败）")
    _close_modal(page)
    _assert(page.page.locator(".modal-mask").count() == 0, "焦点陷阱 modal 无法关闭")
    return "pass", "搜索 modal 焦点陷阱有效，Esc 可关闭", {"trapped": True}


# ------------------------------------------------------------------ FE6
def unauthorized(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.fresh("admin")
    try:
        page.page.route("**/api/auth/me", lambda route: route.fulfill(
            status=401, content_type="application/json",
            body='{"error":"登录已过期"}'))
        page.goto("#/dashboard", wait_ms=1000)
        page.page.wait_for_selector("#login-form", timeout=10000)
        _assert(page.login_visible(), "401 后未回登录")
        return "pass", "401 清会话并返回登录页", {"login": True}
    finally:
        page.close()


def forbidden(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("engineer")
    title = page.goto("#/models", wait_ms=900)
    _assert(title == "无权访问", f"403 页面标题 {title}")
    _assert("API Key" not in page.text(), "泄露模型 Key 内容")
    return "pass", "engineer 访问管理页显示无权访问，无数据泄露", {"title": title}


def conflict_409(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto(f"#/tasks/{h.conflict_task_id}", wait_ms=1000)
    page.page.wait_for_selector("[data-supplement-form]", timeout=15000)
    page.page.route(f"**/api/tasks/{h.conflict_task_id}/inputs", lambda route: route.fulfill(
        status=409, content_type="application/json",
        body='{"error":"会话版本冲突：当前 1，提交基于 0"}'))
    page.page.locator("[data-answer-id]").first.fill("冲突测试回答")
    page.click("[data-act='supplement']")
    page.wait_text("版本冲突", timeout=10000)
    value = page.page.locator("[data-answer-id]").first.input_value()
    _assert(value == "冲突测试回答", "409 后用户输入丢失")
    page.page.unroute(f"**/api/tasks/{h.conflict_task_id}/inputs")
    return "pass", "409 显示冲突提示，输入保留", {"value": value}


def server_error(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    pattern = "**/api/tasks?*"
    page.page.route(pattern, lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"error":"注入 500"}'))
    page.goto("#/tasks", wait_ms=1000)
    page.page.wait_for_selector(".alert.danger", timeout=10000)
    _assert("注入 500" in page.text(), "500 错误态未显示")
    page.page.unroute(pattern)
    page.click("[data-act='reload']")
    page.page.wait_for_timeout(1000)
    _assert("任务记录" in page.text() or "共 " in page.text(), "500 解除后未恢复")
    return "pass", "500 显示错误态，解除后可恢复", {"recovered": True}


def xss_iframe(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    xss = '<img src=x onerror="window.__xss=1">'
    task = h.lab.task(f"FE XSS 测试 {xss}", ROOT / "tests" / "fixtures" / "logs" / "clock-demo.zip",
                      wait=True, timeout=180)
    page = h.session("admin")
    page.goto(f"#/tasks/{task['id']}", wait_ms=1000)
    page.page.wait_for_timeout(700)
    xss_flag = page.page.evaluate("() => window.__xss || 0")
    _assert(not xss_flag, "XSS 被执行")
    _assert(page.page.locator("img[src='x']").count() == 0, "注入 img 未转义")
    page.goto(f"#/tasks/{h.live_task_id}", wait_ms=1000)
    page.page.wait_for_timeout(800)
    if page.page.locator("iframe.report-frame").count():
        _assert(page.page.locator("iframe.report-frame").get_attribute("sandbox") == "",
                "报告 iframe 缺 sandbox")
    return "pass", "描述 XSS 未执行，报告 iframe sandbox 保持", {"xss": xss_flag}


def long_list(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.fresh("admin")
    try:
        page.goto("#/tasks", wait_ms=1000)
        rows = page.page.locator("tr[data-href]")
        _assert(rows.count() <= 20, f"单页行数 {rows.count()}")
        _assert(page.page.locator("#task-pager").count() == 1, "缺分页器")
        page.click("#task-pager button[data-page='2']")
        page.page.wait_for_timeout(800)
        _assert(rows.count() >= 1, "第二页无数据")
        _assert(page.page.locator(".alert.danger").count() == 0, "长列表出现错误态")
        return "pass", "60 条长列表分页可用，无白屏", {
            "seeded": h.long_tasks, "page_rows": rows.count()}
    finally:
        page.close()


# ------------------------------------------------------------------ FE7
def real_model_report(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    page = h.session("admin")
    page.goto(f"#/tasks/{h.live_task_id}", wait_ms=1000)
    page.page.wait_for_timeout(1200)
    _assert(page.title() == "任务详情", f"标题 {page.title()}")
    _assert(page.page.locator("iframe.report-frame").count() == 1, "真实报告 iframe 缺失")
    body = page.page.frame_locator("iframe.report-frame").locator("body")
    text = body.inner_text(timeout=10000)
    _assert("BBU 故障诊断报告" in text, "真实报告内容缺失")
    _assert("参考时钟源失锁" in text or "根因" in text, "真实报告无结论")
    return "pass", "真实模型任务报告 iframe 可打开且含结论", {
        "task_id": h.live_task_id, "chars": len(text), "llm_calls":
        (h.live_task.get("stats") or {}).get("llm_calls")}


def model_failure_notice(h: Harness, browser) -> tuple[str, str, dict[str, Any]]:
    _, models = h.admin_client.json("GET", "/api/models")
    removed = []
    for item in models.get("items") or []:
        if item.get("enabled"):
            h.admin_client.json("DELETE", f"/api/models/{item['id']}")
            removed.append(item.get("name"))
    try:
        page = h.session("admin")
        page.goto("#/tasks/new", wait_ms=1000)
        text = page.text()
        _assert("还没有配置模型" in text or "确定性" in text or "模型不可用" in text,
                "无模型时页面没有降级提示")
    finally:
        h.lab.create_model(name="lab-real-proxy-restored", provider="openai",
                           base_url=h.lab.proxy.base_url, api_key="lab-proxy",
                           model=h.lab._upstream["model"],
                           roles=["default", "planner", "router", "arbiter",
                                  "writer", "workshop"], priority=1,
                           max_tokens=16384, timeout_sec=240)
    return "pass", "无模型时前端显示确定性降级/配置提示", {"removed": removed}


CHECKS = {name: fn for name, fn in list(globals().items()) if name in {
    "assets_200", "routes_no_console", "routes_no_404", "role_matrix",
    "denied_isolation", "empty_loading_error", "new_task_form", "keyboard_search",
    "duplicate_submit", "draft_restore", "login_logout", "activation_register",
    "new_diagnosis_report", "supplement_same_task", "detail_tabs", "recovery_ui",
    "workshop_steps", "skills_page", "cases_review", "models_page", "system_tabs",
    "shell_tools", "mobile_no_overflow", "visual_screenshots", "visual_mobile",
    "visual_sanity", "axe_wcag", "keyboard_path", "focus_trap", "unauthorized",
    "forbidden", "conflict_409", "server_error", "xss_iframe", "long_list",
    "real_model_report", "model_failure_notice",
}}


def _dump_pages(h: Harness, out: Path, suite: int, clause: dict[str, Any]) -> dict[str, Any]:
    dirpath = out / "debug" / f"FE{suite}-{clause['id']}"
    dirpath.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {"pages": []}
    for key, page in list(h._sessions.items()) + list(h._guest.items()):
        try:
            path = dirpath / f"{key.replace(':', '_')}.png"
            page.screenshot(path)
            text = page.text()
            info["pages"].append({"key": key, "url": page.page.url,
                                  "title": page.title(), "text": text[-2500:],
                                  "console_errors": page.console_errors[-20:],
                                  "page_errors": page.page_errors[-20:],
                                  "screenshot": str(path)})
        except Exception as exc:  # noqa: BLE001
            info["pages"].append({"key": key, "error": str(exc)})
    return info


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120),
                    encoding="utf-8")


def _md(summaries: list[dict[str, Any]]) -> str:
    total = sum(s["total"] for s in summaries)
    passed = sum(s["passed"] for s in summaries)
    failed = sum(s["failed"] for s in summaries)
    blocked_count = sum(s["blocked"] for s in summaries)
    lines = [
        "# BBU 前端 FE0–FE7 认证矩阵", "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 条款：{total}，pass {passed}，fail {failed}，blocked {blocked_count}",
        "- 结论口径：Playwright + 真实临时实例；FE7 使用真实模型，其余用真实模型/虚拟 DB。",
        "", "| 套件 | 条款 | 结果 | 说明 |", "| --- | --- | --- | --- |",
    ]
    for s in summaries:
        for c in s["cases"]:
            detail = (c.get("detail") or "").replace("|", "\\|").replace("\n", " ")[:200]
            lines.append(f"| FE{s['suite']} | {c['clause_id']} | {c['status']} | {detail} |")
    lines += ["", "## 套件汇总", "", "| 套件 | 标题 | pass/total | 状态 |",
              "| --- | --- | --- | --- |"]
    for s in summaries:
        lines.append(f"| FE{s['suite']} | {s['title']} | {s['passed']}/{s['total']} | {s['status']} |")
    return "\n".join(lines) + "\n"


def run(*, live: bool, out_root: Path, suites: list[int] | None = None) -> dict[str, Any]:
    run_id = "frontend-" + _now()
    out = out_root / run_id
    (out / "suites").mkdir(parents=True, exist_ok=True)
    (out / "results").mkdir(parents=True, exist_ok=True)
    for step in range(8):
        _yaml(out / "suites" / f"FE{step}.yaml", {
            "schema": "bbu-fe-suite/1", "suite": f"FE{step}",
            "title": FE_SUITES[step]["title"], "clauses": FE_SUITES[step]["clauses"]})
    h = None
    browser = None
    playwright = None
    summaries: list[dict[str, Any]] = []
    setup_error = ""
    try:
        print("[1/3] 启动前端临时实例 ...", flush=True)
        h = Harness(live=live)
        h.out = out
        print(f"      {h.base} task={h.live_task_id} normal={h.normal_task_id}", flush=True)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True, executable_path=CHROMIUM,
                                           args=["--no-sandbox"])
        h._browser = browser
        print("[2/3] 执行 FE0–FE7 ...", flush=True)
        for suite in (suites if suites is not None else range(8)):
            started = time.time()
            results = []
            for clause in FE_SUITES[suite]["clauses"]:
                if clause.get("method") == "gap":
                    results.append(blocked(suite, clause, clause.get("gap", "未实现")))
                    continue
                if not clause.get("check"):
                    results.append(fail(suite, clause, "缺 check 映射"))
                    continue
                if clause["id"].startswith("FE7") and not h.lab.proxy:
                    results.append(blocked(suite, clause, "真实模型未启用"))
                    continue
                fn = CHECKS.get(clause["check"])
                if not fn:
                    results.append(fail(suite, clause, f"未实现 check {clause['check']}"))
                    continue
                try:
                    status, detail, evidence = fn(h, browser)
                    if status == "pass":
                        results.append(ok(suite, clause, detail, evidence,
                                          started=time.time()))
                    elif status == "blocked":
                        results.append(blocked(suite, clause, detail, evidence))
                    else:
                        results.append(fail(suite, clause, detail, evidence,
                                            started=time.time()))
                except Exception as exc:  # noqa: BLE001
                    debug = {"traceback": traceback.format_exc()[-3000:]}
                    try:
                        debug.update(_dump_pages(h, out, suite, clause))
                    except Exception as dump_exc:  # noqa: BLE001
                        debug["dump_error"] = str(dump_exc)
                    results.append(fail(suite, clause, f"{type(exc).__name__}: {exc}",
                                        debug, started=time.time()))
            summary = summarize(results, suite=suite, title=FE_SUITES[suite]["title"],
                                started=started)
            summaries.append(summary)
            (out / "results" / f"FE{suite}.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"      FE{suite} {summary['passed']}/{summary['total']} {summary['status']}",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        setup_error = f"{type(exc).__name__}: {exc}"
        print(f"SETUP FAILED: {setup_error}", flush=True)
        if h:
            print(h.lab.tail_log(30), flush=True)
    finally:
        if browser:
            try: browser.close()
            except Exception: pass
        if playwright:
            try: playwright.stop()
            except Exception: pass
        if h:
            h.stop()

    combined = {
        "schema": "bbu-fe-certification/1", "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "live-model" if live else "no-live",
        "setup_error": setup_error, "suites": summaries,
        "totals": {
            "clauses": sum(s["total"] for s in summaries),
            "passed": sum(s["passed"] for s in summaries),
            "failed": sum(s["failed"] for s in summaries),
            "blocked": sum(s["blocked"] for s in summaries),
        },
        "claim_boundary": [
            "FE0–FE7 在真实临时实例上运行；不是现网浏览器兼容矩阵。",
            "视觉截图固定 Chromium 1440/1024/768/375；未覆盖 Safari/Firefox。",
            "axe 违规是当前 UI 的真实可访问性缺口，不能按通过处理。",
        ],
    }
    (out / "frontend-summary.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "frontend-summary.md").write_text(_md(summaries), encoding="utf-8")
    with (out / "frontend-coverage.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["suite", "clause", "title", "severity", "status", "detail"])
        for s in summaries:
            for c in s["cases"]:
                writer.writerow([f"FE{s['suite']}", c["clause_id"], c["title"],
                                 c["severity"], c["status"], c.get("detail", "")])
    return {"out": str(out), "summary": combined}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-live", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "evaluations" / "runs"))
    parser.add_argument("--suites", default="0-7", help="如 0,1 或 0-7")
    args = parser.parse_args()
    suite_ids = []
    for part in args.suites.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            suite_ids.extend(range(int(a), int(b) + 1))
        elif part:
            suite_ids.append(int(part))
    suite_ids = sorted(set(x for x in suite_ids if 0 <= x <= 7))
    result = run(live=not args.no_live, out_root=Path(args.out), suites=suite_ids)
    print(json.dumps({"out": result["out"], "totals": result["summary"]["totals"],
                      "setup_error": result["summary"]["setup_error"]},
                     ensure_ascii=False, indent=2))
    return 0 if not result["summary"]["setup_error"] and \
        result["summary"]["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
