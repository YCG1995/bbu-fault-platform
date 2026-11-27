"""悬浮助手：平台文档问答、消息检索、管理员运维工具编排。

模型只负责“选工具/组织答案”，真正的数据访问和权限判断在 ToolRegistry。
写操作一律进入待确认；高风险操作还要求管理员密码（由 API 层校验）。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime, timedelta
from typing import Any

from .ai import AIError, Budget, BudgetExceeded, NoModelError, extract_json
from .assistant_kb import KnowledgeBase
from .assistant_tools import RISK_LABELS, ToolContext, ToolError, ToolRegistry
from .external_im import ExternalIMService
from .model_roles import MODEL_ROLES
from .notifications import notification_public
from .util import CST, iso_now, new_id, utc_now

MAX_HISTORY = 10
MAX_MESSAGE_CHARS = 2000
MAX_ANSWER_CHARS = 6000
PENDING_TTL_SEC = 600
DOC_EXCERPT_CHARS = 220


def _clip(text: Any, limit: int = DOC_EXCERPT_CHARS) -> str:
    value = str(text or '').strip()
    return value if len(value) <= limit else value[:limit].rstrip() + '…'


_FOLLOWUP_MARKERS = ("它", "这个", "那个", "上述", "前面", "刚才", "那", "呢", "还")


def _looks_like_fault(text: str) -> bool:
    """区分“设备故障”与“平台使用问题”，避免把登录/部署问题送进故障库。"""
    value = str(text or "").lower()
    fault_terms = ("故障", "告警", "复位", "重启", "失锁", "失步", "断链", "闪断",
                   "光功率", "驻波", "过温", "跑死", "无法识别", "无法建立",
                   "小区退服", "掉电", "误码", "vswr", "watchdog", "core hang",
                   "ssd", "sfp", "ipmb", "rtc", "1pps", "1588")
    platform_terms = ("平台", "页面", "登录", "账号", "密码", "部署", "安装",
                      "升级平台", "模型", "技能", "知识库", "任务", "通知", "权限")
    if any(term in value for term in platform_terms):
        return False
    return any(term in value for term in fault_terms)


def _retrieval_query(history: list[dict[str, str]], question: str) -> str:
    """把“那回滚呢”这类追问补成独立检索问题；无历史时保持原问题。"""
    q = str(question or "").strip()
    if not q:
        return q
    if len(q) > 28 and not any(marker in q for marker in _FOLLOWUP_MARKERS):
        return q
    previous = ""
    for item in reversed(history[:-1]):
        if item.get("role") == "user":
            previous = str(item.get("content") or "").strip()
            if previous:
                break
    if not previous or previous == q:
        return q
    return f"{previous} {q}"[:240]


class AssistantError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class AssistantService:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.kb = KnowledgeBase(app.config.root)
        self.external = ExternalIMService(app.config.data_dir)
        self.registry = ToolRegistry(app, self.kb, self.external)
        self._lock = threading.RLock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._rate: dict[int, list[float]] = {}
        self._last_model = ""

    # ---------------------------------------------------------------- 状态
    def status(self, user: dict[str, Any]) -> dict[str, Any]:
        model_status = self.app.gateway.model_status(user["id"])
        tools = self.registry.public_tools(user)
        return {
            "enabled": bool(self.app.config.get("assistant_enabled", True)),
            "role": user.get("role"),
            "model_usable": bool(model_status.get("usable")),
            "model_source": model_status.get("source") or "",
            "model": model_status,
            "tools": len(tools),
            "tool_names": [t["name"] for t in tools],
            "docs": len(self.kb.list_docs()),
            "external_im": self.external.status(),
            "pending_count": len(self._pending_for(user["id"])),
        }

    def suggestions(self, user: dict[str, Any]) -> list[dict[str, str]]:
        items = [
            {"label": "平台怎么新建诊断？", "prompt": "平台怎么新建诊断任务？需要准备什么？"},
            {"label": "查最近失败任务", "prompt": "帮我查最近失败的任务和最后一条事件"},
            {"label": "查我的未读通知", "prompt": "查一下我的未读站内通知"},
            {"label": "平台怎么升级？", "prompt": "网页自动升级怎么操作？失败怎么回滚？"},
        ]
        if user.get("role") == "admin":
            items.extend([
                {"label": "系统状态", "prompt": "看一下系统当前状态、磁盘和模型是否可用"},
                {"label": "最近审计", "prompt": "查最近 7 天的审计记录，重点看失败操作"},
            ])
        return items[:6]

    # ---------------------------------------------------------------- 对话
    def chat(self, user: dict[str, Any], messages: Any, ip: str = "") -> dict[str, Any]:
        if not bool(self.app.config.get("assistant_enabled", True)):
            raise AssistantError("平台助手未启用", 404)
        history = self._normalize_messages(messages)
        question = history[-1]["content"]
        self._check_rate(user)
        ctx = ToolContext(user=user, ip=ip, source="assistant")
        started = {"tools": [], "model": "", "pending": 0}
        try:
            if self._model_available(user):
                response = self._model_chat(user, ctx, history)
            else:
                raise NoModelError("没有可用模型")
        except (NoModelError, AIError, BudgetExceeded) as exc:
            response = self._fallback(user, ctx, history, question, str(exc))
        except Exception as exc:  # noqa: BLE001 - 助手不能把堆栈暴露给用户
            self.app.db.audit(user["id"], user["username"], "assistant.chat", "assistant",
                              {"query": question[:200], "error": str(exc)[:300]}, ip)
            raise AssistantError(f"助手处理失败: {exc}", 500) from exc
        self.app.db.audit(user["id"], user["username"], "assistant.chat", "assistant",
                          {"query": question[:200],
                           "tools": [t.get("name") for t in response.get("tool_calls") or []],
                           "pending": len(response.get("pending_actions") or []),
                           "model": response.get("model") or "", "ok": True}, ip)
        response.setdefault("message", "")
        response.setdefault("links", [])
        response.setdefault("tool_calls", [])
        response.setdefault("pending_actions", [])
        response.setdefault("sources", [])
        response["message"] = str(response["message"])[:MAX_ANSWER_CHARS]
        response["role"] = user.get("role")
        return response

    def _normalize_messages(self, messages: Any) -> list[dict[str, str]]:
        if not isinstance(messages, list) or not messages:
            raise AssistantError("请输入问题")
        clean: list[dict[str, str]] = []
        for item in messages[-MAX_HISTORY:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role not in ("user", "assistant") or not content:
                continue
            clean.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
        if not clean or clean[-1]["role"] != "user":
            raise AssistantError("最后一条消息必须是用户提问")
        return clean

    def _check_rate(self, user: dict[str, Any]) -> None:
        limit = int(self.app.config.get("assistant_rate_per_min", 12) or 12)
        if limit <= 0:
            return
        now = datetime.now(CST).timestamp()
        with self._lock:
            bucket = [t for t in self._rate.get(user["id"], []) if now - t < 60]
            if len(bucket) >= limit:
                raise AssistantError(f"提问过于频繁，请 {max(1, int(60 - (now - bucket[0]))) } 秒后重试", 429)
            bucket.append(now)
            self._rate[user["id"]] = bucket

    def _model_available(self, user: dict[str, Any]) -> bool:
        try:
            return bool(self.app.gateway.choose("assistant", user_id=user["id"]))
        except NoModelError:
            return False

    def _model_chat(self, user: dict[str, Any], ctx: ToolContext,
                    history: list[dict[str, str]]) -> dict[str, Any]:
        specs = self.registry.visible(user)
        tool_names = [s.name for s in specs]
        system = self._system_prompt(user)
        user_prompt = self._plan_prompt(history, specs)
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "reply": {"type": "string", "maxLength": 2000},
                "calls": {
                    "type": "array", "maxItems": 3,
                    "items": {
                        "type": "object", "additionalProperties": True,
                        "properties": {
                            "tool": {"type": "string", "enum": tool_names},
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool", "arguments"],
                    },
                },
                "clarification": {"type": "string", "maxLength": 500},
            },
            "required": ["reply", "calls"],
        }
        budget = Budget(max_calls=4, max_tokens=12000)
        with self.app.gateway.usage_context(user["id"], task_kind="assistant"):
            plan_result = self.app.gateway.complete(
                "assistant", system, user_prompt, json_schema=schema,
                max_tokens=900, temperature=0.1, purpose="assistant.plan",
                budget=budget, stream=True)
        self._last_model = plan_result.model_name
        plan = plan_result.data if isinstance(plan_result.data, dict) else extract_json(plan_result.text)
        if not isinstance(plan, dict):
            plan = {"reply": str(plan_result.text or "").strip(), "calls": []}
        calls = plan.get("calls") if isinstance(plan.get("calls"), list) else []
        calls = [c for c in calls if isinstance(c, dict)][:3]

        tool_results: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for call in calls:
            name = str(call.get("tool") or "").strip()
            args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            spec = self.registry.get(name)
            if not spec:
                tool_results.append({"tool": name, "ok": False,
                                     "error": "模型选择了不存在的工具"})
                continue
            try:
                spec, args = self.registry.validate_call(name, args, ctx)
            except ToolError as exc:
                tool_results.append({"tool": name, "ok": False, "title": spec.title,
                                     "error": str(exc)})
                continue
            if spec.risk != "read":
                pending.append(self._create_pending(user, spec, args, ctx))
                break
            try:
                result = self.registry.execute(name, args, ctx)
                tool_results.append(result)
            except ToolError as exc:
                tool_results.append({"tool": name, "ok": False, "title": spec.title,
                                     "error": str(exc)})

        if pending:
            message = self._pending_message(tool_results, pending)
            return {"message": message, "tool_calls": tool_results,
                    "pending_actions": pending, "links": self._links(tool_results),
                    "model": plan_result.model_name,
                    "usage": budget.snapshot()}

        if calls:
            answer = self._summarize(user, ctx, history, tool_results, budget)
            return {"message": answer, "tool_calls": tool_results,
                    "links": self._links(tool_results),
                    "sources": self._sources(tool_results),
                    "model": plan_result.model_name,
                    "usage": budget.snapshot()}

        reply = str(plan.get("reply") or "").strip()
        if not reply:
            reply = str(plan.get("clarification") or "").strip()
        if not reply:
            reply = self._format_results(tool_results) if tool_results else (
                "我可以帮你查平台文档、任务/通知/审计消息，管理员还可以做系统运维。请直接说你要查什么。")
        return {"message": reply, "tool_calls": tool_results,
                "links": self._links(tool_results),
                "sources": self._sources(tool_results),
                "model": plan_result.model_name, "usage": budget.snapshot()}

    # ---------------------------------------------------------------- 提示词
    def _system_prompt(self, user: dict[str, Any]) -> str:
        role = {"admin": "管理员", "reviewer": "专家", "engineer": "工程师"}.get(
            user.get("role") or "", user.get("role") or "")
        return f"""你是 BBU FaultScope（BBU 故障诊断平台）内置的运维小助手，服务对象是当前登录用户。
