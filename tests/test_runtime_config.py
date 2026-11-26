"""运行参数白名单、原子保存和环境变量覆盖。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bbu.config import Config


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config.json").write_text(json.dumps({"brand": "保留我"}))
        self.cfg = Config(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_atomic_save_keeps_unknown_config_and_writes_backup(self):
        saved = self.cfg.save_runtime({"max_workers": "6", "max_total_tokens": "900000"})
        self.assertEqual({x["key"] for x in saved}, {"max_workers", "max_total_tokens"})
        data = json.loads((self.tmp / "config.json").read_text())
        self.assertEqual(data["brand"], "保留我")
        self.assertEqual(data["max_workers"], 6)
        self.assertEqual(data["max_total_tokens"], 900000)
        self.assertTrue((self.tmp / "config.json.bak").exists())
        self.assertEqual(self.cfg.get("max_workers"), 6)

    def test_invalid_or_unknown_changes_are_rejected_without_writing(self):
        before = (self.tmp / "config.json").read_text()
        with self.assertRaisesRegex(ValueError, "不允许修改"):
            self.cfg.save_runtime({"data_dir": "/evil"})
        with self.assertRaisesRegex(ValueError, "不能小于"):
            self.cfg.save_runtime({"max_workers": 0})
        self.assertEqual((self.tmp / "config.json").read_text(), before)

    def test_enum_items_expose_choices_and_roundtrip(self):
        enums = ["tool_policy_mode", "tool_sandbox_mode", "agent_engine_mode",
                 "skill_manifest_mode", "model_schema_mode"]
        saved = self.cfg.save_runtime({k: self.cfg.get(k) for k in enums})
        self.assertEqual({x["key"] for x in saved}, set(enums))
        for key in enums:
            item = next(x for x in self.cfg.runtime_items() if x["key"] == key)
            self.assertTrue(item["choices"], key)
            self.assertEqual(set(item["choices"]), set(item["choice_labels"]), key)
            self.assertEqual(self.cfg.get(key), saved[[x["key"] for x in saved].index(key)]["value"])

    def test_environment_override_is_visible_and_not_overwritable(self):
        with patch.dict(os.environ, {"BBU_MAX_WORKERS": "9"}):
            cfg = Config(self.tmp)
            item = next(x for x in cfg.runtime_items() if x["key"] == "max_workers")
            self.assertEqual(item["env_override"], "BBU_MAX_WORKERS")
            with self.assertRaisesRegex(ValueError, "环境变量"):
                cfg.save_runtime({"max_workers": 3})


if __name__ == "__main__":
    unittest.main()
