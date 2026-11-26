#!/usr/bin/env python3
"""三份整段故障资料的生成 + 全根因合成案例回归。使用临时目录，不污染主技能库。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.workshop_spec import compile_profile, derive_skill_name, parse_source  # noqa: E402

META = ROOT / "meta" / "bbu-fault-skill-builder"
KERNEL = "OssLogVsw/kernel_1-1-1-1-HWM_APP.EXE_6/kernel.klog"
BOARD = "BspLog/BSP_1-1-1-1-HWM_APP.EXE_0/BSP/BoardInit.log"
HEALTH = "BspLog/BSP_1-1-1-1-HWM_APP.EXE_0/BSP/BoardHealth.log"
RESET = "OssLog/BBX_1-1-1-1-HWM_APP.EXE_1/BBX/oss/01_BBX_Simplelog.cur"
NORMAL_BOARD = "epld regs: 0x20=0x1 0x630=0x0 0x634=0x0 0x644=0x0\nepld regs: 0x664=0x0 0x674=0x0\n"


def case(skill, cid, kind, root, hit, not_hit, files):
    return {"skill": skill, "id": cid, "kind": kind, "root": root,
            "hit": hit, "not_hit": not_hit, "files": files}


def cases_for(fixture, skill):
    if fixture == "vsw-watchdog.txt":
        return [
            case(skill, "case-01-normal", "negative", "NONE", [], ["E1", "E2", "E3", "E4"],
                 {RESET: "2026-09-01 normal boot\n", KERNEL: "ata17: SATA link up\n", BOARD: NORMAL_BOARD}),
            case(skill, "case-02-mce", "positive", "R1", ["E1", "E2"], ["E3", "E4"],
                 {RESET: "2026-09-01 (07)Watch Dog Underflow\n", KERNEL: "[654050.554230] mce: [Hardware Error]: CPU 13: Machine Check Exception: 4 Bank 4\n", BOARD: NORMAL_BOARD}),
            case(skill, "case-03-caterr", "positive", "R2", ["E3"], ["E1", "E2", "E4"],
                 {KERNEL: "normal boot\n", BOARD: "epld regs: 0x20=0x1 0x630=0x0 0x634=0x10 0x644=0x0\nepld regs: 0x664=0x0 0x674=0x0\n"}),
            case(skill, "case-04-power", "positive", "R3", ["E4"], ["E1", "E2", "E3"],
                 {KERNEL: "normal boot\n", BOARD: "epld regs: 0x20=0x1 0x630=0x0 0x634=0x0 0x644=0x0\nepld regs: 0x664=0x80 0x674=0x0\n"}),
        ]
    if fixture == "vsw-ssd.txt":
        smart_normal = "169 100 94 Remain Life Percentage\n160 100 0 Uncorrectable Sector Count\n5 100 0 Reallocated Sectors Count\n"
        return [
            case(skill, "case-01-normal", "negative", "NONE", [], ["E1", "E2", "E3", "E4", "E5"],
                 {KERNEL: "ata17: SATA link up 3.0 Gbps\ntype:ssd name:ADATA_IM2S3314 sdx:sda\n", BOARD: smart_normal}),
            case(skill, "case-02-io", "positive", "R1", ["E1"], ["E2", "E3", "E4", "E5"],
                 {KERNEL: "blk_update_request: I/O error, dev sda, sector 123\n", BOARD: smart_normal}),
            case(skill, "case-03-sata", "positive", "R2", ["E2"], ["E1", "E3", "E4", "E5"],
                 {KERNEL: "ata17: COMRESET failed (errno=-16)\n", BOARD: smart_normal}),
            case(skill, "case-04-life", "positive", "R3", ["E3"], ["E1", "E2", "E4", "E5"],
                 {KERNEL: "ata17: SATA link up\n", BOARD: "169 100 22 Remain Life Percentage\n160 100 0 Uncorrectable Sector Count\n5 100 0 Reallocated Sectors Count\n"}),
            case(skill, "case-05-sector", "positive", "R4", ["E4", "E5"], ["E1", "E2", "E3"],
                 {KERNEL: "ata17: SATA link up\n", BOARD: "169 100 80 Remain Life Percentage\n160 100 2 Uncorrectable Sector Count\n5 100 6 Reallocated Sectors Count\n"}),
        ]
    return [
        case(skill, "case-01-normal", "negative", "NONE", [], ["E1", "E2", "E3"],
             {KERNEL: "usb 1-1: new high-speed USB device\n", HEALTH: "protect_status: 0\n"}),
        case(skill, "case-02-other-usb", "negative", "NONE", [], ["E1", "E2", "E3"],
             {KERNEL: "usb 1-2: USB disconnect, device number 3\n", HEALTH: "protect_status: 0\n"}),
        case(skill, "case-03-protect", "positive", "R1", ["E2"], ["E1", "E3"],
             {KERNEL: "usb 1-1: new high-speed USB device\n", HEALTH: "protect_status: 20102\n"}),
        case(skill, "case-04-io", "positive", "R2", ["E1"], ["E2", "E3"],
             {KERNEL: "Buffer I/O error on dev sdb4, logical block 123\n", HEALTH: "protect_status: 0\n"}),
        case(skill, "case-05-smart", "positive", "R3", ["E3"], ["E1", "E2"],
             {KERNEL: "usb 1-1: new high-speed USB device\n", HEALTH: "this flash not support\nprotect_status: 0\n"}),
    ]


def main():
    work = Path(tempfile.mkdtemp(prefix="bbu-workshop-regression-"))
    print("工作目录:", work)
    try:
        for fixture in ("vsw-usbflash.txt", "vsw-watchdog.txt", "vsw-ssd.txt"):
            spec = parse_source((ROOT / "tests" / "fixtures" / "intake" / fixture).read_text(encoding="utf-8"))
            skill = derive_skill_name(spec)
            profile = compile_profile(spec, skill)
            # 配方/通用规则默认 verified=false；回归 harness 模拟“专家人工确认规则”，
            # 否则 scaffold 会因根因引用未验证规则而拒绝构建。
            for step in (profile.get("analysis") or {}).get("steps") or []:
                for rule in (step or {}).get("rules") or []:
                    rule["verified"] = True
            profile_path = work / f"{skill}.yaml"
            profile_path.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8")
            for item in cases_for(fixture, skill):
                cdir = work / "cases" / skill / item["id"]
                input_dir = cdir / "input"
                input_dir.mkdir(parents=True)
                for rel, content in item["files"].items():
                    path = input_dir / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                data = {"id": item["id"], "skill": skill, "description": item["id"],
                        "kind": item["kind"], "device": "VSW", "tags": ["synthetic"],
                        "input": {"unpacked_dir": "input"}, "pipeline": ["unpack", "search"],
                        "expected": {"root_cause": item["root"], "hit_rules": item["hit"],
                                     "not_hit_rules": item["not_hit"]}}
                (cdir / "case.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            build_json = work / f"{skill}-build.json"
            cmd = [sys.executable, str(META / "scripts" / "build_child_skill.py"),
                   "--profile", str(profile_path), "--path", str(work / "skills"),
                   "--cases-dir", str(work / "cases"), "--json", str(build_json)]
            proc = subprocess.run(cmd, cwd=META, text=True, capture_output=True, timeout=900)
            print("\n===", fixture, skill, "exit", proc.returncode, "===")
            print(proc.stdout[-7000:])
            if proc.returncode:
                print(proc.stderr[-4000:])
                print("保留失败现场:", work)
                return 1
            built = Path(json.loads(build_json.read_text(encoding="utf-8"))["path"])
            assert not list(built.rglob("__pycache__")), f"{skill} 交付目录含 __pycache__"
            assert not list(built.rglob("*.pyc")), f"{skill} 交付目录含 .pyc"
        shutil.rmtree(work)
        print("\n全部三技能、全根因合成案例回归通过")
        return 0
    except Exception:
        print("保留异常现场:", work)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
