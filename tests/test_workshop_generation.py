"""技能工坊整段文本生成与质量门禁测试。"""
from __future__ import annotations

import sys
import unittest
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.workshop_spec import audit_spec, compile_profile, derive_skill_name, parse_source  # noqa: E402


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


class SkillNameLengthTests(unittest.TestCase):
    def test_generated_name_never_exceeds_schema_limit(self):
        name = derive_skill_name({"fault_name": "超长名称" + "a" * 120})
        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.startswith("bbu-fault-"))
        self.assertNotIn("--", name)


if __name__ == "__main__":
    unittest.main()
