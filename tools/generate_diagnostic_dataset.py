#!/usr/bin/env python3
"""生成 BBU 故障平台的诊断推理数据集。

产物：
- cases/<skill>/<case-id>/          可直接被 run_regression.py 消费的案例（YAML 是唯一断言来源）
- datasets/diagnostic_reasoning/    推理语料、基准题、专家 playbook、种子经验

数据全部为合成日志，用于回归与训练，不冒充真实现网案例。
"""
from __future__ import annotations

import argparse
import json
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS = {
    "bbu-fault-vsw-watchdog": {
        "source": PROJECT_ROOT / "tests/fixtures/intake/vsw-watchdog.txt",
        "fault": "看门狗复位",
    },
    "bbu-fault-vsw-ssd": {
        "source": PROJECT_ROOT / "tests/fixtures/intake/vsw-ssd.txt",
        "fault": "SSD 故障",
    },
    "bbu-fault-vsw-usbflash": {
        "source": PROJECT_ROOT / "tests/fixtures/intake/vsw-usbflash.txt",
        "fault": "USB FLASH 故障",
    },
}

SIMPLELOG_WD = "2026-11-02 03:14:15.123 [BBU] (07)Watch Dog Underflow detected, resetting board"
SIMPLELOG_OK = "2026-11-02 03:14:15.123 [BBU] System startup, reset reason: PowerOn"
KERNEL_NORMAL = "[    0.001] Linux version 5.10.0 (build@bbu) #1 SMP PREEMPT"
KERNEL_MCE = "[  123.456] mce: [Hardware Error]: CPU 13: Machine Check Exception: 4 Bank 4: b200000000000000"
KERNEL_PANIC = "[  123.500] Kernel panic - not syncing: Fatal exception in interrupt"
KERNEL_PANIC_ALT = "[  124.001] Kernel panic - not syncing: watchdog detected hard LOCKUP"
BOARD_HEAD = [
    "BoardInit start",
    "EPLD register dump begin",
    "0x630=0x00000000",
    "0x634=0x00000000",
    "0x664=0x00000000",
    "0x674=0x00000000",
    "EPLD register dump end",
]
BOARD_CATERR = [
    "BoardInit start",
    "EPLD register dump begin",
    "0x630=0x00000000",
    "0x634=0x00000010",
    "0x664=0x00000000",
    "0x674=0x00000000",
    "EPLD register dump end",
]
BOARD_POWER = [
    "BoardInit start",
    "EPLD register dump begin",
    "0x630=0x00000000",
    "0x634=0x00000000",
    "0x664=0x00000080",
    "0x674=0x00000000",
    "EPLD register dump end",
]
BOARD_BOTH = [
    "BoardInit start",
    "EPLD register dump begin",
    "0x630=0x00000004",
    "0x634=0x00000010",
    "0x664=0x00000002",
    "0x674=0x00000000",
    "EPLD register dump end",
]


def _watch(
    case_id: str,
    kind: str,
    root: str,
    hit: list[str],
    not_hit: list[str],
    *,
    simplelog: str = SIMPLELOG_OK,
    kernel: str = KERNEL_NORMAL,
    board: list[str] | None = None,
    description: str,
    tags: list[str] | None = None,
    packaging: str = "unpacked",
    prefix: str = "",
) -> dict[str, Any]:
    files = {
        "OssLog/BBX_1-1-1-1-HWM_APP.EXE_1/BBX/oss/01_BBX_Simplelog.cur": simplelog,
        "OssLogVsw/kernel_1-1-1-1-HWM_APP.EXE_6/kernel.klog": kernel,
        "BspLog/BSP_1-1-1-1-HWM_APP.EXE_0/BSP/BoardInit.log": "\n".join(board or BOARD_HEAD),
    }
    return _case(case_id, kind, root, hit, not_hit, files,
                 "VSW 看门狗复位：" + description, tags or [], packaging, prefix)


def _usb(
    case_id: str,
    kind: str,
    root: str,
    hit: list[str],
    not_hit: list[str],
    *,
    kernel: str,
    smart: str,
    board: str = "protect_status: 0\n",
    description: str,
    tags: list[str] | None = None,
    packaging: str = "unpacked",
    prefix: str = "",
) -> dict[str, Any]:
    files = {
        "OssLogVsw/kernel_1-1-1-1-HWM_APP.EXE_6/kernel.klog": kernel,
        "BspLog/BSP_1-1-1-1-HWM_APP.EXE_0/BSP/BoardHealth.log": board + "\n" + smart,
    }
    return _case(case_id, kind, root, hit, not_hit, files,
                 "VSW 单板 USBFLASH 故障：" + description, tags or [], packaging, prefix)


def _ssd(
    case_id: str,
    kind: str,
    root: str,
    hit: list[str],
    not_hit: list[str],
    *,
    kernel: str,
    smart: str,
    description: str,
    tags: list[str] | None = None,
    packaging: str = "unpacked",
    prefix: str = "",
) -> dict[str, Any]:
    files = {
        "OssLogVsw/kernel_1-1-1-1-HWM_APP.EXE_6/kernel.klog": kernel,
        "BspLog/BSP_1-1-1-1-HWM_APP.EXE_0/BSP/BoardInit.log": smart,
    }
    return _case(case_id, kind, root, hit, not_hit, files,
                 "VSW 单板 SSD 故障：" + description, tags or [], packaging, prefix)


def _case(case_id, kind, root, hit, not_hit, files, description, tags, packaging, prefix):
    return {
        "id": case_id,
        "kind": kind,
        "root": root,
        "hit": hit,
        "not_hit": not_hit,
        "files": {name: text if text.endswith("\n") else text + "\n" for name, text in files.items()},
        "description": description,
        "tags": tags,
        "packaging": packaging,
        "prefix": prefix,
    }


