"""MCP Server：本机 HTTP + Bearer Token，标准 JSON-RPC 2.0。

默认只监听本机请求（除非 config.mcp_allow_remote=true）；Token 绑定创建它的
管理员账号，分为 read（只读）与 admin（可执行允许的写工具）两种模式。
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
    def _record(self) -> dict[str, Any] | None:
        raw = self.app.db.setting(TOKEN_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) and data.get("hash") else None

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def token_info(self, user: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self._record()
        mode = (record or {}).get("mode") or ""
        user_row = None
        if record:
            user_row = self.app.db.one("SELECT * FROM users WHERE id=?",
                                       (int(record.get("user_id") or 0),))
        tools = self.registry.public_tools(
            user or {"id": 0, "role": "admin"}, source="mcp",
            token_mode=mode or "read") if (user or {}).get("role") == "admin" else []
        return {
            "configured": bool(record),
            "mode": mode,
            "created_at": (record or {}).get("created_at"),
            "last_used_at": self.app.db.setting(TOKEN_LAST_USED_KEY) if record else None,
            "username": (user_row or {}).get("username") or (record or {}).get("username") or "",
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
        if mode not in ("read", "admin"):
            raise HTTPError(400, "Token 模式只能是 read 或 admin")
        token = "bbu_mcp_" + secrets.token_urlsafe(36)
        record = {"hash": self._hash(token), "mode": mode,
                  "user_id": actor["id"], "username": actor["username"],
                  "created_at": iso_now()}
        self.app.db.set_setting(TOKEN_KEY, json.dumps(record, ensure_ascii=False))
        self.app.db.execute("DELETE FROM settings WHERE key=?", (TOKEN_LAST_USED_KEY,))
        self.app.db.audit(actor["id"], actor["username"], "mcp.token.create", "mcp",
                          {"mode": mode}, "")
        return {**self.token_info(actor), "token": token}

    def revoke_token(self, actor: dict[str, Any]) -> dict[str, Any]:
        existed = bool(self._record())
        if existed:
            self.app.db.execute("DELETE FROM settings WHERE key=?", (TOKEN_KEY,))
            self.app.db.execute("DELETE FROM settings WHERE key=?", (TOKEN_LAST_USED_KEY,))
            self.app.db.audit(actor["id"], actor["username"], "mcp.token.revoke", "mcp", {})
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
        record = self._record()
        if not record:
            raise HTTPError(401, "MCP Token 尚未配置", {"code": "mcp_token_missing"})
        token = self._bearer(req)
        if not token or not hmac.compare_digest(self._hash(token), str(record["hash"])):
            raise HTTPError(401, "MCP Token 无效")
        row = self.app.db.one("SELECT * FROM users WHERE id=?",
                              (int(record.get("user_id") or 0),))
        if not row or not row.get("active") or str(row.get("status") or "") not in ("active", ""):
            raise HTTPError(401, "MCP Token 绑定的账号已停用")
        user = {"id": row["id"], "username": row["username"],
                "display_name": row.get("display_name") or row["username"],
                "role": row["role"]}
        mode = str(record.get("mode") or "read")
        if mode == "admin" and user["role"] != "admin":
            mode = "read"
        # 最近使用时间单独存键，避免为了更新时间而重写 Token 记录，
        # 防止与轮换并发时把旧 Token 哈希写回来。
        try:
            self.app.db.set_setting(TOKEN_LAST_USED_KEY, iso_now())
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
