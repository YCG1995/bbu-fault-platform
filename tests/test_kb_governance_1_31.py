"""1.31.0 库级可见性、默认库、操作过滤与审核真实总数。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.kb_ingest import parse_paste  # noqa: E402
from bbu.kb_service import KBService  # noqa: E402
from bbu.security import bootstrap_admin  # noqa: E402


class KBGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-governance-"))
        self.config = Config(self.tmp)
        self.db = Database(self.tmp / "data.db")
        bootstrap_admin(self.db, self.tmp, "初始管理员密码.txt")
        self.admin = {"id": 1, "role": "admin", "username": "admin"}
        self.engineer = {"id": 2, "role": "engineer", "username": "engineer"}
        self.svc = KBService(self.db, self.config, start_worker=False)

    def tearDown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def _doc(self, kb_id: int, title: str, text: str) -> str:
        doc = parse_paste(text, title=title)[0]
        self.svc.save_document({"document": doc.public(), "cases": [c.public() for c in doc.cases]},
                               self.admin, kb_id=kb_id)
        return self.svc.list_documents(kb_id=kb_id)["items"][0]["id"]

    def test_visibility_is_enforced_in_status_search_and_library_list(self) -> None:
        hidden = self.svc.create_library(self.admin, name="仅管理员库")
        hidden_id = int(hidden["library"]["id"])
        self._doc(hidden_id, "隐藏案例", "【原因分析】隐藏原因。\n【处理方案】隐藏方案。")
        self.svc.update_library(hidden_id, {"visibility": "admin"}, self.admin)
        status = self.svc.status(self.engineer, kb_id=hidden_id)
        self.assertNotEqual(status["kb_id"], hidden_id)
        self.assertFalse(any(int(x["id"]) == hidden_id for x in status["libraries"]))
        result = self.svc.search("隐藏原因", self.engineer, kb_ids=[hidden_id], log=False)
        self.assertEqual(result["items"], [])
        listed = self.svc.list_libraries()["items"]
        self.assertTrue(any(int(x["id"]) == hidden_id for x in listed))  # 服务层不越权，API 负责过滤

    def test_default_library_is_used_when_kb_id_is_omitted(self) -> None:
        expert = self.svc.create_library(self.admin, name="默认专家库", kind="expert")
        expert_id = int(expert["library"]["id"])
        self.svc.update_library(expert_id, {"is_default": True}, self.admin)
        status = self.svc.status(self.engineer)
        self.assertEqual(int(status["kb_id"]), expert_id)
        self.assertEqual(int(self.svc.default_kb_id()), expert_id)

    def test_operations_can_be_filtered_by_library_and_review_total_is_real(self) -> None:
        expert = self.svc.create_library(self.admin, name="操作专家库")
        expert_id = int(expert["library"]["id"])
        self._doc(expert_id, "操作案例", "【原因分析】操作原因。\n【处理方案】操作方案。")
        self.svc.search("操作原因", self.engineer, kb_ids=[expert_id])
        ops = self.svc.list_operations(self.admin, limit=50, kb_id=expert_id)
        self.assertTrue(any(x.get("kind") == "query" and x.get("query") == "操作原因"
                            for x in ops["items"]), ops)
        case_id = self.svc.get_document(
            self.svc.list_documents(kb_id=expert_id)["items"][0]["id"], reveal=True)["cases"][0]["id"]
        for _ in range(3):
            self.svc.create_review(self.admin, target_type="case", target_id=case_id,
                                   stage="manual", run=False)
        page = self.svc.list_reviews(self.admin, kb_id=expert_id, limit=1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 1)


if __name__ == "__main__":
    unittest.main()