def watchdog_cases() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []
    c.append(_watch("neg-clean-01", "negative", "NONE", [], ["E1", "E2", "E3", "E4"],
                    description="BBU 重启后运行正常，Simplelog 只有正常启动，内核无 MCE/panic，EPLD 寄存器全 0。",
                    tags=["clean", "no-watchdog"]))
    c.append(_watch("neg-mce-no-simplelog", "negative", "NONE", ["E2"], ["E1", "E3", "E4"],
                    kernel=KERNEL_MCE,
                    description="内核出现 MCE，但 Simplelog 没有看门狗复位记录，不能反推为看门狗根因。",
                    tags=["discriminator", "mce-without-watchdog"]))
    c.append(_watch("neg-panic-no-simplelog", "negative", "NONE", ["E2"], ["E1", "E3", "E4"],
                    kernel=KERNEL_PANIC,
                    description="只有一次内核 panic，Simplelog 未记录看门狗复位，按资料不判定看门狗故障。",
                    tags=["boundary", "panic-without-watchdog"]))
    c.append(_watch("neg-clean-uppercase", "boundary", "NONE", [], ["E1", "E2", "E3", "E4"],
                    simplelog="2026-11-02 03:14:15.123 [BBU] normal boot",
                    board=["BoardInit start", "0X630=0X00000000", "0X634=0X00000000",
                           "0X664=0X00000000", "0X674=0X00000000", "EPLD register dump end"],
                    description="日志字段为大写十六进制且全 0，属于正常边界，不能误报寄存器异常。",
                    tags=["boundary", "uppercase-zero"]))
    c.append(_watch("neg-clean-site-path", "negative", "NONE", [], ["E1", "E2", "E3", "E4"],
                    description="站点目录嵌套较深、日志文件名带时间戳，内容仍是干净启动。",
                    tags=["path", "clean"], packaging="unpacked", prefix="DCM_20261101/siteA/BBU"))
    c.append(_watch("pos-mce-bank4", "positive", "R1", ["E1", "E2"], ["E3", "E4"],
                    simplelog=SIMPLELOG_WD, kernel=KERNEL_MCE,
                    description="Simplelog 明确记录看门狗下电复位，内核同时有 MCE Bank4，优先判 MCE。",
                    tags=["mce", "primary"]))
    c.append(_watch("pos-mce-and-panic", "positive", "R1", ["E1", "E2"], ["E3", "E4"],
                    simplelog=SIMPLELOG_WD, kernel=KERNEL_MCE + "\n" + KERNEL_PANIC_ALT,
                    description="看门狗复位伴随 MCE 与 Kernel panic，两个直接证据都指向内核异常。",
                    tags=["mce", "panic"]))
    c.append(_watch("pos-panic-only", "positive", "R1", ["E1", "E2"], ["E3", "E4"],
                    simplelog=SIMPLELOG_WD, kernel=KERNEL_PANIC,
                    description="看门狗复位后只有 Kernel panic，没有 MCE，仍由内核 panic 解释复位。",
                    tags=["panic"]))
    c.append(_watch("pos-mce-bank0", "positive", "R1", ["E1", "E2"], ["E3", "E4"],
                    simplelog=SIMPLELOG_WD,
                    kernel="[  125.100] mce: [Hardware Error]: CPU 5: Machine Check Exception: 4 Bank 0: be00000000000000",
                    description="MCE 发生在 Bank0，说明内核直接报告硬件异常，不能因 Bank 不同而漏判。",
                    tags=["mce", "bank0"]))
    c.append(_watch("pos-caterr-0x634-10", "positive", "R2", ["E3"], ["E1", "E2", "E4"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] system warm reset",
                    board=BOARD_CATERR,
                    description="无看门狗记录、无 MCE，但 EPLD caterr 寄存器 0x634=0x10，判 CPU 中断异常。",
                    tags=["caterr", "register"]))
    c.append(_watch("pos-caterr-0x630", "positive", "R2", ["E3"], ["E1", "E2", "E4"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] system warm reset",
                    board=["BoardInit start", "0x630=0x00000001", "0x634=0x00000000",
                           "0x664=0x00000000", "0x674=0x00000000", "EPLD register dump end"],
                    description="0x630 caterr 标志非 0，0x634 为 0，仍属于 CPU 中断异常。",
                    tags=["caterr", "0x630"]))
    c.append(_watch("pos-caterr-float", "positive", "R2", ["E3"], ["E1", "E2", "E4"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] system warm reset",
                    board=["BoardInit start", "0x630=0x00000000", "0x634=0x00000100",
                           "0x664=0x00000000", "0x674=0x00000000", "EPLD register dump end"],
                    description="0x634 非 0 的高位错误标志，需按数值判断而不是匹配固定字符串。",
                    tags=["caterr", "hex"]))
    c.append(_watch("pos-power-0x664-80", "positive", "R3", ["E4"], ["E1", "E2", "E3"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] system warm reset",
                    board=BOARD_POWER,
                    description="无看门狗、无 MCE、caterr 为 0，但 EPLD 电源寄存器 0x664=0x80。",
                    tags=["power", "register"]))
    c.append(_watch("pos-power-0x674", "positive", "R3", ["E4"], ["E1", "E2", "E3"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] system warm reset",
                    board=["BoardInit start", "0x630=0x00000000", "0x634=0x00000000",
                           "0x664=0x00000000", "0x674=0x00000002", "EPLD register dump end"],
                    description="电源异常落在 0x674，其他寄存器和内核日志均正常。",
                    tags=["power", "0x674"]))
    c.append(_watch("pos-power-large", "positive", "R3", ["E4"], ["E1", "E2", "E3"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] system warm reset",
                    board=["BoardInit start", "0x630=0x00000000", "0x634=0x00000000",
                           "0x664=0x00000001", "0x674=0x00000000", "EPLD register dump end"],
                    description="0x664 末位非 0，不能因数值看起来小而漏判电源异常。",
                    tags=["power", "small-nonzero"]))
    c.append(_watch("pos-combined-mce-int", "positive", "R1", ["E1", "E2", "E3"], ["E4"],
                    simplelog=SIMPLELOG_WD, kernel=KERNEL_MCE, board=BOARD_CATERR,
                    description="看门狗复位同时伴随 MCE 和 caterr，按证据优先级先由 MCE 解释。",
                    tags=["combined", "mce-priority"]))
    c.append(_watch("pos-combined-mce-power", "positive", "R1", ["E1", "E2", "E4"], ["E3"],
                    simplelog=SIMPLELOG_WD, kernel=KERNEL_PANIC,
                    board=["BoardInit start", "0x630=0x00000000", "0x634=0x00000000",
                           "0x664=0x00000080", "0x674=0x00000000", "EPLD register dump end"],
                    description="Kernel panic 与电源寄存器同时异常，内核直接证据优先于电源寄存器。",
                    tags=["combined", "panic-priority"]))
    c.append(_watch("pos-combined-int-power", "positive", "R2", ["E3", "E4"], ["E1", "E2"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] system warm reset",
                    board=BOARD_BOTH,
                    description="无 MCE 时 caterr 与电源标志并存，按资料优先级判 CPU 中断。",
                    tags=["combined", "interrupt-priority"]))
    c.append(_watch("pos-all-signals", "positive", "R1", ["E1", "E2", "E3", "E4"], [],
                    simplelog=SIMPLELOG_WD, kernel=KERNEL_MCE, board=BOARD_BOTH,
                    description="四类信号全部出现，最强直接证据仍是 MCE，其他作为佐证。",
                    tags=["combined", "all"]))
    c.append(_watch("pos-caterr-with-simplelog", "positive", "R2", ["E1", "E3"], ["E2", "E4"],
                    simplelog=SIMPLELOG_WD, board=BOARD_CATERR,
                    description="Simplelog 有看门狗复位，但内核无 MCE，真正区分根因的是 EPLD caterr。",
                    tags=["caterr", "simplelog-present"]))
    c.append(_watch("pos-power-with-simplelog", "positive", "R3", ["E1", "E4"], ["E2", "E3"],
                    simplelog=SIMPLELOG_WD, board=BOARD_POWER,
                    description="Simplelog 有看门狗复位，内核无 MCE、caterr 为 0，电源异常解释复位。",
                    tags=["power", "simplelog-present"]))
    c.append(_watch("pos-caterr-nested", "positive", "R2", ["E3"], ["E1", "E2", "E4"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] warm reset",
                    board=BOARD_CATERR,
                    description="深度目录下的 BoardInit 仍能命中 caterr，不能因路径变化丢证据。",
                    tags=["path", "caterr"], packaging="unpacked",
                    prefix="DCM_20261118/henan/site-07/BBU"))
    c.append(_watch("pos-power-zip", "positive", "R3", ["E4"], ["E1", "E2", "E3"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] warm reset", board=BOARD_POWER,
                    description="采集包为 zip，解压后电源寄存器异常。",
                    tags=["archive", "power"], packaging="zip"))
    c.append(_watch("pos-mce-tar", "positive", "R1", ["E1", "E2"], ["E3", "E4"],
                    simplelog=SIMPLELOG_WD, kernel=KERNEL_MCE,
                    description="采集包为 tar.gz，看门狗复位与 MCE 同时存在。",
                    tags=["archive", "mce"], packaging="tar.gz"))
    c.append(_watch("pos-power-nested-zip", "positive", "R3", ["E4"], ["E1", "E2", "E3"],
                    simplelog="2026-11-02 03:14:16.500 [BBU] warm reset", board=BOARD_POWER,
                    description="外层 zip 内再套一层 zip，验证多层采集包仍能取到电源寄存器。",
                    tags=["archive", "nested", "power"], packaging="nested-zip"))
    return c


