#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill_sync.py — 元 skill「基础能力」与子 skill 之间的双向同步（v2.5.6 新增）

提供两个入口，对应元 skill 的两项基本能力：

1) --sync <child_dir>  子 skill 同步更新（元 skill → 子 skill）
   元 skill（bbu-fault-skill-builder）更新后，把「基础能力」的变更同步到子 skill：
   - 共享脚本（unpack/search/parse_clock/parse_cloudmat/log_format/
     scan_rule_candidates/gen_specialized/fm_alarm_align/render_report/run_analysis）
   - 共享字典（alarm-code/clk-field-dictionary/common-rules.yaml）
   - 公共规则（common-rules.yaml）
   保留子 skill 定制：report_template.*（USER_CUSTOMIZABLE）与 components.extra_scripts
   专项脚本、profile 派生件（fault-profile/evidence-rules 属子 skill 特有，不同步覆盖）。
   无实际变更时幂等（不递增 generation）。
   典型触发：用户说「更新 <xx> skill」→ 定位该子 skill 目录 → 运行 --sync。

2) --reclaim <child_dir> [--apply]  回归回收（子 skill → 元 skill）
   子 skill 在真实数据运行中暴露出元 skill「基础能力」的缺陷，把已确认的修复回灌元 skill：
   - 扫描子 skill 与元 skill 模板/shared 中基础能力文件的差异；
   - 只回收「基础能力」文件（共享脚本 / 共享字典 / 公共规则 / 报告模板）；
   - 明确不回收子 skill 特有内容：profile 派生件（fault-profile/evidence-rules）、
     components.extra_scripts 专项脚本 —— 这些属子 skill 特有，不带入元 skill；
   - 默认 --dry-run 只列出差异，加 --apply 才写回元 skill 模板/shared 与统一同步的脚本。
   回收后必须跑元 skill selftest 回归（scripts/tests/selftest.py）确认不破坏其他子 skill。

用法：
  python3 skill_sync.py --sync <child_dir>
  python3 skill_sync.py --reclaim <child_dir>            # 只列出可回收的基础能力差异
  python3 skill_sync.py --reclaim <child_dir> --diff     # 列出差异并输出行级 +/- diff
  python3 skill_sync.py --reclaim <child_dir> --apply     # 确认后写回元 skill