当前用户：{user.get('username')}（{role}）；本地时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M')}。

你的职责：
1. 回答平台使用问题（新建诊断、任务状态、技能库、技能工坊、模型接入、通知、部署升级/回滚、账号权限）。
2. 检索故障知识库：现场出现看门狗复位、时钟失锁、SSD/光模块/电源/风扇等故障现象，
   或问“这个故障以前怎么处理”时，必须优先调用 search_fault_kb；需要核对原文时再调用 get_fault_case。
3. 检索平台站内消息：通知、任务事件、审计/操作记录、外部 IM 适配器消息。
4. 管理员可执行运维工具：系统状态、备份/空间、升级/回滚、用户、模型、通知偏好。

硬性规则：
- 平台事实必须来自工具结果或对话历史，不得编造任务 ID、用户、数字、时间或页面路径。
- 平台“怎么用/怎么部署/怎么配”用 search_docs；现场故障现象和历史处置用 search_fault_kb，不要拿平台文档冒充故障案例。
- 先判断是否需要工具；需要事实就调用工具，最多 3 个，参数必须是合法 JSON。能直接回答才把 calls 留空。
- 写操作会自动进入“待确认”，你绝不能声称已经执行；只说明将要做什么并提醒用户确认。
- 不输出 API Key、密码、Token、master.key 或完整内部堆栈；审计 IP 只在用户明确要求时给出，且不要展开。
- 回答用简洁中文，先给结论；有页面入口时写清楚“打开 X 页面”。不要复述工具 JSON。

