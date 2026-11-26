#!/usr/bin/env python3
"""
scaffold_child_skill.py — 从 fault-profile.yaml 生成 BBU 故障分析子 Skill

特性：
  - 统一 YAML 输出 evidence-rules（含 rules/temporal/root_causes）
  - 输出 assets/fault-profile.yaml（profile 原件）与 evidence-rules.yaml
  - --update 增量模式：只重渲染派生文件，保留用户定制的报告模板
  - --inspect <子skill目录> 更新度检查：比对版本戳，报告 stale/local-modified/missing/新增件
  - 版本戳 assets/toolkit-version.json：builder 版本 + 受管文件 sha256 + 生成代数
  - --check 只校验 profile 不生成
  - 按需复制脚本与共享资源（components 显式声明或自动推断）
  - 一致性校验：ID 唯一、根因/时序/案例/决策 goto 引用、枚举值、正则可编译、verified:false 不得被引用

用法：
    python3 scaffold_child_skill.py [name] --profile fault-profile.yaml --path <输出目录>
    python3 scaffold_child_skill.py [name] --profile fault-profile.yaml --path <dir> --update
    python3 scaffold_child_skill.py --profile fault-profile.yaml --check
    python3 scaffold_child_skill.py --inspect <子skill目录>
（Windows 下 python3 替换为 python）
"""

import argparse
import difflib
import hashlib
import io
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from domain_pack import (DEFAULT_ID, DomainPackError, check_compatible,
                         load_domain_pack)

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "assets" / "templates" / "child-skill"
SCRIPTS_DIR = Path(__file__).resolve().parent  # builder scripts/：脚本唯一源（生成时复制到子 Skill）
SHARED_DIR = TEMPLATE_DIR.parent.parent / "shared"

# 工具箱版本：模板/脚本/资产有对外行为变化时递增；digest 为内容哈希（自动生成）
TOOLKIT_VERSION = "2.14.0"
CONTRACT_VERSION = "2"
MANIFEST_YAML = "assets/skill-manifest.yaml"
MANIFEST_JSON = "assets/skill-manifest.json"
STAMP_NAME = "toolkit-version.json"
MIN_PYTHON = (3, 11)
# Agent Skills 规范（agentskills.io）：name ≤64 且 hyphen-case 无首尾/连续连字符；description ≤1024；compatibility ≤500
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_NAME, MAX_DESCRIPTION, MAX_COMPATIBILITY = 64, 1024, 500
COMPATIBILITY = (f"Python >={MIN_PYTHON[0]}.{MIN_PYTHON[1]}，零三方依赖（读/生成 .yaml 需 PyYAML）；"
                 "7z/rar 包需系统 7z 或 unar；命令相对 SKILL.md 所在目录执行")
IGNORE_COPY = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".cache")

USER_CUSTOMIZABLE = {"assets/report_template.md", "assets/report_template.html"}

# 导出型派生件：由 profile / shared 源在生成时导出，**不与 builder 副本做字节级源比对**
# （不同平台换行/键序差异会让 --inspect 误报 stale）；仅校验存在性与（可选）版本戳一致性。
DERIVED_RELS = {
    "assets/evidence-rules.yaml",
    "assets/fault-profile.yaml",
    "references/fault-profile.md", "assets/common-rules.yaml",
    "assets/quick-signatures.yaml",
    "assets/skill-manifest.yaml", "assets/skill-manifest.json",
}

# 组件 → 子 skill 内文件（按需复制，不需要的不进子 skill）
SCRIPT_FOR_COMP = {
    "unpack": "scripts/unpack_logs.py",
    "search": "scripts/search_evidence.py",
    "report": "scripts/render_report.py",
    "parse_clock": "scripts/parse_clock.py",
    "parse_cloudmat": "scripts/parse_cloudmat.py",
}
BASE_SCRIPT_RELS = ("scripts/describe.py", "scripts/selfcheck.py", "scripts/domain_pack.py")
ASSET_FOR_COMP = {
    "alarm_dict": "assets/alarm-code-dictionary.yaml",
    "common_rules": "assets/common-rules.yaml",
    "clk_dict": "assets/clk-field-dictionary.yaml",
}
# SKILL.md 条件块标记名
FLAG_FOR_COMP = {
    "unpack": "UNPACK", "search": "SEARCH", "report": "REPORT", "parse_clock": "CLOCK",
    "parse_cloudmat": "CLOUDMAT", "alarm_dict": "ALARM_DICT",
    "common_rules": "COMMON_RULES", "clk_dict": "CLK_DICT",
}
CONFIDENCE_ENUM = {"high", "medium", "low"}
EXECUTION_MODE_ENUM = {"script", "agent"}
TREND_ENUM = {"exceed_threshold", "sustained_above", "monotonic_increase", "monotonic_decrease"}
RULE_TYPE_ENUM = {"text", "numeric_trend", "record_delta"}
CASE_TYPE_ENUM = {"positive", "negative", "boundary", "corruption", "multisite"}


def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass

# 自动推断信号（取自 profile 全文，小写匹配）
CLOCK_TOKENS = ("clockstatuslog", "clockdata", "clockhwinfo", "clockocxolog",
                "gnsssvinfo", "gnssspec", "gnssoriginal", "gnssmov",
                "gnsscross", "gnss", "inputclock", "clk[", "ocxo", "synce",
                "1588", "1pps", "spoofing", "parse_clock")
CLOUDMAT_TOKENS = ("cloudmat", "itemlist", "testitemresult", "getitemresultinfo",
                   "拷机", "工装", "cycling", "保存拷机表", "结果位图")


def collect_rules(profile):
    rules = []
    for step in profile.get("analysis", {}).get("steps", []):
        rules.extend(step.get("rules", []))
    return rules


def infer_components(profile):
    """未显式声明 components 时，按 profile 故障特征自动推断需要哪些组件。
    返回 {组件: bool} 与 {组件: 判定理由}。"""
    text = json.dumps(profile, ensure_ascii=False).lower()
    rules = collect_rules(profile)
    has_rules = bool(rules)

    is_clock = any(t in text for t in CLOCK_TOKENS)
    is_cloudmat = any(t in text for t in CLOUDMAT_TOKENS)

    # 采集包信号：glob 指向包内多文件路径，或出现采集包/网元特征
    globs_text = " ".join(str(g).lower() for r in rules for g in r.get("globs", []))
    archive_signals = any(k in text for k in
                          ("collectinfo", "bsp", "oamlog", "neList", "summary.json",
                           ".zip", ".tar", "采集包", "压缩包")) or "**/" in globs_text
    # 纯 CloudMAT 单文件工装日志：有 cloudmat、无时钟、无采集包、规则 glob 仅指向单 .log
    cloudmat_only = (is_cloudmat and not is_clock and not archive_signals
                     and all(str(g).lower().endswith(".log") and "**/" not in str(g)
                             for r in rules for g in r.get("globs", []))
                     and bool(rules))

    comps = {
        "search": has_rules,
        "parse_clock": is_clock,
        "parse_cloudmat": is_cloudmat,
        "unpack": has_rules and not cloudmat_only,
    }
    comps["alarm_dict"] = bool(
        profile.get("fault", {}).get("alarm_codes")
        or re.search(r"\b19\d{7}\b", text)
        or "fm-active" in text or "告警码" in text)
    comps["clk_dict"] = comps["parse_clock"]
    comps["common_rules"] = comps["unpack"] and comps["search"]
    comps["report"] = comps["search"]

    # 显式声明覆盖推断
    explicit = profile.get("components", {}) or {}
    reasons = {}
    for key in FLAG_FOR_COMP:
        if key in explicit:
            comps[key] = bool(explicit[key])
            reasons[key] = "profile.components 显式"
        else:
            reasons[key] = "自动推断"
    return comps, reasons


_BLOCK_RE = re.compile(r"\{\{\?(\w+)\}\}(.*?)\{\{/\1\}\}", re.S)
_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---", re.S)


