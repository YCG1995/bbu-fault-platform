"""技能工坊十个不同场景的回归测试。

覆盖三条领域配方、四条通用故障资料、信息不完整阻断、配方/原文失效和
完整“规则确认 → 正反例 → 构建 → 发布”闭环。全部使用临时目录，不写主库。
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIGateway  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.skills import SkillRegistry  # noqa: E402
from bbu.workshop import WorkshopService  # noqa: E402
from bbu.workshop_jobs import WorkshopJobService  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "intake"

# 四条通用资料的共同骨架；每条只改变日志路径和关键字段，避免靠领域配方命中。
GENERIC_HEAD = """故障名称（必填）：{fault}
一句话描述（必填）：{summary}
设备 / 制式 / 单板 / 版本（选填）：V9200，所有版本
现场能看到的现象（必填）：{observable}
日志类型 / 来源（设备全量日志 / SDR 网管采集 / DCM / CloudMAT 工装 / 其他）：设备全量日志
大致路径或文件名（说个大概就行）：{path}
关键字段说明（选填）：{signature}
判断思路（必填）：先检查 {source}
可能的原因（按可能性列）：{root}
每种原因怎么处理：{action}
风险 / 注意事项（选填）：操作前备份
怎么确认已恢复（选填）：{recovery}
"""


def generic_text(fault: str, summary: str, observable: str, path: str,
                 signature: str, root: str, action: str, recovery: str,
                 *, source: str = "故障日志", extra: str = "") -> str:
    return GENERIC_HEAD.format(
        fault=fault, summary=summary, observable=observable, path=path,
        signature=signature, root=root, action=action, recovery=recovery,
        source=source) + extra


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class WorkshopTenScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="bbu-workshop-ten-"))
        cls.cfg = Config(cls.tmp)
        cls.cfg.values.update({
            "data_dir": str(cls.tmp / "data"),
            "skills_dir": str(cls.tmp / "skills"),
            "cases_dir": str(cls.tmp / "cases"),
            "meta_skill_dir": str(ROOT / "meta" / "bbu-fault-skill-builder"),
            "install_demo_skills": False,
        })
        cls.cfg.ensure_dirs()
        cls.db = Database(cls.cfg.db_path)
        cls.registry = SkillRegistry(cls.db, cls.cfg.skills_dir, cls.cfg.data_dir)
        cls.gateway = AIGateway(cls.db, cls.cfg)
        cls.service = WorkshopService(cls.db, cls.cfg, cls.registry, cls.gateway)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.db.close()
        except AttributeError:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- 工具 -----------------------------------------------------------
    def create(self, text: str, *, recipe_id: str | None = None) -> dict:
        data: dict = {"source_text": text}
        if recipe_id is not None:
            data["recipe_id"] = recipe_id
        return self.service.create_draft(1, data)

    def assert_unverified_gate(self, draft: dict, expected_rules: list[str]) -> None:
        self.assertEqual(draft["status"], "needs_confirmation")
        self.assertFalse(draft["check"]["ok"])
        self.assertEqual(draft["check"]["unverified_rules"], expected_rules)
        marked = self.service.mark_rules_verified(draft["id"])
        self.assertTrue(marked["ok"])
        self.assertEqual(set(marked["changed"]), set(expected_rules))
        self.assertTrue(self.service.get(draft["id"])["check"]["ok"])

    # 11. 自动案例识别与多文件匹配 ---------------------------------------
    def test_11_auto_match_cases_from_fixture_packages(self) -> None:
        draft = self.create(read_fixture("vsw-ssd.txt"))
        names = [
            "正常kernel_1-1-1-1-HWM_APP.EXE_6.tar",
            "正常BoardInit.log.bak.1",
            "735845200429_SSD IO ERROR[Com COM1] (2023-04-21_144732).7z",
            "SATA COMRESET failed [Com COM12] (2023-06-29_190939).7z",
            "寿命耗尽BoardInit.log.bak.7z",
        ]
        upload_dir = self.tmp / "auto-case-uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for name in names:
            path = upload_dir / name
            path.write_bytes(b"case-payload")
            files.append({"stored_path": str(path), "name": name, "relative_path": name})
        auto = self.service.auto_match_cases(draft["id"], files)
        self.assertEqual(len(auto["matched"]), 4)
        self.assertFalse(auto["unmatched_cases"])
        self.assertFalse(auto["unassigned_files"])
        cases = self.service.list_cases(draft["id"])
        self.assertEqual(len(cases), 4)
        normal = next(case for case in cases if case["case_id"] == "case-01")
        self.assertEqual(normal["kind"], "negative")
        self.assertEqual(normal["expected_root_cause"], "NONE")
        self.assertEqual({item["name"] for item in normal["files"]}, set(names[:2]))
        self.assertTrue(any(case["case_id"] == "case-02" for case in cases))


    # 12. 日志上传到自动案例匹配的完整链路 -------------------------------
    def test_12_attach_logs_auto_creates_cases_from_uploaded_packages(self) -> None:
        text = generic_text(
            "自动案例回归故障", "用于验证上传包自动建例", "设备反复重启",
            "OssLog/app.log", "ERROR watchdog", "看门狗复位", "检查日志并复位", "设备恢复") + """
