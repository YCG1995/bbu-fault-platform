"""MCP Server：本机 HTTP + Bearer Token，标准 JSON-RPC 2.0。

默认只监听本机请求（除非 config.mcp_allow_remote=true）；每个账号拥有独立
Token，分为 read（只读）与 write（可执行该账号获准的写工具）两种模式。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
from typing import Any

from . import __version__
from .assistant_tools import ToolContext, ToolError
from .httpd import HTTPError, response_json
from .security import get_session  # noqa: F401 - 保持模块依赖显式，便于审计
from .util import iso_now

TOKEN_KEY = "mcp_token_v1"
TOKEN_LAST_USED_KEY = "mcp_token_last_used_v1"
MCP_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "bbu-faultscope", "title": "BBU FaultScope MCP Server",
               "version": __version__}


class MCPProtocolError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPService:
    def __init__(self, app: Any, assistant: Any) -> None:
        self.app = app
        self.assistant = assistant
        self.registry = assistant.registry

    # ---------------------------------------------------------------- Token
    def _record(self, user_id: int | None = None) -> dict[str, Any] | None:
        if user_id is None:
            return None
        row = self.app.db.one("SELECT * FROM mcp_tokens WHERE user_id=?", (int(user_id),))
        return dict(row) if row else None

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def token_info(self, user: dict[str, Any]) -> dict[str, Any]:
        record = self._record(user.get("id"))
        mode = (record or {}).get("mode") or ""
        tools = self.registry.public_tools(
            user, source="mcp", token_mode=mode or "read") if record else []
        return {
            "configured": bool(record),
            "mode": mode,
            "created_at": (record or {}).get("created_at"),
            "last_used_at": (record or {}).get("last_used_at"),
            "username": user.get("username") or "",
            "endpoint": "/mcp",
            "local_only": not bool(self.app.config.get("mcp_allow_remote", False)),
            "transport": "HTTP JSON-RPC（POST /mcp）",
            "protocol_versions": list(MCP_PROTOCOLS),
            "tools": len(tools),
            "tool_names": [t["name"] for t in tools],
            "resources": len(self.assistant.kb.list_docs()),
            "hint": ("" if record else "尚未生成 MCP Token；生成后外部 AI 客户端才能连接。"),
        }

    def rotate_token(self, actor: dict[str, Any], mode: str = "read") -> dict[str, Any]:
        mode = str(mode or "read").strip()
        if mode == "admin":
            mode = "write"
        if mode not in ("read", "write"):
            raise HTTPError(400, "Token 模式只能是 read 或 write")
        if mode == "write" and actor.get("role") not in ("admin", "reviewer", "engineer"):
            mode = "read"
        token = "bbu_mcp_" + secrets.token_urlsafe(36)
        now = iso_now()
        self.app.db.execute(
            "INSERT INTO mcp_tokens(user_id,token_hash,mode,created_at,last_used_at) "
            "VALUES(?,?,?,?,NULL) ON CONFLICT(user_id) DO UPDATE SET "
            "token_hash=excluded.token_hash, mode=excluded.mode, created_at=excluded.created_at, "
            "last_used_at=NULL",
            (actor["id"], self._hash(token), mode, now))
        # 旧全局 Token 不再参与鉴权，创建个人 Token 时清理遗留键。
        self.app.db.execute("DELETE FROM settings WHERE key IN (?,?)", (TOKEN_KEY, TOKEN_LAST_USED_KEY))
        self.app.db.audit(actor["id"], actor["username"], "mcp.token.create", "mcp",
                          {"mode": mode, "scope": "user"}, "")
        return {**self.token_info(actor), "token": token}

    def revoke_token(self, actor: dict[str, Any]) -> dict[str, Any]:
        existed = bool(self._record(actor.get("id")))
        if existed:
            self.app.db.execute("DELETE FROM mcp_tokens WHERE user_id=?", (actor["id"],))
            self.app.db.audit(actor["id"], actor["username"], "mcp.token.revoke", "mcp",
                              {"scope": "user"})
        return {**self.token_info(actor), "revoked": existed}

    # ---------------------------------------------------------------- 鉴权
    def _local_request(self, req: Any) -> bool:
        if bool(self.app.config.get("mcp_allow_remote", False)):
            return True
        try:
            return ipaddress.ip_address(str(req.ip or "")).is_loopback
        except ValueError:
            return False

    def _bearer(self, req: Any) -> str:
        auth = str(req.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return str(req.headers.get("X-MCP-Token") or "").strip()

    def authenticate(self, req: Any) -> ToolContext:
        if not self._local_request(req):
            raise HTTPError(403, "MCP 仅允许本机访问；如确需内网访问请配置 mcp_allow_remote")
        token = self._bearer(req)
        if not token:
            raise HTTPError(401, "MCP Token 尚未配置", {"code": "mcp_token_missing"})
        digest = self._hash(token)
        rows = self.app.db.all(
            "SELECT t.user_id AS token_user_id, t.token_hash, t.mode, t.created_at, "
            "t.last_used_at, u.id, u.username, u.display_name, u.role, u.active, u.status "
            "FROM mcp_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=?", (digest,))
        record = None
        for item in rows:
            if hmac.compare_digest(str(item.get("token_hash") or ""), digest):
                record = dict(item)
                break
        if not record:
            raise HTTPError(401, "MCP Token 无效")
        row = record
        if not row.get("active") or str(row.get("status") or "") not in ("active", ""):
            raise HTTPError(401, "MCP Token 绑定的账号已停用")
        user = {"id": row["id"], "username": row["username"],
                "display_name": row.get("display_name") or row["username"],
                "role": row["role"]}
        mode = str(record.get("mode") or "read")
        if mode == "admin":
            mode = "write" if user["role"] == "admin" else "read"
        # 每用户单独更新最近使用时间，不在读取时重写 Token 哈希。
        try:
            self.app.db.execute("UPDATE mcp_tokens SET last_used_at=? WHERE user_id=?",
                                (iso_now(), user["id"]))
        except Exception:
            pass
        return ToolContext(user=user, ip=str(req.ip or ""), source="mcp", token_mode=mode)

    # ---------------------------------------------------------------- HTTP
    def handle_http(self, req: Any) -> tuple[int, dict[str, str], bytes]:
        if req.method != "POST":
            raise HTTPError(405, "MCP 只接受 POST JSON-RPC 请求")
        ctx = self.authenticate(req)
        raw = req.read_body(1024 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return response_json(self._error(None, -32700, f"JSON 解析失败: {exc}"))
        if isinstance(payload, list):
            if not payload:
                return response_json(self._error(None, -32600, "批量请求不能为空"))
            responses = []
            for item in payload[:20]:
                result = self._dispatch(item, ctx)
                if result is not None:
                    responses.append(result)
            if not responses:
                return 202, {"Content-Type": "application/json; charset=utf-8"}, b""
            return response_json(responses)
        result = self._dispatch(payload, ctx)
        if result is None:
            return 202, {"Content-Type": "application/json; charset=utf-8"}, b""
        return response_json(result)

    def _dispatch(self, request: Any, ctx: ToolContext) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return self._error(None, -32600, "无效的 JSON-RPC 请求")
        request_id = request.get("id")
        is_notification = "id" not in request
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request_id, -32600, "缺少 jsonrpc=2.0 或 method")
        method = request["method"]
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        try:
            if method in ("notifications/initialized", "notifications/cancelled"):
                return None
            result = self._call(method, params, ctx)
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except MCPProtocolError as exc:
            if is_notification:
                return None
            error = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": request_id, "error": error}
        except ToolError as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": str(exc)}],
                               "isError": True}}
        except Exception as exc:  # noqa: BLE001 - MCP 请求错误转为协议错误
            if is_notification:
                return None
            return self._error(request_id, -32603, f"内部错误: {exc}")

    def _error(self, request_id: Any, code: int, message: str,
               data: Any = None) -> dict[str, Any]:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    def _call(self, method: str, params: dict[str, Any], ctx: ToolContext) -> Any:
        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            version = requested if requested in MCP_PROTOCOLS else MCP_PROTOCOLS[0]
            return {
                "protocolVersion": version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                    "logging": {},
                },
                "serverInfo": SERVER_INFO,
                "instructions": ("BBU FaultScope 平台 MCP Server。可检索平台文档、任务、通知、"
                                 "审计与外部 IM 适配器消息；管理员 Token 可执行允许的运维工具。"
                                 "工具调用受当前 Token 模式与账号角色双重限制。"),
            }
        if method == "ping":
            return {}
        if method == "logging/setLevel":
            return {}
        if method == "tools/list":
            return {"tools": self.registry.public_tools(
                ctx.user, source="mcp", token_mode=ctx.token_mode)}
        if method == "tools/call":
            name = str(params.get("name") or "").strip()
            if not name:
                raise MCPProtocolError(-32602, "缺少工具名称")
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                result = self.registry.execute(name, args, ctx)
            except ToolError as exc:
                self.app.db.audit(ctx.user["id"], ctx.user["username"], "mcp.tool.call", name,
                                  {"ok": False, "error": str(exc)[:300],
                                    "mode": ctx.token_mode}, ctx.ip)
                return {"content": [{"type": "text", "text": str(exc)}],
                        "isError": True}
            self.app.db.audit(ctx.user["id"], ctx.user["username"], "mcp.tool.call", name,
                              {"ok": True, "mode": ctx.token_mode,
                               "arguments": json.dumps(args, ensure_ascii=False)[:500]}, ctx.ip)
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            return {"content": [{"type": "text", "text": text}],
                    "structuredContent": result, "isError": False}
        if method == "resources/list":
            items = []
            for doc in self.assistant.kb.list_docs():
                items.append({"uri": doc["uri"], "name": doc["title"],
                              "description": doc["description"], "mimeType": "text/markdown"})
            items.append({"uri": "platform://docs", "name": "平台文档索引",
                          "description": "所有可检索的平台文档目录", "mimeType": "application/json"})
            return {"resources": items}
        if method == "resources/read":
            uri = str(params.get("uri") or "").strip()
            if uri == "platform://docs":
                data = {"docs": self.assistant.kb.list_docs()}
                return {"contents": [{"uri": uri, "mimeType": "application/json",
                                      "text": json.dumps(data, ensure_ascii=False, indent=2)}]}
            prefix = "platform://docs/"
            if not uri.startswith(prefix):
                raise MCPProtocolError(-32602, f"不支持的资源 URI: {uri}")
            doc = self.assistant.kb.read(uri[len(prefix):])
            if not doc:
                raise MCPProtocolError(-32602, f"资源不存在: {uri}")
            return {"contents": [{"uri": uri, "mimeType": "text/markdown",
                                  "text": doc["text"]}]}
        if method == "prompts/list":
            return {"prompts": [{
                "name": "platform_usage",
                "title": "BBU FaultScope 使用问答",
                "description": "把当前问题交给平台文档上下文，生成可核查的使用说明。",
                "arguments": [{"name": "question", "description": "用户的问题",
                               "required": True}],
            }]}
        if method == "prompts/get":
            name = str(params.get("name") or "")
            if name != "platform_usage":
                raise MCPProtocolError(-32602, f"未知提示词: {name}")
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            question = str(args.get("question") or "平台怎么用？")
            docs = self.assistant.kb.search(question, 4)
            context = "\n\n".join(
                f"### {d['doc_title']} / {d['heading']}\n{d['excerpt']}" for d in docs) or "（无匹配文档）"
            text = (f"你是 BBU FaultScope 平台助手。请只依据以下文档片段回答：\n\n{context}\n\n"
                    f"用户问题：{question}\n要求：中文、结论先行、不要编造。")
            return {"description": "平台使用问答提示词",
                    "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        if method in ("roots/list", "sampling/createMessage", "completion/complete"):
            raise MCPProtocolError(-32601, f"不支持的方法: {method}")
        raise MCPProtocolError(-32601, f"未知方法: {method}")