def render_blocks(text, flags):
    """渲染 {{?FLAG}}...{{/FLAG}} 条件块；false 整块删除。"""
    prev = None
    while prev != text:
        prev = text
        text = _BLOCK_RE.sub(lambda m: m.group(2) if flags.get(m.group(1)) else "", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def apply_components(dest: Path, comps: dict, update: bool, extra_scripts=()):
    """按组件增删脚本与共享字典。返回 (copied, skipped)。

    extra_scripts：profile.components.extra_scripts 声明的专项脚本（如
    scripts/ocxo_aging_analyzer.py），纳入受管清单供 --update 同步、--inspect 体检；
    若 builder 模板无同名源，则不覆盖/不删除，仅登记跟踪（需子 Skill 自带）。"""
    copied, skipped = [], []
    # 基础运行入口：describe/selfcheck/domain_pack 始终随技能分发，供任意宿主预检。
    for rel in BASE_SCRIPT_RELS:
        target = dest / rel
        src = SCRIPTS_DIR / Path(rel).name
        if src.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append(rel)
    for comp, rel in SCRIPT_FOR_COMP.items():
        target = dest / rel
        src = SCRIPTS_DIR / Path(rel).name
        if comps.get(comp):
            if src.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)  # 脚本为分发件，update 时同步最新（非用户定制）
                copied.append(rel)
        elif target.exists():
            target.unlink()
            skipped.append(rel)
        else:
            skipped.append(f"{rel}(无需)")
    # 共享依赖：log_format.py 为 unpack/search 的公共格式分类模块，随任一复制
    dep = "scripts/log_format.py"
    dep_target = dest / dep
    if comps.get("unpack") or comps.get("search"):
        src = SCRIPTS_DIR / Path(dep).name
        if src.exists():
            dep_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dep_target)
            copied.append(dep)
    elif dep_target.exists():
        dep_target.unlink()
        skipped.append(dep)
    # 报告组件同时分发：专项结论归一化工具与一键分析入口。
    for rel in ("scripts/gen_specialized.py", "scripts/run_analysis.py"):
        target = dest / rel
        if comps.get("report"):
            src = SCRIPTS_DIR / Path(rel).name
            if src.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                copied.append(rel)
        elif target.exists():
            target.unlink()
            skipped.append(rel)
    # FM 告警时间窗对齐工具：fm_alarm_align.py 随 report 组件分发（N4，报告「故障现象与甄别」
    # 中输出时钟告警与规则证据时间窗的对齐结果）
    fal = "scripts/fm_alarm_align.py"
    fal_target = dest / fal
    if comps.get("report"):
        fal_src = SCRIPTS_DIR / Path(fal).name
        if fal_src.exists():
            fal_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fal_src, fal_target)
            copied.append(fal)
    elif fal_target.exists():
        fal_target.unlink()
        skipped.append(fal)
    for comp, rel in ASSET_FOR_COMP.items():
        target = dest / rel
        src = SHARED_DIR / Path(rel).name
        if comps.get(comp):
            if src.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                copied.append(rel)
        elif target.exists():
            target.unlink()
            skipped.append(rel)
        else:
            skipped.append(f"{rel}(无需)")
    # 专项脚本：builder 模板有同名源则同步；无源但子 skill 已有则保留跟踪；都没有则提示人工提供
    for rel in extra_scripts:
        target = dest / rel
        src = SCRIPTS_DIR / Path(rel).name
        if src.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append(rel)
        elif target.exists():
            skipped.append(f"{rel}(已存在，builder 无同名源，按受管跟踪)")
        else:
            skipped.append(f"{rel}(builder 无同名源，需人工提供)")
    return copied, skipped


# ---------------------------------------------------------------------------
# 版本戳 / 更新度检查
# ---------------------------------------------------------------------------
def _sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def profile_has_quick(profile) -> bool:
    return bool(profile.get("quick_signatures"))


def _managed_rel_names(comps: dict, extra_scripts=(), has_quick=False):
    """按组件推断的受管文件名集合。

    注：SKILL.md 不参与哈希（其内含版本戳注释，存在哈希自指）；报告模板为
    USER_CUSTOMIZABLE（update 保留），入戳仅用于观察漂移。
    """
    rels = ["assets/evidence-rules.yaml",
            "assets/fault-profile.yaml",
            "assets/format-policy.json",
            MANIFEST_YAML, MANIFEST_JSON,
            "references/fault-profile.md"]
    rels += list(BASE_SCRIPT_RELS)
    if has_quick:
        rels.append("assets/quick-signatures.yaml")
    rels += [rel for comp, rel in SCRIPT_FOR_COMP.items() if comps.get(comp)]
    if comps.get("unpack") or comps.get("search"):
        rels.append("scripts/log_format.py")
    if comps.get("report"):
        rels.append("scripts/gen_specialized.py")
        rels.append("scripts/fm_alarm_align.py")
    rels += [rel for comp, rel in ASSET_FOR_COMP.items() if comps.get(comp)]
    if comps.get("common_rules"):
        rels.append("assets/common-rules.yaml")
    rels += list(extra_scripts)
    rels += sorted(USER_CUSTOMIZABLE)
    return list(dict.fromkeys(rels))


def _managed_rels(dest: Path, comps: dict, extra_scripts=(), has_quick=False):
    """本次生成/更新写入的受管文件清单（存在才计）。"""
    return [r for r in _managed_rel_names(comps, extra_scripts, has_quick)
            if (dest / r).exists()]


def _extra_scripts(profile):
    """profile.components.extra_scripts → 规范化的 scripts/ 相对路径列表。"""
    comps = profile.get("components", {}) or {}
    es = comps.get("extra_scripts") or []
    out = []
    for e in es:
        rel = e if str(e).startswith("scripts/") else "scripts/" + str(e).lstrip("/")
        if rel not in out:
            out.append(rel)
    return out


def _builder_src(rel: str):
    """受管文件在 builder 侧的源头（脚本→模板目录；共享资产→shared）；派生件无源。"""
    if rel.startswith("scripts/") and (SCRIPTS_DIR / Path(rel).name).exists():
        return SCRIPTS_DIR / Path(rel).name
    if rel.startswith("assets/") and (SHARED_DIR / Path(rel).name).exists():
        return SHARED_DIR / Path(rel).name
    return None


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _rules_yaml_bytes(obj) -> bytes:
    """evidence-rules.yaml 的落盘字节：用 PyYAML 安全导出（统一 LF）。"""
    if not _HAS_YAML:
        sys.exit("生成子 Skill 需要 PyYAML（规则统一 .yaml）：pip install pyyaml")
    buf = io.StringIO()
    yaml.safe_dump(obj, buf, allow_unicode=True, sort_keys=False, width=200)
    return buf.getvalue().encode("utf-8")


def _rules_doc(profile: dict) -> dict:
    doc = {"rules": export_rules(profile),
           "temporal": export_temporal(profile),
           "root_causes": export_root_causes(profile)}
    if profile.get("mode") == "triage":
        doc["mode"] = "triage"
    return doc


def planned_file_hashes(dest: Path, profile: dict, comps: dict, extra,
                        profile_path: Path, name: str = "", domain_pack=None) -> dict:
    """本次生成/更新后，受管文件**应当**落盘的 {rel: sha256}。

    用于 `--update` 幂等预判：复制件取 builder 源字节；派生件在内存中按同一序列化
    逻辑生成（与实际落盘逐字节一致）；USER_CUSTOMIZABLE 与 builder 无同源的专项脚本
    在 --update 时保留磁盘版，故取当前磁盘字节。
    """
    h = {}
    for rel in BASE_SCRIPT_RELS:
        src = SCRIPTS_DIR / Path(rel).name
        if src.exists():
            h[rel] = _sha256(src)
    for comp, rel in SCRIPT_FOR_COMP.items():
        if comps.get(comp):
            src = SCRIPTS_DIR / Path(rel).name
            if src.exists():
                h[rel] = _sha256(src)
    if comps.get("unpack") or comps.get("search"):
        dep_src = SCRIPTS_DIR / "log_format.py"
        if dep_src.exists():
            h["scripts/log_format.py"] = _sha256(dep_src)
    if comps.get("report"):
        for gsp_rel in ("scripts/gen_specialized.py", "scripts/fm_alarm_align.py"):
            gsp_src = SCRIPTS_DIR / Path(gsp_rel).name
            if gsp_src.exists():
                h[gsp_rel] = _sha256(gsp_src)
    for comp, rel in ASSET_FOR_COMP.items():
        if comps.get(comp):
            src = SHARED_DIR / Path(rel).name
            if src.exists():
                h[rel] = _sha256(src)
    for rel in extra:
        src = SCRIPTS_DIR / Path(rel).name
        if src.exists():
            h[rel] = _sha256(src)
        elif (dest / rel).exists():
            h[rel] = _sha256(dest / rel)
    # 派生件（随 profile 变化）
    h["assets/fault-profile.yaml"] = _sha256(profile_path)
    rd = _rules_doc(profile)
    h["assets/evidence-rules.yaml"] = hashlib.sha256(_rules_yaml_bytes(rd)).hexdigest()
    if profile_has_quick(profile):
        qs_doc = {"skill": profile["skill"]["name"], "category": profile.get("category"),
                  "quick_signatures": export_quick_signatures(profile)}
        h["assets/quick-signatures.yaml"] = hashlib.sha256(
            _rules_yaml_bytes(qs_doc)).hexdigest()
    if comps.get("common_rules"):
        cr_pre = SHARED_DIR / "common-rules.yaml"
        if cr_pre.exists():
            h["assets/common-rules.yaml"] = _sha256(cr_pre)
    h["references/fault-profile.md"] = hashlib.sha256(
        render_fault_profile_md(profile).encode("utf-8")).hexdigest()
    if domain_pack:
        h["assets/format-policy.json"] = hashlib.sha256(
            json.dumps(format_policy_doc(domain_pack), ensure_ascii=False,
                       indent=2, sort_keys=False).encode("utf-8")).hexdigest()
    if name and domain_pack:
        manifest = build_manifest(profile, name, domain_pack, dest)
        h[MANIFEST_YAML] = hashlib.sha256(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False,
                           width=200).encode("utf-8")).hexdigest()
        h[MANIFEST_JSON] = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")).hexdigest()
    # 更新时保留用户定制的报告模板 → 取磁盘字节
    for rel in USER_CUSTOMIZABLE:
        if (dest / rel).exists():
            h[rel] = _sha256(dest / rel)
    return h


