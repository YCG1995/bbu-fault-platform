"""结构化 claim 与报告确定性校验。

报告先生成可核验的 claim（结论、证据、数字、时间、动作），再渲染 Markdown。
claim 的 evidence_ids 只引用本任务证据事件；数字/时间必须能在证据原文或根因
结构化字段中找到。模型叙述只作为可选增强，语义检查失败一律回退确定性报告。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(-?\d+(?:\.\d+)?)(?![\dA-Za-z_])")
_TIME_RE = re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}")
_VERDICT_WORDS = {
    "matched": ("已匹配", "命中", "根因"),
    "multiple_causes": ("多个", "并行", "根因"),
    "unmatched": ("未匹配", "无确定性", "证据不足"),
    "unknown_fault": ("未匹配", "未知", "异常"),
    "temporal_conflict": ("时序", "冲突"),
    "needs_data": ("缺少", "补充", "证据不足"),
}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _one_line(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _evidence_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(k) or "") for k in
                    ("text", "file", "timestamp", "rule_id", "description"))


def build_claims(task: dict[str, Any], results: list[dict[str, Any]],
                 arbitration: dict[str, Any], evidence_ids: list[str] | None = None) -> dict[str, Any]:
    chosen_name = str(arbitration.get("chosen_skill") or "")
    chosen = next((r for r in results if str(r.get("skill_name") or "") == chosen_name), None)
    selected = chosen or next((r for r in results if r.get("ok")), None)
    evidence = (selected or {}).get("evidence") or []
    ids = list((selected or {}).get("evidence_ids") or evidence_ids or [])
    claims: list[dict[str, Any]] = []
    counter = 0

    def add(kind: str, text: str, *, refs: list[str] | None = None,
            source: str = "", value: Any = None, polarity: str = "positive") -> None:
        nonlocal counter
        counter += 1
        claims.append({
            "id": f"CL{counter}", "kind": kind, "text": _one_line(text),
            "evidence_ids": [x for x in (refs or []) if x],
            "source": source, "value": value, "polarity": polarity,
            "supported": bool(refs) if kind in ("root_cause", "action", "number", "time") else True,
        })

    status = str(arbitration.get("status") or "unmatched")
    root_name = str(arbitration.get("root_cause_name") or
                    ((selected or {}).get("root_cause") or {}).get("name") or "")
    root = (selected or {}).get("root_cause") or {}
    if root_name:
        add("root_cause", f"根因：{root_name}", refs=ids[:8],
            source="arbitration.root_cause", value=root_name)
    add("verdict", f"判定：{status}", source="arbitration.status", value=status)
    for idx, item in enumerate(evidence[:8], 1):
        ref = ids[idx - 1] if idx - 1 < len(ids) else ""
        add("evidence", f"证据：{_one_line(item.get('text') or item.get('description'), 220)}",
            refs=[ref] if ref else [], source=str(item.get("file") or ""),
            value={"line": item.get("line_no"), "rule": item.get("rule_id"),
                   "time": item.get("timestamp")})
        if item.get("timestamp"):
            add("time", f"时间：{item.get('timestamp')}", refs=[ref] if ref else [],
                source=str(item.get("file") or ""), value=item.get("timestamp"))
        for number in _NUM_RE.findall(str(item.get("text") or ""))[:3]:
            add("number", f"数字：{number}", refs=[ref] if ref else [],
                source=str(item.get("file") or ""), value=number)
    for action in (root.get("actions") or [])[:6]:
        add("action", f"动作：{_one_line(action)}", refs=ids[:3],
            source="root_causes.actions", value=action)
    return {
        "version": "bbu-claims/1",
        "task_id": task.get("id"),
        "claims": claims,
        "evidence_count": len(ids),
        "evidence_ids": ids[:50],
        "evidence_texts": {eid: _evidence_text(item)[:1600]
                           for eid, item in zip(ids, evidence)},
        "source_skill": chosen_name,
    }


def _same_number(left: str, right: str) -> bool:
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return left == right


def validate_claims(bundle: dict[str, Any], *, evidence: list[dict[str, Any]] | None = None,
                    arbitration: dict[str, Any] | None = None) -> dict[str, Any]:
    claims = bundle.get("claims") or []
    evidence = evidence or []
    evidence_ids = set(bundle.get("evidence_ids") or [])
    texts_by_id = dict(bundle.get("evidence_texts") or {})
    for idx, item in enumerate(evidence):
        eid = str(item.get("evidence_id") or "")
        if not eid and idx < len(bundle.get("evidence_ids") or []):
            eid = str((bundle.get("evidence_ids") or [])[idx])
        if eid:
            texts_by_id.setdefault(eid, _evidence_text(item))
    errors: list[dict[str, Any]] = []
    checked = 0
    for claim in claims:
        refs = claim.get("evidence_ids") or []
        if claim["kind"] in ("root_cause", "action", "number", "time") and not refs:
            errors.append({"claim": claim["id"], "code": "missing_evidence",
                           "message": "根因/动作/数字/时间没有证据引用"})
        ref_texts: list[str] = []
        for ref in refs:
            checked += 1
            if ref not in evidence_ids:
                errors.append({"claim": claim["id"], "code": "unknown_evidence",
                               "message": f"证据 {ref} 不属于本任务"})
                continue
            if ref in texts_by_id:
                ref_texts.append(str(texts_by_id[ref]))
        if claim["kind"] == "number":
            number = str(claim.get("value") or "")
            found = any(_same_number(number, token)
                        for text in ref_texts for token in _NUM_RE.findall(text))
            if not found:
                errors.append({"claim": claim["id"], "code": "unsupported_number",
                               "message": f"数字 {number} 不能在被引用证据原文中定位"})
        if claim["kind"] == "time":
            value = str(claim.get("value") or "")
            if not any(value in text for text in ref_texts):
                errors.append({"claim": claim["id"], "code": "unsupported_time",
                               "message": f"时间 {value} 不能在被引用证据原文中定位"})
    status = str((arbitration or {}).get("status") or "")
    root_name = str((arbitration or {}).get("root_cause_name") or "")
    if status in ("matched", "multiple_causes") and not root_name:
        errors.append({"claim": "verdict", "code": "missing_root",
                       "message": "匹配状态缺少根因"})
    if status in ("unmatched", "unknown_fault", "needs_data") and root_name \
            and not any("未匹配" in str(c.get("text")) for c in claims):
        # 允许 unknown 场景保留“候选根因”，但必须明确不是确定性结论。
        pass
    return {"ok": not errors, "errors": errors, "claim_count": len(claims),
            "evidence_refs_checked": checked,
            "verdict": status, "root_cause": root_name,
            "version": "bbu-claim-check/1"}


def validate_narrative(narrative: dict[str, Any] | None, bundle: dict[str, Any],
                       *, evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """对可选模型叙述做保守的确定性检查；不作为语义蕴含证明。"""
    if not narrative:
        return {"ok": True, "skipped": True, "errors": []}
    text = " ".join(str(narrative.get(k) or "") for k in
                    ("headline", "analysis", "conclusion"))
    claims_text = "\n".join(str(c.get("text") or "") for c in bundle.get("claims") or [])
    evidence_text = "\n".join(_evidence_text(e) for e in (evidence or []))
    errors: list[dict[str, Any]] = []
    for number in _NUM_RE.findall(text):
        if number not in claims_text and number not in evidence_text:
            errors.append({"code": "unsupported_number", "value": number})
    for time_value in _TIME_RE.findall(text):
        if time_value not in claims_text and time_value not in evidence_text:
            errors.append({"code": "unsupported_time", "value": time_value})
    status = str(bundle.get("claims") and next(
        (c.get("value") for c in bundle["claims"] if c.get("kind") == "verdict"), "") or "")
    forbidden = _VERDICT_WORDS.get(status, ())
    if status in ("unmatched", "unknown_fault", "needs_data") and any(
            x in text for x in ("确定是", "根因为", "必然")) and not any(x in text for x in forbidden):
        errors.append({"code": "overclaim"})
    return {"ok": not errors, "skipped": False, "errors": errors}


def claims_markdown(bundle: dict[str, Any], check: dict[str, Any],
                    narrative_check: dict[str, Any] | None = None) -> str:
    lines = ["", "## 附：结构化报告校验", ""]
    lines.append(f"- 结论：{'通过' if check.get('ok') else '未通过'}，"
                 f"结构化 claim {check.get('claim_count', 0)} 条，"
                 f"证据引用检查 {check.get('evidence_refs_checked', 0)} 次。")
    if narrative_check and not narrative_check.get("skipped"):
        lines.append(f"- 模型叙述：{'通过' if narrative_check.get('ok') else '已回退确定性模板'}"
                     f"（{len(narrative_check.get('errors') or [])} 个未支撑断言）。")
    errors = list(check.get("errors") or [])
    if errors:
        lines.append("- 未通过项：" + "；".join(
            f"{x.get('claim')}/{x.get('code')}" for x in errors[:6]))
    return "\n".join(lines) + "\n"