可用工具：
{self.registry.prompt_tools(user)}"""

    def _plan_prompt(self, history: list[dict[str, str]], specs: list[Any]) -> str:
        lines = ["## 对话历史"]
        for item in history[:-1]:
            label = "用户" if item["role"] == "user" else "助手"
            lines.append(f"{label}: {item['content']}")
        if len(lines) == 1:
            lines.append("（无）")
        lines.extend(["", "## 当前问题", history[-1]["content"], "",
                      "## 输出要求",
                      "返回 JSON：{\"reply\":\"直接回答或空字符串\",\"calls\":[{\"tool\":\"工具名\","
                      "\"arguments\":{...}}],\"clarification\":\"需要追问时写\"}。",
                      "如果问题需要查平台数据，优先调用工具；如果只是概念解释，可直接回答。"
                      "如果用户要求执行写操作，调用对应工具即可，系统会转成确认卡片。",
                      "检索文档时，若当前问题是追问（如“那回滚呢”），先结合对话历史把它改写成"
                      "独立问题，再调用 search_docs；不要把代词原样当作 query。",
                      "不要调用未列出的工具；arguments 必须严格符合工具参数 Schema。"
                      "枚举字段只能使用 enum 列出的值；整数必须在 minimum/maximum 范围内；"
                      "required 字段必须提供。没有把握时宁可少传可选参数，不要猜枚举。"])
        return "\n".join(lines)

    def _summarize(self, user: dict[str, Any], ctx: ToolContext,
                   history: list[dict[str, str]], results: list[dict[str, Any]],
                   budget: Budget) -> str:
        compact = []
        for item in results:
            try:
                text = json.dumps(item, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(item)
            compact.append(text[:3500])
        prompt = f"""用户问题：{history[-1]['content']}

