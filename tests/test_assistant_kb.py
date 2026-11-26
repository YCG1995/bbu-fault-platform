"""知识库分块、检索契约与小型评测集回归。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.assistant import _retrieval_query  # noqa: E402
from bbu.assistant_kb import KnowledgeBase, _exact_identifiers  # noqa: E402


class KnowledgeBaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kb = KnowledgeBase(ROOT)
        cls.kb._load()
        cls.cases = json.loads(
            (ROOT / "tests" / "kb_retrieval_cases.json").read_text(encoding="utf-8"))

    def test_fenced_code_lines_are_not_headings(self) -> None:
        headings = [chunk.heading for chunk in self.kb._chunks]
        self.assertFalse(any("1) 上传 tar.gz" in heading for heading in headings))
        self.assertFalse(any(heading.strip().startswith("应返回") for heading in headings))
        # 修复伪标题后仍保留真实章节。
        self.assertTrue(any("6. 升级失败怎么处理" in heading for heading in headings))

    def test_public_contract_unchanged(self) -> None:
        docs = self.kb.list_docs()
        self.assertEqual(len(docs), 20)
        self.assertEqual(set(docs[0]), {"slug", "title", "description", "uri", "path"})
        item = self.kb.read("user-guide")
        self.assertIsNotNone(item)
        self.assertIn("新建诊断", item["text"])
        result = self.kb.search("怎么新建诊断任务", 3)
        self.assertTrue(result)
        self.assertEqual(set(result[0]), {"slug", "doc_title", "heading", "score",
                                         "excerpt", "uri"})
        self.assertEqual(result[0]["uri"], "platform://docs/user-guide")

    def test_faq_alias_identifier_and_snippet(self) -> None:
        diagnosis = self.kb.search("怎么新建诊断", 3)[0]
        self.assertEqual(diagnosis["slug"], "user-guide")
        self.assertIn("发起一次诊断", diagnosis["heading"])

        password = self.kb.search("改密码", 3)[0]
        self.assertEqual(password["slug"], "user-guide")

        nnp = self.kb.search("NoNewPrivileges", 3)[0]
        self.assertEqual(nnp["slug"], "upgrade-sudo")
        self.assertIn("NoNewPrivileges", nnp["excerpt"])
        self.assertIn("1.20.3", _exact_identifiers("1.20.3 改了什么"))
        self.assertIn("/api/health", _exact_identifiers("调用 /api/health"))

    def test_intent_routes_for_common_operations(self) -> None:
        rollback = self.kb.search("怎么完整回滚平台", 3)[0]
        self.assertEqual(rollback["slug"], "upgrade-rollback")
        self.assertIn("完整回滚", rollback["heading"])

        install = self.kb.search("Ubuntu16 一键包怎么安装", 3)[0]
        self.assertEqual(install["slug"], "oneclick-deploy")
        self.assertIn("2. 安装", install["heading"])

        web_error = self.kb.search("升级后版本变了但页面报未知错误", 3)[0]
        self.assertEqual(web_error["slug"], "oneclick-deploy")
        self.assertIn("6. 常见问题", web_error["heading"])

        bm25 = self.kb.search("BM25F 是什么", 3)[0]
        self.assertEqual(bm25["slug"], "release-1.20.3")
        self.assertIn("BM25F", bm25["heading"])

    def test_retrieval_eval_set(self) -> None:
        def matched(result: dict, case: dict) -> bool:
            return (result["slug"] == case["slug"]
                    and case["heading"] in result["heading"])

        ranks = []
        for case in self.cases:
            found = next((i + 1 for i, item in enumerate(self.kb.search(case["question"], 5))
                          if matched(item, case)), None)
            ranks.append(found)
        self.assertGreaterEqual(sum(1 for rank in ranks if rank == 1), 50)
        self.assertEqual(sum(1 for rank in ranks if rank and rank <= 5), len(self.cases))
        self.assertGreaterEqual(
            sum((1 / rank if rank else 0) for rank in ranks) / len(ranks), 0.95)

    def test_multi_turn_retrieval_query(self) -> None:
        history = [
            {"role": "user", "content": "网页升级报有效用户 ID 不是 0"},
            {"role": "assistant", "content": "先检查 NoNewPrivileges。"},
            {"role": "user", "content": "那回滚呢"},
        ]
        expanded = _retrieval_query(history, "那回滚呢")
        self.assertIn("网页升级", expanded)
        self.assertIn("那回滚呢", expanded)
        self.assertEqual(_retrieval_query([{"role": "user", "content": "怎么升级"}], "怎么升级"),
                         "怎么升级")


if __name__ == "__main__":
    unittest.main()
