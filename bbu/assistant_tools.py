"""平台助手/MCP 共用的工具注册表。

工具是唯一允许读写平台数据的入口：角色过滤、参数校验、审计和
高风险二次确认都在这一层完成。悬浮窗和 MCP Server 只负责协议适配。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import updater
from .accounts import normalize_status, public_user
from .assistant_kb import KnowledgeBase
from .external_im import ExternalIMService
from .model_roles import MODEL_ROLES
from .notifications import create_notification
from .security import SecretBox, delete_user_sessions
from .util import iso_now, new_id

TASK_STATUSES = ("queued", "running", "done", "failed", "needs_data", "cancelled")
MESSAGE_SCOPES = ("notifications", "task_events", "audit", "external")
RISK_LABELS = {"read": "只读", "medium": "写操作", "high": "高风险"}


class ToolError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ToolContext:
    user: dict[str, Any]
    ip: str = ""
    source: str = "assistant"  # assistant | assistant_confirm | mcp
    token_mode: str = ""
    confirmed: bool = False

    @property
    def is_admin(self) -> bool:
        return self.user.get("role") == "admin"


@dataclass
class ToolSpec:
    name: str
    title: str
    description: str
    schema: dict[str, Any]
    handler: Callable[["ToolRegistry", ToolContext, dict[str, Any]], dict[str, Any]]
    risk: str = "read"
    scope: str = "user"  # user | admin
    mcp: str = "read"  # read | write | never
    summary: Callable[[dict[str, Any]], str] | None = None

    def approval_summary(self, args: dict[str, Any]) -> str:
        if self.summary:
            try:
                return self.summary(args)
            except Exception:  # noqa: BLE001 - 摘要失败不能阻断安全提示
                pass
        return f"{self.title}：{json.dumps(args, ensure_ascii=False)[:300]}"

    @property
    def requires_password(self) -> bool:
        return self.risk == "high"


def _loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (json.JSONDecodeError, TypeError):
        return default


def _truncate(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _task_link(task_id: str) -> str:
    return f"/#/tasks/{task_id}"


def _task_scope_sql(user: dict[str, Any], alias: str = "t") -> tuple[str, tuple[Any, ...]]:
    if user.get("role") == "admin":
        return "", ()
    if user.get("role") == "reviewer":
        return f" AND ({alias}.user_id=? OR {alias}.reviewer_id=?)", (user["id"], user["id"])
    return f" AND {alias}.user_id=?", (user["id"],)


def _can_view_task(user: dict[str, Any], task: dict[str, Any]) -> bool:
    if user.get("role") == "admin":
        return True
    if task.get("user_id") == user.get("id"):
        return True
    return user.get("role") == "reviewer" and task.get("reviewer_id") == user.get("id")


def _task_summary(task: dict[str, Any], events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    verdict = _loads(task.get("verdict_json"), {}) or {}
    stats = _loads(task.get("stats_json"), {}) or {}
    item = {
        "id": task["id"], "title": task["title"], "status": task["status"],
        "stage": task["stage"], "progress": task["progress"], "mode": task["mode"],
        "created_at": task["created_at"], "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"), "error": _truncate(task.get("error"), 500),
        "verdict": {"primary": verdict.get("primary"),
                    "root_cause_name": verdict.get("root_cause_name"),
                    "confidence": verdict.get("confidence")},
        "tokens": int(stats.get("total_tokens") or 0),
        "inputs_state": task.get("inputs_state") or "available",
        "link": _task_link(task["id"]),
    }
    if events is not None:
        item["events"] = events
    return item


def _public_model(app: Any, row: dict[str, Any]) -> dict[str, Any]:
    try:
        plain = app.secret_box.decrypt(row.get("api_key") or "")
        key = SecretBox.mask(plain)
        has_key = bool(plain)
    except ValueError:
        key = "（解密失败）"
        has_key = True
    return {
        "id": row["id"], "name": row["name"], "provider": row["provider"],
        "base_url": row["base_url"], "model": row["model"],
        "api_key_masked": key, "has_key": has_key,
        "roles": _loads(row.get("roles"), []) or [],
        "enabled": bool(row["enabled"]), "priority": int(row.get("priority") or 0),
        "max_tokens": row["max_tokens"], "timeout_sec": row["timeout_sec"],
        "updated_at": row.get("updated_at"),
    }


class ToolRegistry:
    def __init__(self, app: Any, kb: KnowledgeBase, external: ExternalIMService) -> None:
        self.app = app
        self.kb = kb
        self.external = external
        self._tools: dict[str, ToolSpec] = {}
        self._register_all()

    # ---------------------------------------------------------------- 描述
    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(str(name or "").strip())

    def visible(self, user: dict[str, Any], *, source: str = "assistant",
                token_mode: str = "") -> list[ToolSpec]:
        role = user.get("role") or "engineer"
        items: list[ToolSpec] = []
        for spec in self._tools.values():
            if spec.scope == "admin" and role != "admin":
                continue
            if source == "mcp":
                if spec.mcp == "never":
                    continue
                if spec.risk != "read" and token_mode not in ("admin", "write"):
                    continue
            items.append(spec)
        return sorted(items, key=lambda s: (s.scope != "admin", s.risk != "read", s.name))

    def public_tools(self, user: dict[str, Any], *, source: str = "assistant",
                     token_mode: str = "") -> list[dict[str, Any]]:
        result = []
        for spec in self.visible(user, source=source, token_mode=token_mode):
            result.append({
                "name": spec.name, "title": spec.title, "description": spec.description,
                "inputSchema": spec.schema,
                "annotations": {
                    "readOnlyHint": spec.risk == "read",
                    "destructiveHint": spec.risk != "read",
                    "idempotentHint": spec.risk == "read",
                },
                "risk": spec.risk, "scope": spec.scope,
            })
        return result

    def prompt_tools(self, user: dict[str, Any]) -> str:
        lines = []
        for spec in self.visible(user):
            schema_text = json.dumps(spec.schema, ensure_ascii=False, separators=(",", ":"))
            lines.append(
                f"- {spec.name}（{spec.title}，{RISK_LABELS.get(spec.risk, spec.risk)}）"
                f" 参数 Schema: {schema_text} — {spec.description}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 执行
    def validate_call(self, name: str, args: dict[str, Any] | None,
                      ctx: ToolContext) -> tuple[ToolSpec, dict[str, Any]]:
        spec = self.get(name)
        if not spec:
            raise ToolError(f"未知工具: {name}", 404)
        raw = dict(args) if isinstance(args, dict) else {}
        # MCP 的 confirm 是传输层安全参数，不属于工具业务 schema；
        # 先取出，避免被 additionalProperties=false 拒绝，再在下方校验。
        mcp_confirm = raw.pop("confirm", None) if ctx.source == "mcp" else None
        values = raw
        if spec.scope == "admin" and not ctx.is_admin:
            raise ToolError("权限不足：该工具仅管理员可用", 403)
        if ctx.source == "mcp":
            if spec.mcp == "never":
                raise ToolError("该工具不允许通过 MCP 调用", 403)
            if spec.risk != "read":
                if ctx.token_mode not in ("admin", "write"):
                    raise ToolError("当前 MCP Token 为只读模式，不能执行写操作", 403)
                if mcp_confirm is not True:
                    raise ToolError("MCP 写操作需要在参数中传 confirm=true")
        self._validate(spec.schema, values, path=spec.name)
        return spec, values

    def execute(self, name: str, args: dict[str, Any] | None, ctx: ToolContext) -> dict[str, Any]:
        spec, values = self.validate_call(name, args, ctx)
        if spec.risk != "read" and ctx.source == "assistant" and not ctx.confirmed:
            raise ToolError("该操作需要二次确认", 409)
        result = spec.handler(self, ctx, values)
        if not isinstance(result, dict):
            result = {"result": result}
        result.setdefault("ok", True)
        result.setdefault("tool", spec.name)
        result.setdefault("title", spec.title)
        return result

    def _validate(self, schema: dict[str, Any], value: Any, path: str = "") -> None:
        kind = schema.get("type")
        if kind == "object":
            if not isinstance(value, dict):
                raise ToolError(f"{path or '参数'} 必须是对象")
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            for key in required:
                if key not in value or value[key] is None:
                    raise ToolError(f"{path}.{key} 为必填项")
            if schema.get("additionalProperties") is False:
                extra = set(value) - set(props)
                if extra:
                    raise ToolError(f"{path} 包含未知参数: {', '.join(sorted(extra))}")
            for key, sub in props.items():
                if key in value and value[key] is not None:
                    self._validate(sub, value[key], f"{path}.{key}")
        elif kind == "array":
            if not isinstance(value, list):
                raise ToolError(f"{path or '参数'} 必须是数组")
            max_items = schema.get("maxItems")
            if max_items is not None and len(value) > int(max_items):
                raise ToolError(f"{path} 最多 {max_items} 项")
            item_schema = schema.get("items")
            if item_schema:
                for idx, item in enumerate(value):
                    self._validate(item_schema, item, f"{path}[{idx}]")
        elif kind == "string":
            if not isinstance(value, str):
                raise ToolError(f"{path or '参数'} 必须是字符串")
            if schema.get("maxLength") is not None and len(value) > int(schema["maxLength"]):
                raise ToolError(f"{path} 超过最大长度 {schema['maxLength']}")
            enum = schema.get("enum")
            if enum is not None and value not in enum:
                raise ToolError(f"{path} 必须是 {', '.join(map(str, enum))} 之一")
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"{path or '参数'} 必须是整数")
            if schema.get("minimum") is not None and value < int(schema["minimum"]):
                raise ToolError(f"{path} 不能小于 {schema['minimum']}")
            if schema.get("maximum") is not None and value > int(schema["maximum"]):
                raise ToolError(f"{path} 不能大于 {schema['maximum']}")
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolError(f"{path or '参数'} 必须是数字")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ToolError(f"{path or '参数'} 必须是布尔值")

    # ---------------------------------------------------------------- 注册
    def _add(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def _register_all(self) -> None:
        self._add(ToolSpec(
            name="search_docs", title="检索平台文档",
            description="检索使用手册、部署、升级、架构等平台文档，回答平台怎么用。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"query": {"type": "string", "maxLength": 200},
                                   "limit": {"type": "integer", "minimum": 1,
                                             "maximum": 10}},
                    "required": ["query"]},
            handler=lambda reg, ctx, a: reg._search_docs(a),
            summary=lambda a: f"检索文档“{a.get('query', '')}”",
        ))
        self._add(ToolSpec(
            name="search_messages", title="检索平台消息",
            description="检索站内通知、任务事件、审计记录和外部 IM 消息；按角色限制范围。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string", "maxLength": 200},
                        "scopes": {"type": "array", "maxItems": 4,
                                   "items": {"type": "string", "enum": list(MESSAGE_SCOPES)}},
                        "days": {"type": "integer", "minimum": 1, "maximum": 365},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                        "include_all_users": {"type": "boolean"},
                    }},
            handler=lambda reg, ctx, a: reg._search_messages(ctx, a),
            summary=lambda a: f"检索消息“{a.get('query', '') or '最近'}”",
        ))
        self._add(ToolSpec(
            name="get_task", title="查看任务",
            description="按任务 ID 查看状态、结论摘要和最近事件。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"task_id": {"type": "string", "maxLength": 64}},
                    "required": ["task_id"]},
            handler=lambda reg, ctx, a: reg._get_task(ctx, a),
            summary=lambda a: f"查看任务 {a.get('task_id', '')}",
        ))
        self._add(ToolSpec(
            name="list_tasks", title="列出任务",
            description="列出当前账号可见的最近诊断任务，可按状态筛选。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"status": {"type": "string", "enum": list(TASK_STATUSES)},
                                   "limit": {"type": "integer", "minimum": 1,
                                             "maximum": 30}},
                    "required": []},
            handler=lambda reg, ctx, a: reg._list_tasks(ctx, a),
            summary=lambda a: "列出最近任务",
        ))
        self._add(ToolSpec(
            name="search_skills", title="检索技能",
            description="检索已安装技能的名称、版本、状态和适用场景。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"query": {"type": "string", "maxLength": 120},
                                   "status": {"type": "string", "maxLength": 32},
                                   "limit": {"type": "integer", "minimum": 1,
                                             "maximum": 30}},
                    "required": []},
            handler=lambda reg, ctx, a: reg._search_skills(ctx, a),
            summary=lambda a: f"检索技能“{a.get('query', '') or '全部'}”",
        ))
        self._add(ToolSpec(
            name="get_system_status", title="查看系统状态",
            description="查看平台版本、运行时间、磁盘、并发、模型、备份和升级状态。",
            schema={"type": "object", "additionalProperties": False, "properties": {}},
            handler=lambda reg, ctx, a: reg._system_status(ctx),
            scope="admin", risk="read", mcp="read",
            summary=lambda a: "查看系统状态",
        ))
        self._add(ToolSpec(
            name="list_backups", title="列出备份",
            description="列出升级/导入备份及占用空间，标记正在使用和可删除项。",
            schema={"type": "object", "additionalProperties": False, "properties": {}},
            handler=lambda reg, ctx, a: reg._list_backups(ctx),
            scope="admin", risk="read", mcp="read",
            summary=lambda a: "列出备份",
        ))
        self._add(ToolSpec(
            name="delete_backup", title="删除备份",
            description="删除不再被升级任务使用的备份目录；执行前需要二次确认。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"name": {"type": "string", "maxLength": 120}},
                    "required": ["name"]},
            handler=lambda reg, ctx, a: reg._delete_backup(ctx, a),
            scope="admin", risk="medium", mcp="write",
            summary=lambda a: f"删除备份 {a.get('name', '')}",
        ))
        self._add(ToolSpec(
            name="storage_overview", title="查看任务空间",
            description="查看任务原始输入、产物、可释放空间和磁盘余量。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"limit": {"type": "integer", "minimum": 1,
                                             "maximum": 500}},
                    "required": []},
            handler=lambda reg, ctx, a: reg._storage_overview(ctx, a),
            summary=lambda a: "查看任务空间",
        ))
        self._add(ToolSpec(
            name="release_task_storage", title="释放任务空间",
            description="后台释放某任务的原始输入或解压文件；任务运行中不能释放。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"task_id": {"type": "string", "maxLength": 64},
                                   "mode": {"type": "string",
                                            "enum": ["raw_inputs", "extracted", "both"]}},
                    "required": ["task_id"]},
            handler=lambda reg, ctx, a: reg._release_storage(ctx, a),
            risk="medium", mcp="write",
            summary=lambda a: f"释放任务 {a.get('task_id', '')} 的空间"
                              f"（{a.get('mode', 'raw_inputs')}）",
        ))
        self._add(ToolSpec(
            name="get_update_status", title="查看升级状态",
            description="查看当前版本、最近升级任务、执行阶段、备份和磁盘余量。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"limit": {"type": "integer", "minimum": 1,
                                             "maximum": 20}},
                    "required": []},
            handler=lambda reg, ctx, a: reg._update_status(ctx, a),
            scope="admin", risk="read", mcp="read",
            summary=lambda a: "查看升级状态",
        ))
        self._add(ToolSpec(
            name="apply_update", title="应用升级包",
            description="应用已上传并校验通过的离线升级包；需要管理员密码和二次确认。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"update_id": {"type": "string", "maxLength": 64}},
                    "required": ["update_id"]},
            handler=lambda reg, ctx, a: reg._apply_update(ctx, a),
            scope="admin", risk="high", mcp="never",
            summary=lambda a: f"应用升级包 {a.get('update_id', '')}",
        ))
        self._add(ToolSpec(
            name="rollback_update", title="回滚升级",
            description="回滚到指定升级任务的备份；需要管理员密码和二次确认。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"job_id": {"type": "string", "maxLength": 64},
                                   "backup_dir": {"type": "string", "maxLength": 300}},
                    "required": []},
            handler=lambda reg, ctx, a: reg._rollback_update(ctx, a),
            scope="admin", risk="high", mcp="never",
            summary=lambda a: f"回滚升级 {a.get('job_id', '') or a.get('backup_dir', '')}",
        ))
        self._add(ToolSpec(
            name="list_users", title="检索用户",
            description="按用户名/显示名检索账号、角色、状态和最近登录时间。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"query": {"type": "string", "maxLength": 120},
                                   "limit": {"type": "integer", "minimum": 1,
                                             "maximum": 30}},
                    "required": []},
            handler=lambda reg, ctx, a: reg._list_users(ctx, a),
            scope="admin", risk="read", mcp="read",
            summary=lambda a: f"检索用户“{a.get('query', '') or '全部'}”",
        ))
        self._add(ToolSpec(
            name="update_user", title="修改用户",
            description="启用/停用账号或调整角色；不能停用自己或删除最后一个管理员。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"username": {"type": "string", "maxLength": 64},
                                   "active": {"type": "boolean"},
                                   "role": {"type": "string",
                                            "enum": ["admin", "reviewer", "engineer"]}},
                    "required": ["username"]},
            handler=lambda reg, ctx, a: reg._update_user(ctx, a),
            scope="admin", risk="high", mcp="never",
            summary=lambda a: f"修改用户 {a.get('username', '')}"
                              f"（{('启用' if a.get('active') else '停用') if 'active' in a else '角色'}）",
        ))
        self._add(ToolSpec(
            name="list_models", title="列出模型配置",
            description="列出模型配置、角色、优先级和启用状态；不返回 Key 明文。",
            schema={"type": "object", "additionalProperties": False, "properties": {}},
            handler=lambda reg, ctx, a: reg._list_models(ctx),
            scope="admin", risk="read", mcp="read",
            summary=lambda a: "列出模型配置",
        ))
        self._add(ToolSpec(
            name="update_model", title="修改模型配置",
            description="启用/停用模型、调整角色或优先级；不改 API Key。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"model_id": {"type": "integer", "minimum": 1},
                                   "name": {"type": "string", "maxLength": 64},
                                   "enabled": {"type": "boolean"},
                                   "roles": {"type": "array", "maxItems": 8,
                                             "items": {"type": "string", "enum": list(MODEL_ROLES)}},
                                   "priority": {"type": "integer", "minimum": 0,
                                                "maximum": 100}},
                    "required": []},
            handler=lambda reg, ctx, a: reg._update_model(ctx, a),
            scope="admin", risk="medium", mcp="never",
            summary=lambda a: f"修改模型 {a.get('name') or a.get('model_id', '')}",
        ))
        self._add(ToolSpec(
            name="get_notification_prefs", title="查看通知偏好",
            description="查看当前账号的站内通知开关。",
            schema={"type": "object", "additionalProperties": False, "properties": {}},
            handler=lambda reg, ctx, a: reg._get_notification_prefs(ctx),
            summary=lambda a: "查看通知偏好",
        ))
        self._add(ToolSpec(
            name="update_notification_prefs", title="修改通知偏好",
            description="按通知类型开关站内通知，例如 review_assigned/account_status。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"prefs": {"type": "object",
                                              "additionalProperties": {"type": "boolean"}}},
                    "required": ["prefs"]},
            handler=lambda reg, ctx, a: reg._update_notification_prefs(ctx, a),
            risk="medium", mcp="write",
            summary=lambda a: "修改通知偏好",
        ))
        self._add(ToolSpec(
            name="mark_notifications_read", title="标记通知已读",
            description="把当前账号的指定通知或全部未读通知标记为已读。",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"ids": {"type": "array", "maxItems": 100,
                                           "items": {"type": "string", "maxLength": 64}}},
                    "required": []},
            handler=lambda reg, ctx, a: reg._mark_read(ctx, a),
            risk="medium", mcp="write",
            summary=lambda a: "标记通知已读",
        ))

    # ---------------------------------------------------------------- 文档
    def _search_docs(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("请提供文档检索关键词")
        items = self.kb.search(query, int(args.get("limit") or 5))
        return {"items": items,
                "summary": (f"找到 {len(items)} 段相关文档" if items
                            else "平台文档中没有找到直接匹配"),
                "sources": items}

    # ---------------------------------------------------------------- 消息
    def _search_messages(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        scopes = args.get("scopes") or list(MESSAGE_SCOPES)
        scopes = [s for s in scopes if s in MESSAGE_SCOPES]
        days = max(1, min(365, int(args.get("days") or 30)))
        limit = max(1, min(30, int(args.get("limit") or 10)))
        include_all = bool(args.get("include_all_users"))
        cutoff = _days_ago_iso(days)
        payload: dict[str, Any] = {"items": [], "scopes": {}, "summary": ""}
        notes: list[str] = []

        if "notifications" in scopes:
            clauses = []
            params: list[Any] = []
            if include_all and ctx.is_admin:
                pass
            else:
                clauses.append("user_id=?")
                params.append(ctx.user["id"])
            clauses.append("created_at>=?")
            params.append(cutoff)
            if query:
                clauses.append("(title LIKE ? OR body LIKE ? OR kind LIKE ?)")
                params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
            rows = self.app.db.all(
                "SELECT id,user_id,kind,title,body,link,read_at,created_at FROM notifications "
                "WHERE " + " AND ".join(clauses) +
                " ORDER BY created_at DESC, id DESC LIMIT ?", tuple(params + [limit]))
            items = [{"id": r["id"], "kind": r["kind"], "title": r["title"],
                      "body": _truncate(r["body"], 600), "link": r["link"],
                      "read": bool(r["read_at"]), "created_at": r["created_at"],
                      "scope": "notifications"} for r in rows]
            payload["scopes"]["notifications"] = len(items)
            payload["items"].extend(items)
            if include_all and not ctx.is_admin:
                notes.append("只有管理员能检索全部用户的通知，已按当前账号过滤")

        if "task_events" in scopes:
            scope_sql, scope_args = _task_scope_sql(ctx.user, "t")
            clauses = ["te.ts>=?"]
            params = [cutoff]
            if query:
                clauses.append("(te.message LIKE ? OR te.stage LIKE ? OR te.level LIKE ? "
                               "OR te.data_json LIKE ? OR t.title LIKE ? OR te.task_id LIKE ?)")
                params.extend([f"%{query}%"] * 6)
            rows = self.app.db.all(
                "SELECT te.task_id,te.seq,te.ts,te.level,te.stage,te.message,"
                "t.title AS task_title,t.status AS task_status FROM task_events te "
                "JOIN tasks t ON t.id=te.task_id WHERE " + " AND ".join(clauses) + scope_sql +
                " ORDER BY te.ts DESC, te.id DESC LIMIT ?",
                tuple(params + list(scope_args) + [limit]))
            items = [{"task_id": r["task_id"], "task_title": r["task_title"],
                      "task_status": r["task_status"], "seq": r["seq"], "ts": r["ts"],
                      "level": r["level"], "stage": r["stage"],
                      "message": _truncate(r["message"], 500),
                      "link": _task_link(r["task_id"]), "scope": "task_events"}
                     for r in rows]
            payload["scopes"]["task_events"] = len(items)
            payload["items"].extend(items)

        if "audit" in scopes:
            if not ctx.is_admin:
                notes.append("审计/操作记录仅管理员可检索")
                payload["scopes"]["audit"] = 0
            else:
                clauses = ["ts>=?"]
                params = [cutoff]
                q_lower = query.lower()
                failure_intent = any(k in q_lower for k in
                                     ("失败", "错误", "failed", "failure", "error"))
                if query and failure_intent:
                    # 审计动作名是英文（如 auth.login_failed），用户常用中文
                    # “失败/错误”检索；此时按失败状态过滤，不再做中文子串匹配。
                    clauses.append(
                        "(LOWER(action) LIKE '%failed%' OR LOWER(action) LIKE '%error%' "
                        "OR LOWER(detail_json) LIKE '%\"ok\": false%' "
                        "OR LOWER(detail_json) LIKE '%\"ok\":false%' "
                        "OR LOWER(detail_json) LIKE '%\"status\": error%' "
                        "OR LOWER(detail_json) LIKE '%\"status\":error%')")
                    notes.append("审计检索按失败状态筛选（failed/error 或 detail ok=false）")
                elif query:
                    clauses.append("(username LIKE ? OR action LIKE ? OR target LIKE ? "
                                   "OR detail_json LIKE ? OR ip LIKE ?)")
                    params.extend([f"%{query}%"] * 5)
                rows = self.app.db.all(
                    "SELECT id,username,action,target,detail_json,ip,ts FROM audit_logs "
                    "WHERE " + " AND ".join(clauses) +
                    " ORDER BY id DESC LIMIT ?", tuple(params + [limit]))
                items = [{"id": r["id"], "username": r["username"], "action": r["action"],
                          "target": r["target"], "detail": _truncate(r["detail_json"], 500),
                          "ip": r["ip"], "ts": r["ts"], "scope": "audit"} for r in rows]
                payload["scopes"]["audit"] = len(items)
                payload["items"].extend(items)

        if "external" in scopes:
            rows, statuses = self.external.search(query, limit)
            payload["external_status"] = statuses
            payload["scopes"]["external"] = len(rows)
            payload["items"].extend([{**r, "scope": "external"} for r in rows])
            if not self.external.configured():
                notes.append("外部 IM 适配器尚未配置；当前只支持可插拔接口/本地导出文件")

        payload["items"].sort(key=lambda x: x.get("ts") or x.get("created_at") or "", reverse=True)
        payload["items"] = payload["items"][:max(limit, 1) * 2]
        payload["notes"] = notes
        total = len(payload["items"])
        payload["summary"] = f"共返回 {total} 条消息" if total else "没有找到匹配消息"
        payload["links"] = [{"label": "打开通知中心", "href": "/#/dashboard"},
                            {"label": "查看审计日志", "href": "/#/audit"} if ctx.is_admin else
                            {"label": "查看任务记录", "href": "/#/tasks"}]
        return payload

    # ---------------------------------------------------------------- 任务
    def _get_task(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args.get("task_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
            raise ToolError("任务 ID 格式非法")
        task = self.app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task or not _can_view_task(ctx.user, task):
            raise ToolError("任务不存在或无权查看", 404)
        rows = self.app.db.all(
            "SELECT seq,ts,level,stage,message FROM task_events WHERE task_id=? "
            "ORDER BY seq DESC LIMIT 15", (task_id,))
        events = [{"seq": r["seq"], "ts": r["ts"], "level": r["level"],
                   "stage": r["stage"], "message": _truncate(r["message"], 400)}
                  for r in reversed(rows)]
        item = _task_summary(task, events)
        report = str(task.get("report_md") or "")
        if report:
            item["report_excerpt"] = _truncate(report, 1800)
        return {"task": item, "summary": f"任务 {task_id} 当前为 {task['status']}",
                "links": [{"label": "打开任务详情", "href": _task_link(task_id)}]}

    def _list_tasks(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        status = str(args.get("status") or "").strip()
        limit = max(1, min(30, int(args.get("limit") or 10)))
        scope_sql, scope_args = _task_scope_sql(ctx.user, "t")
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("t.status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else " WHERE 1=1"
        rows = self.app.db.all(
            "SELECT t.* FROM tasks t" + where + scope_sql +
            " ORDER BY t.created_at DESC LIMIT ?", tuple(params + list(scope_args) + [limit]))
        items = [_task_summary(r) for r in rows]
        return {"items": items, "summary": f"返回 {len(items)} 个任务",
                "links": [{"label": "打开任务记录", "href": "/#/tasks"}]}

    def _search_skills(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        if ctx.user.get("role") == "reviewer":
            raise ToolError("专家账号不能查看技能库", 403)
        query = str(args.get("query") or "").strip()
        status = str(args.get("status") or "").strip()
        limit = max(1, min(30, int(args.get("limit") or 10)))
        clauses = []
        params: list[Any] = []
        if query:
            clauses.append("(name LIKE ? OR title LIKE ? OR description LIKE ? OR category LIKE ?)")
            params.extend([f"%{query}%"] * 4)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.app.db.all(
            "SELECT name,title,version,category,description,status,verified,channel,updated_at "
            "FROM skills" + where + " ORDER BY updated_at DESC LIMIT ?", tuple(params + [limit]))
        items = [{"name": r["name"], "title": r["title"], "version": r["version"],
                  "category": r["category"], "description": _truncate(r["description"], 400),
                  "status": r["status"], "verified": bool(r["verified"]),
                  "channel": r["channel"], "updated_at": r["updated_at"],
                  "link": "/#/skills"} for r in rows]
        return {"items": items, "summary": f"返回 {len(items)} 个技能",
                "links": [{"label": "打开技能库", "href": "/#/skills"}]}

    # ---------------------------------------------------------------- 运维
    def _system_status(self, ctx: ToolContext) -> dict[str, Any]:
        import platform as _platform
        data_dir = self.app.config.data_dir
        try:
            disk = shutil.disk_usage(data_dir)
            disk_info = {"total_bytes": disk.total, "free_bytes": disk.free,
                         "used_bytes": disk.used,
                         "free_percent": round(disk.free * 100 / max(1, disk.total), 1)}
        except OSError:
            disk_info = {}
        backups = self.app.backups.overview().get("summary") or {}
        active_update = updater.active_execution(self.app)
        model_status = self.app.gateway.model_status(ctx.user["id"])
        return {
            "version": updater.current_version(),
            "python": _platform.python_version(),
            "platform": _platform.platform(),
            "uptime_sec": round(time.time() - self.app.start_time, 1),
            "workers": self.app.worker.stats(),
            "disk": disk_info,
            "backups": backups,
            "active_update": (active_update or {}).get("id"),
            "current_update_version": (active_update or {}).get("target_version"),
            "model": model_status,
            "counts": {
                "tasks": self.app.db.one("SELECT COUNT(*) AS n FROM tasks")["n"],
                "users": self.app.db.one("SELECT COUNT(*) AS n FROM users")["n"],
                "skills": self.app.db.one("SELECT COUNT(*) AS n FROM skills")["n"],
            },
            "summary": "系统状态已读取",
            "links": [{"label": "打开系统信息", "href": "/#/system"},
                      {"label": "打开备份", "href": "/#/system"}],
        }

    def _list_backups(self, ctx: ToolContext) -> dict[str, Any]:
        data = self.app.backups.overview()
        items = [{k: v for k, v in x.items() if k != "path"} for x in data.get("items", [])[:30]]
        return {"items": items, "storage": data.get("summary") or {},
                "summary": f"共 {len(items)} 个备份，可删除 "
                           f"{(data.get('summary') or {}).get('deletable_count', 0)} 个",
                "links": [{"label": "打开备份管理", "href": "/#/system"}]}

    def _delete_backup(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        try:
            result = self.app.backups.delete(name)
        except ValueError as exc:
            raise ToolError(str(exc), 404 if "不存在" in str(exc) else 400) from exc
        self.app.db.audit(ctx.user["id"], ctx.user["username"], "backup.delete", name,
                          {"size_bytes": result.get("size_bytes"),
                           "file_count": result.get("file_count"), "source": ctx.source},
                          ctx.ip)
        return {"result": result, "summary": f"已删除备份 {name}，释放 "
                                             f"{result.get('size_bytes', 0)} 字节",
                "links": [{"label": "打开备份管理", "href": "/#/system"}]}

    def _storage_overview(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(500, int(args.get("limit") or 200)))
        data = self.app.storage.overview(
            ctx.user["id"], is_admin=ctx.is_admin, limit=limit)
        items = sorted(data.get("items") or [],
                       key=lambda x: int(x.get("total_bytes") or 0), reverse=True)[:10]
        return {"summary_data": data.get("summary") or {},
                "top_tasks": [{"id": x["id"], "title": x["title"], "status": x["status"],
                               "total_bytes": x["total_bytes"],
                               "deletable_bytes": x["deletable_bytes"],
                               "link": _task_link(x["id"])} for x in items],
                "summary": "任务空间已读取",
                "links": [{"label": "打开任务空间", "href": "/#/system"}]}

    def _release_storage(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args.get("task_id") or "").strip()
        mode = str(args.get("mode") or "raw_inputs")
        task = self.app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task or not (_can_view_task(ctx.user, task) or ctx.is_admin):
            raise ToolError("任务不存在或无权释放", 404)
        if task["status"] in ("queued", "running"):
            raise ToolError("任务正在运行，结束后才能释放空间")
        try:
            job = self.app.storage.create(task_id, ctx.user["id"], mode)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        self.app.db.audit(ctx.user["id"], ctx.user["username"], "task.storage.release",
                          task_id, {"mode": mode, "job_id": job.get("id"),
                                    "source": ctx.source}, ctx.ip)
        return {"job": job, "summary": f"已提交空间释放任务 {job.get('id')}（{mode}）",
                "links": [{"label": "打开任务详情", "href": _task_link(task_id)}]}

    def _update_status(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(20, int(args.get("limit") or 8)))
        jobs = updater.history_public(self.app)
        items = []
        for job in jobs[:limit]:
            items.append({k: job.get(k) for k in (
                "id", "action", "phase", "old_version", "target_version",
                "created_at", "updated_at", "error", "backup_dir", "message",
                "progress")})
        active = updater.active_execution(self.app)
        return {"current_version": updater.current_version(),
                "active": (active or {}).get("id"),
                "items": items,
                "disk_free_mb": updater.disk_free(self.app) // (1024 * 1024),
                "summary": f"当前版本 {updater.current_version()}，共 {len(items)} 条升级记录",
                "links": [{"label": "打开升级页面", "href": "/#/system"}]}

    def _apply_update(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        update_id = str(args.get("update_id") or "").strip()
        try:
            status = updater.apply_job(self.app, update_id)
        except Exception as exc:  # noqa: BLE001 - 升级服务错误需原样转给管理员
            raise ToolError(str(exc), 409 if isinstance(exc, updater.UpdateConflict) else 400) from exc
        self.app.db.audit(ctx.user["id"], ctx.user["username"], "system.update_apply",
                          update_id, {"target": status.get("target_version"),
                                      "source": ctx.source}, ctx.ip)
        return {"update": status, "summary": f"已提交升级任务 {update_id}",
                "links": [{"label": "查看升级状态", "href": "/#/system"}]}

    def _rollback_update(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id") or "").strip()
        backup_dir = str(args.get("backup_dir") or "").strip()
        if not job_id and not backup_dir:
            raise ToolError("请提供升级任务 ID 或备份目录")
        try:
            status = updater.rollback_job(self.app, job_id=job_id, backup_dir=backup_dir)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(str(exc), 409 if isinstance(exc, updater.UpdateConflict) else 400) from exc
        self.app.db.audit(ctx.user["id"], ctx.user["username"], "system.update_rollback",
                          status["id"], {"backup": status.get("backup_dir"),
                                         "source": ctx.source}, ctx.ip)
        return {"update": status, "summary": f"已提交回滚任务 {status['id']}",
                "links": [{"label": "查看升级状态", "href": "/#/system"}]}

    def _list_users(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        limit = max(1, min(30, int(args.get("limit") or 10)))
        clauses = []
        params: list[Any] = []
        if query:
            clauses.append("(username LIKE ? OR display_name LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.app.db.all(
            "SELECT * FROM users" + where + " ORDER BY id DESC LIMIT ?", tuple(params + [limit]))
        items = [public_user(r) for r in rows]
        return {"items": items, "summary": f"返回 {len(items)} 个账号",
                "links": [{"label": "打开用户管理", "href": "/#/users"}]}

    def _update_user(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        username = str(args.get("username") or "").strip()
        if not username:
            raise ToolError("请提供用户名")
        target = self.app.db.one("SELECT * FROM users WHERE username=?", (username,))
        if not target:
            raise ToolError("用户不存在", 404)
        has_active = "active" in args and args.get("active") is not None
        has_role = "role" in args and args.get("role") is not None
        if not has_active and not has_role:
            raise ToolError("请至少提供 active 或 role")
        if target["id"] == ctx.user["id"] and has_active and not args.get("active"):
            raise ToolError("不能停用当前登录账号")
        if target["id"] == ctx.user["id"] and has_role and args.get("role") != target["role"]:
            raise ToolError("不能修改当前登录账号的角色")
        role = str(args.get("role") or target["role"])
        if role not in ("admin", "reviewer", "engineer"):
            raise ToolError("角色只能是 admin / reviewer / engineer")
        active = 1 if target["active"] else 0
        status = normalize_status(target)
        if has_active:
            active = 1 if args.get("active") else 0
            status = "active" if active else "disabled"
        if target["role"] == "admin" and (role != "admin" or not active):
            admins = self.app.db.one(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 "
                "AND status='active'")["n"]
            if admins <= 1:
                raise ToolError("至少保留一个启用的管理员账号")
        if active and status == "disabled":
            status = "active"
        self.app.db.execute(
            "UPDATE users SET role=?, active=?, status=?, updated_at=? WHERE id=?",
            (role, active, status, iso_now(), target["id"]))
        revoked = delete_user_sessions(self.app.db, target["id"]) if not active else 0
        self.app.db.audit(ctx.user["id"], ctx.user["username"], "user.update",
                          target["username"], {"role": role, "active": active,
                                               "status": status,
                                               "sessions_revoked": revoked,
                                               "source": ctx.source}, ctx.ip)
        if normalize_status(target) != status:
            labels = {"invited": "待激活", "active": "已启用", "disabled": "已停用",
                      "expired": "已到期"}
            create_notification(self.app.db, target["id"], "account_status",
                                "账号状态变化",
                                f"你的账号状态已变为{labels.get(status, status)}",
                                "/#/dashboard")
        row = self.app.db.one("SELECT * FROM users WHERE id=?", (target["id"],))
        return {"user": public_user(row), "summary": f"已更新用户 {username}："
                                                     f"角色 {role}，状态 {status}",
                "links": [{"label": "打开用户管理", "href": "/#/users"}]}

    def _list_models(self, ctx: ToolContext) -> dict[str, Any]:
        rows = self.app.db.all("SELECT * FROM model_configs ORDER BY priority, id")
        items = [_public_model(self.app, r) for r in rows]
        return {"items": items, "roles": list(MODEL_ROLES),
                "summary": f"共 {len(items)} 个模型配置",
                "links": [{"label": "打开模型接入", "href": "/#/models"}]}

    def _update_model(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        model_id = args.get("model_id")
        name = str(args.get("name") or "").strip()
        if model_id is not None:
            row = self.app.db.one("SELECT * FROM model_configs WHERE id=?", (int(model_id),))
        elif name:
            row = self.app.db.one("SELECT * FROM model_configs WHERE name=?", (name,))
        else:
            raise ToolError("请提供 model_id 或 name")
        if not row:
            raise ToolError("模型配置不存在", 404)
        if not any(k in args for k in ("enabled", "roles", "priority")):
            raise ToolError("请至少提供 enabled / roles / priority")
        role_values = [r for r in (_loads(row.get("roles"), []) or [])]
        enabled = 1 if row["enabled"] else 0
        priority = int(row.get("priority") or 0)
        if "enabled" in args and args.get("enabled") is not None:
            enabled = 1 if args.get("enabled") else 0
        if "roles" in args and args.get("roles") is not None:
            role_values = [str(r) for r in args.get("roles") or []]
            role_values = [r for r in role_values if r in MODEL_ROLES] or ["default"]
        if "priority" in args and args.get("priority") is not None:
            priority = max(0, min(100, int(args.get("priority"))))
        if not enabled and "default" in role_values:
            others = self.app.db.one(
                "SELECT COUNT(*) AS n FROM model_configs WHERE enabled=1 AND id<>? "
                "AND roles LIKE '%default%'", (row["id"],))["n"]
            if others == 0:
                raise ToolError("至少保留一个启用的 default 模型")
        self.app.db.execute(
            "UPDATE model_configs SET roles=?, enabled=?, priority=?, updated_at=? WHERE id=?",
            (json.dumps(role_values, ensure_ascii=False), enabled, priority, iso_now(),
             row["id"]))
        self.app.db.audit(ctx.user["id"], ctx.user["username"], "model.update",
                          row["name"], {"roles": role_values, "enabled": enabled,
                                        "priority": priority, "source": ctx.source}, ctx.ip)
        updated = self.app.db.one("SELECT * FROM model_configs WHERE id=?", (row["id"],))
        return {"model": _public_model(self.app, updated),
                "summary": f"已更新模型 {row['name']}：启用={bool(enabled)}，优先级={priority}",
                "links": [{"label": "打开模型接入", "href": "/#/models"}]}

    def _get_notification_prefs(self, ctx: ToolContext) -> dict[str, Any]:
        row = self.app.db.one("SELECT notification_prefs FROM users WHERE id=?",
                              (ctx.user["id"],))
        return {"prefs": _loads((row or {}).get("notification_prefs"), {}),
                "summary": "通知偏好已读取"}

    def _update_notification_prefs(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        prefs = args.get("prefs")
        if not isinstance(prefs, dict):
            raise ToolError("prefs 必须是对象")
        if len(prefs) > 30:
            raise ToolError("通知偏好数量过多")
        clean: dict[str, bool] = {}
        for key, value in prefs.items():
            key = str(key).strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", key):
                raise ToolError(f"通知类型非法: {key}")
            if not isinstance(value, bool):
                raise ToolError(f"通知类型 {key} 的值必须是布尔值")
            clean[key] = value
        row = self.app.db.one("SELECT notification_prefs FROM users WHERE id=?",
                              (ctx.user["id"],))
        current = _loads((row or {}).get("notification_prefs"), {}) or {}
        current.update(clean)
        self.app.db.execute(
            "UPDATE users SET notification_prefs=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), iso_now(), ctx.user["id"]))
        self.app.db.audit(ctx.user["id"], ctx.user["username"],
                          "profile.notification_prefs.update", ctx.user["username"],
                          {"keys": sorted(clean), "source": ctx.source}, ctx.ip)
        return {"prefs": current, "summary": "通知偏好已更新"}

    def _mark_read(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        ids = args.get("ids") or []
        if ids:
            ids = [str(x) for x in ids if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(x))]
            if not ids:
                raise ToolError("通知 ID 格式非法")
            rows = self.app.db.all(
                "SELECT id FROM notifications WHERE user_id=? AND id IN (%s)"
                % ",".join("?" for _ in ids), tuple([ctx.user["id"]] + ids))
            valid = [r["id"] for r in rows]
            if valid:
                self.app.db.execute(
                    "UPDATE notifications SET read_at=COALESCE(read_at, ?) WHERE user_id=? "
                    "AND id IN (%s)" % ",".join("?" for _ in valid),
                    tuple([iso_now(), ctx.user["id"]] + valid))
            count = len(valid)
        else:
            count = self.app.db.execute(
                "UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                (iso_now(), ctx.user["id"]))
        return {"count": int(count or 0), "summary": f"已标记 {count} 条通知为已读"}


def _days_ago_iso(days: int) -> str:
    from datetime import timedelta
    from .util import utc_now
    return (utc_now() - timedelta(days=max(1, days))).strftime("%Y-%m-%dT%H:%M:%SZ")
