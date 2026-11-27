"""1.34.0 知识库：问答历史/进度、模型轨迹、现象去重、审计跳转与近重复预览。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIError  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.kb_duplicates import KBDuplicates  # noqa: E402
from bbu.kb_service import KBService  # noqa: E402
from bbu.kb_text import build_cases, strip_title_prefix  # noqa: E402


class _FakeTrace:
    run_id = "T1"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))

    def error(self, **kwargs) -> None:
        self.events.append(("model_error", kwargs))

    def finish(self, status: str = "success", **kwargs) -> None:
        self.events.append(("finish", {"status": status, **kwargs}))


class _FakeTraces:
    def __init__(self) -> None:
        self.trace = _FakeTrace()

    def start(self, **kwargs) -> _FakeTrace:
        self.meta = kwargs
        return self.trace


class _TimeoutGateway:
    def has_real_model(self, role: str) -> bool:
        return True

    def complete(self, *args, **kwargs):
        raise AIError("模型请求超时（30s）", kind="timeout")


class _NoModelGateway:
    def has_real_model(self, role: str) -> bool:
        return False

    def complete(self, *args, **kwargs):
        raise AssertionError("不应调用模型")


class KB134Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-134-"))
        self.config = Config(self.tmp)
        self.config.values["examples_dir"] = str(ROOT / "examples")
        self.db = Database(self.tmp / "data.db")
        from bbu.security import bootstrap_admin, hash_password
        from bbu.util import iso_now
        bootstrap_admin(self.db, self.tmp, "初始管理员密码.txt")
        now = iso_now()
        self.db.execute(
            "INSERT INTO users(username, display_name, role, password_hash, must_change, "
            "active, created_at, status, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("engineer", "工程师", "engineer", hash_password("Engineer-2026"), 0, 1,
             now, "active", now))
        self.admin = {"id": 1, "role": "admin", "username": "admin"}
        self.engineer = {"id": 2, "role": "engineer", "username": "engineer"}

    def tearDown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def service(self, gateway=None, traces=None) -> KBService:
        svc = KBService(self.db, self.config, gateway=gateway, traces=traces,
                        start_worker=False)
        svc.import_sample(self.admin)
        svc.rebuild_index()
        return svc

    def test_chat_history_progress_and_delete(self) -> None:
        svc = self.service(gateway=_NoModelGateway())
        try:
            session = svc.create_chat_session(self.admin, kb_id=1)["session"]
            sent = svc.send_chat_message(session["id"], self.admin,
                                         query="看门狗复位")
            assistant = sent["assistant_message"]
            self.assertEqual(assistant["status"], "done")
            self.assertEqual(assistant["progress"]["percent"], 100)
            self.assertEqual(assistant["payload"]["model_status"]["state"], "no_model")
            self.assertEqual(assistant["payload"]["model_status"]["message"], "")
            self.assertTrue(assistant["payload"]["query_id"])
            detail = svc.get_chat(session["id"], self.admin)
            self.assertEqual(len(detail["messages"]), 2)
            self.assertEqual(detail["running_message_id"], "")
            listed = svc.list_chats(self.admin)["items"]
            self.assertEqual(listed[0]["id"], session["id"])
            self.assertEqual(listed[0]["message_count"], 2)
            self.assertTrue(listed[0]["title"].startswith("看门狗"))
            with self.assertRaises(Exception):
                svc.get_chat(session["id"], self.engineer)
            svc.delete_chat(session["id"], self.admin)
            self.assertEqual(svc.list_chats(self.admin)["items"], [])
        finally:
            svc.shutdown()

    def test_chat_surfaces_model_timeout_and_trace(self) -> None:
        traces = _FakeTraces()
        svc = self.service(gateway=_TimeoutGateway(), traces=traces)
        try:
            session = svc.create_chat_session(self.admin, kb_id=1)["session"]
            sent = svc.send_chat_message(session["id"], self.admin,
                                         query="看门狗复位")
            assistant = sent["assistant_message"]
            self.assertEqual(assistant["status"], "done", assistant)
            status = assistant["payload"]["model_status"]
            self.assertEqual(status["state"], "timeout")
            self.assertIn("超时", status["message"])
            self.assertGreaterEqual(int(status["latency_ms"]), 0)
            self.assertEqual(assistant["payload"]["degraded"], True)
            self.assertEqual(assistant["payload"]["trace_id"], "T1")
            self.assertTrue(any(event == "model_error" and kwargs.get("phase") == "answer"
                                for event, kwargs in traces.trace.events))
            self.assertTrue(any(event == "answer" for event, _ in traces.trace.events))
        finally:
            svc.shutdown()

    def test_symptom_title_dedupe_and_repair(self) -> None:
        title = "故障知识库示例.csv 第 2265 条"
        cases, _ = build_cases(title, "【详细描述】单板反复复位。\n【原因分析】喂狗异常")
        self.assertEqual(cases[0].symptom, "单板反复复位。")
        self.assertEqual(strip_title_prefix(f"{title}：复位", title), "复位")
        dirty = f"{title}\n（详细描述的附件可以贴在“附件”页中）\n单板反复复位。"
        self.assertEqual(strip_title_prefix(dirty, title), "单板反复复位。")
        svc = self.service()
        try:
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            dirty = f"{case['title']}\n{case['symptom'] or '历史现象'}"
            self.db.execute("UPDATE kb_cases SET symptom=? WHERE id=?",
                            (dirty, case["id"]))
            view = svc.get_case(case["id"])["case"]
            self.assertNotIn(case["title"], view["symptom"][:len(case["title"]) + 1])
            repaired = svc.repair_symptom_titles(self.admin)
            self.assertGreaterEqual(repaired["fixed"], 1)
            row = self.db.one("SELECT symptom FROM kb_cases WHERE id=?", (case["id"],))
            self.assertFalse(row["symptom"].startswith(case["title"]))
        finally:
            svc.shutdown()

    def test_disabled_case_still_visible_and_recoverable(self) -> None:
        svc = self.service()
        try:
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            svc.set_case_status(case["id"], "disabled", self.admin, reason="test")
            detail = svc.get_document(doc["id"], reveal=True)
            target = next(x for x in detail["cases"] if x["id"] == case["id"])
            self.assertEqual(target["status"], "disabled")
            svc.set_case_status(case["id"], "active", self.admin, reason="恢复")
            self.assertEqual(svc.get_case(case["id"])["case"]["status"], "active")
        finally:
            svc.shutdown()

    def test_audit_and_doc_revisions_deep_link(self) -> None:
        svc = self.service()
        try:
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            svc.update_case(case["id"], {"solution": "新的处理方案", "reason": "134 测试"},
                            self.admin)
            revisions = svc.list_doc_revisions(doc["id"], self.admin)
            self.assertTrue(revisions["items"])
            self.assertIn(f"#/kb/docs/{doc['id']}", revisions["items"][0]["href"])
            audit = svc.list_audit(self.admin, limit=50)
            items = audit["items"]
            self.assertTrue(items)
            linked = [x for x in items if x.get("doc_id") == doc["id"]]
            self.assertTrue(linked)
            self.assertTrue(linked[0]["href"].startswith(f"#/kb/docs/{doc['id']}"))
        finally:
            svc.shutdown()

    def test_noop_case_save_does_not_create_revision(self) -> None:
        svc = self.service()
        try:
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            before = svc.list_doc_revisions(doc["id"], self.admin)["items"]
            with self.assertRaises(Exception) as ctx:
                svc.update_case(case["id"], {"solution": case["solution"], "reason": "noop"},
                                self.admin)
            self.assertEqual(getattr(ctx.exception, "status", 0), 400)
            after = svc.list_doc_revisions(doc["id"], self.admin)["items"]
            self.assertEqual(len(after), len(before))
        finally:
            svc.shutdown()

    def test_duplicate_preview_shows_all_fields_and_cases(self) -> None:
        svc = self.service()
        try:
            first = svc.create_paste(
                self.admin, title="重复预览 A",
                text="【故障现象】单板反复复位\n【原因分析】喂狗异常\n【处理方案】升级逻辑",
            )
            second = svc.create_paste(
                self.admin, title="重复预览 B",
                text="【故障现象】单板反复复位\n【原因分析】喂狗异常\n【处理方案】升级逻辑版本",
            )
            duplicates = KBDuplicates(svc)
            preview = duplicates.preview(first["doc_id"], second["doc_id"], self.admin)
            fields = {x["field"]: x for x in preview["fields"]}
            for name in ("标题", "现象", "原因", "处理方案", "预防"):
                self.assertIn(name, fields)
            self.assertEqual(fields["现象"]["source"], "单板反复复位")
            self.assertTrue(fields["现象"]["same"])
            self.assertFalse(fields["处理方案"]["same"])
            self.assertTrue(preview["source_case_list"])
            self.assertTrue(preview["source_case_list"][0]["symptom"])
        finally:
            svc.shutdown()

    def test_polish_requires_model(self) -> None:
        svc = self.service(gateway=_NoModelGateway())
        try:
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            with self.assertRaises(Exception) as ctx:
                svc.polish_case(case["id"], self.admin)
            self.assertEqual(getattr(ctx.exception, "status", 0), 503)
        finally:
            svc.shutdown()


if __name__ == "__main__":
    unittest.main()
