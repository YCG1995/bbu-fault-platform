"""工坊 AI 契约回归：精简模型上下文、保留评审结论、失败调用记录。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIError, Usage  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.prompts import workshop_extract_user  # noqa: E402
from bbu.workshop import WorkshopService  # noqa: E402
from bbu.workshop_spec import model_spec, parse_source, public_spec  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "intake"


class _ReviewFailGateway:
    """第一次提取、第二次评审返回 error，第三次修复失败。"""

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def has_real_model(self, role):
        return role == "workshop"

    def available(self, role):
        return role == "workshop"

    def configured(self, role):
        return role == "workshop"

    def complete(self, role, system, user, **kwargs):
        self.calls += 1
        self.prompts.append(user)
        if self.calls == 1:
            return SimpleNamespace(model_name="fake-extract", latency_ms=1,
                                   usage=Usage(10, 5, 15), data={"summary": "测试摘要"})
        if self.calls == 2:
            return SimpleNamespace(model_name="fake-review", latency_ms=1,
                                   usage=Usage(8, 4, 12),
                                   data={"issues": [{"severity": "error",
                                                     "field": "evidence_rules[0]",
                                                     "message": "规则缺少日志锚点"}],
                                         "questions": [], "score": 50})
        raise AIError("fake repair failed")


class WorkshopAIContractTests(unittest.TestCase):
    def test_model_spec_omits_internal_metadata_and_shrinks_prompt(self):
        source = (FIXTURES / "vsw-watchdog.txt").read_text(encoding="utf-8")
        spec = parse_source(source)
        compact = model_spec(spec)
        compact_json = json.dumps(compact, ensure_ascii=False)
        public_json = json.dumps(public_spec(spec), ensure_ascii=False)
        self.assertNotIn("provenance", compact_json)
        self.assertNotIn("diagnostic_reasoning", compact_json)
        self.assertIn("evidence_rules", compact)
        self.assertLess(len(compact_json), len(public_json) * 0.35)
        compact_prompt = workshop_extract_user(source, compact)
        public_prompt = workshop_extract_user(source, public_spec(spec))
        self.assertNotIn("provenance", compact_prompt)
        self.assertLess(len(compact_prompt), len(public_prompt) * 0.75)

    def test_repair_failure_preserves_review_error(self):
        tmp = Path(tempfile.mkdtemp(prefix="bbu-workshop-ai-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = Config(tmp)
        cfg.values.update({
            "data_dir": str(tmp / "data"),
            "skills_dir": str(tmp / "skills"),
            "cases_dir": str(tmp / "cases"),
            "meta_skill_dir": str(ROOT / "meta" / "bbu-fault-skill-builder"),
            "install_demo_skills": False,
        })
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        service = WorkshopService(db, cfg, None, _ReviewFailGateway())
        service.check = lambda _yaml: {"ok": True, "output": "test stub"}
        source = (FIXTURES / "vsw-watchdog.txt").read_text(encoding="utf-8")
        draft = service.create_draft(1, {"source_text": source})
        analysis = draft["analysis"]
        self.assertEqual(analysis["extract_status"], "success")
        self.assertEqual(analysis["review_status"], "success")
        self.assertEqual(analysis["repair_status"], "failed")
        self.assertFalse(analysis["quality"]["ok"])
        self.assertTrue(any("规则缺少日志锚点" in str(item.get("message") or "")
                            for item in analysis["quality"].get("errors") or []))
        self.assertTrue(any("AI 评审/修复未完成" in note for note in analysis["notes"]))
        self.assertEqual(draft["status"], "needs_confirmation")


if __name__ == "__main__":
    unittest.main()