def is_noop_update(prev_stamp: dict, future: dict, dest: Path) -> bool:
    """本次 --update 是否确实无需落盘。需同时满足：
      1) builder 版本未变、受管文件集合与版本戳一致；
      2) 磁盘当前内容已经等于目标内容（无本地漂移需回写）、无因组件关闭待删除的残留脚本。
    否则必须真正执行 --update（回写并在升级报告中提示本地改动已覆盖）。"""
    if not prev_stamp or prev_stamp.get("version") != TOOLKIT_VERSION:
        return False
    prev_files = prev_stamp.get("files", {})
    if set(prev_files) != set(future):
        return False
    if not all(prev_files.get(k) == v for k, v in future.items()):
        return False
    # 磁盘已被本地改动（派生件/受管脚本/资产）→ 需要回写，不算无变化
    for rel, h in future.items():
        if rel in USER_CUSTOMIZABLE:
            continue  # 报告模板 update 保留本地版（其漂移已由上面的哈希对比捕获）
        p = dest / rel
        if not p.exists() or _sha256(p) != h:
            return False
    # 组件关闭后应删除的脚本/资产仍在磁盘 → 需真正执行清理
    gated = set(SCRIPT_FOR_COMP.values()) | set(ASSET_FOR_COMP.values()) | {
        "scripts/log_format.py", "scripts/gen_specialized.py", "scripts/fm_alarm_align.py"}
    for rel in gated:
        if rel not in future and (dest / rel).exists():
            return False
    return True


def read_stamp(dest: Path):
    p = dest / "assets" / STAMP_NAME
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _engine_files() -> list[Path]:
    rels = set(SCRIPT_FOR_COMP.values()) | set(BASE_SCRIPT_RELS) | {
        "scripts/log_format.py", "scripts/run_analysis.py",
        "scripts/fm_alarm_align.py", "scripts/gen_specialized.py",
        "scripts/unpack_logs.py", "scripts/search_evidence.py",
        "scripts/render_report.py",
    }
    return [SCRIPTS_DIR / Path(rel).name for rel in sorted(rels)
            if (SCRIPTS_DIR / Path(rel).name).exists()]


def engine_digest() -> str:
    """引擎源码摘要：与产物/清单无关，避免 manifest 自指。"""
    h = hashlib.sha256()
    for path in _engine_files():
        h.update(path.name.encode("utf-8") + b"\0")
        h.update(path.read_bytes())
    return "eng-" + h.hexdigest()[:12]


def format_policy_doc(domain_pack: dict) -> dict:
    """域包格式策略的技能内快照；运行期只读它，不回头找域包目录。"""
    pack = domain_pack or {}
    return {
        "schema": "skill-format-policy/1",
        "domain": {"pack": str(pack.get("id") or DEFAULT_ID),
                   "version": str(pack.get("version") or "")},
        "format_policy": dict(pack.get("format_policy") or {}),
    }


