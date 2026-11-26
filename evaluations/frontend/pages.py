"""Playwright Page Object：统一登录态、路由加载、标题与错误观察。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page


class RolePage:
    def __init__(self, browser: Browser, base: str, *, cookie: str = "",
                 viewport: dict[str, int] | None = None, name: str = "role") -> None:
        self.base = base.rstrip("/")
        self.name = name
        self.context: BrowserContext = browser.new_context(
            viewport=viewport or {"width": 1440, "height": 1000},
            locale="zh-CN", timezone_id="Asia/Shanghai")
        if cookie:
            self.context.add_cookies([{
                "name": "bbu_session", "value": cookie.split("=", 1)[-1],
                "domain": "127.0.0.1", "path": "/", "httpOnly": True,
                "secure": False, "sameSite": "Strict"}])
        self.page: Page = self.context.new_page()
        self.page.set_default_timeout(15000)
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.responses: list[dict[str, Any]] = []
        self.page.on("console", lambda m: self.console_errors.append(
            f"{m.type}: {m.text}") if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        self.page.on("response", lambda r: self.responses.append(
            {"url": r.url, "status": r.status, "type": r.request.resource_type}))

    def close(self) -> None:
        self.context.close()

    def clear_errors(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()
        self.responses.clear()

    def goto(self, route: str, *, wait_ms: int = 800) -> str:
        self.page.goto(self.base + "/" + route, wait_until="domcontentloaded")
        self.page.wait_for_timeout(wait_ms)
        return self.title()

    def title(self) -> str:
        try:
            return self.page.locator("#page-title").inner_text(timeout=4000).strip()
        except Exception:
            return ""

    def text(self) -> str:
        try:
            return self.page.locator("#view").inner_text(timeout=4000)
        except Exception:
            return ""

    def login_visible(self) -> bool:
        return self.page.locator("#login-form").count() > 0 and \
            self.page.locator("#login-form").is_visible()

    def screenshot(self, path: Path, full_page: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=full_page)

    def overflow_x(self) -> bool:
        return bool(self.page.evaluate(
            "() => document.documentElement.scrollWidth > window.innerWidth + 3"))

    def wait_text(self, text: str, timeout: int = 15000) -> None:
        self.page.wait_for_function(
            "(t) => document.body.innerText.includes(t)", arg=text, timeout=timeout)

    def wait_selector(self, selector: str, timeout: int = 15000) -> None:
        self.page.wait_for_selector(selector, timeout=timeout)

    def click(self, selector: str) -> None:
        self.page.locator(selector).first.click()

    def fill(self, selector: str, value: str) -> None:
        self.page.locator(selector).first.fill(value)


ROUTES = [
    "#/dashboard", "#/tasks/new", "#/tasks", "#/tasks/{task}", "#/cases",
    "#/skills", "#/workshop", "#/models", "#/users", "#/audit", "#/system",
]

ROUTE_TITLES = {
    "#/dashboard": "总览",
    "#/tasks/new": "新建诊断",
    "#/tasks": "任务记录",
    "#/tasks/{task}": "任务详情",
    "#/cases": "真实案例",
    "#/skills": "技能库",
    "#/workshop": "技能工坊",
    "#/models": "模型接入",
    "#/users": "用户管理",
    "#/audit": "审计日志",
    "#/system": "系统信息",
}

ROLE_ALLOWED = {
    "admin": set(ROUTES),
    "engineer": {"#/dashboard", "#/tasks/new", "#/tasks", "#/tasks/{task}",
                 "#/cases", "#/skills", "#/workshop"},
    "reviewer": {"#/dashboard", "#/tasks", "#/tasks/{task}", "#/cases"},
}


def route_key(route: str) -> str:
    return "#/tasks/{task}" if route.startswith("#/tasks/T") else route


def expected_title(route: str, role: str) -> str:
    key = route_key(route)
    if role == "guest":
        return "登录"
    if key not in ROLE_ALLOWED[role]:
        return "无权访问"
    return ROUTE_TITLES[key]


def resolve_route(route: str, task_id: str) -> str:
    return route.replace("{task}", task_id)
