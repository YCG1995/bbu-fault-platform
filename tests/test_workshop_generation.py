"""技能工坊整段文本生成与质量门禁测试。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.workshop import WorkshopService  # noqa: E402
from bbu.workshop_spec import (audit_spec, compile_profile, derive_skill_name, merge_specs,
                               parse_source)  # noqa: E402


class IntakeGenerationTests(unittest.TestCase):
    def spec(self, fixture: str):
        text = (ROOT / "tests" / "fixtures" / "intake" / fixture).read_text(encoding="utf-8")
        return parse_source(text)

    def test_watchdog_register_rules_and_cases(self):
        spec = self.spec("vsw-watchdog.txt")
        self.assertEqual(derive_skill_name(spec), "bbu-fault-vsw-watchdog")
        self.assertEqual(len(spec["evidence_rules"]), 4)
        self.assertEqual(len(spec["root_causes"]), 3)
        self.assertIn("0x(?:630|634)", spec["evidence_rules"][2]["any_of"][0])
        self.assertEqual([c["expected_root_cause"] for c in spec["cases"]], ["NONE", "R1", "R2", "R3"])
        self.assertTrue(audit_spec(spec)["ok"])

    def test_ssd_thresholds_are_executable(self):
        spec = self.spec("vsw-ssd.txt")
        self.assertEqual(len(spec["evidence_rules"]), 5)
        self.assertEqual(len(spec["root_causes"]), 4)
        self.assertIn("[12][0-9]", spec["evidence_rules"][2]["any_of"][0])
        self.assertIn("[1-9]", spec["evidence_rules"][3]["any_of"][0])
        self.assertEqual([c["expected_root_cause"] for c in spec["cases"]], ["NONE", "R1", "R2", "R3"])

    def test_usbflash_root_exclusions(self):
        spec = self.spec("vsw-usbflash.txt")
        conditions = [r["condition"] for r in spec["root_causes"]]
        self.assertEqual(conditions, ["E2", "E1 and not E2", "E3 and not E2 and not E1"])
        self.assertEqual(len(spec["cases"]), 5)

    def test_recipe_rules_are_unverified_by_default(self):
        for fixture, recipe_id in (("vsw-watchdog.txt", "vsw-watchdog"),
                                   ("vsw-ssd.txt", "vsw-ssd"),
                                   ("vsw-usbflash.txt", "vsw-usbflash")):
            spec = self.spec(fixture)
            self.assertEqual(spec["compiler"], "recipe")
            self.assertEqual(spec["recipe"]["id"], recipe_id)
            self.assertFalse(spec["recipe"]["verified"])
            self.assertTrue(spec["recipe"]["version_scope"])
            self.assertTrue(all(not rule["verified"] for rule in spec["evidence_rules"]))
            self.assertTrue(all(rule["recipe_id"] == recipe_id for rule in spec["evidence_rules"]))
            self.assertTrue(all(rule["sample_cases"].get("positive") for rule in spec["evidence_rules"]))
            quality = audit_spec(spec)
            self.assertTrue(any("默认未验证" in w["message"] for w in quality["warnings"]), quality)

    def test_recipe_can_be_disabled_without_injecting_domain_rules(self):
        spec = parse_source(
            (ROOT / "tests" / "fixtures" / "intake" / "vsw-watchdog.txt").read_text(encoding="utf-8"),
            recipe_id="none")
        self.assertEqual(spec["compiler"], "generic")
        self.assertEqual(spec["recipe"], {})
        self.assertFalse(any(rule.get("recipe_id") for rule in spec["evidence_rules"]))
        self.assertFalse(any(root.get("recipe_id") for root in spec["root_causes"]))

    def test_unknown_recipe_is_rejected_instead_of_silently_falling_back(self):
        with self.assertRaises(ValueError):
            parse_source("故障名称：测试\n日志路径：**/*.log\n", recipe_id="not-a-recipe")

    def test_ai_merge_does_not_overwrite_recipe_contract_fields(self):
        spec = self.spec("vsw-ssd.txt")
        recipe = spec["recipe"]
        merged = merge_specs(spec, {
            "category": "AI 猜测类别",
            "domain": {"version_scope": "AI 猜测版本"},
            "aliases": ["AI 别名"],
            "symptoms": {"keywords": ["AI 关键词"]},
        })
        self.assertEqual(merged["category"], recipe["category"])
        self.assertEqual(merged["domain"]["version_scope"], recipe["version_scope"])
        self.assertIn(recipe["aliases"][0], merged["aliases"])
        self.assertIn(recipe["keywords"][0], merged["symptoms"]["keywords"])

    def test_compile_uses_real_rule_and_root_ids(self):
        spec = self.spec("vsw-watchdog.txt")
        profile = compile_profile(spec, "bbu-fault-vsw-watchdog")
        rules = [rule for step in profile["analysis"]["steps"] for rule in step["rules"]]
        self.assertEqual({r["id"] for r in rules}, {"E1", "E2", "E3", "E4"})
        self.assertEqual({r["id"] for r in profile["root_causes"]}, {"R1", "R2", "R3"})
        self.assertNotIn("mode", profile)

    def test_missing_evidence_is_not_silently_successful(self):
        spec = parse_source("故障名称（必填）：测试故障\n一句话描述（必填）：测试")
        quality = audit_spec(spec)
        self.assertFalse(quality["ok"])
        self.assertTrue(any(q["blocking"] for q in quality["questions"]))
    def test_watchdog_register_evidence_does_not_require_simplelog(self):
        spec = self.spec("vsw-watchdog.txt")
        self.assertEqual([r["condition"] for r in spec["root_causes"]],
                         ["E1 and E2", "E3 and not E2", "E4 and not E2 and not E3"])
        pattern = spec["evidence_rules"][2]["any_of"][0]
        self.assertIsNotNone(re.search(pattern, "0x634=0x00000001"))
        self.assertIsNone(re.search(pattern, "0x634=0x0 "))

    def test_ssd_threshold_boundaries(self):
        spec = self.spec("vsw-ssd.txt")
        life = spec["evidence_rules"][2]["any_of"][0]
        self.assertIsNotNone(re.search(life, "169 100 29 Remain Life Percentage"))
        self.assertIsNone(re.search(life, "169 100 30 Remain Life Percentage"))
        sector = spec["evidence_rules"][3]["any_of"][0]
        self.assertIsNotNone(re.search(sector, "160 100 2 Uncorrectable Sector Count"))
        self.assertIsNone(re.search(sector, "160 100 0 Uncorrectable Sector Count"))

    def test_usb_protect_zero_is_not_an_error(self):
        spec = self.spec("vsw-usbflash.txt")
        protect = next(r for r in spec["evidence_rules"] if "protect_status" in r["any_of"][0])
        pattern = protect["any_of"][0]
        self.assertIsNotNone(re.search(pattern, "protect_status: 20102"))
        self.assertIsNone(re.search(pattern, "protect_status: 0"))

    def test_guided_placeholder_labels_keep_short_fault_name(self):
        text = """故障名称：VSW 看门狗复位