def _usb_kernel_normal() -> str:
    return "\n".join([
        KERNEL_NORMAL,
        "[   12.001] usb 1-1: new high-speed USB device number 3 using dwc2",
        "[   12.023] usb 1-1: New USB device found, idVendor=0951, idProduct=1666",
        "[   12.024] usb 1-1: Product: USB FLASH Drive",
    ])


def _usb_smart_normal() -> str:
    return "\n".join([
        "Device Model: USB FLASH Drive",
        "SMART support is: Available - device has SMART capability.",
        "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE",
        "  1 Raw_Read_Error_Rate     0x000b   100   100   050    Pre-fail  Always       -       0",
        "  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0",
        "169 Remain Life Percentage 0x0032   100   100   000    Old_age   Always       -       100",
    ])


def usb_cases() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []
    c.append(_usb("neg-clean-01", "negative", "NONE", [], ["E1", "E2", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  description="USB FLASH 正常识别、读写正常，protect_status=0，SMART 正常。",
                  tags=["clean"]))
    c.append(_usb("neg-other-port-disconnect", "negative", "NONE", [], ["E1", "E2", "E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  122.450] usb 1-2: USB disconnect, device number 7"]),
                  smart=_usb_smart_normal(), board="protect_status: 0\n",
                  description="断开的是 usb 1-2，不是本设备 usb 1-1，不能归因到本 USB FLASH。",
                  tags=["discriminator", "other-port"]))
    c.append(_usb("neg-smart-normal-zero", "negative", "NONE", [], ["E1", "E2", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  description="SMART 各项 raw_value 为 0，不能把正常字段当介质故障。",
                  tags=["boundary", "smart-zero"]))
    c.append(_usb("neg-protect-zero", "boundary", "NONE", [], ["E1", "E2", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  board="protect_status: 0\ndevice_state: ready\n",
                  description="protect_status 等于 0 是正常状态，是本资料最容易误报的边界。",
                  tags=["boundary", "protect-zero"]))
    c.append(_usb("neg-protect-00", "boundary", "NONE", [], ["E1", "E2", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  board="protect_status: 00\n",
                  description="protect_status 写成前导 0 的 00，数值仍是 0，不能误判写保护。",
                  tags=["boundary", "protect-leading-zero"]))
    c.append(_usb("neg-io-other-dev", "negative", "NONE", [], ["E1", "E2", "E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  120.100] Buffer I/O error on dev sdc, logical block 3, async page read",
                                    "[  120.200] usb 1-1: reset high-speed USB device"]),
                  smart=_usb_smart_normal(), board="protect_status: 0\n",
                  description="I/O 错误发生在 sdc，USB 只是被 reset；不能把别的块设备错误算到本设备。",
                  tags=["discriminator", "other-dev"]))
    c.append(_usb("pos-protect-1", "positive", "R1", ["E2"], ["E1", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  board="protect_status: 1\n",
                  description="protect_status=1，USB FLASH 处于写保护。",
                  tags=["protect", "primary"]))
    c.append(_usb("pos-protect-2", "positive", "R1", ["E2"], ["E1", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  board="protect_status: 2\n",
                  description="写保护状态码为 2，不属于正常值，判写保护。",
                  tags=["protect", "code2"]))
    c.append(_usb("pos-protect-20102", "positive", "R1", ["E2"], ["E1", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  board="protect_status: 20102\n",
                  description="protect_status 为非 0 的复合错误码，按资料判写保护。",
                  tags=["protect", "composite-code"]))
    c.append(_usb("pos-protect-65535", "positive", "R1", ["E2"], ["E1", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  board="protect_status: 65535\n",
                  description="写保护寄存器全 1，需先解除写保护再判断介质。",
                  tags=["protect", "all-ones"]))
    c.append(_usb("pos-io-buffer", "positive", "R2", ["E1"], ["E2", "E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  118.220] Buffer I/O error on dev sdb, logical block 10, async page read"]),
                  smart=_usb_smart_normal(), board="protect_status: 0\n",
                  description="protect_status=0，但本设备 sdb 出现 Buffer I/O error，判器件或 I/O 故障。",
                  tags=["io-error", "sdb"]))
    c.append(_usb("pos-io-critical", "positive", "R2", ["E1"], ["E2", "E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  119.010] blk_update_request: critical medium error, dev sdb, sector 2048"]),
                  smart=_usb_smart_normal(), board="protect_status: 0\n",
                  description="sdb 出现 critical medium error，说明不是写保护而是介质读写失败。",
                  tags=["io-error", "medium"]))
    c.append(_usb("pos-io-sdb4", "positive", "R2", ["E1"], ["E2", "E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  119.500] Buffer I/O error on dev sdb4, logical block 2, lost async page write"]),
                  smart=_usb_smart_normal(), board="protect_status: 0\n",
                  description="I/O 错误落在 sdb4 分区，仍属于本 USB 设备的介质 I/O。",
                  tags=["io-error", "partition"]))
    c.append(_usb("pos-disconnect", "positive", "R2", ["E1"], ["E2", "E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  121.000] usb 1-1: USB disconnect, device number 3"]),
                  smart=_usb_smart_normal(), board="protect_status: 0\n",
                  description="本设备 usb 1-1 发生断开，且写保护为 0，按器件或 I/O 故障排查。",
                  tags=["disconnect", "usb-1-1"]))
    c.append(_usb("pos-protect-and-io", "positive", "R1", ["E1", "E2"], ["E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  118.220] Buffer I/O error on dev sdb, logical block 10, async page read"]),
                  smart=_usb_smart_normal(), board="protect_status: 1\n",
                  description="写保护与 I/O 错误同时存在，按优先级先判写保护，同时记录 I/O 佐证。",
                  tags=["combined", "protect-priority"]))
    c.append(_usb("pos-smart-unsupported", "positive", "R3", ["E3"], ["E1", "E2"],
                  kernel=_usb_kernel_normal(),
                  smart="this flash not support\nDevice Model: USB FLASH Drive\n",
                  board="protect_status: 0\n",
                  description="设备可识别但 SMART 不支持，写保护和 I/O 均无证据，判 SMART/识别异常。",
                  tags=["smart", "unsupported"]))
    c.append(_usb("pos-smart-vendor-zero", "positive", "R3", ["E3"], ["E1", "E2"],
                  kernel=_usb_kernel_normal(),
                  smart="\n".join(["Device Model: USB FLASH Drive",
                                   "idVendor = 0",
                                   "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE",
                                   "  1 Raw_Read_Error_Rate     0x000b   000   000   050    Pre-fail  Always       -       0"]),
                  board="protect_status: 0\n",
                  description="SMART 属性全为 0 且无可读阈值，识别信息异常但无直接 I/O 错误。",
                  tags=["smart", "vendor-zero"]))
    c.append(_usb("pos-smart-manufacturer-null", "positive", "R3", ["E3"], ["E1", "E2"],
                  kernel=_usb_kernel_normal(),
                  smart="Manufacturer:(null)\nDevice Model: \nSMART support is: Unavailable\n",
                  board="protect_status: 0\n",
                  description="SMART 厂商与型号字段为空，无法读取设备身份，按识别异常处理。",
                  tags=["smart", "empty-identity"]))
    c.append(_usb("pos-smart-and-io", "positive", "R2", ["E1", "E3"], ["E2"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  118.220] Buffer I/O error on dev sdb, logical block 10, async page read"]),
                  smart="this flash not support\nDevice Model: USB FLASH Drive\n",
                  board="protect_status: 0\n",
                  description="SMART 不可用同时存在 sdb I/O 错误，按更高优先级的器件 I/O 判定，SMART 作为佐证。",
                  tags=["combined", "io-priority"]))
    c.append(_usb("pos-smart-and-protect", "positive", "R1", ["E2", "E3"], ["E1"],
                  kernel=_usb_kernel_normal(),
                  smart="this flash not support\nDevice Model: USB FLASH Drive\n",
                  board="protect_status: 1\n",
                  description="写保护与 SMART 不可用并存，先处理写保护，SMART 作为次要异常。",
                  tags=["combined", "protect-priority"]))
    c.append(_usb("pos-all-signals", "positive", "R1", ["E1", "E2", "E3"], [],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  121.000] usb 1-1: USB disconnect, device number 3"]),
                  smart="this flash not support\nDevice Model: USB FLASH Drive\n",
                  board="protect_status: 3\n",
                  description="写保护、断开和 SMART 不可用同时出现，仍按写保护优先。",
                  tags=["combined", "all"]))
    c.append(_usb("pos-io-nested", "positive", "R2", ["E1"], ["E2", "E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  118.220] Buffer I/O error on dev sdb, logical block 10, async page read"]),
                  smart=_usb_smart_normal(), board="protect_status: 0\n",
                  description="深度站点目录下的 sdb I/O 错误仍要命中。",
                  tags=["path", "io"], packaging="unpacked",
                  prefix="DCM_20261029/sichuan/BBU-03"))
    c.append(_usb("pos-protect-zip", "positive", "R1", ["E2"], ["E1", "E3"],
                  kernel=_usb_kernel_normal(), smart=_usb_smart_normal(),
                  board="protect_status: 7\n",
                  description="采集包为 zip，解压后 protect_status 非 0。",
                  tags=["archive", "protect"], packaging="zip"))
    c.append(_usb("pos-io-nested-zip", "positive", "R2", ["E1"], ["E2", "E3"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  118.220] Buffer I/O error on dev sdb, logical block 10, async page read"]),
                  smart=_usb_smart_normal(), board="protect_status: 0\n",
                  description="双层 zip 下的 sdb I/O 错误，验证解包链路和规则同时生效。",
                  tags=["archive", "nested", "io"], packaging="nested-zip"))
    return c