案例编号：C1
案例类型（勾选）：正常（应排除）
日志包（必填，上传文件）：正常.zip
故障现象（一句话）：正常
期望结论（必填）：应排除本故障
案例编号：C2
案例类型（勾选）：故障正例
日志包（必填，上传文件）：故障.zip
故障现象（一句话）：设备反复重启
期望结论（必填）：是本故障，根因是看门狗复位
"""
        draft = self.service.create_draft(1, {"source_text": text}, deferred=True)
        upload_dir = self.tmp / "auto-log-uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        normal = upload_dir / "正常.zip"
        with zipfile.ZipFile(normal, "w") as zf:
            zf.writestr("app.log", "watchdog normal\n")
        bad = upload_dir / "故障.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("app.log", "ERROR watchdog reset\n")
        result = self.service.attach_logs(draft["id"], [
            {"stored_path": str(normal), "name": normal.name},
            {"stored_path": str(bad), "name": bad.name},
        ], auto_cases=True)
        auto = result.get("auto_cases") or {}
        self.assertEqual(len(auto.get("matched") or []), 2)
        cases = self.service.list_cases(draft["id"])
        self.assertEqual(len(cases), 2)
        self.assertEqual({case["kind"] for case in cases}, {"positive", "negative"})
        positive = next(case for case in cases if case["kind"] == "positive")
        self.assertEqual([item["name"] for item in positive["files"]], ["故障.zip"])

    # 13. 文件夹上传中的同名文件不能在任务暂存阶段互相覆盖 --------------
    def test_13_upload_staging_keeps_same_basename_from_different_folders(self) -> None:
        draft = self.service.create_draft(1, {"source_text": generic_text(
            "同名文件上传", "验证目录上传", "设备重启", "OssLog/app.log",
            "ERROR watchdog", "看门狗复位", "检查日志", "设备恢复")}, deferred=True)
        parent = self.tmp / "same-name-folders"
        (parent / "fault-a").mkdir(parents=True, exist_ok=True)
        (parent / "fault-b").mkdir(parents=True, exist_ok=True)
        first = parent / "fault-a" / "same.zip"
        second = parent / "fault-b" / "same.zip"
        first.write_bytes(b"fault-a")
        second.write_bytes(b"fault-b")
        jobs = WorkshopJobService(self.db, self.cfg, self.service, self.gateway)
        try:
            staged = jobs._stage_uploads(draft["id"], "WJtest", [
                {"stored_path": str(first), "name": "fault-a/same.zip",
                 "relative_path": "fault-a/same.zip"},
                {"stored_path": str(second), "name": "fault-b/same.zip",
                 "relative_path": "fault-b/same.zip"},
            ])
        finally:
            jobs.shutdown()
        self.assertEqual(len(staged), 2)
        self.assertEqual(len({item["stored_path"] for item in staged}), 2)
        self.assertEqual({item["relative_path"] for item in staged},
                         {"fault-a/same.zip", "fault-b/same.zip"})
        self.assertEqual({Path(item["stored_path"]).read_bytes() for item in staged},
                         {b"fault-a", b"fault-b"})


    def test_01_watchdog_recipe_generates_rules_and_roots(self) -> None:
        draft = self.create(read_fixture("vsw-watchdog.txt"))
        intake = draft["analysis"]["intake"]
        self.assertEqual(draft["recipe_id"], "auto")
        self.assertEqual(intake["recipe"]["id"], "vsw-watchdog")
        self.assertEqual(intake["compiler"], "recipe")
        self.assertEqual({r["id"] for r in intake["evidence_rules"]}, {"E1", "E2", "E3", "E4"})
        self.assertEqual({r["id"] for r in intake["root_causes"]}, {"R1", "R2", "R3"})
        self.assertEqual(draft["analysis"]["quality"]["counts"]["log_sources"], 3)
        self.assertTrue(draft["analysis"]["quality"]["ok"])
        self.assertEqual([c["expected_root_cause"] for c in intake["cases"]], ["NONE", "R1", "R2", "R3"])
        self.assert_unverified_gate(draft, ["E1", "E2", "E3", "E4"])

    # 2. SSD 阈值配方 -----------------------------------------------------
    def test_02_ssd_recipe_threshold_boundaries(self) -> None:
        draft = self.create(read_fixture("vsw-ssd.txt"))
        intake = draft["analysis"]["intake"]
        self.assertEqual(intake["recipe"]["id"], "vsw-ssd")
        self.assertEqual({r["id"] for r in intake["evidence_rules"]}, {"E1", "E2", "E3", "E4", "E5"})
        self.assertEqual({r["id"] for r in intake["root_causes"]}, {"R1", "R2", "R3", "R4"})
        life = next(r for r in intake["evidence_rules"] if "Remain Life" in r["any_of"][0])
        sector = next(r for r in intake["evidence_rules"] if "Uncorrectable" in r["any_of"][0])
        self.assertIsNotNone(re.search(life["any_of"][0], "169 100 29 Remain Life Percentage"))
        self.assertIsNone(re.search(life["any_of"][0], "169 100 30 Remain Life Percentage"))
        self.assertIsNotNone(re.search(sector["any_of"][0], "160 100 2 Uncorrectable Sector Count"))
        self.assertIsNone(re.search(sector["any_of"][0], "160 100 0 Uncorrectable Sector Count"))
        self.assert_unverified_gate(draft, ["E1", "E2", "E3", "E4", "E5"])

    # 3. USB FLASH 排除配方 ------------------------------------------------
    def test_03_usbflash_recipe_exclusion_order(self) -> None:
        draft = self.create(read_fixture("vsw-usbflash.txt"))
        intake = draft["analysis"]["intake"]
        self.assertEqual(intake["recipe"]["id"], "vsw-usbflash")
        conditions = [r["condition"] for r in intake["root_causes"]]
        self.assertEqual(conditions, ["E2", "E1 and not E2", "E3 and not E2 and not E1"])
        protect = next(r for r in intake["evidence_rules"] if "protect_status" in r["any_of"][0])
        self.assertIsNotNone(re.search(protect["any_of"][0], "protect_status: 20102"))
        self.assertIsNone(re.search(protect["any_of"][0], "protect_status: 0"))
        self.assertEqual(len(intake["cases"]), 5)
        self.assert_unverified_gate(draft, ["E1", "E2", "E3"])

    # 4. 通用时钟故障：非配方路径 -----------------------------------------
    def test_04_generic_clock_skill_without_recipe(self) -> None:
        text = generic_text(
            "时钟失锁", "BBU 设备时钟失锁，业务中断", "网元上报时钟失锁告警",
            "OssLog/clockstatuslog.txt", "LOCKED=0 表示失锁",
            "时钟参考源丢失", "检查参考源和链路", "LOCKED=1")
        draft = self.create(text)
        intake = draft["analysis"]["intake"]
        self.assertEqual(intake["compiler"], "generic")
        self.assertEqual(intake["recipe"], {})
        self.assertEqual([r["id"] for r in intake["evidence_rules"]], ["E1"])
        self.assertEqual({r["id"] for r in intake["root_causes"]}, {"R1"})
        self.assertEqual(intake["root_causes"][0]["condition"], "E1")
        self.assertTrue(draft["analysis"]["quality"]["ok"])
        self.assertEqual(draft["analysis"]["quality"]["counts"]["provenance_coverage"], 1.0)
        self.assert_unverified_gate(draft, ["E1"])

    # 5. 通用光模块：接收功率异常 -----------------------------------------
    def test_05_generic_optical_rx_power(self) -> None:
        text = generic_text(
            "光模块接收功率异常", "光模块 RX 功率过低导致链路倒换", "端口上报 LOS",
            "OssLog/optical.log", "RX_POWER=-18.5",
            "光模块接收功率过低", "检查光纤和光模块", "RX_POWER 恢复到 -5 以内",
            extra="阈值（选填）：接收光功率 低于 -18\n")
        draft = self.create(text)
        intake = draft["analysis"]["intake"]
        self.assertEqual(intake["compiler"], "generic")
        self.assertIn("RX_POWER=\\-18\\.5", intake["evidence_rules"][0]["any_of"])
        self.assertEqual(len(intake["thresholds"]), 1)
        self.assertEqual(intake["thresholds"][0]["metric"], "接收光功率")
        self.assertEqual(intake["thresholds"][0]["value"], -18.0)
        self.assertEqual(intake["thresholds"][0]["version_scope"], "")
        self.assertTrue(draft["analysis"]["quality"]["ok"])
        self.assert_unverified_gate(draft, ["E1"])

    # 6. 通用电源：电压跌落 ------------------------------------------------
    def test_06_generic_power_voltage_drop(self) -> None:
        text = generic_text(
            "单板电源电压跌落", "12V 电源电压跌落导致单板复位", "单板反复复位",
            "BspLog/power.log", "VOLT_12V=10.2",
            "电源模块输出跌落", "检查电源模块和背板", "VOLT_12V 恢复到 12.0 附近",
            extra="阈值（选填）：VOLT_12V 低于 10.5\n")
        draft = self.create(text)
        intake = draft["analysis"]["intake"]
        self.assertEqual(intake["compiler"], "generic")
        self.assertIn("VOLT_12V=10\\.2", intake["evidence_rules"][0]["any_of"])
        self.assertEqual(len(intake["thresholds"]), 1)
        self.assertEqual(intake["thresholds"][0]["operator"], "<")
        self.assertEqual(intake["thresholds"][0]["value"], 10.5)
        self.assertEqual(intake["thresholds"][0]["version_scope"], "")
        self.assertEqual(intake["root_causes"][0]["actions"], ["检查电源模块和背板"])
        self.assert_unverified_gate(draft, ["E1"])

    # 7. 通用风扇：转速为 0 ------------------------------------------------
    def test_07_generic_fan_speed_zero(self) -> None:
        text = generic_text(
            "风扇转速异常", "风扇转速为 0 导致温度升高", "风扇告警和温度升高",
            "BspLog/fan.log", "FAN_SPEED=0",
            "风扇堵转或供电异常", "清理风扇并检查供电", "FAN_SPEED 恢复到正常区间")
        draft = self.create(text)
        intake = draft["analysis"]["intake"]
        self.assertEqual(intake["compiler"], "generic")
        self.assertEqual(intake["evidence_rules"][0]["any_of"], ["FAN_SPEED=0"])
        self.assertEqual(intake["root_causes"][0]["name"], "风扇堵转或供电异常")
        self.assertTrue(draft["analysis"]["quality"]["ok"])
        self.assert_unverified_gate(draft, ["E1"])

    # 8. 资料不完整：必须阻断在质量门禁 --------------------------------
    def test_08_incomplete_intake_blocks_at_quality_gate(self) -> None:
        text = """故障名称（必填）：不完整故障
