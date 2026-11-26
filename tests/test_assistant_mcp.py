"""悬浮助手与 MCP Server 的权限、检索、Token 和协议回归。"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIResult  # noqa: E402
from bbu.api import App  # noqa: E402
from bbu.assistant_tools import ToolContext  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.httpd import HTTPError  # noqa: E402
from bbu.notifications import create_notification  # noqa: E402
from bbu.security import hash_password  # noqa: E402


class _Req:
    def __init__(self, method: str, body: bytes = b"", token: str = "",
                 ip: str = "127.0.0.1", headers: dict[str, str] | None = None) -> None:
        self.method = method
        self.ip = ip
        self.body = body
        self.headers = dict(headers or {})
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def read_body(self, _limit: int) -> bytes:
        return self.body


def _rpc(method: str, params: dict | None = None, request_id: int = 1) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                       "params": params or {}}).encode()


class AssistantMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-assistant-")
        root = Path(self.tmp.name)
        self.cfg = Config(ROOT)
        self.cfg.values.update({
            "data_dir": str(root / "data"), "work_dir": str(root / "work"),
            "skills_dir": str(root / "skills"), "cases_dir": str(root / "cases"),
            "examples_dir": str(root / "examples"), "meta_skill_dir": str(root / "meta"),
            "install_demo_skills": False, "max_queue": 4, "max_workers": 1,
            "learning_autostart": False, "assistant_enabled": True,
            "mcp_allow_remote": False,
        })
        self.cfg.ensure_dirs()
        for name in ("demo-bbu-clock-loss", "demo-bbu-optical-rx"):
            src = ROOT / "examples" / "skills" / name
            if src.exists():
                shutil.copytree(src, self.cfg.skills_dir / name)
        self.app = App(self.cfg)
        self.app.worker.shutdown()
        self.admin_row = self.app.db.one("SELECT * FROM users WHERE username='admin'")
        self.admin = {"id": self.admin_row["id"], "username": "admin",
                      "display_name": "管理员", "role": "admin"}
        now = "2026-11-26T00:00:00Z"
        self.eng_id = self.app.db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,must_change,active,"
            "created_at,status,domain,notification_prefs) "
            "VALUES(?,?,?,?,0,1,?,'active','', '{}')",
            ("engineer1", "工程师一", "engineer", hash_password("Engineer!2026x"), now))
        row = self.app.db.one("SELECT * FROM users WHERE id=?", (self.eng_id,))
        self.engineer = {"id": row["id"], "username": row["username"],
                         "display_name": row["display_name"], "role": row["role"]}

    def tearDown(self) -> None:
        self.app.shutdown_services()
        self.tmp.cleanup()

    # ------------------------------------------------------------ 助手
    def test_permissions_and_document_search(self) -> None:
        admin_status = self.app.assistant.status(self.admin)
        eng_status = self.app.assistant.status(self.engineer)
        self.assertTrue(admin_status["enabled"])
        self.assertEqual(admin_status["docs"], 18)
        self.assertGreaterEqual(admin_status["tools"], 18)
        self.assertLess(eng_status["tools"], admin_status["tools"])
        self.assertIn("search_docs", eng_status["tool_names"])
        self.assertIn("get_system_status", admin_status["tool_names"])
        self.assertNotIn("get_system_status", eng_status["tool_names"])
        self.assertNotIn("list_users", eng_status["tool_names"])

        answer = self.app.assistant.chat(
            self.engineer, [{"role": "user", "content": "平台怎么新建诊断任务？"}], "127.0.0.1")
        self.assertIn("使用手册", answer["message"])
        self.assertGreaterEqual(len(answer["sources"]), 1)
        self.assertFalse(answer["pending_actions"])

    def test_model_tool_planning_path(self) -> None:
        plan = AIResult(
            text=json.dumps({"reply": "我来查平台文档。", "calls": [
                {"tool": "search_docs", "arguments": {"query": "怎么新建诊断", "limit": 3}}
            ]}, ensure_ascii=False),
            data={"reply": "我来查平台文档。", "calls": [
                {"tool": "search_docs", "arguments": {"query": "怎么新建诊断", "limit": 3}}
            ]},
            model_name="assistant-mock")
        answer = AIResult(text="按使用手册：打开新建诊断，填写故障描述并上传日志包。",
                          model_name="assistant-mock")
        calls = {"plan": 0, "answer": 0}

        def complete(role, system, prompt, **kwargs):
            if kwargs.get("json_schema"):
                calls["plan"] += 1
                return plan
            calls["answer"] += 1
            return answer

        with (patch.object(self.app.gateway, "choose", return_value={"id": 999}),
              patch.object(self.app.gateway, "complete", side_effect=complete)):
            response = self.app.assistant.chat(
                self.admin, [{"role": "user", "content": "平台怎么新建诊断任务？"}],
                "127.0.0.1")
        self.assertEqual(calls, {"plan": 1, "answer": 1})
        self.assertIn("使用手册", response["message"])
        self.assertEqual(response["tool_calls"][0]["tool"], "search_docs")
        self.assertEqual(response["model"], "assistant-mock")

    def test_message_isolation_and_audit_denial(self) -> None:
        create_notification(self.app.db, self.admin["id"], "system", "ADMIN ONLY", "管理员通知")
        create_notification(self.app.db, self.engineer["id"], "system", "ENG ONLY", "工程师通知")
        ctx = ToolContext(user=self.engineer, ip="127.0.0.1")
        result = self.app.assistant.registry.execute(
            "search_messages", {"query": "", "scopes": ["notifications"]}, ctx)
        titles = [item["title"] for item in result["items"]]
        self.assertIn("ENG ONLY", titles)
        self.assertNotIn("ADMIN ONLY", titles)
        self.assertEqual(result["scopes"]["notifications"], 1)

        audit = self.app.assistant.registry.execute(
            "search_messages", {"query": "", "scopes": ["audit"]}, ctx)
        self.assertEqual(audit["scopes"]["audit"], 0)
        self.assertTrue(any("仅管理员" in note for note in audit["notes"]))

        # 审计动作名常用英文；中文“失败”不能因为字面不匹配而漏掉 failed 记录。
        self.app.db.execute(
            "INSERT INTO audit_logs(user_id,username,action,target,detail_json,ip,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (self.admin["id"], "admin", "auth.login_failed", "admin",
             '{"ok": false}', "127.0.0.1", "2026-11-26T00:00:00Z"))
        admin_ctx = ToolContext(user=self.admin, ip="127.0.0.1")
        failed = self.app.assistant.registry.execute(
            "search_messages", {"query": "失败", "scopes": ["audit"], "days": 30},
            admin_ctx)
        actions = [item["action"] for item in failed["items"]]
        self.assertIn("auth.login_failed", actions)

        with self.assertRaises(Exception) as caught:
            self.app.assistant.registry.execute("get_system_status", {}, ctx)
        self.assertIn("权限不足", str(caught.exception))

    def test_pending_action_confirm_and_high_risk_password(self) -> None:
        ctx = ToolContext(user=self.admin, ip="127.0.0.1")
        spec = self.app.assistant.registry.get("update_notification_prefs")
        action = self.app.assistant._create_pending(
            self.admin, spec, {"prefs": {"review_assigned": False}}, ctx)
        self.assertFalse(action["requires_password"])
        self.assertEqual(self.app.assistant.status(self.admin)["pending_count"], 1)
        result = self.app.assistant.confirm_action(self.admin, action["id"], "127.0.0.1")
        self.assertTrue(result["result"]["ok"])
        prefs = json.loads(self.app.db.one(
            "SELECT notification_prefs FROM users WHERE id=?", (self.admin["id"],))[
            "notification_prefs"])
        self.assertIs(prefs.get("review_assigned"), False)
        with self.assertRaises(Exception) as caught:
            self.app.assistant.confirm_action(self.admin, action["id"], "127.0.0.1")
        self.assertIn("不存在", str(caught.exception))
        self.assertTrue(self.app.assistant.registry.get("apply_update").requires_password)

    def test_create_skill_diagnosis_tool_reuses_source_inputs(self) -> None:
        source_id = "T-SKILL-SRC"
        source_dir = Path(self.cfg.data_dir) / "uploads" / source_id
        source_dir.mkdir(parents=True, exist_ok=True)
        target = source_dir / "clock-demo.zip"
        shutil.copy2(ROOT / "tests" / "fixtures" / "logs" / "clock-demo.zip", target)
        now = "2026-11-26T00:00:00Z"
        self.app.db.execute(
            "INSERT INTO tasks(id,user_id,title,description,title_source,status,stage,progress,mode,"
            "input_json,options_json,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, self.engineer["id"], "CLK032 时钟失锁", "PLL 无法锁定，需要指定技能复核",
             "manual", "done", "done", 100, "fast",
             json.dumps([{"name": target.name, "stored_name": target.name,
                          "stored_path": str(target), "size": target.stat().st_size,
                          "sha256": ""}], ensure_ascii=False), "{}", now, now))
        ctx = ToolContext(user=self.engineer, ip="127.0.0.1", confirmed=True)
        result = self.app.assistant.registry.execute("create_skill_diagnosis", {
            "source_task_id": source_id, "skill": "demo-bbu-clock-loss"}, ctx)
        task = result["task"]
        self.assertEqual(task["mode"], "skill")
        row = self.app.db.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
        options = json.loads(row["options_json"])
        self.assertEqual(options["skill_name"], "demo-bbu-clock-loss")
        self.assertEqual(options["skill_names"], ["demo-bbu-clock-loss"])
        copied = json.loads(row["input_json"])[0]
        self.assertNotEqual(copied["stored_path"], str(target))
        self.assertEqual(Path(copied["stored_path"]).read_bytes(), target.read_bytes())
        self.assertTrue(Path(copied["stored_path"]).is_file())
        with self.assertRaises(Exception) as caught:
            self.app.assistant.registry.execute("create_skill_diagnosis", {
                "source_task_id": source_id, "skill": "missing-skill"}, ctx)
        self.assertIn("不存在或不可用", str(caught.exception))
        audit = self.app.db.all(
            "SELECT action,target,detail_json FROM audit_logs WHERE target=? ORDER BY id DESC",
            (task["id"],))[0]
        self.assertEqual(audit["action"], "task.create")
        self.assertEqual(json.loads(audit["detail_json"])["via"], "assistant")

        # MCP 写 Token 才能看到并按 confirm=true 调用；只读 Token 不可见。
        read_token = self.app.mcp.rotate_token(self.engineer, "read")["token"]
        result = self.app.mcp.handle_http(_Req("POST", _rpc("tools/list"), token=read_token))
        names = [tool["name"] for tool in json.loads(result[2])["result"]["tools"]]
        self.assertNotIn("create_skill_diagnosis", names)
        write_token = self.app.mcp.rotate_token(self.engineer, "write")["token"]
        result = self.app.mcp.handle_http(_Req("POST", _rpc("tools/list"), token=write_token))
        names = [tool["name"] for tool in json.loads(result[2])["result"]["tools"]]
        self.assertIn("create_skill_diagnosis", names)
        call = self.app.mcp.handle_http(_Req("POST", _rpc("tools/call", {
            "name": "create_skill_diagnosis",
            "arguments": {"source_task_id": source_id, "skill": "demo-bbu-clock-loss",
                          "confirm": True},
        }), token=write_token))
        payload = json.loads(call[2])
        self.assertFalse(payload["result"].get("isError"), payload)
        self.assertIn("已创建任务", payload["result"]["content"][0]["text"])

    # ------------------------------------------------------------ MCP
    def test_token_hash_rotation_revoke_and_local_only(self) -> None:
        info = self.app.mcp.rotate_token(self.admin, "read")
        token = info["token"]
        self.assertTrue(token.startswith("bbu_mcp_"))
        record = self.app.db.one("SELECT * FROM mcp_tokens WHERE user_id=?", (self.admin["id"],))
        self.assertNotIn(token, record["token_hash"])
        self.assertEqual(record["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(self.app.mcp.token_info(self.admin)["mode"], "read")

        with self.assertRaises(HTTPError) as caught:
            self.app.mcp.handle_http(_Req("GET", token=token))
        self.assertEqual(caught.exception.status, 405)
        with self.assertRaises(HTTPError) as caught:
            self.app.mcp.handle_http(_Req("POST", token=token, ip="10.0.0.8"))
        self.assertEqual(caught.exception.status, 403)

        result = self.app.mcp.handle_http(_Req("POST", _rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}), token=token))
        self.assertEqual(result[0], 200)
        payload = json.loads(result[2])
        self.assertEqual(payload["result"]["serverInfo"]["name"], "bbu-faultscope")

        old = token
        token = self.app.mcp.rotate_token(self.admin, "admin")["token"]
        with self.assertRaises(HTTPError) as caught:
            self.app.mcp.authenticate(_Req("POST", token=old))
        self.assertEqual(caught.exception.status, 401)
        self.app.mcp.revoke_token(self.admin)
        with self.assertRaises(HTTPError) as caught:
            self.app.mcp.authenticate(_Req("POST", token=token))
        self.assertEqual(caught.exception.status, 401)

    def test_mcp_read_write_boundary(self) -> None:
        token = self.app.mcp.rotate_token(self.admin, "read")["token"]
        result = self.app.mcp.handle_http(_Req("POST", _rpc("tools/list"), token=token))
        names = [tool["name"] for tool in json.loads(result[2])["result"]["tools"]]
        self.assertIn("search_docs", names)
        self.assertIn("get_system_status", names)
        self.assertNotIn("delete_backup", names)
        self.assertNotIn("apply_update", names)

        call = self.app.mcp.handle_http(_Req("POST", _rpc("tools/call", {
            "name": "update_notification_prefs",
            "arguments": {"prefs": {"review_assigned": True}, "confirm": True},
        }), token=token))
        payload = json.loads(call[2])
        self.assertTrue(payload["result"]["isError"])
        self.assertIn("只读模式", payload["result"]["content"][0]["text"])

        admin_token = self.app.mcp.rotate_token(self.admin, "admin")["token"]
        result = self.app.mcp.handle_http(_Req("POST", _rpc("tools/list"), token=admin_token))
        names = [tool["name"] for tool in json.loads(result[2])["result"]["tools"]]
        self.assertIn("delete_backup", names)
        self.assertNotIn("apply_update", names)
        call = self.app.mcp.handle_http(_Req("POST", _rpc("tools/call", {
            "name": "update_notification_prefs",
            "arguments": {"prefs": {"review_assigned": True}, "confirm": True},
        }), token=admin_token))
        payload = json.loads(call[2])
        self.assertFalse(payload["result"].get("isError"))
        self.assertTrue(payload["result"]["content"])

        # 非管理员不能获得 admin 专属工具；write 只开放其角色可用的写工具。
        eng_token = self.app.mcp.rotate_token(self.engineer, "write")["token"]
        ctx = self.app.mcp.authenticate(_Req("POST", token=eng_token))
        self.assertEqual(ctx.user["id"], self.engineer["id"])
        self.assertEqual(ctx.user["username"], self.engineer["username"])
        self.assertEqual(ctx.token_mode, "write")
        used = self.app.db.one("SELECT last_used_at FROM mcp_tokens WHERE user_id=?",
                               (self.engineer["id"],))
        self.assertTrue(used["last_used_at"])
        result = self.app.mcp.handle_http(_Req("POST", _rpc("tools/list"), token=eng_token))
        names = [tool["name"] for tool in json.loads(result[2])["result"]["tools"]]
        self.assertNotIn("delete_backup", names)
        self.assertNotIn("list_users", names)
        self.assertIn("release_task_storage", names)

    def test_mcp_resources_and_docs_tools(self) -> None:
        token = self.app.mcp.rotate_token(self.admin, "read")["token"]
        docs = self.app.assistant.kb.list_docs()
        self.assertEqual(len(docs), 18)
        result = self.app.mcp.handle_http(_Req("POST", _rpc("resources/list"), token=token))
        resources = json.loads(result[2])["result"]["resources"]
        self.assertGreaterEqual(len(resources), 11)
        uri = next(item["uri"] for item in resources if item["uri"].endswith("user-guide"))
        result = self.app.mcp.handle_http(_Req("POST", _rpc("resources/read", {"uri": uri}),
                                               token=token))
        contents = json.loads(result[2])["result"]["contents"]
        self.assertIn("新建诊断", contents[0]["text"])

        result = self.app.mcp.handle_http(_Req("POST", _rpc("tools/call", {
            "name": "search_docs", "arguments": {"query": "怎么新建诊断任务", "limit": 3},
        }), token=token))
        payload = json.loads(result[2])
        text = payload["result"]["content"][0]["text"]
        self.assertIn("使用手册", text)

        result = self.app.mcp.handle_http(_Req("POST", _rpc("prompts/list"), token=token))
        self.assertTrue(json.loads(result[2])["result"]["prompts"])
        result = self.app.mcp.handle_http(_Req("POST", _rpc("prompts/get", {
            "name": "platform_usage", "arguments": {"question": "怎么升级？"}}), token=token))
        self.assertTrue(json.loads(result[2])["result"]["messages"])


if __name__ == "__main__":
    unittest.main()
