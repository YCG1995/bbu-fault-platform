"""技能工坊语义规格：整段文本解析、AI 规格归一、确定性 profile 编译与质量审查。

模型只输出结构化 FaultSpec；本模块负责所有 YAML 结构、规则 ID、根因表达式和质量门禁。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


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
        # 同一行常见的“1、… 2、…”也拆开。
        parts = re.split(r"(?=\d+\s*[、.]\s*)", line)
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
          *, all_of: list[str] | None = None, source: str = "") -> dict[str, Any]:
    return {"id": rid, "description": description, "globs": globs,
            "all_of": all_of or [], "any_of": any_of, "none_of": [],
            "min_matches": 1, "verified": True, "source_excerpt": source or any_of[0]}


def _root(name: str, condition: str, actions: list[str], mechanism: str = "",
          confidence: str = "medium") -> dict[str, Any]:
    return {"name": name, "condition": condition, "mechanism": mechanism or "待补充",
            "confidence": confidence, "actions": actions, "risks": "待补充",
            "rollback": "待补充", "verify": [], "escalation": "待补充"}


def _domain_spec(text: str, sources: list[dict[str, Any]], actions: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """对资料中语义明确的常见寄存器/存储字段做确定性编译，避免模型改写阈值。"""
    low = text.lower()
    by_name = {s["name"]: s["globs"] for s in sources}
    kernel = by_name.get("内核日志") or ["**/*kernel*"]
    board = by_name.get("BoardInit 启动日志") or ["**/*BoardInit.log*"]
    reset = by_name.get("复位日志") or ["**/*Simplelog*"]
    rules: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    if "watch dog underflow" in low:
        rules.append(_rule("E1", "复位日志记录看门狗复位", reset,
                           [_lit("(07)Watch Dog Underflow")], source="(07)Watch Dog Underflow"))
        next_id = 2
        mce_id = ""
        interrupt_id = ""
        if "hardware error" in low or "kernel panic" in low:
            pats = []
            if "hardware error" in low:
                pats.append(_lit("mce: [Hardware Error]"))
            if "kernel panic" in low:
                pats.append("Kernel panic")
            mce_id = f"E{next_id}"
            rules.append(_rule(mce_id, "内核记录 MCE 或 Kernel panic", kernel, pats,
                               source="mce: [Hardware Error] / Kernel panic"))
            roots.append(_root("MCE/Kernel panic 导致看门狗复位", f"E1 and {mce_id}", actions,
                               "CPU 机器检查异常或内核 panic 触发主控板看门狗复位"))
            next_id += 1
        if "0x630" in low and "0x634" in low:
            pattern = r"0x(?:630|634)=0x(?!0+(?:\s|$))[0-9a-fA-F]+"
            interrupt_id = f"E{next_id}"
            rules.append(_rule(interrupt_id, "EPLD 中断状态/事件寄存器非零", board, [pattern],
                               source="0x630/0x634 任意一个非 0"))
            condition = interrupt_id + (f" and not {mce_id}" if mce_id else "")
            roots.append(_root("CPU 中断异常导致看门狗复位", condition, actions,
                               "EPLD 中断状态或事件寄存器非零，说明 CPU 上报过中断；该证据可独立判定，不要求案例必须同时包含 Simplelog"))
            next_id += 1
        if "0x664" in low and "0x674" in low:
            pattern = r"0x(?:664|674)=0x(?!0+(?:\s|$))[0-9a-fA-F]+"
            power_id = f"E{next_id}"
            rules.append(_rule(power_id, "EPLD 电源 PG 时间寄存器非零", board, [pattern],
                               source="0x664/0x674 任意一个非 0"))
            exclusions = [x for x in (mce_id, interrupt_id) if x]
            condition = power_id + "".join(f" and not {x}" for x in exclusions)
            roots.append(_root("板内电源跌落导致看门狗复位", condition, actions,
                               "电源 PG 时间寄存器非零，说明板内电源出现过跌落；该证据可独立判定，不要求案例必须同时包含 Simplelog"))
    elif "ssd" in low and ("remain life percentage" in low or "comreset failed" in low):
        io_patterns = ["blk_update_request: I/O error", "Buffer I/O error on dev sda",
                       "READ LOG DMA EXT failed", "failed command: READ FPDMA QUEUED",
                       "failed command: WRITE FPDMA QUEUED"]
        sata_patterns = ["COMRESET failed", "ATA bus error", "softreset failed", "SError:"]
        next_id = 1
        io_id = sata_id = ""
        if any(x.lower() in low for x in io_patterns):
            io_id = f"E{next_id}"
            rules.append(_rule(io_id, "内核记录 SSD 直接 I/O 错误", kernel,
                               [_lit(x) for x in io_patterns if x.lower() in low], source="SSD I/O error"))
            roots.append(_root("SSD I/O error", io_id, actions, "SSD 读写或命令执行失败"))
            next_id += 1
        if any(x.lower() in low for x in sata_patterns):
            sata_id = f"E{next_id}"
            rules.append(_rule(sata_id, "内核记录 SSD SATA 链路异常", kernel,
                               [_lit(x) for x in sata_patterns if x.lower() in low], source="COMRESET failed"))
            roots.append(_root("SSD SATA 链路异常", sata_id + (f" and not {io_id}" if io_id else ""),
                               actions, "SSD SATA 链路协商或复位失败"))
            next_id += 1
        if "remain life percentage" in low:
            rid = f"E{next_id}"
            pattern = r"169\s+\d+\s+(?:[0-9]|[12][0-9])\s+Remain Life Percentage"
            rules.append(_rule(rid, "SMART 剩余寿命低于 30%", board, [pattern],
                               source="Remain Life Percentage 当前值低于 30"))
            exclusion = "".join(f" and not {x}" for x in (io_id, sata_id) if x)
            roots.append(_root("SSD 寿命耗尽", rid + exclusion, actions,
                               "SMART Remain Life Percentage 原始值低于 30，SSD 剩余寿命不足"))
            next_id += 1
        smart_rules = []
        if "uncorrectable sector count" in low:
            rid = f"E{next_id}"
            pattern = r"160\s+\d+\s+[1-9]\d*\s+Uncorrectable Sector Count"
            rules.append(_rule(rid, "SMART 存在不可修复扇区", board, [pattern],
                               source="Uncorrectable Sector Count 当前值高于 0"))
            smart_rules.append(rid)
            next_id += 1
        if "reallocated sectors count" in low:
            rid = f"E{next_id}"
            pattern = r"5\s+\d+\s+[1-9]\d*\s+Reallocated Sectors Count"
            rules.append(_rule(rid, "SMART 存在重映射扇区", board, [pattern],
                               source="Reallocated Sectors Count 当前值高于 0"))
            smart_rules.append(rid)
        if smart_rules:
            exclusion = "".join(f" and not {x}" for x in (io_id, sata_id) if x)
            roots.append(_root("SSD 扇区介质异常", "(" + " or ".join(smart_rules) + ")" + exclusion,
                               actions, "SMART 报告不可修复或重映射扇区"))
    elif "usb" in low and "flash" in low and "protect_status" in low:
        health = by_name.get("BoardHealth/SMART 日志") or board
        rules.extend([
            _rule("E1", "USB FLASH 设备断开或块设备 I/O 错误", kernel,
                  [_lit("usb 1-1: USB disconnect"), _lit("blk_update_request: critical medium error"),
                   _lit("Buffer I/O error on dev sdb")], source="USB FLASH I/O error"),
            _rule("E2", "USB FLASH 写保护状态非零", health,
                  [r"protect_status\s*[:=]\s*(?!0+(?:\s|$))\d+"], source="protect_status 非 0"),
            _rule("E3", "USB FLASH SMART/识别信息异常", health,
                  [_lit("this flash not support"), _lit("idVendor = 0"), _lit("Manufacturer:(null)")],
                  source="this flash not support / idVendor = 0"),
        ])
        roots.extend([
            _root("USB FLASH 写保护", "E2", actions, "protect_status 非零，介质处于写保护状态"),
            _root("USB FLASH 器件或 I/O 故障", "E1 and not E2", actions,
                  "USB FLASH 断开或块设备出现介质/I/O 错误"),
            _root("USB FLASH SMART 信息获取失败", "E3 and not E2 and not E1", actions,
                  "设备识别或 SMART 信息异常，无法取得有效健康状态"),
        ])
    return rules, roots


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
    """把资料里的“判断思路”映射到已编译规则；无法映射时按日志源分批。"""
    rules = spec.get("evidence_rules") or []
    fault = str(spec.get("fault_name") or "")
    hints: list[tuple[str, list[str]]]
    if "看门狗" in fault:
        hints = [("复位日志", ["E1"]), ("simplelog", ["E1"]), ("watch dog", ["E1"]),
                 ("频率", ["E1"]), ("内核", ["E2"]), ("mce", ["E2"]), ("panic", ["E2"]),
                 ("boardinit", ["E3", "E4"]), ("0x630", ["E3"]), ("0x634", ["E3"]),
                 ("caterr", ["E3"]), ("中断", ["E3"]), ("0x664", ["E4"]),
                 ("0x674", ["E4"]), ("电源", ["E4"]), ("寄存器", ["E3", "E4"])]
    elif re.search(r"SSD", fault, re.I):
        hints = [("内核", ["E1", "E2"]), ("i/o", ["E1"]), ("sata", ["E2"]),
                 ("smart", ["E3", "E4", "E5"]), ("寿命", ["E3"]), ("扇区", ["E4", "E5"])]
    elif re.search(r"USB\s*FLASH", fault, re.I):
        hints = [("写保护", ["E2"]), ("protect_status", ["E2"]),
                 ("i/o", ["E1"]), ("断开", ["E1"]), ("smart", ["E3"])]
    else:
        hints = []
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


def _build_reasoning(spec: dict[str, Any], logic_text: str, threshold_text: str,
                     field_notes: str) -> dict[str, Any]:
    """生成可执行的诊断推理数据：竞争假设、检查顺序、鉴别点、误判陷阱和停止条件。"""
    fault = str(spec.get("fault_name") or "")
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
    if re.search(r"看门狗", fault):
        strategy = "先由 Simplelog 确认确实发生看门狗复位，再用 kernel 判断 MCE/panic，最后用 BoardInit 的 EPLD 寄存器区分 CPU 中断与电源跌落；MCE 优先于寄存器异常，中断优先于电源。"
        domain_pitfalls = [
            "只有告警名或单次复位记录，不能直接判定根因；必须先看复位频率和可用日志源。",
            "0x630/0x634、0x664/0x674 全 0 表示无对应异常，不能把“寄存器存在”当成“寄存器异常”。",
            "caterr 与电源寄存器同时非 0 时，按证据优先级先判 CPU 中断；只有 BoardInit 时也可独立判定，但仍应在报告中提示缺少 Simplelog。",
            "MCE/Kernel panic 若与寄存器异常并存，优先由内核直接证据解释复位。",
        ]
        stop = ["E1+E2 成立：优先判 MCE/Kernel panic，停止再扩根因。",
                "E3 成立且 E2 不成立：判 CPU 中断，停止。",
                "E4 成立且 E2/E3 均不成立：判电源跌落，停止。",
                "所有根因条件不成立：输出 unmatched，不猜测。"]
    elif re.search(r"SSD", fault, re.I):
        strategy = "先找内核里的 I/O 与 SATA 直接错误，再看 BoardInit 的 SMART 退化；直接 I/O/SATA 优先于 SMART，寿命耗尽优先于扇区介质异常。"
        domain_pitfalls = [
            "SATA link down 可能是未使用端口，不能单独当 SSD 故障；要看 COMRESET failed、ATA bus error 等直接错误。",
            "SMART 的 raw_value 是每行第三个数值，不能把 current 值或序号当阈值。",
            "Remain Life Percentage 等于 30 不算低于 30；只有小于 30 才判寿命耗尽。",
            "Uncorrectable/Reallocated 必须大于 0；0 是正常。寿命与扇区同时异常时，优先寿命耗尽。",
        ]
        stop = ["E1 成立：直接判 SSD I/O error，停止扩根因。",
                "E2 成立且 E1 不成立：判 SATA 链路异常，停止。",
                "E3 成立且 E1/E2 不成立：判寿命耗尽，停止。",
                "E4 或 E5 成立且 E1/E2/E3 不成立：判扇区介质异常，停止。"]
    elif re.search(r"USB\s*FLASH", fault, re.I):
        strategy = "先看 protect_status 是否非 0，再看内核是否有该 USB 设备的断开/介质 I/O 错误，最后看 SMART/识别信息是否可取；写保护优先于器件 I/O，器件 I/O 优先于 SMART 获取失败。"
        domain_pitfalls = [
            "protect_status=0 是正常，不能因为字段存在就判写保护。",
            "其他 USB 口（如 usb 1-2）的断开不能直接归因到本 USB FLASH，必须对齐设备实例。",
            "SMART 不支持不等于器件损坏；需结合是否可读写、是否有 I/O 错误判断。",
            "写保护与 I/O 错误同时存在时，先处理写保护，因为它会阻止所有写入。",
        ]
        stop = ["E2 成立：先判写保护；E1 同时成立时在报告中并列提示。",
                "E1 成立且 E2 不成立：判器件或 I/O 故障，停止。",
                "E3 成立且 E1/E2 不成立：判 SMART/识别异常，停止。"]
    else:
        strategy = "先确认现象对应的直接证据，再检查支持规则；只有证据能够区分竞争假设时才下结论，否则输出证据不足。"
        domain_pitfalls = ["告警和文件名只用于路由，不能替代日志证据。",
                           "缺少某个日志源时，不能把未检查当成阴性证据。"]
        stop = ["某一根因条件成立且优先级更高者不成立：停止继续扩展。",
                "所有条件不成立：输出 unmatched，不猜根因。"]
    pitfalls = list(dict.fromkeys(domain_pitfalls))
    data_needed = [str(s.get("name") or "") for s in spec.get("log_sources") or []]
    return {
        "strategy": strategy,
        "decision_order": _reasoning_order(spec, logic_lines),
        "hypotheses": hypotheses,
        "discriminators": discriminators,
        "checks": thresholds,
        "pitfalls": pitfalls,
        "stop_conditions": stop,
        "data_needed": [x for x in data_needed if x],
    }


def parse_source(source_text: str) -> dict[str, Any]:
    text = _clean(source_text)
    fields = _extract_fields(text)
    fault_name = _one_line(fields.get("fault_name")) or "未命名故障"
    aliases = _list(fields.get("aliases"))
    summary = _one_line(fields.get("summary"))
    domain_text = _one_line(fields.get("domain"))
    log_types = _one_line(fields.get("log_types"))
    sources = _log_sources(fields.get("paths", ""), text)
    rules, roots = _domain_spec(text, sources, _list(fields.get("actions")))
    compiler = "domain" if rules else "generic"
    if not rules:
        rules = _generic_rules(fields, sources)
    if not roots and rules:
        causes = _list(fields.get("root_text")) or [fault_name]
        condition = " or ".join(rule["id"] for rule in rules)
        roots = [_root(causes[0], condition, _list(fields.get("actions")), confidence="low")]
    symptoms = {
        "observable": _list(fields.get("observable")),
        "keywords": list(dict.fromkeys(_list(fields.get("keywords")) + [fault_name] + aliases)),
        "lookalikes": [{"fault": _one_line(fields.get("lookalikes")), "differentiator": "待确认"}]
        if _one_line(fields.get("lookalikes")) and _one_line(fields.get("lookalikes")) not in ("无", "无，故障明确") else [],
        "impact": _one_line(fields.get("impact")) or "待补充",
        "time_pattern": _one_line(fields.get("time_pattern")) or "待补充",
        "triggers": _list(fields.get("observable")),
        "exclusions": [],
    }
    # 别名/关键词补全：资料里常写“SSD 故障”，现场却只说 SSD；不补会让目录检索漏掉整个技能。
    extra_aliases: list[str] = []
    extra_keywords: list[str] = []
    joined = fault_name + " " + " ".join(symptoms["keywords"])
    if re.search(r"SSD|固态硬盘", joined, re.I):
        extra_aliases += ["SSD 故障", "SSD故障"]
        extra_keywords += ["SSD", "固态硬盘", "寿命", "SATA"]
    if re.search(r"USB\s*FLASH|USBFLASH", joined, re.I):
        extra_aliases += ["USB FLASH 故障", "USBFLASH 故障"]
        extra_keywords += ["USB FLASH", "USBFLASH", "U盘", "写保护"]
    if "看门狗" in joined:
        extra_aliases += ["看门狗", "看门狗复位"]
        extra_keywords += ["看门狗", "watchdog", "EPLD", "MCE"]
    aliases = list(dict.fromkeys(aliases + extra_aliases))
    symptoms["keywords"] = list(dict.fromkeys(symptoms["keywords"] + extra_keywords))
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
        "category": "单板存储类" if re.search(r"SSD|USB\s*FLASH", fault_name, re.I)
        else ("复位类" if re.search(r"复位|看门狗", fault_name) else "硬件故障"),
        "aliases": aliases,
        "summary": summary,
        "domain": {"device_types": device_types or ["BBU/OSS"], "modes": "待补充",
                   "boards": domain_text or "待补充",
                   "version_scope": domain_text or "待补充",
                   "collection_sources": collection or ["device-tar"]},
        "symptoms": symptoms,
        "log_sources": sources,
        "evidence_rules": rules,
        "root_causes": roots,
        "cases": _parse_cases(text),
        "questions": [],
        "compiler": compiler,
        "recovery": _one_line(fields.get("recovery")),
        "risks": _one_line(fields.get("risks")),
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
    return spec


def _map_cases(spec: dict[str, Any]) -> None:
    roots = spec.get("root_causes") or []
    for case in spec.get("cases") or []:
        if case.get("kind") == "negative":
            case["expected_root_cause"] = "NONE"
            continue
        text = (case.get("expected_text", "") + " " + case.get("description", "")).lower()
        chosen = ""
        priorities = [
            (("mce", "machine check", "kernel panic"), ("mce", "kernel")),
            (("caterr", "cpu报", "中断"), ("cpu", "中断", "caterr")),
            (("vcc", "电源", "跌落"), ("电源", "vcc")),
            (("sata", "comreset"), ("sata",)),
            (("i/o", "io error"), ("i/o",)),
            (("寿命",), ("寿命",)), (("写保护",), ("写保护",)),
            (("smart",), ("smart",)), (("扇区",), ("扇区",)),
        ]
        for needles, root_needles in priorities:
            if any(x in text for x in needles):
                chosen = next((r["id"] for r in roots
                               if any(x in r.get("name", "").lower() for x in root_needles)), "")
                if chosen:
                    break
        if not chosen:
            chosen = next((r["id"] for r in roots if r.get("name", "").lower() in text), "")
        case["expected_root_cause"] = chosen or (roots[0]["id"] if roots else "")


def derive_skill_name(spec: dict[str, Any], explicit: str = "") -> str:
    prefix = "bbu-fault-"
    candidate = _one_line(explicit or spec.get("skill_name")).lower()
    if (candidate.startswith(prefix) and len(candidate) <= 64
            and "--" not in candidate
            and re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", candidate[len(prefix):])):
        return candidate
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
    """AI 只补充语义；空值不覆盖确定性解析结果，规则与根因经归一后再编译。"""
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
    protected = base.get("compiler") == "domain"
    for key in ("log_sources", "evidence_rules", "root_causes", "cases"):
        if protected and key in ("log_sources", "evidence_rules", "root_causes"):
            continue
        if isinstance(ai.get(key), list) and ai[key]:
            merged[key] = ai[key]
    merged["questions"] = ai.get("questions") if isinstance(ai.get("questions"), list) else []
    _normalize_spec(merged)
    _map_cases(merged)
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
    for idx, item in enumerate(spec.get("evidence_rules") or [], 1):
        if not isinstance(item, dict):
            continue
        rid = f"E{idx}"
        used.add(rid)
        any_of = _list(item.get("any_of"))
        all_of = _list(item.get("all_of"))
        if not any_of and not all_of:
            continue
        rules.append({"id": rid, "description": _one_line(item.get("description")) or "故障证据",
                      "globs": _list(item.get("globs")) or default_globs,
                      "all_of": all_of, "any_of": any_of, "none_of": _list(item.get("none_of")),
                      "min_matches": max(1, int(item.get("min_matches") or 1)),
                      "verified": bool(item.get("verified", True)),
                      "source_excerpt": _one_line(item.get("source_excerpt"))})
    # 保留模型条件中的旧 E 编号；仅在 ID 本来就重复/非法时重排。
    original = [x for x in (spec.get("evidence_rules") or []) if isinstance(x, dict)]
    if original and all(re.fullmatch(r"E\d+", str(x.get("id") or "")) for x in original) \
            and len({str(x.get("id")) for x in original}) == len(original):
        for rule, item in zip(rules, original):
            rule["id"] = str(item["id"])
    spec["evidence_rules"] = rules
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
                      "escalation": _one_line(item.get("escalation")) or "待补充"})
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
                      "packages": _list(item.get("packages"))})
    spec["cases"] = cases


def compile_profile(spec: dict[str, Any], skill_name: str) -> dict[str, Any]:
    _normalize_spec(spec)
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
        if not rule.get("verified", True):
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
        issue(warnings, "cases", "文本中没有可识别的故障正例")
        questions.append({"field": "cases", "question": "请补充至少一个故障正例，并写明期望根因和关键证据。", "blocking": False})
    if "negative" not in kinds:
        issue(warnings, "cases", "文本中没有可识别的正常/反例")
        questions.append({"field": "cases", "question": "请补充一个正常或易混淆反例，防止规则误命中。", "blocking": False})
    for item in spec.get("questions") or []:
        if isinstance(item, dict) and _one_line(item.get("question")):
            questions.append({"field": _one_line(item.get("field")) or "general",
                              "question": _one_line(item.get("question")),
                              "blocking": bool(item.get("blocking"))})
    # 去重，限制一次最多四个问题。
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
                       "cases": len(spec.get("cases") or []), "log_sources": len(spec["log_sources"])}}


def public_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """返回给 UI 的规格，避免重复发送大段 raw_fields/source_text。"""
    _normalize_spec(spec)
    return {key: value for key, value in spec.items() if key not in ("raw_fields", "questions")}
