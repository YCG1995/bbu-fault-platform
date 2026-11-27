#!/usr/bin/env python3
"""R2 语义 lint：对 fault-profile 做 10 类可定位检查。

用法：
    python3 rule_lint.py --profile fault-profile.yaml [--samples samples.json]
输出 JSON：{ok, issues:[{code,severity,field_path,rule_id,root_cause_id,message,fix}], counts}
samples 可选：{"E1":[0,2,2], "E2":{"eligible":5,"hits":[1,1,1,1,1]}}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RULE_ID_RE = re.compile(r"\b[A-Z]\d+\b")


def positive_refs(cond: str) -> set[str]:
    return set(RULE_ID_RE.findall(re.sub(r"\bnot\s+[A-Z]\d+\b", " ", cond or "")))


def _issue(code, severity, field, message, fix, rule_id=None, root_cause_id=None):
    return {"code": code, "severity": severity, "field_path": field,
            "rule_id": rule_id, "root_cause_id": root_cause_id,
            "message": message, "fix": fix}


def _rule_key(r: dict) -> str:
    return json.dumps([r.get("globs"), r.get("all_of"), r.get("any_of"),
                       r.get("min_matches"), r.get("min_files"), r.get("file_scope")],
                      ensure_ascii=False, sort_keys=True)


def lint_profile(profile: dict, samples: dict | None = None) -> dict:
    samples = samples or {}
    rules, temporal, root_causes = [], [], []
    for si, step in enumerate(profile.get("analysis", {}).get("steps", []) or []):
        for ri, rule in enumerate(step.get("rules", []) or []):
            rule = dict(rule)
            rule["_field"] = f"analysis.steps[{si}].rules[{ri}]"
            rules.append(rule)
    temporal = profile.get("temporal", []) or []
    root_causes = profile.get("root_causes", []) or []
    issues = []
    used = set()
    for rc in root_causes:
        used |= set(RULE_ID_RE.findall(rc.get("condition", "") or ""))
        for t in rc.get("temporal", []) or []:
            used.add(str(t))
    for t in temporal:
        used.add(str(t.get("id", "")))
        used.add(str(t.get("first", "")))
        used.add(str(t.get("second", "")))
    # 1 未引用
    for r in rules:
        if r["id"] not in used:
            issues.append(_issue("RULE_UNREFERENCED", "warning", r["_field"],
                                 "规则未被任何根因/时序引用，命中不会进入结论",
                                 "删除规则，或在 root_causes[].condition / temporal 中引用",
                                 r["id"]))
    # 2 恒真/恒假
    for r in rules:
        for p in r.get("any_of", []) or []:
            if str(p).strip() in ("", ".*", "^.*$", ".+", "^.+$", "\\w*", "\\d*"):
                issues.append(_issue("RULE_TAUTOLOGY", "error", r["_field"] + ".any_of",
                                     f"any_of 含恒真模式 {p!r}，规则会命中几乎所有行",
                                     "删除恒真模式，改为具体故障码/关键字", r["id"]))
        for p in r.get("none_of", []) or []:
            if str(p).strip() in ("", ".*", "^.*$", ".+", "^.+$", "\\w*", "\\d*"):
                issues.append(_issue("RULE_TAUTOLOGY", "error", r["_field"] + ".none_of",
                                     f"none_of 含恒假/全匹配模式 {p!r}，规则永远不会命中",
                                     "删除该 none_of 或改为具体排除词", r["id"]))
    # 3 纯否定根因
    for rc in root_causes:
        cond = rc.get("condition", "") or ""
        if "not" in cond and not positive_refs(cond):
            issues.append(_issue("ROOT_PURE_NEGATIVE", "error", "root_causes",
                                 f"根因 {rc.get('id')} 只有否定证据，没有正证据",
                                 "至少引用一条正向规则，否定规则只做排除", root_cause_id=rc.get("id")))
    # 4 过宽模式
    for r in rules:
        for key in ("all_of", "any_of", "none_of"):
            for p in r.get(key, []) or []:
                s = str(p)
                if len(s.strip()) <= 1 or s.strip() in ("\\w*", ".+", ".*") or ("^" not in s and len(s.strip()) <= 3):
                    issues.append(_issue("RULE_OVERBROAD", "warning", r["_field"] + "." + key,
                                         f"模式 {s!r} 过短/过宽，容易误命中",
                                         "补充上下文/故障码，必要时加锚点", r["id"]))
    # 5 glob 过宽
    for r in rules:
        for g in r.get("globs", []) or []:
            gs = str(g)
            if gs in ("**/*", "**/*.*", "*") or (gs.endswith("**/*") and not re.search(r"\.[A-Za-z0-9]+$", gs)):
                issues.append(_issue("GLOB_TOO_BROAD", "warning", r["_field"] + ".globs",
                                     f"glob {gs!r} 未限制后缀/格式，可能扫描全部文件",
                                     "改为 **/clockstatuslog*.txt 等带特征的文件名", r["id"]))
    # 6 temporal 无时间提取
    by_id = {r["id"]: r for r in rules}
    for ti, t in enumerate(temporal):
        for rid in (t.get("first"), t.get("second")):
            r = by_id.get(rid)
            if r and r.get("type", "text") == "text" and not r.get("time_extract"):
                issues.append(_issue("TEMPORAL_NO_TIME", "error", f"temporal[{ti}]",
                                     f"时序规则引用的 {rid} 没有 time_extract，无法比较先后",
                                     "为该规则补 time_extract.pattern/format", rid))
    # 7 重复
    seen = {}
    for r in rules:
        k = _rule_key(r)
        if k in seen:
            issues.append(_issue("RULE_DUPLICATE", "warning", r["_field"],
                                 f"规则与 {seen[k]} 的 glob/模式/阈值完全相同",
                                 "合并重复规则，避免同一证据重复计权", r["id"]))
        else:
            seen[k] = r["id"]
    # 8/9/10 基于样本可达性
    for r in rules:
        rid = r["id"]
        sample = samples.get(rid)
        min_m = int(r.get("min_matches") or 1)
        min_files = int(r.get("min_files") or 0)
        scope = str(r.get("file_scope") or "any")
        if isinstance(sample, dict):
            hits = [int(x) for x in sample.get("hits", [])]
            eligible = int(sample.get("eligible", len(hits)))
        elif isinstance(sample, list):
            hits = [int(x) for x in sample]
            eligible = len(hits)
        else:
            hits, eligible = None, None
        if hits is not None:
            if max(hits or [0]) < min_m:
                issues.append(_issue("MIN_MATCHES_UNREACHABLE", "error", r["_field"] + ".min_matches",
                                     f"min_matches={min_m}，样本单文件最多 {max(hits or [0])} 次命中",
                                     "降低 min_matches 或补更强的模式", rid))
            if scope in ("at_least", "each") and min_files > eligible:
                issues.append(_issue("MIN_FILES_UNREACHABLE", "error", r["_field"] + ".min_files",
                                     f"min_files={min_files}，样本可用文件仅 {eligible} 个",
                                     "降低 min_files 或扩大 glob 覆盖", rid))
        elif scope in ("at_least", "each"):
            issues.append(_issue("MIN_FILES_UNREACHABLE", "warning", r["_field"] + ".min_files",
                                 "无样本，无法验证 min_files 是否可达",
                                 "上传至少一个案例或运行试跑覆盖矩阵", rid))
    # associate key
    for rc in root_causes:
        if str(rc.get("scope") or "site").lower() != "associate":
            continue
        for rid in sorted(positive_refs(rc.get("condition", ""))):
            r = by_id.get(rid)
            if r and not (r.get("associate_by") or {}):
                issues.append(_issue("ASSOCIATE_KEY_MISSING", "error", r["_field"] + ".associate_by",
                                     f"根因 {rc.get('id')} scope=associate，规则 {rid} 缺少 associate_by",
                                     "补 associate_by.pattern 或 {site:true}", rid, rc.get("id")))
    return {"ok": not any(i["severity"] == "error" for i in issues),
            "issues": issues,
            "counts": {"issues": len(issues),
                       "errors": sum(1 for i in issues if i["severity"] == "error"),
                       "warnings": sum(1 for i in issues if i["severity"] == "warning"),
                       "rules": len(rules), "root_causes": len(root_causes)}}


def main():
    ap = argparse.ArgumentParser(description="BBU 规则语义 lint")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--samples")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()
    import yaml
    profile = yaml.safe_load(Path(args.profile).read_text(encoding="utf-8"))
    samples = json.loads(Path(args.samples).read_text(encoding="utf-8")) if args.samples else None
    result = lint_profile(profile or {}, samples)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