def _ssd_kernel_normal() -> str:
    return "\n".join([
        KERNEL_NORMAL,
        "[    1.900] ata17: SATA link up 3.0 Gbps (SStatus 123 SControl 300)",
        "[    1.901] ata17.00: ATA-11: SAMSUNG MZ7LH480HAHQ-00005, 4L2Q, max UDMA/133",
        "[    1.902] sd 9:0:0:0: [sda] 937703088 512-byte logical blocks",
    ])


def _ssd_smart_normal() -> str:
    return "\n".join([
        "Device Model: SAMSUNG MZ7LH480HAHQ-00005",
        "SMART support is: Available - device has SMART capability.",
        "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE",
        "169 100 100 Remain Life Percentage",
        "160 100 0 Uncorrectable Sector Count",
        "5 100 0 Reallocated Sectors Count",
    ])


def _smart_life(value: int) -> str:
    return "\n".join([
        "Device Model: SAMSUNG MZ7LH480HAHQ-00005",
        "SMART support is: Available - device has SMART capability.",
        "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE",
        f"169 100 {value} Remain Life Percentage",
        "160 100 0 Uncorrectable Sector Count",
        "5 100 0 Reallocated Sectors Count",
    ])


def ssd_cases() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []
    c.append(_ssd("neg-clean-01", "negative", "NONE", [], ["E1", "E2", "E3", "E4", "E5"],
                  kernel=_ssd_kernel_normal(), smart=_ssd_smart_normal(),
                  description="SSD 链路正常、无 I/O 错误，寿命和扇区计数均为正常值。",
                  tags=["clean"]))
    c.append(_ssd("neg-life-30", "boundary", "NONE", [], ["E1", "E2", "E3", "E4", "E5"],
                  kernel=_ssd_kernel_normal(), smart=_smart_life(30),
                  description="Remain Life Percentage 恰好等于 30，按资料不算低于 30，不能判寿命耗尽。",
                  tags=["boundary", "life-30"]))
    c.append(_ssd("neg-sector-zero", "boundary", "NONE", [], ["E1", "E2", "E3", "E4", "E5"],
                  kernel=_ssd_kernel_normal(), smart=_ssd_smart_normal(),
                  description="Uncorrectable 与 Reallocated 均为 0，0 是正常值，不能误报扇区介质故障。",
                  tags=["boundary", "sector-zero"]))
    c.append(_ssd("neg-sata-link-down-only", "boundary", "NONE", [], ["E1", "E2", "E3", "E4", "E5"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[    2.100] ata20: SATA link down (SStatus 4 SControl 300)"]),
                  smart=_ssd_smart_normal(),
                  description="只有一个未使用端口的 SATA link down，无 COMRESET/ATA bus error，不能判链路故障。",
                  tags=["boundary", "link-down-only"]))
    c.append(_ssd("neg-no-smart", "negative", "NONE", [], ["E1", "E2", "E3", "E4", "E5"],
                  kernel=_ssd_kernel_normal(),
                  smart="SMART support is: Unavailable\nDevice Model: SAMSUNG MZ7LH480HAHQ-00005\n",
                  description="SMART 不支持但没有 I/O、链路或阈值证据，不能仅凭“读不到 SMART”判 SSD 故障。",
                  tags=["discriminator", "no-smart"]))
    c.append(_ssd("pos-io-blk-update", "positive", "R1", ["E1"], ["E2", "E3", "E4", "E5"],
                  kernel="\n".join([_ssd_kernel_normal(),
                                    "[  300.100] blk_update_request: I/O error, dev sda, sector 123456 op 0x0:(READ)"]),
                  smart=_ssd_smart_normal(),
                  description="内核对 sda 直接报 I/O error，链路和 SMART 都正常，判 SSD I/O 故障。",
                  tags=["io", "blk-update"]))
    c.append(_ssd("pos-io-buffer", "positive", "R1", ["E1"], ["E2", "E3", "E4", "E5"],
                  kernel="\n".join([_ssd_kernel_normal(),
                                    "[  300.200] Buffer I/O error on dev sda, logical block 88, async page read"]),
                  smart=_ssd_smart_normal(),
                  description="sda 出现 Buffer I/O error，是直接的介质/控制器 I/O 证据。",
                  tags=["io", "buffer"]))
    c.append(_ssd("pos-io-read-log", "positive", "R1", ["E1"], ["E2", "E3", "E4", "E5"],
                  kernel="\n".join([_ssd_kernel_normal(),
                                    "[  301.010] ata17.00: READ LOG DMA EXT failed, sense key 0x3"]),
                  smart=_ssd_smart_normal(),
                  description="内核 READ LOG DMA EXT failed，说明不是链路协商问题而是实际读失败。",
                  tags=["io", "fs-read-error"]))
    c.append(_ssd("pos-io-failed-read", "positive", "R1", ["E1"], ["E2", "E3", "E4", "E5"],
                  kernel="\n".join([_ssd_kernel_normal(),
                                    "[  301.020] sd 9:0:0:0: [sda] failed command: READ FPDMA QUEUED"]),
                  smart=_ssd_smart_normal(),
                  description="SCSI 层 READ FPDMA QUEUED 命令失败，属于直接 I/O 故障。",
                  tags=["io", "scsi-failed"]))
    c.append(_ssd("pos-io-failed-write", "positive", "R1", ["E1"], ["E2", "E3", "E4", "E5"],
                  kernel="\n".join([_ssd_kernel_normal(),
                                    "[  301.030] sd 9:0:0:0: [sda] failed command: WRITE FPDMA QUEUED"]),
                  smart=_ssd_smart_normal(),
                  description="写命令失败，仍属于 SSD I/O 故障，不因读写方向不同而改变根因。",
                  tags=["io", "write"]))
    c.append(_ssd("pos-sata-comreset", "positive", "R2", ["E2"], ["E1", "E3", "E4", "E5"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  210.400] ata17: COMRESET failed (errno=-16)",
                                    "[  210.401] ata17: hard resetting link"]),
                  smart=_ssd_smart_normal(),
                  description="SATA 链路反复 COMRESET 失败，无上层 I/O 错误，判链路异常。",
                  tags=["sata", "comreset"]))
    c.append(_ssd("pos-sata-ata-bus-error", "positive", "R2", ["E2"], ["E1", "E3", "E4", "E5"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  211.000] ata17.00: exception Emask 0x0 SAct 0x0 SErr 0x0 action 0x6",
                                    "[  211.001] ata17.00: irq_stat 0x40000001",
                                    "[  211.002] ata17: SError: { RecovComm PHYRdyChg }"]),
                  smart=_ssd_smart_normal(),
                  description="ATA bus error 与 SError 明确指向链路层，避免误判为介质坏块。",
                  tags=["sata", "bus-error"]))
    c.append(_ssd("pos-sata-softreset", "positive", "R2", ["E2"], ["E1", "E3", "E4", "E5"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  212.000] ata17: softreset failed (device not ready)",
                                    "[  212.001] ata17: limiting SATA link speed to 1.5 Gbps"]),
                  smart=_ssd_smart_normal(),
                  description="softreset 失败并降速，说明链路训练异常，I/O 未报错时仍先判链路。",
                  tags=["sata", "softreset"]))
    c.append(_ssd("pos-life-29", "positive", "R3", ["E3"], ["E1", "E2", "E4", "E5"],
                  kernel=_ssd_kernel_normal(), smart=_smart_life(29),
                  description="Remain Life Percentage=29，低于 30，且无 I/O/链路异常，判寿命耗尽。",
                  tags=["life", "29"]))
    c.append(_ssd("pos-life-1", "positive", "R3", ["E3"], ["E1", "E2", "E4", "E5"],
                  kernel=_ssd_kernel_normal(), smart=_smart_life(1),
                  description="寿命只剩 1%，即使尚未报 I/O 错误也应作为根因。",
                  tags=["life", "1"]))
    c.append(_ssd("pos-life-0", "positive", "R3", ["E3"], ["E1", "E2", "E4", "E5"],
                  kernel=_ssd_kernel_normal(), smart=_smart_life(0),
                  description="寿命为 0，写入能力接近耗尽，优先安排更换。",
                  tags=["life", "0"]))
    c.append(_ssd("pos-sector-uncorrectable", "positive", "R4", ["E4"], ["E1", "E2", "E3", "E5"],
                  kernel=_ssd_kernel_normal(),
                  smart="\n".join([
                      "Device Model: SAMSUNG MZ7LH480HAHQ-00005",
                      "169 100 100 Remain Life Percentage",
                      "160 100 8 Uncorrectable Sector Count",
                      "5 100 0 Reallocated Sectors Count",
                  ]),
                  description="不可纠正扇区计数为 8，寿命正常，判扇区介质异常。",
                  tags=["sector", "uncorrectable"]))
    c.append(_ssd("pos-sector-reallocated", "positive", "R4", ["E5"], ["E1", "E2", "E3", "E4"],
                  kernel=_ssd_kernel_normal(),
                  smart="\n".join([
                      "Device Model: SAMSUNG MZ7LH480HAHQ-00005",
                      "169 100 100 Remain Life Percentage",
                      "160 100 0 Uncorrectable Sector Count",
                      "5 100 16 Reallocated Sectors Count",
                  ]),
                  description="重分配扇区计数为 16，尚未影响寿命，判扇区介质异常。",
                  tags=["sector", "reallocated"]))
    c.append(_ssd("pos-sector-both", "positive", "R4", ["E4", "E5"], ["E1", "E2", "E3"],
                  kernel=_ssd_kernel_normal(),
                  smart="\n".join([
                      "Device Model: SAMSUNG MZ7LH480HAHQ-00005",
                      "169 100 100 Remain Life Percentage",
                      "160 100 4 Uncorrectable Sector Count",
                      "5 100 24 Reallocated Sectors Count",
                  ]),
                  description="不可纠正与重分配计数同时非 0，两个扇区规则共同支持介质异常。",
                  tags=["sector", "both"]))
    c.append(_ssd("pos-combined-io-life", "positive", "R1", ["E1", "E3"], ["E2", "E4", "E5"],
                  kernel="\n".join([_ssd_kernel_normal(),
                                    "[  300.100] blk_update_request: I/O error, dev sda, sector 123456 op 0x0:(READ)"]),
                  smart=_smart_life(12),
                  description="寿命低且已出现 I/O 错误，按优先级由直接 I/O 故障解释，寿命作为背景。",
                  tags=["combined", "io-priority"]))
    c.append(_ssd("pos-combined-sata-life", "positive", "R2", ["E2", "E3"], ["E1", "E4", "E5"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  210.400] ata17: COMRESET failed (errno=-16)",
                                    "[  210.401] ata17: hard resetting link"]),
                  smart=_smart_life(20),
                  description="链路复位失败伴随寿命下降，按资料先判 SATA 链路而非寿命耗尽。",
                  tags=["combined", "sata-priority"]))
    c.append(_ssd("pos-combined-life-sector", "positive", "R3", ["E3", "E4", "E5"], ["E1", "E2"],
                  kernel=_ssd_kernel_normal(),
                  smart="\n".join([
                      "Device Model: SAMSUNG MZ7LH480HAHQ-00005",
                      "169 100 18 Remain Life Percentage",
                      "160 100 3 Uncorrectable Sector Count",
                      "5 100 12 Reallocated Sectors Count",
                  ]),
                  description="寿命 18、扇区计数也异常，按优先级判寿命耗尽，扇区作为佐证。",
                  tags=["combined", "life-priority"]))
    c.append(_ssd("pos-combined-io-sector", "positive", "R1", ["E1", "E4"], ["E2", "E3", "E5"],
                  kernel="\n".join([_ssd_kernel_normal(),
                                    "[  300.200] Buffer I/O error on dev sda, logical block 88, async page read"]),
                  smart="\n".join([
                      "Device Model: SAMSUNG MZ7LH480HAHQ-00005",
                      "169 100 100 Remain Life Percentage",
                      "160 100 2 Uncorrectable Sector Count",
                      "5 100 0 Reallocated Sectors Count",
                  ]),
                  description="I/O 错误与不可纠正扇区并存，按直接 I/O 故障优先。",
                  tags=["combined", "io-priority"]))
    c.append(_ssd("pos-life-nested", "positive", "R3", ["E3"], ["E1", "E2", "E4", "E5"],
                  kernel=_ssd_kernel_normal(), smart=_smart_life(25),
                  description="深度目录下的 SMART 信息仍要正确解析第三列 raw_value。",
                  tags=["path", "life"], packaging="unpacked",
                  prefix="DCM_20261107/guangdong/BBU-11"))
    c.append(_ssd("pos-sata-zip", "positive", "R2", ["E2"], ["E1", "E3", "E4", "E5"],
                  kernel="\n".join([KERNEL_NORMAL,
                                    "[  210.400] ata17: COMRESET failed (errno=-16)",
                                    "[  210.401] ata17: hard resetting link"]),
                  smart=_ssd_smart_normal(),
                  description="zip 采集包中的 SATA COMRESET 仍能命中。",
                  tags=["archive", "sata"], packaging="zip"))
    c.append(_ssd("pos-life-nested-zip", "positive", "R3", ["E3"], ["E1", "E2", "E4", "E5"],
                  kernel=_ssd_kernel_normal(), smart=_smart_life(5),
                  description="双层 zip 中读取 SMART 寿命 5%，验证嵌套解包。",
                  tags=["archive", "nested", "life"], packaging="nested-zip"))
    return c