def write_format_policy(dest: Path, domain_pack: dict) -> dict:
    doc = format_policy_doc(domain_pack)
    (dest / "assets").mkdir(parents=True, exist_ok=True)
    (dest / "assets" / "format-policy.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def _major(version: str) -> int:
    try:
        return int(str(version).split(".")[0])
    except (TypeError, ValueError):
        return 0


def _major_minor(version: str) -> str:
    parts = str(version or "0").split(".")
    return ".".join(parts[:2]) if len(parts) > 1 else (parts[0] or "0")


def build_manifest(profile: dict, name: str, domain_pack: dict, dest: Path) -> dict:
    skill = profile.get("skill") or {}
    fault = profile.get("fault") or {}
    symptoms = profile.get("symptoms") or {}
    pack = domain_pack or {}
    quick = [str(q.get("id")) for q in profile.get("quick_signatures") or []
             if isinstance(q, dict) and q.get("id")]
    alarm_codes = []
    for item in fault.get("alarm_codes") or []:
        if isinstance(item, dict) and item.get("code"):
            alarm_codes.append(str(item["code"]))
        elif isinstance(item, str):
            alarm_codes.append(item)
    entrypoints: dict[str, dict] = {}
    run_py = dest / "scripts" / "run_analysis.py"
    if run_py.exists():
        entrypoints["run"] = {
            "argv": ["{python}", "scripts/run_analysis.py", "{inputs}",
                     "--description", "{description}", "--out", "{out}",
                     "--result-json", "{out}/run-result.json"],
            "timeout_sec": 900,
        }
    describe_py = dest / "scripts" / "describe.py"
    if describe_py.exists():
        entrypoints["describe"] = {"argv": ["{python}", "scripts/describe.py", "--json"]}
    selfcheck_py = dest / "scripts" / "selfcheck.py"
    if selfcheck_py.exists():
        entrypoints["selfcheck"] = {"argv": ["{python}", "scripts/selfcheck.py", "--json"]}

    manifest = {
        "schema": "skill-manifest/2",
        "name": name,
        "version": str(skill.get("version") or "1.0.0"),
        "title": str(skill.get("title") or name),
        "description": build_description(profile, domain_pack),
        "domain": {"pack": str(pack.get("id") or DEFAULT_ID),
                   "version": str(pack.get("version") or "")},
        "contract": {"profile": 2, "rules": 2, "result": 1, "case": 1},
        "engine": {"name": "diagnostic-engine", "version": TOOLKIT_VERSION,
                   "digest": engine_digest()},
        "entrypoints": entrypoints,
        "routing": {
            "category": str(profile.get("category") or ""),
            "aliases": [str(x) for x in ([fault.get("name")] + list(fault.get("aliases") or [])) if x],
            "keywords": [str(x) for x in symptoms.get("description_keywords") or []],
            "alarm_codes": alarm_codes,
            "device_types": [str(x) for x in (fault.get("domain") or {}).get("device_types") or []],
            "exclusions": [str(x) for x in fault.get("exclusions") or []],
            "quick_signatures": quick,
        },
        "components": {str(k): bool(v) for k, v in infer_components(profile)[0].items()},
        "format_policy": dict(pack.get("format_policy") or {}),
        "capabilities": {
            "domains": [str(pack.get("id") or DEFAULT_ID)],
            "archives": ["zip", "tar", "gzip", "bzip2", "xz"],
            "formats": ["text", "csv", "xlsx"],
            "analysis": ["text_rules", "entity_rules", "temporal", "multi_site", "verdict"],
            "reports": ["md", "html"],
        },
        "requires": {
            "python": f">={MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            "engine": f">={_major_minor(TOOLKIT_VERSION)},<{_major(TOOLKIT_VERSION) + 1}",
            "contract": CONTRACT_VERSION,
            "contract_max": str(int(CONTRACT_VERSION) + 1),
            "domain_pack": f">={_major_minor(str(pack.get('version') or '0'))},"
                           f"<{_major(str(pack.get('version') or '0')) + 1}",
            "third_party": [],
            "external": ["7z/unar（可选，缺失时登记 unsupported）"],
        },
        "permissions": {
            "risk": "low", "approval": "none", "network": "none",
            "data_policy": "local_only",
            "filesystem": {"read": ["task_inputs", "skill_dir"],
                           "write": ["task_output"]},
            "process": {"allow_subprocess": True, "executables": []},
        },
        "limits": {"max_inputs": 64, "timeout_sec": 900},
        "profile_sha256": _sha256(dest / "assets" / "fault-profile.yaml")
        if (dest / "assets" / "fault-profile.yaml").exists() else "",
        "rules_sha256": _sha256(dest / "assets" / "evidence-rules.yaml")
        if (dest / "assets" / "evidence-rules.yaml").exists() else "",
    }
    return manifest


def write_manifest(dest: Path, profile: dict, name: str, domain_pack: dict) -> dict:
    manifest = build_manifest(profile, name, domain_pack, dest)
    (dest / "assets").mkdir(parents=True, exist_ok=True)
    (dest / MANIFEST_YAML).write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, width=200),
        encoding="utf-8")
    (dest / MANIFEST_JSON).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def write_stamp(dest: Path, rels, prev=None):
    """写版本戳：受管文件 sha256 + digest + 生成代数。返回 stamp dict。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    files = {rel: _sha256(dest / rel) for rel in rels}
    digest_src = "\n".join(f"{k}:{v}" for k, v in sorted(files.items()))
    stamp = {
        "builder": "diagnostic-skill-builder",
        "version": TOOLKIT_VERSION,
        "digest": "tk-" + hashlib.sha256(digest_src.encode()).hexdigest()[:12],
        "generated_at": (prev or {}).get("generated_at", now),
        "updated_at": now,
        "generation": int((prev or {}).get("generation", 0)) + 1,
        "files": files,
    }
    (dest / "assets" / STAMP_NAME).write_text(
        json.dumps(stamp, ensure_ascii=False, indent=2), encoding="utf-8")
    return stamp


def _diff_stat(local_text: str, builder_text: str):
    """返回 (新增行数, 删除行数) 概览（difflib 统一换行，零依赖）。"""
    a = builder_text.splitlines()
    b = local_text.splitlines()
    sm = difflib.SequenceMatcher(None, a, b)
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _template_baseline(rel: str, name: str, title: str) -> str:
    """子 skill 报告模板的 builder 基线：占位符已按本子 skill 替换后的文本。"""
    return (TEMPLATE_DIR / rel).read_text(encoding="utf-8", errors="replace") \
        .replace("{{TITLE}}", title).replace("{{NAME}}", name)


def _template_drifted(rel: str, local: Path, name: str, title: str):
    """报告模板相对 builder 基线（占位替换后）是否有实质差异；返回 (是否漂移, 增, 删)。"""
    local_text = local.read_text(encoding="utf-8", errors="replace")
    add, rm = _diff_stat(local_text, _template_baseline(rel, name, title))
    return (add + rm) > 0, add, rm


def inspect_dir(dest: Path, diff_rel: str = None, adopt: bool = False):
    """更新度检查。退出码分级：
      0 = 与 builder 同步；
      1 = 仅本地改动 / 报告模板漂移（可交付，建议人工看一眼，不必 --update）；
      2 = 有 stale/missing/new 受管文件（需 --update）。
    """
    if not dest.is_dir():
        print(f"[x] 目录不存在: {dest}")
        return 2
    stamp = read_stamp(dest)
    need_update = review = 0
    if stamp is None:
        print(f"[!] {dest}: 无版本戳（v2.5.1 之前的旧版 builder 生成或手工创建），"
              f"按 builder 当前模板直接比对。")
        print("    建议：先执行一次 `scaffold_child_skill.py <名> --profile <profile> "
              "--path <父目录> --update` 打戳建立基线，之后再 --inspect 才能区分"
              "「builder 更新」与「本地改动」。")
        prev_files = {}
    else:
        prev_files = stamp.get("files", {})
        print(f"{dest.name}: toolkit v{stamp.get('version')} "
              f"{stamp.get('digest')} 第{stamp.get('generation')}代 "
              f"(生成 {stamp.get('generated_at','?')} / 更新 {stamp.get('updated_at','?')})")

    def _missing(rel):
        nonlocal need_update
        print(f"  [miss]  {rel}  ← 缺失（--update 可补齐）")
        need_update += 1

    # 先加载子 skill 自带 profile（推断组件 + 报告模板占位替换基线）
    expected = set()
    profdata = None
    prof = dest / "assets" / "fault-profile.yaml"
    if prof.exists():
        try:
            profdata = load_profile(prof)
            comps_guess, _ = infer_components(profdata)
            expected = set(_managed_rel_names(comps_guess, _extra_scripts(profdata),
                                              has_quick=profile_has_quick(profdata)))
        except Exception:
            pass
    ne_name = (profdata or {}).get("skill", {}).get("name", dest.name)
    ne_title = (profdata or {}).get("skill", {}).get("title", ne_name)

    # 版本戳内文件三方比对
    for rel, h in sorted(prev_files.items()):
        p = dest / rel
        if not p.exists():
            _missing(rel)
            continue
        if rel in DERIVED_RELS:
            continue  # 导出型派生件：仅校验存在，不做字节/源比对（换行/键序差异不应报 stale）
        cur = _sha256(p)
        if rel in USER_CUSTOMIZABLE:
            if cur != h:
                print(f"  [custom]  {rel}  ← 本地已定制（与生成时不同，update 会保留）")
                review += 1
            else:
                drifted, add, rm = _template_drifted(rel, p, ne_name, ne_title)
                if drifted:
                    print(f"  [tpl-drift] {rel}  ← builder 报告模板有新版（本地保留，差异 +{add}/-{rm} 行；"
                          f"--diff {rel} 查看）")
                    review += 1
            continue
        if cur != h:
            print(f"  [local-modified]  {rel}  ← 与生成时不同（本地改动；--update 会覆盖）")
            review += 1
            continue
        src = _builder_src(rel)
        if src is not None and _sha256(src) != h:
            print(f"  [stale] {rel}  ← builder 已有新版（--update 同步）")
            need_update += 1

    # 戳中无记录的期望文件
    for rel in sorted(expected - set(prev_files)):
        p = dest / rel
        if rel in DERIVED_RELS:
            if not p.exists():
                _missing(rel)
            continue
        if rel in USER_CUSTOMIZABLE:
            if not p.exists():
                _missing(rel)
            else:
                drifted, add, rm = _template_drifted(rel, p, ne_name, ne_title)
                if drifted:
                    print(f"  [tpl-drift] {rel}  ← builder 报告模板有新版（本地保留，差异 +{add}/-{rm} 行）")
                    review += 1
            continue
        src = _builder_src(rel)
        if src is None:
            continue
        if not p.exists():
            print(f"  [new]   {rel}  ← builder 新增件（--update 可获取）")
            need_update += 1
        elif _sha256(p) != _sha256(src):
            print(f"  [local-modified]  {rel}  ← 与 builder 当前版不同")
            review += 1

    # 目录中存在但不受管、且非 builder 模板自带的 .py
    managed_scripts = {r for r in expected if r.startswith("scripts/")}
    extra_scripts = []
    sp = dest / "scripts"
    if sp.exists():
        for p in sorted(sp.glob("*.py")):
            rel = "scripts/" + p.name
            if rel in managed_scripts or _builder_src(rel) is not None:
                continue
            extra_scripts.append(rel)
            print(f"  [extra] {rel}  ← 不受管脚本（若为专项脚本，可在 "
                  f"profile.components.extra_scripts 声明，或用 --adopt-extra-scripts 一键采纳）")

    if diff_rel:
        d = diff_rel if diff_rel.startswith("assets/") else "assets/" + diff_rel.lstrip("/")
        lp = dest / d
        if d in USER_CUSTOMIZABLE and lp.exists():
            a = _template_baseline(d, ne_name, ne_title).splitlines()
            b = lp.read_text(encoding="utf-8", errors="replace").splitlines()
            udiff = list(difflib.unified_diff(
                a, b, fromfile=f"builder:{d}（占位替换基线）", tofile=f"本地:{d}", lineterm=""))
            print("\n" + "=" * 70 + f"\n diff {d}\n" + "=" * 70)
            print("\n".join(udiff[:200])
                  + (f"\n…（仅显示前 200 行，共 {len(udiff)} 行）" if len(udiff) > 200 else ""))
        else:
            print(f"[x] --diff 仅支持报告模板：{sorted(USER_CUSTOMIZABLE)}")

    if adopt:
        adopt_extra_scripts(dest, extra_scripts)

    if need_update:
        print(f"  → {need_update} 个 stale/missing/new 受管文件：建议 --update")
        return 2
    if review:
        print(f"  → {review} 处本地改动/模板漂移（不影响交付；模板漂移可用 --diff 评估后人工合并）")
        return 1
    print("  [OK] 与 builder 同步")
    return 0


def adopt_extra_scripts(dest: Path, extra_scripts):
    """把检测到的非受管 .py 追加进子 skill profile.components.extra_scripts。"""
    prof_path = dest / "assets" / "fault-profile.yaml"
    if not extra_scripts:
        print("  [adopt] 未检测到需要采纳的不受管脚本")
        return
    if not _HAS_YAML:
        print("  [adopt] 当前环境无 PyYAML，无法改写 profile。请把以下片段粘贴进 "
              "assets/fault-profile.yaml 后重跑 --update：")
        print("  components:\n    extra_scripts:")
        for rel in extra_scripts:
            print(f"      - {rel}")
        return
    if not prof_path.exists():
        print(f"  [x] 未找到 {prof_path}，无法采纳")
        return
    data = yaml.safe_load(prof_path.read_text(encoding="utf-8")) or {}
    comps = data.setdefault("components", {})
    cur = comps.setdefault("extra_scripts", []) or []
    add = [r for r in extra_scripts if r not in cur]
    if not add:
        print("  [adopt] 所有脚本均已声明，无需改动")
        return
    bak = prof_path.with_suffix(".yaml.bak")
    shutil.copy2(prof_path, bak)
    cur.extend(add)
    comps["extra_scripts"] = cur
    prof_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                                        width=200), encoding="utf-8")
    print(f"  [adopt] 已追加 {add} 到 components.extra_scripts（备份：{bak.name}）")
    print("  [adopt] 请重跑：scaffold_child_skill.py <名> --profile "
          f"{prof_path} --path {dest.parent} --update")


def load_yaml_or_json(path):
    """读 .yaml/.yml（需 PyYAML）；统一规则/配置只支持 yaml。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() not in (".yaml", ".yml"):
        sys.exit(f"{p} 仅支持 .yaml 规则/配置：pip install pyyaml")
    if not _HAS_YAML:
        sys.exit(f"{p} 为 YAML 但当前环境缺少 PyYAML：pip install pyyaml")
    return yaml.safe_load(text)


def load_profile(path: Path):
    data = load_yaml_or_json(path)
    if not isinstance(data, dict) or "skill" not in data or "fault" not in data:
        sys.exit("profile 缺少 skill 或 fault 段")
    return data


def yaml_scalar(s) -> str:
    """frontmatter 安全标量：双引号 JSON 转义（YAML 双引号字符串是 JSON 的超集）。"""
    return json.dumps("" if s is None else str(s), ensure_ascii=False)


def valid_skill_name(name) -> bool:
    return bool(SKILL_NAME_RE.fullmatch(str(name))) and len(str(name)) <= MAX_SKILL_NAME


def _domain_title(domain_pack: dict | None) -> str:
    return str((domain_pack or {}).get("title") or "通用故障")