一句话描述（必填）：只有现象，没有日志路径和日志特征
现场能看到的现象（必填）：业务偶发中断
判断思路（必填）：先凭经验判断
可能的原因（按可能性列）：单板故障
每种原因怎么处理：更换单板
"""
        draft = self.create(text)
        quality = draft["analysis"]["quality"]
        self.assertFalse(quality["ok"])
        fields = {e["field"] for e in quality["errors"]}
        self.assertIn("log_sources", fields)
        self.assertIn("evidence_rules", fields)
        result = self.service.build(draft["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "quality")

    # 9. 配方切换/旧 profile：下游结果必须失效 --------------------------
    def test_09_recipe_switch_and_stale_profile_invalidation(self) -> None:
        draft = self.create(read_fixture("vsw-watchdog.txt"))
        old_profile = draft["profile_yaml"]
        switched = self.service.update_draft(draft["id"], {"recipe_id": "none"})
        self.assertEqual(switched["recipe_id"], "none")
        self.assertEqual(switched["analysis_json"], "{}")
        self.assertEqual(switched["check_json"], "{}")
        self.assertEqual(switched["build_json"], "{}")
        # 模拟切换配方后仍拿旧 profile 构建：必须拒绝，而不是静默用旧规则。
        self.db.execute("UPDATE workshop_drafts SET profile_yaml=? WHERE id=?",
                        (old_profile, draft["id"]))
        result = self.service.build(draft["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "analysis")
        # 原文变化同样清空下游结果。
        updated = self.service.update_draft(draft["id"], {
            "source_text": read_fixture("vsw-ssd.txt")})
        self.assertEqual(updated["analysis_json"], "{}")
        self.assertEqual(updated["check_json"], "{}")
        self.assertEqual(updated["build_json"], "{}")

    # 10. 完整闭环：规则确认、正反例、构建、发布 ------------------------
    def test_10_full_lifecycle_build_and_publish(self) -> None:
        text = generic_text(
            "时钟失锁", "BBU 设备时钟失锁，业务中断", "网元上报时钟失锁告警",
            "OssLog/clockstatuslog.txt", "LOCKED=0 表示失锁",
            "时钟参考源丢失", "检查参考源和链路", "LOCKED=1")
        draft = self.create(text)
        self.assert_unverified_gate(draft, ["E1"])
        name = draft["name"]

        positive = self.tmp / "case-positive.zip"
        with zipfile.ZipFile(positive, "w") as zf:
            zf.writestr("clockstatuslog.txt", "LOCKED=0 表示失锁\n")
        result = self.service.attach_case(draft["id"], [
            {"stored_path": str(positive), "name": positive.name}], {
            "case_id": "case-01", "kind": "positive",
            "root_cause": "R1", "hit_rules": ["E1"],
        })
        self.assertTrue(result["ok"])
        first_build = self.service.build(draft["id"])
        self.assertTrue(first_build["ok"])
        self.assertTrue(first_build["verified"])
        self.assertEqual(first_build["regression"]["total"], 1)
        with self.assertRaisesRegex(ValueError, "至少需要 1 个故障正例和 1 个正常/反例"):
            self.service.publish(draft["id"], "admin")

        negative = self.tmp / "case-negative.zip"
        with zipfile.ZipFile(negative, "w") as zf:
            zf.writestr("clockstatuslog.txt", "LOCKED=1 正常\n")
        result = self.service.attach_case(draft["id"], [
            {"stored_path": str(negative), "name": negative.name}], {
            "case_id": "case-02", "kind": "negative",
        })
        self.assertTrue(result["ok"])
        final_build = self.service.build(draft["id"])
        self.assertTrue(final_build["ok"])
        self.assertTrue(final_build["verified"])
        self.assertEqual(final_build["regression"]["total"], 2)
        published = self.service.publish(draft["id"], "admin")
        self.assertTrue(published["ok"])
        self.assertTrue(published["verified"])
        skills = {s.name: s for s in self.registry.scan()}
        self.assertIn(name, skills)
        self.assertEqual(skills[name].status, "ready")
        self.assertTrue(skills[name].verified)
        self.assertEqual(skills[name].health["manifest_status"], "verified")
        with self.assertRaisesRegex(ValueError, "已发布的草稿不可直接编辑"):
            self.service.update_draft(draft["id"], {"title": "不允许"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
