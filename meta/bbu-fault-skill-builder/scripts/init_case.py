#!/usr/bin/env python3
"""
init_case.py — 在【skill 之外】的案例库中初始化一个标准 case 目录。

产物结构：
    <cases-dir>/<skill-slug>/<case-id>/
      ├── case.yaml     # 元数据 + 结构化预期（断言唯一来源）
      ├── input/        # 放案例归档（zip/tar/gz/bz2）或解压后的目录树
      └── expected/     # 可选：期望报告片段等附件

案例数据绝不打进 skill 包；案例库由 --cases-dir 单独管理与传递。

用法：
    python3 init_case.py --cases-dir <cases-root> --skill <skill-slug> \
        --id <case-id> --kind positive
"""
import argparse
import re
import sys
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

CASE_TEMPLATE = """# 案例定义（run_regression.py 的唯一断言来源）
# 稳定性铁律：不得依赖绝对路径、行号；证据只断言关键字/文件后缀/次数。

id: "{case_id}"
skill: "{skill}"
description: "一句话描述案例（现象 -> 已知根因）"
kind: {kind}            # positive 正例 | negative 反例 | boundary 边界 | corruption 损坏 | multisite 多站点
device: ""              # 如 ITBBU / SDR；采集形态可注明
tags: []
# 输入：归档文件（相对本目录）或解压后的目录（unpacked_dir）
input:
  archive: "input/<把案例包放到这里>"
# 执行流水线（可按需增删 parse_clock）
pipeline: [unpack, search{pipeline_clock}]
# 结构化预期（不需要的断言整段删除；run_regression 只校验这里列出的项）
expected:
  root_cause: "R1"            # 期望根因 id；无结论反例用 "NONE"
  hit_rules: [E1]
  not_hit_rules: []
  temporal_satisfied: []
  temporal_violated: []
  match_counts:               # 命中次数，支持 >= / == / <= 数字
    E1: ">=1"
  evidence_contains:          # 至少一条证据同时满足（不比对整行/行号/绝对路径）
    - rule: E1
      all_of: ["关键字1", "关键字2"]
      file_suffix: ""        # 如 clockstatuslog.txt（路径后缀，跨层级稳定）
      file_basename: ""
  manifest:                   # 解压清单断言（可整段删除）
    sites: 1                 # 识别到的站点数
    warnings: 0
    unsupported: 0
    corrupt: 0               # 损坏成员数
{clock_block}
# 说明：
# - 正例：root_cause 与对应 hit_rules 命中；
# - 反例（negative）：root_cause 应为 NONE 或其它故障 id，且 not_hit_rules 列出本故障独有规则；
# - 损坏案例（corruption）：manifest.corrupt >=1 且其余好成员仍能提取；
# - 多站点（multisite）：manifest.sites >=2，且时序不得跨站点（由 search 的 associate_by 保证）。
"""

CLOCK_BLOCK = """  parse_clock:                 # 时钟类案例（含 parse_clock 流水线时）
    alarms: [{alarm_type: "AntOpen", action: "-a", ref: 1}]
    reset_reasons: [{reason_id: 13}]
    health_bits: ["PLL参考源频偏"]
    anomaly_contains: []
    spoofing_suspicions: 0     # 支持 >= / == 表达式
    timestamp_fixed: 0
    total_records: ">=1"
"""


def main():
    ap = argparse.ArgumentParser(description="初始化标准 case 目录（位于 skill 外）")
    ap.add_argument("--cases-dir", required=True, help="案例库根目录")
    ap.add_argument("--skill", required=True, help="子 skill slug（如 bbu-fault-gnss-clock）")
    ap.add_argument("--id", required=True, dest="case_id", help="案例 id（如 case-01-ant-open）")
    ap.add_argument("--kind", default="positive",
                    choices=["positive", "negative", "boundary", "corruption", "multisite"])
    ap.add_argument("--clock", action="store_true", help="生成时钟解析（parse_clock）断言模板")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    for label, val in (("--skill", args.skill), ("--id", args.case_id)):
        if not SLUG_RE.fullmatch(val):
            raise SystemExit(
                f"{label} 必须是小写 slug（[a-z0-9][a-z0-9_-]*），当前: {val!r}")

    case_dir = Path(args.cases_dir) / args.skill / args.case_id
    if case_dir.exists() and not args.force:
        raise SystemExit(f"案例目录已存在（--force 覆盖）: {case_dir}")
    (case_dir / "input").mkdir(parents=True, exist_ok=True)
    (case_dir / "expected").mkdir(parents=True, exist_ok=True)

    content = CASE_TEMPLATE.format(
        case_id=args.case_id, skill=args.skill, kind=args.kind,
        pipeline_clock=", parse_clock" if args.clock else "",
        clock_block=CLOCK_BLOCK if args.clock else "")
    (case_dir / "case.yaml").write_text(content, encoding="utf-8")
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    print(f"[OK] 已初始化案例: {case_dir}")
    print("   1) 把案例包放入 input/；2) 编辑 case.yaml 的 expected；3) 运行：")
    print(f"   python3 run_regression.py --case {case_dir} --skills-dir <skills根>")


if __name__ == "__main__":
    main()