def _domain_devices(profile: dict, domain_pack: dict | None) -> str:
    fault = profile.get("fault") or {}
    devices = [str(d) for d in (fault.get("domain") or {}).get("device_types", [])]
    if not devices:
        devices = [str(d) for d in ((domain_pack or {}).get("applies_to") or {}).get("device_types", [])]
    return "、".join(devices) or "目标设备"


def build_description(profile: dict, domain_pack: dict | None = None) -> str:
    fault = profile["fault"]
    symptoms = profile.get("symptoms", {})
    domain_title = _domain_title(domain_pack)
    devices = _domain_devices(profile, domain_pack)
    names = [str(fault.get("name", ""))] + [str(a) for a in fault.get("aliases", [])]
    kw = [str(k) for k in symptoms.get("description_keywords", [])]
    codes = [str(a.get("code", "")).strip() for a in fault.get("alarm_codes", []) or []
             if isinstance(a, dict) and a.get("code")]
    triggers = "、".join(dict.fromkeys(n for n in names + kw if n))[:160]
    code_txt = f"告警码 {'/'.join(codes[:8])}；" if codes else ""
    desc = (
        f"{domain_title}故障分析技能：{fault.get('name', '')}（适用：{devices}）。"
        f"当用户提供 {devices} 日志压缩包或采集日志，"
        f"并描述以下故障时使用：{code_txt}{triggers}。"
        f"功能：递归解压、证据检索（时序/关联键/数值趋势/多站点）、根因判定、MD+HTML 报告。"
        + ("本技能为分诊候选集（规则未验证，仅提供检索线索，不作根因判定）。"
           if profile.get("mode") == "triage" else "仅覆盖本技能 profile 中已验证的根因分支。")
        + f"Use for {domain_title} fault analysis: log archive evidence search, "
        f"root-cause verdict and MD/HTML report for '{fault.get('name', '')}'."
    )
    return desc if len(desc) <= MAX_DESCRIPTION else desc[:MAX_DESCRIPTION - 1] + "…"


def render_frontmatter(name: str, profile: dict, domain_pack: dict | None = None) -> str:
    """子 Skill frontmatter：name/description 必需，compatibility/metadata 供宿主预检。"""
    pack = domain_pack or {}
    pack_id = str(pack.get("id") or DEFAULT_ID)
    pack_ver = str(pack.get("version") or "")
    return "\n".join([
        "---",
        f"name: {name}",
        f"description: {yaml_scalar(build_description(profile, domain_pack))}",
        f"compatibility: {yaml_scalar(COMPATIBILITY)}",
        "metadata:",
        "  builder: diagnostic-skill-builder",
        f"  toolkit_version: {TOOLKIT_VERSION}",
        f"  domain_pack: {pack_id}@{pack_ver}",
        "---",
    ])


def export_rules(profile: dict):
    rules = []
    for step in profile.get("analysis", {}).get("steps", []):
        for r in step.get("rules", []):
            rule = {
                "id": r["id"],
                "step": step["id"],
                "description": r.get("description", ""),
                "type": r.get("type", "text"),
                "globs": r.get("globs", []),
                "encoding": r.get("encoding", "auto"),
                "format": r.get("format", "text"),
            }
            for k in ("columns", "all_of", "any_of", "none_of", "time_extract",
                      "associate_by", "value_pattern", "trend", "threshold",
                      "abs_threshold", "sustained_count", "window_minutes",
                      "record", "entity"):
                if r.get(k) is not None:
                    rule[k] = r[k]
            for k in ("min_matches", "context_lines", "case_sensitive"):
                if k in r:
                    rule[k] = r[k]
            if r.get("verified") is False:
                rule["verified"] = False
            rules.append(rule)
    return rules


def export_temporal(profile: dict):
    return profile.get("temporal", []) or []


def export_root_causes(profile: dict):
    """导出根因判定所需的最小字段（供 search_evidence 计算 verdict）。"""
    out = []
    for rc in profile.get("root_causes", []) or []:
        item = {"id": rc.get("id"), "name": rc.get("name", ""),
                "condition": rc.get("condition", ""),
                "confidence": rc.get("confidence", "medium")}
        if rc.get("temporal"):
            item["temporal"] = list(rc["temporal"])
        out.append(item)
    return out


def export_quick_signatures(profile: dict):
    """导出浅度扫描特征清单（供 router 浅扫，机读）。"""
    out = []
    for q in profile.get("quick_signatures", []) or []:
        out.append({"id": q.get("id"),
                    "file_hints": list(q.get("file_hints", []) or []),
                    "any_of": list(q.get("any_of", []) or []),
                    "note": q.get("note", "")})
    return out


_COND_TOKEN_RE = re.compile(r"\b[A-Z]\d+\b|\b(?:and|or|not)\b|[()]|\s+")


def _check_regex(errors, where, pattern):
    try:
        re.compile(str(pattern))
    except re.error as e:
        errors.append(f"{where} 正则无法编译: {pattern!r} ({e})")


