"""数据出域策略、根目录防护与工坊模型载荷回归。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.data_egress import DataEgressPolicy  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.workshop import WorkshopService  # noqa: E402


class DataEgressPolicyTests(unittest.TestCase):
    def test_redaction_preserves_operational_values(self):
        policy = DataEgressPolicy.for_diagnostic(type("C", (), {
            "get": lambda self, key, default=None: 8000})())
        text = ("CLK032 失锁 threshold=-3.2dBm register 0x634=0x1 "
                "contact 13800138000 Authorization: Bearer SECRET_ABC "
                "api_key=KEY_123 password=p@ss")
        safe = policy.sanitize_text(text)
        for token in ("CLK032", "-3.2dBm", "0x634", "0x1"):
            self.assertIn(token, safe)
        for secret in ("13800138000", "SECRET_ABC", "KEY_123", "p@ss"):
            self.assertNotIn(secret, safe)
        self.assertIn("<REDACTED>", safe)

    def test_excerpt_sends_relevant_lines_not_full_noise(self):
        policy = DataEgressPolicy("workshop", "excerpt", 1200)
        text = "\n".join([
            "故障名称：时钟失锁",
            "现场现象：CLK032 告警，PLL 无法锁定",
            *[f"无关调试行 {i} " + "x" * 40 for i in range(200)],
            "关键日志：CLK_LOSS register 0x634=1",
        ])
        sent, audit = policy.excerpt(text)
        self.assertIn("CLK032", sent)
        self.assertIn("0x634", sent)
        self.assertNotIn("无关调试行 150", sent)
        self.assertLess(audit["sent_chars"], audit["source_chars"])
        self.assertEqual(audit["purpose"], "workshop")
        self.assertFalse(audit["raw_source_allowed"])

    def test_payload_audit_has_no_content(self):
        policy = DataEgressPolicy("diagnostic", "evidence_only", 8000)
        original = {"description": "secret token=ABC", "evidence": ["raw line"]}
        safe = policy.sanitize_structure(original)
        audit = policy.payload_audit(original, safe)
        self.assertNotIn("secret token=ABC", json.dumps(audit, ensure_ascii=False))
        self.assertEqual(audit["purpose"], "diagnostic")
        self.assertNotEqual(audit["source_sha256"], audit["sent_sha256"])
        self.assertEqual(len(audit["source_sha256"]), 64)


class ConfigRootGuardTests(unittest.TestCase):
    def test_root_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "BBU_WORK_DIR": "/", "BBU_DATA_DIR": "/", "BBU_SKILLS_DIR": "/",
            "BBU_CASES_DIR": "/",
        }, clear=True):
            with self.assertRaisesRegex(SystemExit, "文件系统根目录"):
                Config(Path(tmp))

    def test_temp_work_dir_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "BBU_WORK_DIR": str(Path(tmp) / "work"),
        }, clear=True):
            cfg = Config(Path(tmp))
            self.assertEqual(cfg.work_dir, (Path(tmp) / "work").resolve())
            self.assertTrue(cfg.data_dir.is_dir())


class _FakeUsage:
    input_tokens = 7
    output_tokens = 3


class _FakeGateway:
    def __init__(self):
        self.prompts: list[str] = []

    def has_real_model(self, role):
        return role == "workshop"

    def available(self, role):
        return role == "workshop"

    def configured(self, role):
        return role == "workshop"

    def complete(self, role, system, user, **kwargs):
        self.prompts.append(user)
        return SimpleNamespace(model_name="fake-workshop", latency_ms=2, usage=_FakeUsage(),
                               data={})


class WorkshopEgressIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(self.tmp)
        self.cfg.values.update({
            "data_dir": str(self.tmp / "data"),
            "skills_dir": str(self.tmp / "skills"),
            "cases_dir": str(self.tmp / "cases"),
            "work_dir": str(self.tmp / "work"),
            "install_demo_skills": False,
        })
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.gateway = _FakeGateway()
        self.service = WorkshopService(self.db, self.cfg, None, self.gateway)
        self.service.check = lambda _yaml: {"ok": True, "output": "test stub"}
        source = (ROOT / "tests" / "fixtures" / "intake" / "vsw-watchdog.txt").read_text(
            encoding="utf-8")
        self.source = source + "\nAuthorization: Bearer SECRET_DO_NOT_LEAK\napi_key=SECRET_KEY"
        now = "2026-01-01T00:00:00Z"
        self.db.execute(
            "INSERT INTO workshop_drafts(id,owner_id,name,title,fault_name,category,status,"
            "profile_yaml,description,source_text,analysis_json,check_json,build_json,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("W1", 1, "bbu-fault-test", "测试草稿", "测试故障", "时钟", "editing",
             "skill:\n  name: bbu-fault-test\n", "", self.source, "{}", "{}", "{}", now, now))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_workshop_prompts_and_audit_never_contain_secret(self):
        row = self.service._generate("W1")
        self.assertTrue(self.gateway.prompts)
        combined = "\n".join(self.gateway.prompts)
        self.assertNotIn("SECRET_DO_NOT_LEAK", combined)
        self.assertNotIn("SECRET_KEY", combined)
        self.assertIn("经策略筛选", combined)
        self.assertNotIn("SECRET_DO_NOT_LEAK", row["analysis_json"])
        self.assertIn("data_egress", row["analysis_json"])
        audit = self.db.one(
            "SELECT action, detail_json FROM audit_logs WHERE action='workshop_model_egress'")
        self.assertIsNotNone(audit)
        self.assertNotIn("SECRET_DO_NOT_LEAK", audit["detail_json"])


if __name__ == "__main__":
    unittest.main()