工具结果（按调用顺序）：
{chr(10).join(compact)}

请根据工具结果直接回答用户。要求：
- 先给结论，再给必要细节；列出具体任务 ID/用户/通知标题/审计动作等证据。
- 不要编造工具结果中没有的信息；工具失败时明确说失败原因。
- 如果结果包含待确认操作，提醒用户点击确认卡片；不要声称已执行。
- 管理员操作给出页面入口；不要输出密钥、密码、Token。
- 300 字以内，中文。"""
        try:
            with self.app.gateway.usage_context(user["id"], task_kind="assistant"):
                result = self.app.gateway.complete(
                    "assistant", "你是 BBU FaultScope 平台助手，只能依据给定工具结果回答。",
                    prompt, max_tokens=900, temperature=0.2,
                    purpose="assistant.answer", budget=budget, stream=True)
            text = str(result.text or "").strip()
            if text:
                return text[:MAX_ANSWER_CHARS]
        except (AIError, NoModelError, BudgetExceeded):
            pass
        return self._format_results(results)

    # ---------------------------------------------------------------- 降级
    def _fallback(self, user: dict[str, Any], ctx: ToolContext,
                  history: list[dict[str, str]], question: str,
                  reason: str = "") -> dict[str, Any]:
        docs = self.kb.search(_retrieval_query(history, question), 5)
        tool_results: list[dict[str, Any]] = []
        q_lower = question.lower()
        fault_intent = _looks_like_fault(question)
        fault_hit = False
        if fault_intent and self.registry.fault_kb:
            try:
                result = self.registry.execute("search_fault_kb",
                                               {"query": question, "limit": 6}, ctx)
                tool_results.append(result)
                fault_hit = bool(result.get("items")) and not result.get("no_answer")
            except ToolError as exc:
                tool_results.append({"tool": "search_fault_kb", "ok": False,
                                     "error": str(exc)})
        scopes: list[str] = []
        if any(k in q_lower for k in ("未读通知", "我的通知", "查通知", "消息", "提醒")):
            scopes.append("notifications")
        if (("失败" in q_lower and "任务" in q_lower)
                or any(k in q_lower for k in ("最近任务", "任务事件", "任务日志",
                                             "查任务", "任务状态"))):
            scopes.append("task_events")
        if user.get("role") == "admin" and any(k in q_lower for k in ("审计", "操作记录")):
            scopes.append("audit")
        if any(k in q_lower for k in ("飞书", "豆包", "im", "外部消息")):
            scopes.append("external")
        if scopes:
            try:
                tool_results.append(self.registry.execute(
                    "search_messages", {"query": question, "scopes": scopes, "days": 30,
                                        "limit": 6}, ctx))
            except ToolError as exc:
                tool_results.append({"tool": "search_messages", "ok": False,
                                     "error": str(exc)})
        if user.get("role") == "admin" and any(
                k in q_lower for k in ("系统状态", "磁盘", "模型可用", "运行状态")):
            try:
                tool_results.append(self.registry.execute("get_system_status", {}, ctx))
            except ToolError as exc:
                tool_results.append({"tool": "get_system_status", "ok": False,
                                     "error": str(exc)})

        lines: list[str] = []
        if tool_results:
            lines.append(self._format_results(tool_results))
            # 故障问题有知识库命中时，不再拿平台文档充当故障资料。
            if not fault_hit and docs and any(k in q_lower for k in ("怎么", "如何", "配置", "使用", "步骤")):
                lines.append("")
                lines.append("相关操作说明：")
                for item in docs[:2]:
                    lines.append(f"- **{item['doc_title']} / {item['heading']}**：{_clip(item['excerpt'])}")
        elif docs:
            lines.append("先按平台文档给你定位到以下内容：")
            for item in docs[:2]:
                lines.append(f"- **{item['doc_title']} / {item['heading']}**：{_clip(item['excerpt'])}")
        if not lines:
            if reason and "模型" in reason:
                lines.append("当前没有可用的模型配置，暂时无法进行自然语言推理。"
                             "请管理员在“模型接入”页面配置并启用模型。")
            elif fault_intent:
                lines.append("故障知识库没有找到足够相关的历史案例。请补充板卡型号、"
                             "软件/逻辑版本、告警码和完整现象后再查；不要据此直接下结论。")
            else:
                lines.append("没有检索到直接相关的平台文档或消息。可以换一种说法，"
                             "或提供任务 ID、用户名、通知标题等关键字。")
        if not self._model_available(user):
            lines.append("\n（当前为本地文档/数据检索模式；配置模型后可进行更完整的问答。）")
        links = self._links(tool_results)
        kb_sources = self._sources(tool_results)
        return {"message": "\n".join(lines)[:MAX_ANSWER_CHARS],
                "tool_calls": tool_results, "links": links,
                "sources": kb_sources or docs, "model": "", "usage": {}}

    def _format_results(self, results: list[dict[str, Any]]) -> str:
        if not results:
            return "没有可展示的结果。"
        lines = []
        for item in results[:5]:
            title = item.get("title") or item.get("tool") or "工具结果"
            if item.get("ok") is False:
                lines.append(f"- {title}：失败 — {item.get('error') or '未知错误'}")
                continue
            summary = item.get("summary") or ""
            lines.append(f"- {title}：{summary}" if summary else f"- {title}：已完成")
            data = item.get("items")
            if isinstance(data, list):
                for row in data[:5]:
                    if not isinstance(row, dict):
                        continue
                    label = (row.get("title") or row.get("task_title")
                             or row.get("username") or row.get("id") or "")
                    detail = (row.get("body") or row.get("message") or row.get("content")
                              or row.get("symptom") or row.get("cause")
                              or row.get("action") or "")
                    when = row.get("ts") or row.get("created_at") or ""
                    line = f"  - {label}"
                    if detail:
                        line += f"：{str(detail)[:180]}"
                    if when:
                        line += f"（{when}）"
                    lines.append(line)
            task = item.get("task")
            if isinstance(task, dict):
                lines.append(f"  - {task.get('id')} {task.get('title')} · {task.get('status')}"
                             f" · {task.get('stage')}")
            if item.get("version") and item.get("disk"):
                disk = item["disk"]
                model = item.get("model") or {}
                lines.append(f"  - 版本 {item['version']}；磁盘剩余 "
                             f"{round(int(disk.get('free_bytes') or 0) / 1024 / 1024)} MB；"
                             f"模型可用={bool(model.get('usable'))}")
                counts = item.get("counts") or {}
                if counts:
                    lines.append(f"  - 任务 {counts.get('tasks', 0)} · 用户 "
                                 f"{counts.get('users', 0)} · 技能 {counts.get('skills', 0)}")
        return "\n".join(lines)

    def _links(self, results: list[dict[str, Any]]) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in results:
            for link in item.get("links") or []:
                if not isinstance(link, dict):
                    continue
                href = str(link.get("href") or "").strip()
                label = str(link.get("label") or "").strip()
                if not href or not label or href in seen:
                    continue
                if not (href.startswith("/#/") or href.startswith("#/")):
                    continue
                seen.add(href)
                links.append({"label": label, "href": href})
        return links[:6]

    def _sources(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in results:
            for source in item.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                key = str(source.get("uri") or source.get("slug") or source.get("path") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                sources.append({"title": source.get("doc_title") or source.get("title") or "",
                                "heading": source.get("heading") or "",
                                "uri": source.get("uri") or "",
                                "excerpt": source.get("excerpt") or ""})
        return sources[:6]

    # ---------------------------------------------------------------- 待确认
    def _create_pending(self, user: dict[str, Any], spec: Any,
                        args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        now = datetime.now(CST).timestamp()
        with self._lock:
            self._cleanup_pending(now)
            mine = [k for k, v in self._pending.items() if v["user_id"] == user["id"]]
            if len(mine) >= 5:
                for key in sorted(mine, key=lambda k: self._pending[k]["created_ts"])[:len(mine) - 4]:
                    self._pending.pop(key, None)
            action_id = "PA" + datetime.now(CST).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            item = {
                "id": action_id, "user_id": user["id"], "username": user["username"],
                "role": user.get("role"), "tool": spec.name, "title": spec.title,
                "arguments": args, "risk": spec.risk,
                "requires_password": spec.requires_password,
                "summary": spec.approval_summary(args),
                "created_ts": now, "expires_at": iso_now(),
                "expires_ts": now + PENDING_TTL_SEC,
            }
            self._pending[action_id] = item
        return self._public_pending(item)

    def _public_pending(self, item: dict[str, Any]) -> dict[str, Any]:
        expires = datetime.fromtimestamp(item["expires_ts"], tz=CST).isoformat()
        return {"id": item["id"], "tool": item["tool"], "title": item["title"],
                "summary": item["summary"], "risk": item["risk"],
                "risk_label": RISK_LABELS.get(item["risk"], item["risk"]),
                "requires_password": bool(item["requires_password"]),
                "expires_at": expires}

    def _cleanup_pending(self, now: float | None = None) -> None:
        now = now or datetime.now(CST).timestamp()
        expired = [k for k, v in self._pending.items() if now >= v["expires_ts"]]
        for key in expired:
            self._pending.pop(key, None)

    def _pending_for(self, user_id: int) -> list[dict[str, Any]]:
        now = datetime.now(CST).timestamp()
        with self._lock:
            self._cleanup_pending(now)
            return [self._public_pending(v) for v in self._pending.values()
                    if v["user_id"] == user_id]

    def _pending_message(self, results: list[dict[str, Any]],
                        pending: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        if results:
            lines.append("先查到的信息：")
            lines.append(self._format_results(results))
            lines.append("")
        for item in pending:
            lines.append(f"待确认：{item['summary']}（{item['risk_label']}）。")
        if any(item.get("requires_password") for item in pending):
            lines.append("这个操作需要管理员密码；确认卡片会要求输入密码。")
        else:
            lines.append("请点击下方确认卡片执行；未确认前不会产生任何修改。")
        return "\n".join(lines)[:MAX_ANSWER_CHARS]

    def action_public(self, user: dict[str, Any], action_id: str) -> dict[str, Any]:
        now = datetime.now(CST).timestamp()
        with self._lock:
            self._cleanup_pending(now)
            item = self._pending.get(str(action_id or ""))
            if not item or item["user_id"] != user["id"]:
                raise AssistantError("确认操作不存在或不属于当前账号", 404)
            if now >= item["expires_ts"]:
                self._pending.pop(item["id"], None)
                raise AssistantError("确认操作已过期，请重新发起", 410)
            return self._public_pending(item)

    def confirm_action(self, user: dict[str, Any], action_id: str,
                       ip: str = "") -> dict[str, Any]:
        now = datetime.now(CST).timestamp()
        with self._lock:
            self._cleanup_pending(now)
            item = self._pending.get(str(action_id or ""))
            if not item or item["user_id"] != user["id"]:
                raise AssistantError("确认操作不存在或不属于当前账号", 404)
            if now >= item["expires_ts"]:
                self._pending.pop(item["id"], None)
                raise AssistantError("确认操作已过期，请重新发起", 410)
            self._pending.pop(item["id"], None)
        spec = self.registry.get(item["tool"])
        if not spec:
            raise AssistantError("工具已不存在，请重新发起", 410)
        ctx = ToolContext(user=user, ip=ip, source="assistant_confirm", confirmed=True)
        try:
            result = self.registry.execute(spec.name, item["arguments"], ctx)
        except ToolError as exc:
            self.app.db.audit(user["id"], user["username"], "assistant.action.confirm",
                              spec.name, {"action_id": action_id, "ok": False,
                                          "error": str(exc)[:300], "source": "assistant"},
                              ip)
            raise AssistantError(str(exc), exc.status) from exc
        self.app.db.audit(user["id"], user["username"], "assistant.action.confirm",
                          spec.name, {"action_id": action_id, "ok": True,
                                      "source": "assistant"}, ip)
        return {"message": f"已执行：{result.get('summary') or spec.title}",
                "tool_calls": [result], "pending_actions": [],
                "links": self._links([result]), "sources": self._sources([result]),
                "result": result}

    def cancel_action(self, user: dict[str, Any], action_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._pending.get(str(action_id or ""))
            if item and item["user_id"] == user["id"]:
                self._pending.pop(item["id"], None)
        return {"ok": True, "cancelled": action_id}