def validate(profile: dict):
    errors, warnings = [], []
    rule_ids, step_ids, unverified = set(), set(), set()
    # 分诊型子 skill（profile.mode: "triage"）不要求 analysis.steps / root_causes
    # —— 它们是分诊入口，不是单故障根因判定；有 steps/root_causes 仍按常规校验。
    is_triage = profile.get("mode") == "triage"
    steps = profile.get("analysis", {}).get("steps", []) or []
    if not steps and not is_triage:
        errors.append("analysis.steps 为空")
    for step in steps:
        sid = step.get("id")
        if not sid:
            errors.append("存在缺少 id 的步骤")
            continue
        if sid in step_ids:
            errors.append(f"步骤 ID 重复: {sid}")
        step_ids.add(sid)
        for r in step.get("rules", []) or []:
            rid = r.get("id")
            if not rid:
                errors.append(f"步骤 {sid} 存在缺少 id 的规则")
                continue
            if rid in rule_ids:
                errors.append(f"规则 ID 重复: {rid}")
            rule_ids.add(rid)
            if r.get("verified") is False:
                unverified.add(rid)
            rtype = r.get("type", "text")
            if rtype not in RULE_TYPE_ENUM:
                errors.append(f"规则 {rid} type 非法: {rtype}（应为 {sorted(RULE_TYPE_ENUM)}）")
            if not r.get("globs"):
                errors.append(f"规则 {rid} 缺少 globs")
            if rtype == "numeric_trend":
                if not r.get("value_pattern"):
                    errors.append(f"数值趋势规则 {rid} 缺少 value_pattern")
                else:
                    _check_regex(errors, f"规则 {rid} value_pattern", r["value_pattern"])
                if r.get("trend", "exceed_threshold") not in TREND_ENUM:
                    errors.append(f"规则 {rid} trend 非法: {r.get('trend')}")
            elif rtype == "record_delta":
                rec = r.get("record") or {}
                if not isinstance(rec, dict) or not rec.get("start"):
                    errors.append(f"record_delta 规则 {rid} 缺少 record.start")
                else:
                    _check_regex(errors, f"规则 {rid} record.start", rec["start"])
                fields = (rec or {}).get("fields") or {}
                if not isinstance(fields, dict) or not fields:
                    errors.append(f"record_delta 规则 {rid} 缺少 record.fields")
                else:
                    for fname, fpat in fields.items():
                        _check_regex(errors, f"规则 {rid} record.fields.{fname}", fpat)
                if not (rec or {}).get("entity"):
                    errors.append(f"record_delta 规则 {rid} 缺少 record.entity")
                if not ((rec or {}).get("require") or (rec or {}).get("any")):
                    errors.append(f"record_delta 规则 {rid} 缺少 require/any 比较")
            elif not (r.get("all_of") or r.get("any_of")):
                errors.append(f"规则 {rid} all_of/any_of 均为空，永远不会命中")
            for key in ("all_of", "any_of", "none_of"):
                for p in r.get(key) or []:
                    _check_regex(errors, f"规则 {rid} {key}", p)
            if (r.get("time_extract") or {}).get("pattern"):
                _check_regex(errors, f"规则 {rid} time_extract", r["time_extract"]["pattern"])
            ab = r.get("associate_by") or {}
            if ab and not ab.get("site") and not ab.get("pattern"):
                errors.append(f"规则 {rid} associate_by 需给 pattern 或 site: true")
            if ab.get("pattern"):
                _check_regex(errors, f"规则 {rid} associate_by", ab["pattern"])

    root_causes = profile.get("root_causes", []) or []
    rc_ids = set()
    if not root_causes and not is_triage:
        errors.append("root_causes 为空：子 Skill 必须至少登记一个根因")
    for rc in root_causes:
        rid = rc.get("id", "?")
        if rid in rc_ids:
            errors.append(f"根因 ID 重复: {rid}")
        rc_ids.add(rid)
        cond = rc.get("condition", "") or ""
        if not cond.strip():
            errors.append(f"根因 {rid} 缺少 condition")
        leftover = _COND_TOKEN_RE.sub("", cond)
        if leftover:
            errors.append(f"根因 {rid} condition 含非法片段: {leftover!r}（只允许规则 ID/and/or/not/括号）")
        refs = set(re.findall(r"\b[A-Z]\d+\b", cond))
        missing = refs - rule_ids
        if missing:
            errors.append(f"根因 {rid} 引用了不存在的规则: {sorted(missing)}")
        bad = refs & unverified
        if bad:
            errors.append(f"根因 {rid} 引用了 verified: false 的规则: {sorted(bad)}")
        if rc.get("confidence", "medium") not in CONFIDENCE_ENUM:
            errors.append(f"根因 {rid} confidence 非法: {rc.get('confidence')}")
        if not rc.get("actions"):
            errors.append(f"根因 {rid} 缺少 actions（处置动作）")

    # 宽松命中提示（不阻断）：规则描述含“候选/宽松/筛查/疑似”语义时，其命中
    # 可能是正常值候选，root_causes 直接引用会产出与专项脚本冲突的 verdict
    for step in steps:
        for r in step.get("rules", []) or []:
            rid = r.get("id")
            desc = str(r.get("description", ""))
            if rid and any(k in desc for k in ("候选", "宽松", "筛查", "疑似")):
                warnings.append(f"规则 {rid} 描述含宽松/候选语义（{desc[:30]}…）："
                                f"若被 root_causes 引用，verdict 可能与专项脚本冲突，"
                                f"请收窄命中口径或用 --specialized 复核")

    temporal = profile.get("temporal", []) or []
    t_ids = set()
    for con in temporal:
        tid = con.get("id", "?")
        if tid in t_ids:
            errors.append(f"时序约束 ID 重复: {tid}")
        t_ids.add(tid)
        for key in ("first", "second"):
            if con.get(key) not in rule_ids:
                errors.append(f"时序约束 {tid} 的 {key} 引用不存在: {con.get(key)}")
        if con.get("first") == con.get("second"):
            errors.append(f"时序约束 {tid} first 与 second 相同")
        mn, mx = con.get("min_gap_minutes", 0), con.get("max_gap_minutes")
        if mx is not None and mn is not None and float(mx) < float(mn):
            errors.append(f"时序约束 {tid} max_gap_minutes < min_gap_minutes")
    for rc in root_causes:
        for tid in rc.get("temporal") or []:
            if tid not in t_ids:
                errors.append(f"根因 {rc.get('id')} 关联的时序约束不存在: {tid}")

    goto_targets = step_ids | rc_ids | {"EXCLUDE"}
    for step in steps:
        d = step.get("decision") or {}
        for branch in ("on_hit", "on_miss"):
            g = (d.get(branch) or {}).get("goto")
            if g is not None and g not in goto_targets:
                errors.append(f"步骤 {step.get('id')} decision.{branch}.goto 引用不存在: {g}")

    for case in profile.get("cases", []) or []:
        cid = case.get("id", "?")
        if case.get("type") not in CASE_TYPE_ENUM:
            errors.append(f"案例 {cid} type 非法: {case.get('type')}")
        erc = case.get("expected_root_cause")
        if erc not in rc_ids | {"NONE", None}:
            errors.append(f"案例 {cid} expected_root_cause 引用不存在: {erc}")
        for ref in (case.get("expected_evidence") or []) + (case.get("not_expected_evidence") or []):
            if ref not in rule_ids:
                errors.append(f"案例 {cid} 引用不存在的规则: {ref}")
        both = set(case.get("expected_evidence") or []) & set(case.get("not_expected_evidence") or [])
        if both:
            errors.append(f"案例 {cid} 同一规则既期望命中又期望不命中: {sorted(both)}")

    for key in profile.get("components", {}) or {}:
        if key not in FLAG_FOR_COMP and key != "extra_scripts":
            errors.append(f"components 未知键: {key}（可用: {sorted(FLAG_FOR_COMP)} 或 extra_scripts）")
    es = (profile.get("components", {}) or {}).get("extra_scripts", [])
    if es:
        if not isinstance(es, list) or not all(isinstance(x, str) for x in es):
            errors.append("components.extra_scripts 应为脚本相对路径字符串列表（如 ['scripts/ocxo_aging_analyzer.py']）")
        else:
            for x in es:
                if not str(x).endswith(".py"):
                    errors.append(f"components.extra_scripts 应为 .py 脚本路径: {x!r}")
    name = (profile.get("skill") or {}).get("name", "")
    if not valid_skill_name(name):
        errors.append(f"skill.name 非法（hyphen-case，≤{MAX_SKILL_NAME} 字符，无首尾/连续连字符）: {name!r}")
    if not (profile.get("skill") or {}).get("title"):
        errors.append("skill.title 缺失")
    # execution_mode（可选，默认 script）
    em = (profile.get("skill") or {}).get("execution_mode", "script")
    if em not in EXECUTION_MODE_ENUM:
        errors.append(f"skill.execution_mode 非法: {em}（应为 {sorted(EXECUTION_MODE_ENUM)}）")
    # category（可选，字符串）
    cat = profile.get("category")
    if cat is not None and not str(cat).strip():
        errors.append("category 为空字符串；不使用大类时请整段删除而非留空")
    # quick_signatures（可选，列表；每项 id 唯一、any_of 非空）
    qids = set()
    for q in profile.get("quick_signatures", []) or []:
        qid = q.get("id", "?")
        if qid in qids:
            errors.append(f"quick_signatures id 重复: {qid}")
        qids.add(qid)
        if not (q.get("any_of") or []):
            errors.append(f"quick_signatures {qid} 缺少 any_of（浅扫特征字为空，永远不会疑似）")
        if not isinstance(q.get("file_hints", []) or [], list):
            errors.append(f"quick_signatures {qid} file_hints 应为列表")
    if len(COMPATIBILITY) > MAX_COMPATIBILITY:
        errors.append("compatibility 超过 500 字符（builder 内部常量）")
    return errors, warnings


