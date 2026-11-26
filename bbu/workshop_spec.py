"""技能工坊语义规格：整段文本解析、AI 规格归一、确定性 profile 编译与质量审查。

模型只输出结构化 FaultSpec；本模块负责所有 YAML 结构、规则 ID、根因表达式和质量门禁。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .provenance import (attach_provenance, audit_provenance, locked_values,
                         restore_locked)
from . import workshop_recipes as recipes_module


def _clean(text: Any) -> str:
    return str(text or "").replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")


def _one_line(text: Any) -> str:
    return re.sub(r"\s+", " ", _clean(text)).strip()


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_one_line(x) for x in value if _one_line(x)]
    text = _clean(value).strip()
    if not text:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 同一行常见的“1、… 2、…”也拆开；小数点不是列表标记（10.5 不能拆成 10 和 5）。
        parts = re.split(r"(?=\d+\s*[、.](?!\d)\s*)", line)
        for part in parts:
            part = re.sub(r"^\d+\s*[、.]\s*", "", part).strip(" \t;；")
            if part:
                rows.append(part)
    if len(rows) <= 1:
        rows = [x.strip() for x in re.split(r"[，,、;；\n]+", text) if x.strip()]
    return rows


_LABELS = [
    ("fault_name", "故障名称"), ("aliases", "别名"), ("summary", "一句话描述"),
    ("domain", "适用设备"), ("domain", "设备类型"), ("domain", "设备"),
    ("keywords", "常见关键词"),
    ("observable", "现场能看到的现象"), ("observable", "现场现象"), ("observable", "主要现象"),
    ("impact", "影响"), ("time_pattern", "出现规律"),
    ("lookalikes", "容易混淆的故障"), ("log_types", "日志类型"),
    ("paths", "日志路径"), ("paths", "日志文件"), ("paths", "大致路径或文件名"),
    ("samples", "关键日志"), ("samples", "贴"),
    ("field_notes", "关键字段说明"), ("logic", "判断思路"), ("logic", "诊断思路"),
    ("thresholds", "阈值"), ("site_key", "一条日志里怎么区分"),
    ("root_text", "可能的原因"), ("root_text", "根因与处理"), ("root_text", "根因"),
    ("actions", "处理建议"), ("actions", "处置建议"), ("actions", "每种原因怎么处理"),
    ("risks", "风险"), ("recovery", "怎么确认已恢复"),
]


def _label(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([^：:]{1,90})[：:]\s*(.*)$", line)
    if not match:
        return None
    left, rest = match.group(1).strip(), match.group(2).strip()
    plain = re.sub(r"[（(][^）)]*[）)]", "", left)
    plain = re.sub(r"\s+", "", plain)
    for key, prefix in _LABELS:
        if plain.startswith(re.sub(r"\s+", "", prefix)):
            return key, rest
    return None


def _extract_fields(text: str) -> dict[str, str]:
    fields: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"^案例编号\s*[：:]", line):
            current = ""
            continue
        marked = _label(line)
        if marked:
            current, rest = marked
            fields.setdefault(current, [])
            if rest:
                fields[current].append(rest)
            continue
        if re.match(r"^\d+\s*[.、]\s*[^0-9]", line) and not current:
            continue
        if current:
            fields[current].append(raw.rstrip())
    return {key: "\n".join(value).strip() for key, value in fields.items()}


def _globs(text: str) -> list[str]:
    low = _clean(text).lower()
    if "boardhealth" in low:
        return ["**/*BoardHealth.log*", "**/BoardHealth*"]
    if "boardinit" in low:
        return ["**/*BoardInit.log*", "**/BoardInit*"]
    if "simplelog" in low or "bbx" in low:
        return ["**/*Simplelog*", "**/01_BBX_Simplelog*"]
    if "kernel" in low or ".klog" in low:
        return ["**/*kernel*.klog*", "**/*kernel*.log*", "**/*kernel*"]
    names = re.findall(r"[A-Za-z0-9_.()\[\]-]+\.(?:log(?:\.\w+)?|klog(?:\.\w+)?|cur|txt|csv)", text, re.I)
    if names:
        return [f"**/{name}*" for name in dict.fromkeys(names)]
    return []


def _source_name(globs: list[str]) -> str:
    joined = " ".join(globs).lower()
    if "simplelog" in joined:
        return "复位日志"
    if "boardinit" in joined:
        return "BoardInit 启动日志"
    if "boardhealth" in joined:
        return "BoardHealth/SMART 日志"
    if "kernel" in joined:
        return "内核日志"
    return "现场日志"


def _log_sources(paths: str, full_text: str) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = {}
    candidates = _list(paths)
    # 路径说明常被写成一整段；按日志类型再扫一次全文，不引入未出现的路径。
    for kind in ("BoardInit", "BoardHealth", "Simplelog", "kernel"):
        if kind.lower() in full_text.lower():
            candidates.append(kind)
    for item in candidates:
        globs = _globs(item)
        if not globs:
            continue
        key = globs[0]
        groups.setdefault(key, [])
        for glob in globs:
            if glob not in groups[key]:
                groups[key].append(glob)
    return [{"name": _source_name(globs), "globs": globs, "paths": [], "notes": ""}
            for globs in groups.values()]


def _lit(text: str) -> str:
    return re.escape(text) if re.search(r"[\\^$.|?*+()\[\]{}]", text) else text


def _rule(rid: str, description: str, globs: list[str], any_of: list[str],
          *, all_of: list[str] | None = None, source: str = "",
          verified: bool = False) -> dict[str, Any]:
    return {"id": rid, "description": description, "globs": globs,
            "all_of": all_of or [], "any_of": any_of, "none_of": [],
            "min_matches": 1, "verified": bool(verified),
            "source_excerpt": source or (any_of[0] if any_of else "")}


def _root(name: str, condition: str, actions: list[str], mechanism: str = "",
          confidence: str = "medium") -> dict[str, Any]:
    return {"name": name, "condition": condition, "mechanism": mechanism or "待补充",
            "confidence": confidence, "actions": actions, "risks": "待补充",
            "rollback": "待补充", "verify": [], "escalation": "待补充"}


def _recipe_spec(text: str, sources: list[dict[str, Any]], actions: list[str],
                 recipe_id: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[str]]:
    """按显式配方或自动候选加载领域规则。

    配方规则一律 verified=False；领域知识不再以 if-else 形式硬编码在编译器里。
    """
    recipe_id = _one_line(recipe_id)
    candidates = recipes_module.detect_recipes(text) if recipe_id in ("", "auto") else []
    candidates_ids = [str(x.get("id") or "") for x in candidates]
    if recipe_id in ("", "auto"):
        recipe = candidates[0] if candidates else None
        selection = "auto" if recipe else "none"
    elif recipe_id in ("none", "generic", "off"):
        recipe = None
        selection = "none"
    else:
        recipe = recipes_module.get_recipe(recipe_id)
        if not recipe:
            raise recipes_module.RecipeError(f"未知配方: {recipe_id}")
        selection = "explicit"
    if not recipe:
        return [], [], {}, candidates_ids
    rules, roots, meta = recipes_module.apply_recipe(recipe, text, sources, actions, selection)
    meta["auto_selected"] = selection == "auto"
    meta["trigger_matched"] = recipes_module.recipe_matches(recipe, text)
    meta["candidates"] = candidates_ids
    return rules, roots, meta, candidates_ids


def _generic_rules(fields: dict[str, str], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = "\n".join([fields.get("thresholds", ""), fields.get("field_notes", "")])
    candidates: list[str] = []
    for line in text.splitlines():
        line = line.strip().strip("-•")
        if not line or len(line) > 180 or re.match(r"^\d+\s*[、.]", line):
            continue
        if re.search(r"(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}", line):
            continue
        # 明确的关键字行：保留日志原文，去掉“--说明”。
        token = re.split(r"\s+--\s*|--", line, maxsplit=1)[0].strip()
        if 3 <= len(token) <= 120 and re.search(r"[A-Za-z]", token) and not _globs(token):
            if not re.search(r"当前值|若|如果|说明|关注|寄存器|关键字|如下|正常|异常", token):
                candidates.append(token)
    candidates = list(dict.fromkeys(candidates))[:24]
    if not candidates:
        return []
    globs = [g for source in sources for g in source["globs"]] or ["**/*.log*", "**/*.txt"]
    return [_rule("E1", "命中资料中明确给出的故障日志特征", globs,
                  [_lit(x) for x in candidates], source=candidates[0])]


def _parse_cases(text: str) -> list[dict[str, Any]]:
    matches = list(re.finditer(r"案例编号\s*[：:]\s*(C\d+)", text, re.I))
    cases = []
    for idx, match in enumerate(matches):
        block = text[match.end():matches[idx + 1].start() if idx + 1 < len(matches) else len(text)]
        if not re.search(r"案例类型|期望结论", block):
            continue
        negative = bool(re.search(r"正常\s*（?应排除|应排除本故障", block)) and not re.search(r"故障正例", block.split("期望结论", 1)[0].split("日志包", 1)[0])
        expected = ""
        exp = re.search(r"期望结论[^\n]*[：:]\s*([^\n]+)", block)
        if exp:
            expected = exp.group(1).strip()
            if "应排除" in expected or "排除本故障" in expected:
                negative = True
        phenomenon = re.search(r"故障现象[^\n]*[：:]\s*([^\n]+)", block)
        packages = re.findall(r"([^\n（()]+\.(?:7z|zip|tar\.gz|tgz|tar|gz|log(?:\.\w+)?))", block, re.I)
        cases.append({"id": match.group(1).upper(), "kind": "negative" if negative else "positive",
                      "description": phenomenon.group(1).strip() if phenomenon else expected,
                      "expected_root_cause": "" if negative else expected,
                      "expected_text": expected, "evidence": [],
                      "packages": [_one_line(x) for x in packages]})
    return cases


def _refs(condition: str) -> tuple[list[str], list[str]]:
    pos, neg = [], []
    for match in re.finditer(r"(not\s+)?\bE\d+\b", condition or ""):
        rid = re.sub(r"\s+", "", match.group(0)).replace("not", "")
        (neg if match.group(1) else pos).append(rid)
    return list(dict.fromkeys(pos)), list(dict.fromkeys(neg))


def _reasoning_order(spec: dict[str, Any], logic_lines: list[str]) -> list[dict[str, Any]]:
    """把资料里的“判断思路”映射到已编译规则；配方提供关键词映射，编译器不再按故障名分支。"""
    rules = spec.get("evidence_rules") or []
    recipe = spec.get("recipe") or {}
    hints: list[tuple[str, list[str]]] = []
    reasoning = recipe.get("reasoning") if isinstance(recipe.get("reasoning"), dict) else {}
    for item in (reasoning or {}).get("hints") or []:
        if isinstance(item, dict):
            keyword = _one_line(item.get("keyword"))
            ids = _list(item.get("rule_ids"))
            if keyword and ids:
                hints.append((keyword, ids))
    valid = {str(r.get("id")) for r in rules}
    steps = []
    used: set[str] = set()
    for idx, raw in enumerate(logic_lines, 1):
        text = re.sub(r"^\s*\d+\s*[.、:：)]\s*", "", raw).strip()
        if not text:
            continue
        low = text.lower()
        ids: list[str] = []
        for keyword, candidates in hints:
            if keyword.lower() in low:
                ids.extend(x for x in candidates if x in valid)
        if not ids:
            # 退化策略：按规则描述与步骤文本的字面重叠挑规则，仍无则不绑定 ID。
            for rule in rules:
                desc = str(rule.get("description") or "")
                grams = {desc[i:i + 2] for i in range(max(0, len(desc) - 1))}
                if sum(1 for g in grams if g and g in text) >= 2:
                    ids.append(str(rule.get("id")))
        ids = list(dict.fromkeys(ids))
        used.update(ids)
        steps.append({"order": idx, "action": text, "rule_ids": ids,
                      "if_hit": "继续用命中证据校验对应根因条件",
                      "if_miss": "记录该证据缺失，不用缺失反推根因"})
    if not steps:
        sources = spec.get("log_sources") or []
        for idx, source in enumerate(sources, 1):
            ids = [str(r.get("id")) for r in rules
                   if set(r.get("globs") or []) & set(source.get("globs") or [])]
            steps.append({"order": idx, "action": f"先检查{source.get('name') or '相关日志'}",
                          "rule_ids": ids,
                          "if_hit": "按根因条件判断是否支持某个假设",
                          "if_miss": "该日志源无证据，继续检查下一来源"})
    # 未被判断思路覆盖的规则补在末尾，避免数据存在但平台不检查。
    leftovers = [str(r.get("id")) for r in rules if str(r.get("id")) not in used]
    if leftovers:
        steps.append({"order": len(steps) + 1,
                      "action": "补充核对未覆盖的证据规则：" + "、".join(leftovers),
                      "rule_ids": leftovers,
                      "if_hit": "纳入根因条件评估", "if_miss": "记录为未命中"})
    return steps


_THRESHOLD_RE = re.compile(
    r"^(?P<metric>.+?)\s*(?:当前值|值|数值)?\s*"
    r"(?P<op>低于|小于|高于|大于|不超过|至少|>=|<=|>|<)\s*"
    r"(?P<value>[-+]?\d+(?:\.\d+)?)\s*(?P<unit>%|MB|GB|KB|℃|ms|s|次|分钟|小时)?"
    r"(?:\s*(?:->|→|：|:)\s*(?P<meaning>.*))?$")


def parse_thresholds(text: str) -> list[dict[str, Any]]:
    """把“指标 当前值低于30%”拆成指标/比较符/数值/单位/时间窗/来源。"""
    rows: list[dict[str, Any]] = []
    for raw in _list(text):
        line = _one_line(raw)
        match = _THRESHOLD_RE.match(line)
        if not match:
            continue
        metric = _one_line(match.group("metric")).strip(" ：:，,。")
        if len(metric) < 2:
            continue
        op_raw = match.group("op")
        op = {"低于": "<", "小于": "<", "高于": ">", "大于": ">",
              "不超过": "<=", "至少": ">="}.get(op_raw, op_raw)
        rows.append({
            "metric": metric, "operator": op,
            "value": float(match.group("value")),
            "unit": match.group("unit") or "",
            "window": next((m.group(0) for m in re.finditer(
                r"\d+\s*(?:分钟|小时|秒|天)", line)), ""),
            # 版本号必须有 V/v 前缀或“版本”上下文，不能把阈值 10.5 误当成版本。
            "version_scope": next((m.group(0) for m in re.finditer(
                r"(?:版本\s*)?(?:V|v)\d+(?:\.\d+){1,3}", line)), ""),
            "meaning": _one_line(match.group("meaning")),
            "source_excerpt": line, "status": "parsed",
            "source_revision": "",
        })
    return rows


def _build_reasoning(spec: dict[str, Any], logic_text: str, threshold_text: str,
                     field_notes: str) -> dict[str, Any]:
    """生成可执行的诊断推理数据：竞争假设、检查顺序、鉴别点、误判陷阱和停止条件。"""
    rules = {str(r.get("id")): r for r in spec.get("evidence_rules") or []}
    roots = spec.get("root_causes") or []
    logic_lines = _list(logic_text)
    thresholds = _list(threshold_text)
    hypotheses = []
    for idx, root in enumerate(roots, 1):
        pos, neg = _refs(str(root.get("condition") or ""))
        hypotheses.append({
            "id": f"H{idx}", "root_cause": str(root.get("id") or f"R{idx}"),
            "name": root.get("name") or f"假设{idx}",
            "condition": root.get("condition") or "",
            "supporting_rules": pos,
            "excluding_rules": neg,
            "expected_evidence": [str((rules.get(x) or {}).get("description") or x) for x in pos],
            "why": root.get("mechanism") or "待补充机理",
        })
    discriminators = []
    for rid, rule in rules.items():
        pos_roots = [h["name"] for h in hypotheses if rid in h["supporting_rules"]]
        neg_roots = [h["name"] for h in hypotheses if rid in h["excluding_rules"]]
        if pos_roots and neg_roots:
            discriminators.append({
                "rule_id": rid, "meaning": rule.get("description") or rid,
                "supports": pos_roots, "excludes": neg_roots,
                "use": f"先看 {rid}；命中时优先验证{'、'.join(pos_roots)}，并排除{'、'.join(neg_roots)}",
            })
    recipe_reasoning = (spec.get("recipe") or {}).get("reasoning")
    if not isinstance(recipe_reasoning, dict):
        recipe_reasoning = {}
    strategy = _one_line(recipe_reasoning.get("strategy")) or (
        "先确认现象对应的直接证据，再检查支持规则；只有证据能够区分竞争假设时才下结论，否则输出证据不足。")
    domain_pitfalls = _list(recipe_reasoning.get("pitfalls")) or [
        "告警和文件名只用于路由，不能替代日志证据。",
        "缺少某个日志源时，不能把未检查当成阴性证据。",
    ]
    stop = _list(recipe_reasoning.get("stop_conditions")) or [
        "某一根因条件成立且优先级更高者不成立：停止继续扩展。",
        "所有条件不成立：输出 unmatched，不猜根因。",
    ]
    pitfalls = list(dict.fromkeys(domain_pitfalls))
    data_needed = [str(s.get("name") or "") for s in spec.get("log_sources") or []]
    return {
        "strategy": strategy,
        "decision_order": _reasoning_order(spec, logic_lines),
        "hypotheses": hypotheses,
        "discriminators": discriminators,
        "checks": thresholds,
        "thresholds": parse_thresholds(threshold_text),
        "pitfalls": pitfalls,
        "stop_conditions": stop,
        "data_needed": [x for x in data_needed if x],
    }


def parse_source(source_text: str, *, recipe_id: str = "") -> dict[str, Any]:
    text = _clean(source_text)
    fields = _extract_fields(text)
    fault_name = _one_line(fields.get("fault_name")) or "未命名故障"
    aliases = _list(fields.get("aliases"))
    summary = _one_line(fields.get("summary"))
    domain_text = _one_line(fields.get("domain"))
    log_types = _one_line(fields.get("log_types"))
    sources = _log_sources(fields.get("paths", ""), text)
    actions = _list(fields.get("actions"))
    rules, roots, recipe, recipe_candidates = _recipe_spec(
        text, sources, actions, recipe_id)
    compiler = "recipe" if recipe else "generic"
    if not rules:
        rules = _generic_rules(fields, sources)
    if not roots and rules:
        causes = _list(fields.get("root_text")) or [fault_name]
        condition = " or ".join(rule["id"] for rule in rules)
        roots = [_root(causes[0], condition, actions, confidence="low")]
    if recipe:
        aliases = list(dict.fromkeys(aliases + list(recipe.get("aliases") or [])))
        fault_name = fault_name if fault_name != "未命名故障" else str(recipe.get("name") or fault_name)
    symptoms = {
        "observable": _list(fields.get("observable")),
        "keywords": list(dict.fromkeys(
            _list(fields.get("keywords")) + [fault_name] + aliases
            + list((recipe or {}).get("keywords") or []))),
        "lookalikes": [{"fault": _one_line(fields.get("lookalikes")), "differentiator": "待确认"}]
        if _one_line(fields.get("lookalikes")) and _one_line(fields.get("lookalikes")) not in ("无", "无，故障明确") else [],
        "impact": _one_line(fields.get("impact")) or "待补充",
        "time_pattern": _one_line(fields.get("time_pattern")) or "待补充",
        "triggers": _list(fields.get("observable")),
        "exclusions": [],
    }
    device_types = ["BBU/OSS"] if re.search(r"VSW|V9200|BBU|ITRAN", domain_text + fault_name, re.I) else []
    if "SDR" in (domain_text + log_types).upper():
        device_types.append("SDR")
    collection = []
    if "DCM" in log_types.upper():
        collection.append("dcm-zip")
    if "CLOUDMAT" in log_types.upper():
        collection.append("cloudmat-text")
    if re.search(r"设备全量|登录故障单板|设备侧", log_types):
        collection.append("device-tar")
    spec = {
        "fault_name": fault_name,
        "title": f"{fault_name}分析",
        "skill_name": "",
        "category": (recipe or {}).get("category") or "硬件故障",
        "aliases": aliases,
        "summary": summary,
        "domain": {"device_types": device_types or ["BBU/OSS"], "modes": "待补充",
                   "boards": domain_text or "待补充",
                   "version_scope": (recipe or {}).get("version_scope") or domain_text or "待补充",
                   "collection_sources": collection or ["device-tar"]},
        "symptoms": symptoms,
        "log_sources": sources,
        "evidence_rules": rules,
        "root_causes": roots,
        "cases": _parse_cases(text),
        "questions": [],
        "compiler": compiler,
        "recipe": recipe or {},
        "recipe_candidates": recipe_candidates,
        "recovery": _one_line(fields.get("recovery")),
        "risks": _one_line(fields.get("risks")),
        "thresholds": parse_thresholds(fields.get("thresholds", "")),
        "raw_fields": fields,
    }
    spec["diagnostic_reasoning"] = _build_reasoning(
        spec, fields.get("logic", ""), fields.get("thresholds", ""),
        fields.get("field_notes", ""))
    for idx, root in enumerate(spec["root_causes"], 1):
        root["id"] = f"R{idx}"
        if spec["risks"] and spec["risks"] != "无":
            root["risks"] = spec["risks"]
        if spec["recovery"]:
            root["escalation"] = "恢复确认：" + spec["recovery"]
    _map_cases(spec)
    spec["_source_text"] = text
    attach_provenance(spec, text)
    _mark_recipe_provenance(spec)
    return spec


def _map_cases(spec: dict[str, Any]) -> None:
    roots = spec.get("root_causes") or []
    for case in spec.get("cases") or []:
        if case.get("kind") == "negative":
            case["expected_root_cause"] = "NONE"
            continue
        text = (case.get("expected_text", "") + " " + case.get("description", "")).lower()
        scored: list[tuple[int, str]] = []
        for root in roots:
            hints = [str(x).lower() for x in (root.get("case_keywords") or []) if str(x).strip()]
            score = sum(3 for hint in hints if hint in text)
            if not hints:
                # 无配方提示时，用根因名与案例文本的字面重叠做保守匹配。
                name = str(root.get("name") or "").lower().replace("/", "")
                score = sum(1 for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", name)
                            if len(token) >= 2 and token in text)
            scored.append((score, str(root.get("id") or "")))
        best = max(scored, default=(0, ""))
        case["expected_root_cause"] = best[1] if best[0] > 0 else (roots[0]["id"] if roots else "")


def _mark_recipe_provenance(spec: dict[str, Any]) -> None:
    """配方规则不是用户原文直引，标注为 recipe 派生来源，避免被当成 missing_source。"""
    provenance = spec.get("provenance") or {}
    for i, rule in enumerate(spec.get("evidence_rules") or []):
        recipe_id = str((rule or {}).get("recipe_id") or "")
        if not recipe_id:
            continue
        prefix = f"/evidence_rules/{i}/"
        for path, item in provenance.items():
            if path.startswith(prefix):
                item["origin"] = "recipe"
                item["validation_status"] = "derived"
                item["derivation"] = f"recipe:{recipe_id}"
    for i, root in enumerate(spec.get("root_causes") or []):
        recipe_id = str((root or {}).get("recipe_id") or "")
        if not recipe_id:
            continue
        prefix = f"/root_causes/{i}/"
        for path, item in provenance.items():
            if path.startswith(prefix):
                item["origin"] = "recipe"
                item["validation_status"] = "derived"
                item["derivation"] = f"recipe:{recipe_id}"
    if spec.get("recipe"):
        spec["recipe"]["provenance"] = "derived_from_recipe_file"
        for path in ("/domain/version_scope", "/category", "/fault/aliases"):
            item = provenance.get(path)
            if item:
                item["origin"] = "recipe"
                item["validation_status"] = "derived"
                item["derivation"] = f"recipe:{spec['recipe'].get('id')}"


def derive_skill_name(spec: dict[str, Any], explicit: str = "") -> str:
    prefix = "bbu-fault-"
    candidate = _one_line(explicit or spec.get("skill_name")).lower()
    if (candidate.startswith(prefix) and len(candidate) <= 64
            and "--" not in candidate
            and re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", candidate[len(prefix):])):
        return candidate
    recipe_slug = _one_line((spec.get("recipe") or {}).get("skill_slug"))
    if recipe_slug and re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", recipe_slug) \
            and len(prefix + recipe_slug) <= 64:
        return prefix + recipe_slug
    fault = spec.get("fault_name", "")
    low = fault.lower()
    parts = []
    if "vsw" in low:
        parts.append("vsw")
    if "看门狗" in fault:
        parts.append("watchdog")
    if "usb" in low and "flash" in low:
        parts.append("usbflash")
    elif "ssd" in low:
        parts.append("ssd")
    elif "复位" in fault and "看门狗" not in fault:
        parts.append("reset")
    ascii_words = re.findall(r"[a-z0-9]+", low)
    for word in ascii_words:
        if word not in ("vsw", "ssd", "usb", "flash", "bbu") and word not in parts:
            parts.append(word)
    if not parts:
        digest = hashlib.sha1(fault.encode("utf-8")).hexdigest()[:8]
        parts = ["custom", digest]
    tail = "-".join(parts)[:64 - len(prefix)].strip("-")
    while "--" in tail:
        tail = tail.replace("--", "-")
    return prefix + tail


def merge_specs(base: dict[str, Any], ai: dict[str, Any]) -> dict[str, Any]:
    """AI 只补充语义；空值不覆盖确定性解析结果，规则与根因经归一后再编译。

    人工锁定的字段在合并前后取原值，模型重写只能改未锁字段。
    """
    locked = locked_values(base)
    merged = dict(base)
    for key in ("fault_name", "title", "category", "summary", "skill_name", "recovery", "risks"):
        if _one_line(ai.get(key)):
            merged[key] = _one_line(ai.get(key))
    for key in ("aliases",):
        if ai.get(key):
            merged[key] = _list(ai.get(key))
    for section in ("domain", "symptoms"):
        value = dict(base.get(section) or {})
        for key, item in (ai.get(section) or {}).items():
            if item not in (None, "", [], {}):
                value[key] = item
        merged[section] = value
    protected = base.get("compiler") in ("domain", "recipe")
    for key in ("log_sources", "evidence_rules", "root_causes", "cases"):
        if protected and key in ("log_sources", "evidence_rules", "root_causes"):
            continue
        if isinstance(ai.get(key), list) and ai[key]:
            merged[key] = ai[key]
    if protected:
        # 配方的类别、版本范围、别名和关键词是配方契约的一部分，不允许 AI 语义改写覆盖。
        recipe = base.get("recipe") or {}
        if recipe.get("category"):
            merged["category"] = recipe["category"]
        if recipe.get("version_scope"):
            merged.setdefault("domain", {})["version_scope"] = recipe["version_scope"]
        if recipe.get("aliases"):
            merged["aliases"] = list(dict.fromkeys(
                list(recipe.get("aliases") or []) + list(merged.get("aliases") or [])))
        if recipe.get("keywords"):
            symptoms = merged.setdefault("symptoms", {})
            symptoms["keywords"] = list(dict.fromkeys(
                list(recipe.get("keywords") or []) + list(symptoms.get("keywords") or [])))
    merged["questions"] = ai.get("questions") if isinstance(ai.get("questions"), list) else []
    _normalize_spec(merged)
    _map_cases(merged)
    if locked:
        merged["locked_restored"] = restore_locked(merged, locked)
    source_text = str(merged.get("_source_text") or base.get("_source_text") or "")
    attach_provenance(merged, source_text)
    _mark_recipe_provenance(merged)
    return merged


def _normalize_spec(spec: dict[str, Any]) -> None:
    spec["fault_name"] = _one_line(spec.get("fault_name")) or "未命名故障"
    spec["title"] = _one_line(spec.get("title")) or spec["fault_name"] + "分析"
    spec["aliases"] = _list(spec.get("aliases"))
    spec["summary"] = _one_line(spec.get("summary"))
    domain = spec.setdefault("domain", {})
    domain["device_types"] = _list(domain.get("device_types")) or ["BBU/OSS"]
    domain["collection_sources"] = _list(domain.get("collection_sources")) or ["device-tar"]
    for key in ("modes", "boards", "version_scope"):
        domain[key] = _one_line(domain.get(key)) or "待补充"
    symptoms = spec.setdefault("symptoms", {})
    for key in ("observable", "keywords", "triggers", "exclusions"):
        symptoms[key] = _list(symptoms.get(key))
    symptoms["keywords"] = list(dict.fromkeys(symptoms["keywords"] + [spec["fault_name"]] + spec["aliases"]))
    for key in ("impact", "time_pattern"):
        symptoms[key] = _one_line(symptoms.get(key)) or "待补充"
    symptoms["lookalikes"] = symptoms.get("lookalikes") if isinstance(symptoms.get("lookalikes"), list) else []
    sources = []
    for item in spec.get("log_sources") or []:
        if not isinstance(item, dict):
            continue
        globs = _list(item.get("globs")) or _globs(str(item.get("paths") or item.get("name") or ""))
        if globs:
            sources.append({"name": _one_line(item.get("name")) or _source_name(globs),
                            "globs": globs, "paths": _list(item.get("paths")),
                            "notes": _one_line(item.get("notes"))})
    spec["log_sources"] = sources
    default_globs = [g for s in sources for g in s["globs"]] or ["**/*.log*", "**/*.txt"]
    rules = []
    used = set()
    next_id = 1
    for item in spec.get("evidence_rules") or []:
        if not isinstance(item, dict):
            continue
        any_of = _list(item.get("any_of"))
        all_of = _list(item.get("all_of"))
        if not any_of and not all_of:
            continue
        candidate = _one_line(item.get("id"))
        rid = candidate if re.fullmatch(r"E\d+", candidate) and candidate not in used else ""
        while not rid or rid in used:
            rid = f"E{next_id}"
            next_id += 1
        used.add(rid)
        rules.append({"id": rid, "description": _one_line(item.get("description")) or "故障证据",
                      "globs": _list(item.get("globs")) or default_globs,
                      "all_of": all_of, "any_of": any_of, "none_of": _list(item.get("none_of")),
                      "min_matches": max(1, int(item.get("min_matches") or 1)),
                      "verified": bool(item.get("verified", False)),
                      "source_excerpt": _one_line(item.get("source_excerpt")),
                      "recipe_id": _one_line(item.get("recipe_id")),
                      "sample_cases": dict(item.get("sample_cases") or {}),
                      "version_scope": _one_line(item.get("version_scope")),
                      "provenance": item.get("provenance"),
                      "locked": bool(item.get("locked") or False)})
    spec["evidence_rules"] = rules
    spec["thresholds"] = [x for x in (spec.get("thresholds") or []) if isinstance(x, dict)]
    valid_ids = {r["id"] for r in rules}
    roots = []
    for idx, item in enumerate(spec.get("root_causes") or [], 1):
        if not isinstance(item, dict) or not _one_line(item.get("name")):
            continue
        condition = _one_line(item.get("condition"))
        refs = re.findall(r"\bE\d+\b", condition)
        if not condition or any(ref not in valid_ids for ref in refs):
            continue
        roots.append({"id": f"R{idx}", "name": _one_line(item.get("name")), "condition": condition,
                      "mechanism": _one_line(item.get("mechanism")) or "待补充",
                      "confidence": item.get("confidence") if item.get("confidence") in ("high", "medium", "low") else "medium",
                      "actions": _list(item.get("actions")),
                      "prerequisites": _list(item.get("prerequisites")),
                      "risks": _one_line(item.get("risks")) or "待补充",
                      "rollback": _one_line(item.get("rollback")) or "待补充",
                      "verify": item.get("verify") if isinstance(item.get("verify"), list) else [],
                      "escalation": _one_line(item.get("escalation")) or "待补充",
                      "recipe_id": _one_line(item.get("recipe_id")),
                      "sample_cases": dict(item.get("sample_cases") or {}),
                      "version_scope": _one_line(item.get("version_scope")),
                      "case_keywords": _list(item.get("case_keywords")),
                      "provenance": item.get("provenance"),
                      "locked": bool(item.get("locked") or False)})
    spec["root_causes"] = roots
    cases = []
    for idx, item in enumerate(spec.get("cases") or [], 1):
        if not isinstance(item, dict):
            continue
        cases.append({"id": re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("id") or f"C{idx}")),
                      "kind": "negative" if item.get("kind") in ("negative", "正常", "反例") else "positive",
                      "description": _one_line(item.get("description")),
                      "expected_root_cause": _one_line(item.get("expected_root_cause")),
                      "expected_text": _one_line(item.get("expected_text")),
                      "evidence": _list(item.get("evidence")),
                      "packages": _list(item.get("packages")),
                      "locked": bool(item.get("locked") or False)})
    spec["cases"] = cases


def compile_profile(spec: dict[str, Any], skill_name: str) -> dict[str, Any]:
    _normalize_spec(spec)
    if spec.get("_source_text"):
        attach_provenance(spec, str(spec.get("_source_text") or ""))
    steps = []
    rules = spec["evidence_rules"]
    # 按日志路径分组，避免一个宽泛步骤掩盖各证据源。
    groups: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        key = "\n".join(rule["globs"])
        groups.setdefault(key, []).append(rule)
    for idx, group in enumerate(groups.values(), 1):
        globs = group[0]["globs"]
        steps.append({
            "id": f"S{idx}", "title": f"检索{_source_name(globs)}证据",
            "objective": "确认是否存在资料中明确登记的故障证据",
            "targets": [{"globs": globs, "format": "text", "encoding": "auto"}],
            "rules": [{k: v for k, v in rule.items() if k != "source_excerpt"} for rule in group],
            "decision": {"on_hit": {"conclusion": "发现本组故障证据，按根因条件判定"},
                         "on_miss": {"conclusion": "未命中本组证据，继续检查其他证据"}},
        })
    profile = {
        "skill": {"name": skill_name, "title": spec["title"], "version": "0.1.0",
                  "execution_mode": "script"},
        "category": spec.get("category") or "硬件故障",
        "fault": {"name": spec["fault_name"], "aliases": list(dict.fromkeys([spec["fault_name"]] + spec["aliases"])),
                  "alarm_codes": [], "domain": spec["domain"],
                  "triggers": spec["symptoms"].get("triggers") or spec["symptoms"].get("observable") or [],
                  "exclusions": spec["symptoms"].get("exclusions") or []},
        "symptoms": {"observable": spec["symptoms"].get("observable") or [],
                     "description_keywords": spec["symptoms"].get("keywords") or [spec["fault_name"]],
                     "lookalikes": spec["symptoms"].get("lookalikes") or [],
                     "impact": spec["symptoms"].get("impact") or "待补充",
                     "time_pattern": spec["symptoms"].get("time_pattern") or "待补充"},
        "diagnostic_reasoning": spec.get("diagnostic_reasoning") or {},
        "provenance": spec.get("provenance") or {},
        "thresholds": spec.get("thresholds") or [],
        "recipe": spec.get("recipe") or {},
        "analysis": {"steps": steps},
        "components": {"unpack": True, "search": True, "report": True},
    }
    if spec["root_causes"]:
        profile["root_causes"] = spec["root_causes"]
    else:
        profile["mode"] = "triage"
    return profile


def audit_spec(spec: dict[str, Any]) -> dict[str, Any]:
    _normalize_spec(spec)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    questions: list[dict[str, Any]] = []

    def issue(bucket: list[dict[str, str]], field: str, message: str) -> None:
        bucket.append({"field": field, "message": message})

    if not spec.get("summary"):
        issue(warnings, "summary", "缺少一句话故障描述")
        questions.append({"field": "summary", "question": "请用一句话说明这个故障是什么、什么场景使用该技能。", "blocking": False})
    if not spec["symptoms"].get("observable"):
        issue(warnings, "symptoms.observable", "缺少现场可观察现象")
    if not spec["log_sources"]:
        issue(errors, "log_sources", "没有识别出日志路径或文件名，无法形成可检索规则")
        questions.append({"field": "log_sources", "question": "请补充故障相关日志的大致路径或文件名。", "blocking": True})
    if not spec["evidence_rules"]:
        issue(errors, "evidence_rules", "没有可执行的证据规则")
        questions.append({"field": "evidence_rules", "question": "请贴 1～2 条能区分正常与故障的真实日志行，并说明哪些字段异常。", "blocking": True})
    for rule in spec["evidence_rules"]:
        patterns = rule["all_of"] + rule["any_of"]
        if any(re.fullmatch(r"(?:19|20)\d{2}[-T:0-9]+", p) for p in patterns):
            issue(errors, rule["id"], "不能把日志时间戳当成故障规则")
        if any(len(p.strip()) < 3 for p in patterns):
            issue(warnings, rule["id"], "存在过短匹配词，容易误命中")
        if not rule.get("verified", False):
            issue(warnings, rule["id"], "规则未经确认，不会进入根因条件")
    if not spec["root_causes"]:
        issue(warnings, "root_causes", "尚未形成可判定的根因分支，只能作为分诊草稿")
        questions.append({"field": "root_causes", "question": "请补充可能的根因，以及每类根因对应哪些日志证据。", "blocking": False})
    for root in spec["root_causes"]:
        refs = re.findall(r"\bE\d+\b", root["condition"])
        if not refs:
            issue(errors, root["id"], "根因条件没有引用证据规则")
        if not root.get("actions"):
            issue(warnings, root["id"], f"根因“{root['name']}”缺少处理动作")
        if root.get("mechanism") == "待补充":
            issue(warnings, root["id"], f"根因“{root['name']}”缺少故障机理")
    kinds = {case.get("kind") for case in spec.get("cases") or []}
    if "positive" not in kinds:
        issue(warnings, "cases", "文本中没有可识别的故障案例")
        questions.append({"field": "cases", "question": "请补充至少一个故障案例，并写明期望根因和关键证据。", "blocking": False})
    if "negative" not in kinds:
        issue(warnings, "cases", "文本中没有可识别的正常案例")
        questions.append({"field": "cases", "question": "请补充一个正常案例或易混淆案例，防止规则误命中。", "blocking": False})
    for item in spec.get("questions") or []:
        if isinstance(item, dict) and _one_line(item.get("question")):
            questions.append({"field": _one_line(item.get("field")) or "general",
                              "question": _one_line(item.get("question")),
                              "blocking": bool(item.get("blocking"))})
    recipe = spec.get("recipe") or {}
    if recipe:
        issue(warnings, "recipe",
              f"领域配方“{recipe.get('name') or recipe.get('id')}”的规则默认未验证；"
              "需人工确认或真实正例/反例回归后才能进入根因条件")
        if recipe.get("auto_selected"):
            issue(warnings, "recipe",
                  "配方为自动匹配；请确认设备/单板/版本范围与本案例一致，避免套用错配方")
        if not (recipe.get("sample_cases") or {}).get("positive"):
            issue(warnings, "recipe", "配方未登记正例样本，不能据此声称已验证")
        if recipe.get("trigger_matched") is False:
            issue(warnings, "recipe", "所选配方的触发词未在资料中出现；请确认设备/单板/版本范围，避免套用不适用规则")
    # 去重，限制一次最多四个问题。
    prov = audit_provenance(spec)
    for path in prov.get("missing") or []:
        issue(warnings, path, "字段来源未在原文中定位，不能视为已验证")
    unique_questions = []
    seen = set()
    for item in questions:
        if item["question"] in seen:
            continue
        seen.add(item["question"])
        unique_questions.append(item)
    score = max(0, 100 - 25 * len(errors) - 6 * len(warnings))
    return {"ok": not errors, "score": score, "errors": errors, "warnings": warnings,
            "questions": unique_questions[:4],
            "counts": {"rules": len(spec["evidence_rules"]), "root_causes": len(spec["root_causes"]),
                       "cases": len(spec.get("cases") or []), "log_sources": len(spec["log_sources"]),
                       "provenance_coverage": prov.get("coverage"),
                       "provenance_missing": len(prov.get("missing") or [])},
            "provenance": prov}


def public_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """返回给 UI 的规格，避免重复发送大段 raw_fields/source_text。"""
    _normalize_spec(spec)
    return {key: value for key, value in spec.items()
            if key not in ("raw_fields", "questions", "_source_text")}


# 模型只需要业务语义，不需要 UI 溯源、诊断路径和配方候选等内部元数据。
# public_spec 仍保留这些字段供页面展示；prompt 必须走 model_spec，避免把
# provenance（单场景可达 1.8 万字符）等大对象反复发给模型。
MODEL_SPEC_KEYS = (
    "fault_name", "title", "skill_name", "category", "summary", "aliases",
    "domain", "symptoms", "log_sources", "evidence_rules", "root_causes",
    "cases", "recovery", "risks", "thresholds",
)
_MODEL_INTERNAL_KEYS = ("provenance", "locked")


def _strip_model_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_model_metadata(item)
                for key, item in value.items() if key not in _MODEL_INTERNAL_KEYS}
    if isinstance(value, list):
        return [_strip_model_metadata(item) for item in value]
    return value


def model_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """裁剪为模型提示词专用的 FaultSpec，去掉内部溯源和诊断元数据。"""
    public = public_spec(spec)
    compact = {key: public[key] for key in MODEL_SPEC_KEYS if key in public}
    return _strip_model_metadata(compact)