适用设备：BBU 基站，VSW 交换板
现场现象：业务偶发中断，VSW 板看门狗计数增长后复位。
日志路径：/var/log/vsw/simplelog.log、/var/log/system/kernel.log
关键日志：出现关键字(07)Watch Dog Underflow
判断思路：先确认看门狗寄存器递增，再检查 SSD 寿命。
根因：SSD 寿命耗尽导致 IO 阻塞。
处理建议：更换 SSD。
"""
        spec = parse_source(text)
        self.assertEqual(spec["fault_name"], "VSW 看门狗复位")
        self.assertEqual(derive_skill_name(spec), "bbu-fault-vsw-watchdog")
        self.assertIn("simplelog", " ".join(x["globs"][0] for x in spec["log_sources"]).lower())


class WorkshopDraftAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(self.tmp)
        self.cfg.values.update({
            "data_dir": str(self.tmp / "data"),
            "skills_dir": str(self.tmp / "skills"),
            "cases_dir": str(self.tmp / "cases"),
            "meta_skill_dir": str(Path(__file__).resolve().parents[1] / "meta" / "bbu-fault-skill-builder"),
        })
        self.cfg.ensure_dirs()
        self.db = Database(self.cfg.db_path)
        self.service = WorkshopService(self.db, self.cfg, None, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _draft(self):
        now = "2026-01-01T00:00:00Z"
        self.db.execute(
            "INSERT INTO workshop_drafts(id,owner_id,name,title,fault_name,category,status,"
            "profile_yaml,description,source_text,analysis_json,check_json,build_json,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("W1", 1, "bbu-fault-test", "测试草稿", "测试故障", "时钟", "editing",
             "skill:\n  name: bbu-fault-test\n", "", "原始资料 " * 8,
             json.dumps({"intake": {"fault_name": "旧"}}), json.dumps({"quality": {"ok": True}}),
             json.dumps({"verified": True}), now, now))
        draft_dir = self.cfg.data_dir / "workshop" / "W1" / "extracted"
        draft_dir.mkdir(parents=True)
        (draft_dir / "a.log").write_text("x", encoding="utf-8")

    def test_rule_trial_run_uses_extracted_logs(self):
        self._draft()
        profile = (
            "skill:\n  name: bbu-fault-test\n  title: 测试\n"
            "analysis:\n  steps:\n    - id: S1\n      rules:\n"
            "        - id: E1\n          description: PLL 失锁\n          type: text\n"
            "          globs: ['**/*.log']\n          any_of: ['CLK032 PLL unlocked']\n"
            "          min_matches: 1\n"
        )
        self.db.execute("UPDATE workshop_drafts SET profile_yaml=? WHERE id='W1'", (profile,))
        (self.cfg.data_dir / "workshop" / "W1" / "extracted" / "a.log").write_text(
            "2026-01-01 CLK032 PLL unlocked\n", encoding="utf-8")
        result = self.service.try_rules("W1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["rules"], 1)
        self.assertEqual(result["hits"][0]["id"], "E1")

    def test_source_change_resets_downstream_results(self):
        self._draft()
        row = self.service.update_draft("W1", {"source_text": "新的完整资料 " * 8})
        self.assertEqual(row["analysis_json"], "{}")
        self.assertEqual(row["check_json"], "{}")
        self.assertEqual(row["build_json"], "{}")
        self.assertEqual(row["source_text"], ("新的完整资料 " * 8).strip())

    def test_profile_change_clears_stale_build(self):
        self._draft()
        self.db.execute(
            "UPDATE workshop_drafts SET build_json=?, skill_version='1.0.0' WHERE id='W1'",
            (json.dumps({"ok": True, "verified": True, "version": "1.0.0"}),))
        row = self.service.update_draft("W1", {
            "profile_yaml": "skill:\n  name: bbu-fault-test\n  title: 新标题\n"})
        self.assertEqual(row["build_json"], "{}")
        self.assertIsNone(row["skill_version"])

    def test_recipe_change_resets_downstream_results_and_rejects_unknown(self):
        self._draft()
        row = self.service.update_draft("W1", {"recipe_id": "vsw-ssd"})
        self.assertEqual(row["recipe_id"], "vsw-ssd")
        self.assertEqual(row["analysis_json"], "{}")
        self.assertEqual(row["check_json"], "{}")
        self.assertEqual(row["build_json"], "{}")
        with self.assertRaisesRegex(ValueError, "未知领域配方"):
            self.service.update_draft("W1", {"recipe_id": "not-a-recipe"})

    def test_build_rejects_stale_profile_after_recipe_switch(self):
        self._draft()
        self.db.execute(
            "UPDATE workshop_drafts SET recipe_id='none', profile_yaml=? WHERE id='W1'",
            ("skill:\n  name: bbu-fault-test\nrecipe:\n  id: vsw-ssd\n",))
        result = self.service.build("W1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "analysis")

    def test_delete_removes_record_and_directory_but_rejects_active_job(self):
        self._draft()
        now = "2026-01-01T00:00:00Z"
        self.db.execute(
            "INSERT INTO workshop_jobs(id,draft_id,user_id,kind,status,stage,progress,message,"
            "args_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("J1", "W1", 1, "build", "running", "building", 30, "", "{}", "{}", now, now))
        with self.assertRaisesRegex(ValueError, "后台任务"):
            self.service.delete_draft("W1")
        self.db.execute("UPDATE workshop_jobs SET status='done' WHERE id='J1'")
        result = self.service.delete_draft("W1")
        self.assertTrue(result["ok"])
        self.assertFalse((self.cfg.data_dir / "workshop" / "W1").exists())
        self.assertFalse(self.db.one("SELECT id FROM workshop_drafts WHERE id='W1'"))



class RecipeGateTests(unittest.TestCase):
    def test_unverified_recipe_rules_are_blocked_by_scaffold(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = Config(tmp)
            cfg.values.update({
                "data_dir": str(tmp / "data"),
                "skills_dir": str(tmp / "skills"),
                "cases_dir": str(tmp / "cases"),
                "meta_skill_dir": str(ROOT / "meta" / "bbu-fault-skill-builder"),
            })
            cfg.ensure_dirs()
            service = WorkshopService(Database(cfg.db_path), cfg, None, None)
            spec = parse_source((ROOT / "tests" / "fixtures" / "intake" / "vsw-watchdog.txt")
                                .read_text(encoding="utf-8"))
            import yaml
            profile_yaml = yaml.safe_dump(compile_profile(spec, "bbu-fault-vsw-watchdog"),
                                          allow_unicode=True, sort_keys=False)
            check = service.check(profile_yaml)
            self.assertFalse(check["ok"])
            self.assertEqual(check["unverified_rules"], ["E1", "E2", "E3", "E4"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SkillNameLengthTests(unittest.TestCase):
    def test_generated_name_never_exceeds_schema_limit(self):
        name = derive_skill_name({"fault_name": "超长名称" + "a" * 120})
        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.startswith("bbu-fault-"))
        self.assertNotIn("--", name)


if __name__ == "__main__":
    unittest.main()