def render_fault_profile_md(profile: dict) -> str:
    skill = profile["skill"]
    fault = profile["fault"]
    dom = fault.get("domain", {})
    lines = [
        f"# {skill['title']}",
        "",
        f"> 版本 {skill.get('version', '1.0.0')}。由 fault-profile.yaml 生成，修改请回到生成器更新。",
        "",
        "## 1. 故障标识",
        "",
        f"- 规范名称：{fault.get('name', '')}",
        f"- 别名：{('、'.join(str(a) for a in fault.get('aliases', [])) or '无')}",
        "- 告警码：" + ("、".join(f"{a.get('code')} {a.get('name')}" for a in fault.get("alarm_codes", [])) or "无"),
        f"- 设备形态：{'、'.join(str(d) for d in dom.get('device_types', []))}",
        f"- 制式：{'、'.join(str(m) for m in dom.get('modes', [])) or '待补充'}",
        f"- 版本范围：{dom.get('version_scope', '待补充')}",
        f"- 执行模式：{skill.get('execution_mode', 'script')}",
        f"- 故障大类：{profile.get('category') or '无'}",
    ]
    if profile.get("quick_signatures"):
        lines += ["", "### 浅度扫描特征（quick_signatures）", ""]
        for q in profile["quick_signatures"]:
            hints = ", ".join(str(h) for h in q.get("file_hints", [])) or "不限文件"
            anyof = " / ".join(str(a) for a in q.get("any_of", []))
            lines.append(f"- **{q.get('id')}**（{hints}）：{anyof}。{q.get('note', '')}")
    lines += [
        "",
        "## 2. 故障现象",
        "",
        f"- 可感知现象：{'；'.join(str(o) for o in profile.get('symptoms', {}).get('observable', [])) or '待补充'}",
        f"- 描述关键词：{'、'.join(str(k) for k in profile.get('symptoms', {}).get('description_keywords', []))}",
        "",
    ]
    reasoning = profile.get("diagnostic_reasoning") or {}
    if reasoning:
        lines += ["## 2.5 诊断推理与决策链", "",
                  f"- 总体策略：{reasoning.get('strategy') or '先证据、后结论'}"]
        order = reasoning.get("decision_order") or []
        if order:
            lines.append("- 排查顺序：")
            for step in order:
                ids = ("（" + "、".join(step.get("rule_ids") or []) + "）") if step.get("rule_ids") else ""
                lines.append(f"  {step.get('order')}. {step.get('action')}{ids}")
        hypotheses = reasoning.get("hypotheses") or []
        if hypotheses:
            lines.append("- 竞争假设：")
            for h in hypotheses:
                support = "、".join(h.get("supporting_rules") or []) or "无"
                exclude = "、".join(h.get("excluding_rules") or []) or "无"
                lines.append(f"  - {h.get('id', '?')} / {h.get('root_cause')}：{h.get('name')}；"
                             f"条件 `{h.get('condition', '')}`；支持 {support}；排除 {exclude}")
        discriminators = reasoning.get("discriminators") or []
        if discriminators:
            lines.append("- 关键鉴别点：")
            for d in discriminators:
                lines.append(f"  - {d.get('rule_id')}：{d.get('meaning')}；{d.get('use')}")
        if reasoning.get("checks"):
            lines.append("- 阈值/检查口径：")
            for check in reasoning["checks"]:
                lines.append(f"  - {check}")
        if reasoning.get("pitfalls"):
            lines.append("- 常见误判：")
            for pitfall in reasoning["pitfalls"]:
                lines.append(f"  - {pitfall}")
        if reasoning.get("stop_conditions"):
            lines.append("- 停止与升级：")
            for cond in reasoning["stop_conditions"]:
                lines.append(f"  - {cond}")
        lines.append("")

    lines += [
        "## 3. 分析步骤与判定决策链",
        "",
    ]
    for step in profile.get("analysis", {}).get("steps", []):
        lines += [f"### {step['id']}：{step.get('title', '')}",
                  f"- 目标：{step.get('objective', '')}"]
        for r in step.get("rules", []):
            rtype = r.get("type", "text")
            flag = "，**未验证**" if r.get("verified") is False else ""
            lines.append(f"- 规则 **{r['id']}**（{rtype}{flag}）：{r.get('description', '')}")
            lines.append(f"  - 路径：`{', '.join(r.get('globs', []))}`")
            if rtype == "numeric_trend":
                lines.append(f"  - 数值模式：{r.get('value_pattern')}；趋势：{r.get('trend')}；阈值：{r.get('threshold')}")
            elif rtype == "record_delta":
                rec = r.get("record") or {}
                lines.append(f"  - 记录起始：`{rec.get('start', '')}`；实体：{rec.get('entity')}")
                lines.append(f"  - 字段：{sorted((rec.get('fields') or {}).keys())}；"
                             f"必须满足：{rec.get('require') or {}}；任一满足：{rec.get('any') or {}}")
            for key in ("all_of", "any_of", "none_of"):
                if r.get(key):
                    lines.append(f"  - {key}: {r[key]}")
            if r.get("associate_by"):
                lines.append(f"  - 关联键：{r['associate_by'].get('pattern')}")
        d = step.get("decision", {})
        lines.append(f"- 命中 → {d.get('on_hit', {}).get('conclusion', '')}")
        lines.append(f"- 未命中 → {d.get('on_miss', {}).get('conclusion', '')}")
        lines.append("")

    temporal = profile.get("temporal", []) or []
    if temporal:
        lines += ["## 3.5 时序约束", ""]
        for con in temporal:
            gap = con.get("max_gap_minutes")
            gap_txt = f"{con.get('min_gap_minutes', 0)}~{gap} 分钟" if gap is not None else f"≥{con.get('min_gap_minutes', 0)} 分钟"
            lines.append(f"- **{con.get('id', '?')}**：{con.get('first')} 先于 {con.get('second')}，间隔 {gap_txt}。{con.get('note', '')}")
        lines.append("")

    lines += ["## 4. 根因与处理方案", ""]
    for rc in profile.get("root_causes", []):
        lines += [f"### {rc['id']}：{rc.get('name', '')}",
                  f"- 判定条件：`{rc.get('condition', '')}`",
                  f"- 关联时序约束：{'、'.join(map(str, rc.get('temporal') or [])) or '自动（condition 中肯定引用的规则两两相关的约束）'}",
                  f"- 置信度：{rc.get('confidence', 'medium')}",
                  "- 处置动作："]
        lines += [f"  {i}. {a}" for i, a in enumerate(rc.get("actions", []), 1)]
        for key, label in (("prerequisites", "前提条件"), ("risks", "风险"), ("rollback", "回退"), ("escalation", "升级路径")):
            v = rc.get(key)
            if v:
                lines.append(f"- {label}：{'；'.join(map(str, v)) if isinstance(v, list) else v}")
        lines.append("")

    lines += ["## 5. 已登记案例", ""]
    for c in profile.get("cases", []):
        lines.append(f"- **{c['id']}**（{c.get('type', '')}）：{c.get('description', '')}")
        lines.append(f"  - 期望根因：{c.get('expected_root_cause', '')}；期望证据：{c.get('expected_evidence', [])}")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="生成诊断子 Skill（域包可替换）")
    ap.add_argument("skill_name", nargs="?")
    ap.add_argument("--profile", help="fault-profile.yaml；--update 时可省略（复用子 skill 内已有的 profile）")
    ap.add_argument("--path", help="输出目录（--check 时可省略）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--update", action="store_true", help="增量模式：保留用户定制文件；无实际变更时不递增 generation")
    ap.add_argument("--check", action="store_true", help="只校验 profile，不生成")
    ap.add_argument("--inspect", metavar="DIR",
                    help="更新度检查（0=同步；1=仅本地改动/模板漂移；2=有 stale/missing/new 需 update）")
    ap.add_argument("--diff", metavar="REL",
                    help="配合 --inspect：显示指定报告模板（report_template.md/html）的本地 vs builder 差异")
    ap.add_argument("--adopt-extra-scripts", action="store_true",
                    help="配合 --inspect：把检测到的非受管脚本自动写入 components.extra_scripts")
    ap.add_argument("--domain-pack",
                    help="域包 id 或 domain-pack.yaml/json 路径；默认 bbu（domains/bbu）")
    ap.add_argument("--json", action="store_true",
                    help="--check 时输出机器可读 JSON（成功/失败均输出）")
    args = ap.parse_args()
    _utf8_stdio()

    if args.inspect:
        sys.exit(inspect_dir(Path(args.inspect), diff_rel=args.diff,
                             adopt=args.adopt_extra_scripts))

    # --update 允许省略 --profile：复用子 skill 内已有的 profile（避免静默用旧 profile 覆盖磁盘改动）
    profile_path = None
    if args.profile:
        profile_path = Path(args.profile)
    elif args.update:
        if not args.skill_name:
            ap.error("--update 省略 --profile 时需提供 skill 名（<skill-name>）")
        if not args.path:
            ap.error("--update 省略 --profile 时需提供 --path")
        candidate = Path(args.path) / args.skill_name / "assets" / "fault-profile.yaml"
        if not candidate.exists():
            sys.exit(f"未找到子 skill 内 profile: {candidate}；请用 --profile 显式传入")
        profile_path = candidate
        print(f"[i] 未指定 --profile，复用子 skill 内 profile: {candidate}")
    if profile_path is None:
        ap.error("--profile 必填（--inspect 或 --update 省略时复用子 skill 内 profile 除外）")

    profile = load_profile(profile_path)
    explicit_profile = bool(args.profile)
    try:
        if args.domain_pack and ("/" in args.domain_pack or args.domain_pack.endswith((".yaml", ".yml", ".json"))):
            domain_pack = load_domain_pack(pack_path=args.domain_pack)
        else:
            domain_pack = load_domain_pack(args.domain_pack or DEFAULT_ID)
    except DomainPackError as exc:
        if args.json:
            print(json.dumps({"ok": False, "errors": [str(exc)],
                              "warnings": [], "domain_pack": None}, ensure_ascii=False))
            return 2
        print(f"[x] 域包加载失败: {exc}", file=sys.stderr)
        return 2
    compat_errors = check_compatible(domain_pack, CONTRACT_VERSION, TOOLKIT_VERSION)
    errors, warnings = validate(profile)
    errors.extend(compat_errors)
    if errors:
        if args.json:
            print(json.dumps({"ok": False, "errors": errors, "warnings": warnings,
                              "domain_pack": {"id": domain_pack.get("id"),
                                              "version": domain_pack.get("version")}},
                             ensure_ascii=False))
            return 1
        print(f"[x] profile 校验失败（{len(errors)} 项）：", file=sys.stderr)
        for e in errors:
            print(" -", e, file=sys.stderr)
        sys.exit(1)
    for w in warnings:
        print(f"[!] {w}", file=sys.stderr)
    comps, reasons = infer_components(profile)
    extra = _extra_scripts(profile)
    if args.check:
        rules = collect_rules(profile)
        summary = {
            "ok": True,
            "profile": str(profile_path),
            "toolkit_version": TOOLKIT_VERSION,
            "contract_version": CONTRACT_VERSION,
            "domain_pack": {"id": domain_pack.get("id"),
                            "version": domain_pack.get("version"),
                            "source": domain_pack.get("_source")},
            "rules": len(rules),
            "temporal": len(profile.get("temporal") or []),
            "root_causes": len(profile.get("root_causes") or []),
            "cases": len(profile.get("cases") or []),
            "components": comps,
            "errors": [],
            "warnings": warnings,
        }
        if args.json:
            print(json.dumps(summary, ensure_ascii=False))
        else:
            print(f"[OK] profile 校验通过: {profile_path}")
            print(f"   toolkit v{TOOLKIT_VERSION}；域包 {domain_pack.get('id')}@{domain_pack.get('version')}")
            print(f"   规则 {len(rules)}；时序 {len(profile.get('temporal') or [])}；"
                  f"根因 {len(profile.get('root_causes') or [])}；案例 {len(profile.get('cases') or [])}")
            print("   组件推断：" + ", ".join(f"{k}={'Y' if v else 'N'}" for k, v in comps.items()))
        return 0
    if not args.path:
        ap.error("--path 必填（除非 --check）")

    name = args.skill_name or profile["skill"]["name"]
    if not valid_skill_name(name):
        sys.exit(f"非法 skill 名: {name}")
    out_parent = Path(args.path)
    out_parent.mkdir(parents=True, exist_ok=True)
    dest = out_parent / name

    if args.update:
        if not dest.exists():
            sys.exit(f"--update 目标不存在: {dest}")
        print(f"[UPD] 增量更新: {dest}")
        prev_stamp = read_stamp(dest)
        # 覆盖前快照：区分"本地改动"（磁盘≠戳）与"builder 更新"（戳≠新内容）
        pre_disk = {}
        for rel in _managed_rel_names(comps, extra, has_quick=profile_has_quick(profile)):
            p = dest / rel
            if p.exists():
                try:
                    pre_disk[rel] = _sha256(p)
                except OSError:
                    pass
        # 幂等短路：本次将写入内容与版本戳完全一致（且 builder 版本未变）→ 不写盘、不递增 generation
        future = planned_file_hashes(dest, profile, comps, extra, profile_path,
                                     name=name, domain_pack=domain_pack)
        if is_noop_update(prev_stamp, future, dest):
            print(f"[=] 受管内容无变化，已是最新（toolkit v{TOOLKIT_VERSION} "
                  f"第 {prev_stamp.get('generation')} 代，generation 保持不变），跳过写入。")
            return
    else:
        prev_stamp = None
        pre_disk = {}
        if dest.exists():
            if not args.force:
                sys.exit(f"目标已存在: {dest}（--force 或 --update）")
            shutil.rmtree(dest)
        shutil.copytree(TEMPLATE_DIR, dest, ignore=IGNORE_COPY)

    # 渲染 SKILL.md（全新用复制进来的 .tmpl；update 时从模板源重新渲染，以反映组件变化）
    # frontmatter 由 render_frontmatter 整体生成（JSON 转义，别名含 : # " 也不破坏 YAML）
    tmpl_path = dest / "SKILL.md.tmpl"
    tmpl_src = tmpl_path if tmpl_path.exists() else TEMPLATE_DIR / "SKILL.md.tmpl"
    tmpl = tmpl_src.read_text(encoding="utf-8")
    tmpl = _FRONTMATTER_RE.sub("{{FRONTMATTER}}", tmpl, count=1)
    tmpl = tmpl.replace("{{DOMAIN_PACK}}",
                        f"{domain_pack.get('id')}@{domain_pack.get('version')}")
    tmpl = tmpl.replace("{{DOMAIN_PREFIX}}",
                        str((domain_pack.get("routing") or {}).get("name_prefix")
                            or f"{domain_pack.get('id')}-fault"))
    flags = {FLAG_FOR_COMP[k]: bool(v) for k, v in comps.items()}
    flags["TRIAGE"] = profile.get("mode") == "triage"
    flags["NON_TRIAGE"] = not flags["TRIAGE"]
    flags["AGENT"] = (profile.get("skill") or {}).get("execution_mode", "script") == "agent"
    flags["SCRIPT_MODE"] = not flags["AGENT"]
    rendered = render_blocks(
        tmpl.replace("{{FRONTMATTER}}", render_frontmatter(name, profile, domain_pack))
        .replace("{{NAME}}", name)
        .replace("{{TITLE}}", profile["skill"]["title"]),
        flags)
    (dest / "SKILL.md").write_text(rendered, encoding="utf-8")
    if tmpl_path.exists():
        tmpl_path.unlink()

    # 按需复制/删除脚本与共享字典（不需要的组件不进子 skill）
    copied, skipped = apply_components(dest, comps, args.update, extra)

    # 报告模板（--update 时保留用户定制）
    for tpl_name in ("report_template.md", "report_template.html"):
        p = dest / "assets" / tpl_name
        if args.update and p.exists():
            continue
        if p.exists():
            p.write_text(p.read_text(encoding="utf-8")
                         .replace("{{TITLE}}", profile["skill"]["title"])
                         .replace("{{NAME}}", name), encoding="utf-8")

    # 写 profile（YAML 原件）与 evidence-rules（统一 YAML）
    # 统一用字节写入（LF），避免 Windows 文本模式 CRLF 导致与 builder 副本/规划哈希不一致
    profile_dst = dest / "assets" / "fault-profile.yaml"
    if profile_path.resolve() != profile_dst.resolve():
        shutil.copy2(profile_path, profile_dst)
    rules_doc = _rules_doc(profile)
    (dest / "assets" / "evidence-rules.yaml").write_bytes(_rules_yaml_bytes(rules_doc))

    # 浅度扫描特征清单（profile 声明 quick_signatures 时才生成，供 router 浅扫）
    if profile_has_quick(profile):
        qs_doc = {"skill": name, "category": profile.get("category"),
                  "quick_signatures": export_quick_signatures(profile)}
        (dest / "assets" / "quick-signatures.yaml").write_bytes(_rules_yaml_bytes(qs_doc))

    # 公共规则库（common_rules 组件开启时，预检命令直接 --rules 使用 .yaml）
    if comps.get("common_rules"):
        cr_src = SHARED_DIR / "common-rules.yaml"
        cr_dst = dest / "assets" / "common-rules.yaml"
        if cr_src.exists():
            shutil.copy2(cr_src, cr_dst)

    # 人读 fault-profile.md
    (dest / "references").mkdir(exist_ok=True)
    (dest / "references" / "fault-profile.md").write_bytes(
        render_fault_profile_md(profile).encode("utf-8"))

    for s in (dest / "scripts").glob("*.py"):
        s.chmod(0o755)

    # 格式策略快照：运行期由 log_format 读取，core 不再内置域文件名
    write_format_policy(dest, domain_pack)

    # manifest：宿主读到的第一份文件；生成后再打戳（manifest 不含 stamp digest，避免自指）
    manifest = write_manifest(dest, profile, name, domain_pack)

    # 版本戳 + SKILL.md 戳注 + 升级报告
    stamp = write_stamp(dest, _managed_rels(dest, comps, extra,
                        has_quick=profile_has_quick(profile)), prev_stamp)
    with open(dest / "SKILL.md", "a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- 由 diagnostic-skill-builder v{TOOLKIT_VERSION} "
                 f"生成/更新 | digest {stamp['digest']} | {stamp['updated_at']} -->\n")

    mode = "增量更新" if args.update else "生成"
    print(f"[OK] 子 Skill 已{mode}: {dest}")
    print(f"   版本戳: toolkit v{TOOLKIT_VERSION} {stamp['digest']} 第{stamp['generation']}代")
    if args.update:
        old_files = (prev_stamp or {}).get("files", {})
        if not prev_stamp:
            print("   旧版无版本戳 → 首次打戳")
        upgraded, local_over, added, regen = [], [], [], []
        prof_rel = "assets/fault-profile.yaml"
        for rel, h in sorted(stamp["files"].items()):
            if rel in USER_CUSTOMIZABLE or rel == prof_rel:
                continue  # 报告模板保留本地版（单独提示）；profile 覆盖语义单独判定
            old_h = old_files.get(rel)
            if old_h is None:
                added.append(rel)
            elif pre_disk.get(rel) and pre_disk[rel] != old_h:
                local_over.append(rel)
            elif h != old_h:
                (regen if rel in DERIVED_RELS else upgraded).append(rel)
        # profile 覆盖语义（A3）：区分"由 --profile 输入更新"与"磁盘本地改动被覆盖"
        new_prof, old_prof = stamp["files"].get(prof_rel), old_files.get(prof_rel)
        in_place = profile_path.resolve() == (dest / prof_rel).resolve()
        if not in_place:
            disk_prof = pre_disk.get(prof_rel)
            if disk_prof is not None and disk_prof != new_prof and disk_prof != old_prof:
                print(f"   [!] 磁盘上的 {prof_rel} 曾被本地修改（与上次打戳不同），已被本次 "
                      f"--profile 输入覆盖。若需保留磁盘改动，请先把改动同步到 --profile 源文件后再更新。")
            elif new_prof != old_prof:
                print(f"   [i] {prof_rel} 已按 --profile 输入更新")
        if upgraded:
            print(f"   builder 更新: {', '.join(upgraded)}")
        if regen:
            print(f"   随 profile 重生成: {', '.join(regen)}")
        if added:
            print(f"   新增受管件: {', '.join(added)}")
        if local_over:
            print(f"   [!] 本地改动的受管脚本/资产已被 builder 版覆盖: {', '.join(local_over)}")
        for rel in USER_CUSTOMIZABLE:
            dst = dest / rel
            if dst.exists():
                drifted, add, rm = _template_drifted(rel, dst, name, profile["skill"]["title"])
                if drifted:
                    print(f"   [i] 报告模板有 builder 新版（本地保留，差异 +{add}/-{rm} 行；"
                          f"--inspect --diff {rel} 可查看）: {rel}")
        if not (upgraded or added or local_over or regen or (new_prof != old_prof)):
            print("   受管文件与 builder 当前版一致")
    print(f"   规则: {len(rules_doc['rules'])}；时序: {len(profile.get('temporal') or [])}；根因: {len(profile.get('root_causes') or [])}；案例: {len(profile.get('cases') or [])}")
    print("   输出: evidence-rules.yaml（含 root_causes）+ fault-profile.yaml")
    print("   组件按需取舍：")
    for comp in FLAG_FOR_COMP:
        state = "[+] 复制  " if comps.get(comp) else "[-] 不复制"
        print(f"     {state}  {comp:<14}（{reasons.get(comp)}）")
    if skipped:
        print(f"   已跳过/移除：{', '.join(skipped)}")
    unverified = [c["id"] for c in profile.get("cases") or [] if "待补充" in str(c.get("archive", ""))]
    if unverified:
        print(f"   [!] 待补充案例: {unverified}")


if __name__ == "__main__":
    sys.exit(main())