依赖：纯 stdlib（读 .yaml 差异比对用文本 diff，不需 PyYAML）。
"""
import argparse
import difflib
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _setup_utf8():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


# 基础能力：可双向同步的共享文件（builder 模板/shared ↔ 子 skill）
# - 脚本：builder scripts/ 与模板 scripts/ 同源分发
BASE_SCRIPTS = [
    "unpack_logs.py", "search_evidence.py", "parse_clock.py", "parse_cloudmat.py",
    "log_format.py", "scan_rule_candidates.py",
    "gen_specialized.py", "fm_alarm_align.py", "render_report.py", "run_analysis.py",
]
# - 共享资产（shared/ 分发到 assets/）
BASE_ASSETS = ["alarm-code-dictionary.yaml", "clk-field-dictionary.yaml",
               "common-rules.yaml"]
# - 公共规则（shared/common-rules.yaml，仅存在性校验）
BASE_COMMON_RULES = "common-rules.yaml"
# - 报告模板（USER_CUSTOMIZABLE，回收时需 --include-template 才纳入，避免覆盖子 skill 定制）
REPORT_TEMPLATES = ["report_template.md", "report_template.html"]
# 子 skill 特有，永不回收进元 skill
CHILD_ONLY_RELS = {"assets/fault-profile.yaml",
                   "assets/evidence-rules.yaml",
                   "references/fault-profile.md", "scripts/*_analyzer.py"}


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _find_builder_root(start=None):
    """定位 builder 根目录：本脚本上级（assets/templates+assets/shared），
    或环境变量 BBU_BUILDER_ROOT 指定的目录。"""
    import os
    here = Path(__file__).resolve().parent  # .../bbu-fault-skill-builder/scripts
    for cand in (here.parent, os.environ.get("BBU_BUILDER_ROOT", "")):
        try:
            c = Path(cand)
        except TypeError:
            continue
        if (c / "assets" / "templates" / "child-skill").is_dir() \
                and (c / "assets" / "shared").is_dir():
            return c
    sys.exit("找不到 builder 根目录（无 assets/templates/child-skill 与 assets/shared）；"
             "可用环境变量 BBU_BUILDER_ROOT 指定")


# ---------------------------------------------------------------------------
# 能力 1：同步更新（元 skill → 子 skill）
# ---------------------------------------------------------------------------
def do_sync(child_dir: Path, builder_root: Path):
    if not child_dir.is_dir():
        sys.exit(f"子 skill 目录不存在: {child_dir}")
    # 定位子 skill 的 profile 与 skill.name
    prof = None
    for cand in ("assets/fault-profile.yaml",):
        if (child_dir / cand).exists():
            prof = child_dir / cand
            break
    if prof is None:
        sys.exit(f"{child_dir} 不是合法子 skill（无 assets/fault-profile.yaml）")
    skill_name = _profile_skill_name(prof) or child_dir.name
    # scaffold 用 skill_name 定位 dest = <path>/<skill_name>；若目录名与 profile 名不一致，
    # --sync 会写到 <父目录>/<skill_name> 这个别的目录，必须先拦住。
    if skill_name != child_dir.name:
        sys.exit(f"[SYNC] 目录名与 profile skill.name 不一致：目录 {child_dir.name} vs "
                 f"profile {skill_name}；请先把目录重命名为 {skill_name} 再同步，"
                 "否则更新会落到错误目录")
    scaff = builder_root / "scripts" / "scaffold_child_skill.py"
    if not scaff.exists():
        sys.exit(f"找不到脚手架脚本: {scaff}")
    print(f"[SYNC] 把元 skill（builder {_builder_version(builder_root)}）基础能力同步到子 skill：{child_dir}")
    # scaffold 用 skill_name（profile.skill.name）定位 dest = <path>/<skill_name>，故 path 传父目录
    cmd = [sys.executable, str(scaff), skill_name, "--update", "--path", str(child_dir.parent),
           "--profile", str(prof)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "").strip()
    if r.returncode == 0:
        print(out)
        print(f"[SYNC] 子 skill 已同步至元 skill 最新基础能力（generation 保持/递增由脚手架决定）")
    else:
        print(out, file=sys.stderr)
        print((r.stderr or "").strip(), file=sys.stderr)
        sys.exit(f"[SYNC] 同步失败（退出码 {r.returncode}）")


def _builder_version(builder_root: Path):
    try:
        import re
        txt = (builder_root / "scripts" / "scaffold_child_skill.py").read_text(encoding="utf-8")
        m = re.search(r'TOOLKIT_VERSION\s*=\s*"([^"]+)"', txt)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def _profile_skill_name(prof: Path):
    """读 fault-profile 的 skill.name（JSON 直接；YAML 尝试极简 key 提取，无需 PyYAML）。"""
    try:
        if prof.suffix == ".json":
            d = json.loads(prof.read_text(encoding="utf-8"))
            return (d.get("skill") or {}).get("name")
        txt = prof.read_text(encoding="utf-8")
        import re
        m = re.search(r'^\s*name\s*:\s*["\']?([\w-]+)["\']?\s*$', txt, re.M)
        return m.group(1) if m else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 能力 2：回归回收（子 skill → 元 skill）
# ---------------------------------------------------------------------------
def _base_candidates(child_dir: Path):
    """列出子 skill 中属于基础能力的文件及其元 skill 侧映射。返回
    [(子skill rel, child 文件, builder 源文件, kind)]。kind in scripts/assets/common/template。"""
    out = []
    for sc in BASE_SCRIPTS:
        cf = child_dir / "scripts" / sc
        if cf.exists():
            out.append((f"scripts/{sc}", cf, None, "scripts"))
    for a in BASE_ASSETS:
        cf = child_dir / "assets" / a
        if cf.exists():
            out.append((f"assets/{a}", cf, None, "assets"))
    cr = child_dir / "assets" / BASE_COMMON_RULES
    if cr.exists():
        out.append((f"assets/{BASE_COMMON_RULES}", cr, None, "common"))
    for t in REPORT_TEMPLATES:
        cf = child_dir / "assets" / t
        if cf.exists():
            out.append((f"assets/{t}", cf, None, "template"))
    return out


def _builder_src_for(rel: str, builder_root: Path):
    """元 skill 侧该 rel 对应的源文件（脚本→builder scripts/；资产→shared；模板→templates）。"""
    if rel.startswith("scripts/"):
        return builder_root / "scripts" / Path(rel).name
    if rel == "assets/common-rules.yaml":
        return builder_root / "assets" / "shared" / "common-rules.yaml"
    if rel.startswith("assets/") and rel.endswith(".yaml"):
        return builder_root / "assets" / "shared" / Path(rel).name
    if rel.startswith("assets/report_template"):
        return builder_root / "assets" / "templates" / "child-skill" / rel
    return None


def do_reclaim(child_dir: Path, builder_root: Path, apply: bool, include_template: bool,
               show_diff: bool = False):
    if not child_dir.is_dir():
        sys.exit(f"子 skill 目录不存在: {child_dir}")
    diffs = []  # (rel, child文件, builder源, kind, 差异行)
    for rel, cf, _bs, kind in _base_candidates(child_dir):
        if kind == "template" and not include_template:
            continue
        bsrc = _builder_src_for(rel, builder_root)
        if bsrc is None or not bsrc.exists():
            # 元 skill 无此基础文件（子 skill 新增？）→ 不回收，跳过并提示
            print(f"[i] 元 skill 无对应基础文件 {rel}（子 skill 新增/定制，不自动回收）")
            continue
        if _sha256(cf) == _sha256(bsrc):
            continue  # 与元 skill 一致，无差异
        try:
            a = bsrc.read_text(encoding="utf-8")
            b = cf.read_text(encoding="utf-8")
            diff = list(difflib.unified_diff(
                a.splitlines(), b.splitlines(), fromfile=f"builder/{rel}",
                tofile=f"child/{rel}", lineterm=""))
        except Exception:
            diff = ["[二进制/不可读文本，仅字节不同]"]
        diffs.append((rel, cf, bsrc, kind, diff))
    if not diffs:
        print("[RECLAIM] 未发现子 skill 与元 skill 存在基础能力差异（全部同步）。")
        return 0
    print(f"[RECLAIM] 发现 {len(diffs)} 个基础能力文件存在差异（子 skill 侧），可回收修复元 skill：\n")
    for rel, cf, bsrc, kind, diff in diffs:
        add = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        rem = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        print(f"  [{kind}] {rel}   (+{add}/-{rem})")
        # M7: --diff 时输出行级差异，回收决策不再凭文件名猜
        if show_diff:
            for l in diff:
                marker = l[:1] if l.startswith(("+", "-", "@")) else " "
                print(f"      {marker} {l[1:] if l.startswith(('+', '-')) else l}")
    if not apply:
        print("\n[RECLAIM] 以上为基础能力差异。加 --apply 回灌元 skill；"
              "回灌前请人工确认差异确属基础能力修复（而非子 skill 特有逻辑）。")
        return 0
    # apply：写回元 skill
    for rel, cf, bsrc, kind, diff in diffs:
        bsrc.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cf, bsrc)
        print(f"[APPLY] 已回灌元 skill: {rel}")
    # 脚本回灌后，同时同步到 builder scripts/（同源）
    n_synced = 0
    for rel, cf, bsrc, kind, diff in diffs:
        if kind == "scripts":
            b_scripts = builder_root / "scripts" / Path(rel).name
            if b_scripts.exists() and _sha256(b_scripts) != _sha256(cf):
                shutil.copy2(cf, b_scripts)
                print(f"[APPLY] 已同步 builder scripts/: {Path(rel).name}")
                n_synced += 1
    print("\n[RECLAIM] 回灌完成。请运行元 skill 回归（tests/selftest.py）确认不破坏其他子 skill；"
          "若差异含子 skill 特有逻辑，应只保留基础能力部分再回灌。")
    return 0


def main():
    _setup_utf8()
    ap = argparse.ArgumentParser(description="元 skill 与子 skill 的基础能力双向同步")
    ap.add_argument("--sync", metavar="CHILD_DIR", help="同步更新：元 skill 基础能力 → 子 skill")
    ap.add_argument("--reclaim", metavar="CHILD_DIR", help="回归回收：子 skill 基础能力修复 → 元 skill")
    ap.add_argument("--apply", action="store_true", help="回收时写回元 skill（默认仅列出差异）")
    ap.add_argument("--include-template", action="store_true",
                    help="回收时纳入报告模板（默认排除，避免覆盖子 skill 定制）")
    ap.add_argument("--diff", action="store_true",
                    help="--reclaim 时输出每个差异文件的行级 +/- diff（回收决策参考）")
    a = ap.parse_args()
    if not a.sync and not a.reclaim:
        ap.print_help()
        sys.exit(1)
    builder_root = _find_builder_root()
    if a.sync:
        do_sync(Path(a.sync), builder_root)
    if a.reclaim:
        do_reclaim(Path(a.reclaim), builder_root, a.apply, a.include_template, a.diff)


if __name__ == "__main__":
    main()