def render_case(case: dict[str, Any], out_root: Path) -> None:
    case_dir = out_root / case["id"]
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    input_dir = case_dir / "input"
    input_dir.mkdir()
    if case["packaging"] == "unpacked":
        for rel, text in case["files"].items():
            target = input_dir / case["prefix"] / rel if case["prefix"] else input_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        input_spec = {"unpacked_dir": "input"}
    else:
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td) / "payload"
            for rel, text in case["files"].items():
                target = stage / case["prefix"] / rel if case["prefix"] else stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            if case["packaging"] == "zip":
                archive = input_dir / "package.zip"
                _zip_dir(stage, archive)
                input_spec = {"archive": "input/package.zip"}
            elif case["packaging"] == "tar.gz":
                archive = input_dir / "package.tar.gz"
                with tarfile.open(archive, "w:gz") as tf:
                    for item in sorted(stage.rglob("*")):
                        tf.add(item, arcname=item.relative_to(stage))
                input_spec = {"archive": "input/package.tar.gz"}
            else:
                inner = Path(td) / "inner.zip"
                _zip_dir(stage, inner)
                outer = input_dir / "package.zip"
                with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(inner, "inner.zip")
                input_spec = {"archive": "input/package.zip"}
    yaml_doc = {
        "id": case["id"],
        "skill": out_root.name,
        "kind": case["kind"],
        "description": case["description"],
        "tags": case["tags"],
        "input": input_spec,
        "pipeline": ["unpack", "search"],
        "expected": {
            "root_cause": case["root"],
            "hit_rules": case["hit"],
            "not_hit_rules": case["not_hit"],
        },
    }
    (case_dir / "case.yaml").write_text(
        yaml.safe_dump(yaml_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _zip_dir(stage: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(stage.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(stage))


def _load_specs() -> dict[str, dict[str, Any]]:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from bbu.workshop_spec import compile_profile, parse_source  # noqa: WPS433

    specs = {}
    for skill, meta in SKILLS.items():
        spec = parse_source(meta["source"].read_text(encoding="utf-8"))
        profile = compile_profile(spec, skill_name=skill)
        specs[skill] = profile
    return specs


def build_dataset(cases_root: Path, dataset_root: Path) -> dict[str, Any]:
    specs = _load_specs()
    all_cases = {
        "bbu-fault-vsw-watchdog": watchdog_cases(),
        "bbu-fault-vsw-ssd": ssd_cases(),
        "bbu-fault-vsw-usbflash": usb_cases(),
    }
    dataset_root.mkdir(parents=True, exist_ok=True)
    playbooks = dataset_root / "playbooks"
    playbooks.mkdir(parents=True, exist_ok=True)
    corpus_all: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    for skill, cases in all_cases.items():
        skill_root = cases_root / skill
        skill_root.mkdir(parents=True, exist_ok=True)
        profile = specs[skill]
        reasoning = profile.get("diagnostic_reasoning") or {}
        roots = {str(r["id"]): r for r in profile.get("root_causes", [])}
        ids = {case["id"] for case in cases}
        assert len(ids) == len(cases), f"{skill} 存在重复 case id"
        corpus: list[dict[str, Any]] = []
        root_counts: dict[str, int] = {}
        for case in cases:
            render_case(case, skill_root)
            root_counts[case["root"]] = root_counts.get(case["root"], 0) + 1
            root = roots.get(case["root"], {})
            record = {
                "id": case["id"],
                "skill": skill,
                "fault": profile["fault"]["name"],
                "case_kind": case["kind"],
                "root_cause_id": case["root"],
                "root_cause_name": root.get("name") if root else "",
                "paragraph": f"现场描述：{case['description']} 已获得日志：{'、'.join(case['files'])}。",
                "query": case["description"],
                "strategy": reasoning.get("strategy", ""),
                "checks": reasoning.get("checks", []),
                "observed_rules": case["hit"],
                "missing_evidence": case["not_hit"],
                "hypotheses": reasoning.get("hypotheses", []),
                "decision_order": reasoning.get("decision_order", []),
                "discriminators": reasoning.get("discriminators", []),
                "pitfalls": reasoning.get("pitfalls", []),
                "stop_conditions": reasoning.get("stop_conditions", []),
                "actions": root.get("actions", []),
                "difficulty": "negative" if case["root"] == "NONE" else (
                    "combined" if len(case["hit"]) >= 2 else "single"),
                "tags": case["tags"],
            }
            corpus.append(record)
            corpus_all.append(record)
        (dataset_root / f"{skill}.corpus.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in corpus) + "\n", encoding="utf-8")
        (dataset_root / f"{skill}.benchmark.jsonl").write_text(
            "\n".join(json.dumps({"id": x["id"], "skill": skill, "query": x["query"],
                                  "root_cause": x["root_cause_id"],
                                  "expected_rules": x["observed_rules"]},
                                 ensure_ascii=False) for x in corpus) + "\n", encoding="utf-8")
        (playbooks / f"{skill}.yaml").write_text(
            yaml.safe_dump(reasoning, allow_unicode=True, sort_keys=False), encoding="utf-8")
        stats[skill] = {"cases": len(cases), "root_counts": root_counts,
                        "kinds": {k: sum(1 for x in cases if x["kind"] == k)
                                  for k in ("positive", "negative", "boundary")}}
    (dataset_root / "corpus.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in corpus_all) + "\n", encoding="utf-8")
    (dataset_root / "summary.yaml").write_text(
        yaml.safe_dump({"generated_for": "BBU 故障诊断平台", "synthetic": True,
                        "total_cases": len(corpus_all), "skills": stats},
                       allow_unicode=True, sort_keys=False), encoding="utf-8")
    (dataset_root / "README.md").write_text(
        "# 诊断推理数据集\n\n"
        "全部为合成日志，用于技能回归、planner 检索评估和报告推理链展示，不代表真实现网验证。\n\n"
        "- `*.corpus.jsonl`：按案例生成的推理语料，含现场描述、竞争假设、证据顺序、鉴别点、误判陷阱和处置动作。\n"
        "- `*.benchmark.jsonl`：离线基准题，字段 `query` 用于拼 planner 输入，`root_cause` 与 `expected_rules` 为金标准。\n"
        "- `playbooks/*.yaml`：从技能 profile 抽出的专家排查策略，可直接作为技能目录的 reasoning 摘要来源。\n"
        "- `summary.yaml`：案例数量、根因分布和类型分布。\n\n"
        "这些语料不是模型自由生成的思维链；每条推理都绑定到真实规则编号和案例断言，报告只能陈述实际命中与未命中。\n",
        encoding="utf-8")
    return {"total_cases": len(corpus_all), "stats": stats}


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 BBU 诊断推理案例与语料")
    ap.add_argument("--cases-dir", default=str(PROJECT_ROOT / "cases"))
    ap.add_argument("--dataset-dir", default=str(PROJECT_ROOT / "datasets/diagnostic_reasoning"))
    args = ap.parse_args()
    summary = build_dataset(Path(args.cases_dir), Path(args.dataset_dir))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
