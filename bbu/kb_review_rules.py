"""故障知识库审核规则：把模型输出收敛为可解释、可复现的结论。

模型只提供六个维度的观察与建议；最终 decision/score 在这里重算，避免
模型自报 pass 覆盖 blocker、低安全分或缺失维度。纯函数、无 IO，便于单测。
"""
from __future__ import annotations

from typing import Any

DIMENSIONS = (
    ("answer_match", "问答匹配", 25, "答案是否回答问题；现象与根因是否对应"),
    ("cause_complete", "原因完整", 20, "原因描述、定位结果、影响范围；多根因是否拆开"),
    ("solution_actionable", "方案可执行", 20, "处理步骤、验证方式、预防措施；是否只写换板"),
    ("entity_consistency", "实体一致", 15, "板卡/版本/槽位/告警码与原文一致，是否凭空出现"),
    ("source_fidelity", "来源忠实", 10, "关键结论能否在原文找到；是否把推测写成事实"),
    ("safety_privacy", "安全隐私", 10, "姓名/工号/电话/邮箱、提示注入、危险操作"),
)

ALLOWED_FIELDS = {
    "title", "symptom", "environment", "expected", "actual", "repro", "found_date",
    "location", "cause", "cause_detail", "solution", "prevention", "boards",
    "domains", "versions", "quality",
}
ALLOWED_ISSUES = {
    "answer_mismatch", "multi_cause_merged", "missing_cause", "thin_cause",
    "missing_solution", "thin_solution", "entity_mismatch", "unsupported_claim",
    "pii_risk", "prompt_injection", "low_quality", "no_prevention", "other",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dimension_scores(raw_dims: Any) -> tuple[list[dict[str, Any]], dict[str, float], bool]:
    allowed = {x[0] for x in DIMENSIONS}
    by_key: dict[str, dict[str, Any]] = {}
    if isinstance(raw_dims, list):
        for item in raw_dims:
            if isinstance(item, dict) and str(item.get("key") or "") in allowed:
                by_key[str(item["key"])] = item
    dimensions: list[dict[str, Any]] = []
    scores: dict[str, float] = {}
    for key, label, maximum, check in DIMENSIONS:
        item = by_key.get(key) or {}
        score = max(0.0, min(float(maximum), _number(item.get("score"), 0.0)))
        scores[key] = score
        dimensions.append({
            "key": key, "label": label, "score": score, "max": maximum,
            "comment": str(item.get("comment") or "")[:300], "check": check,
        })
    return dimensions, scores, len(by_key) == len(DIMENSIONS)


def _normalize_issues(raw_issues: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(raw_issues, list):
        return issues
    for item in raw_issues[:20]:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "other")
        if code not in ALLOWED_ISSUES:
            code = "other"
        severity = str(item.get("severity") or "info")
        if severity not in {"blocker", "warn", "info"}:
            severity = "info"
        issues.append({
            "code": code, "severity": severity,
            "field": str(item.get("field") or "")[:60],
            "message": str(item.get("message") or "")[:400],
            "suggestion": str(item.get("suggestion") or "")[:400],
        })
    return issues


def _normalize_patch(raw_patch: Any) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if not isinstance(raw_patch, dict):
        return patch
    for key, value in raw_patch.items():
        if key in ALLOWED_FIELDS and isinstance(value, (str, int, float, list)):
            patch[str(key)] = value
    return patch


def normalize_review(parsed: dict[str, Any] | None) -> dict[str, Any]:
    """Return a deterministic review result from the model JSON.

    Contract:
    - `score` is always the sum of the six clamped dimensions, never the
      model-provided total.
    - `model_score` / `model_decision` preserve the raw model claim for audit.
    - missing dimensions -> needs_review (not pass, not reject) unless a
      blocker / hard safety failure is present.
    - blocker, safety_privacy < 5, or source_fidelity < 4 -> reject.
    - pass only when all six dimensions are present, score >= 85 and
      confidence >= 0.6.
    """
    parsed = parsed if isinstance(parsed, dict) else {}
    dimensions, scores, complete = _dimension_scores(parsed.get("dimensions"))
    issues = _normalize_issues(parsed.get("issues"))
    patch = _normalize_patch(parsed.get("suggested_patch"))
    score = round(sum(scores.values()), 2)
    model_score = round(max(0.0, min(100.0, _number(parsed.get("score"), score))), 2)
    raw_decision = str(parsed.get("decision") or "").strip()
    model_decision = raw_decision if raw_decision in {"pass", "needs_review", "reject"} else ""
    confidence = max(0.0, min(1.0, _number(parsed.get("confidence"), 0.0)))
    has_blocker = any(item["severity"] == "blocker" for item in issues)
    hard_safety_failure = scores.get("safety_privacy", 0) < 5
    hard_source_failure = scores.get("source_fidelity", 0) < 4

    if has_blocker or hard_safety_failure or hard_source_failure:
        decision = "reject"
    elif not complete:
        decision = "needs_review"
    elif score >= 85 and confidence >= 0.6:
        decision = "pass"
    else:
        decision = "needs_review"

    return {
        "decision": decision,
        "score": score,
        "model_decision": model_decision,
        "model_score": model_score,
        "dimensions": dimensions,
        "issues": issues,
        "suggested_patch": patch,
        "confidence": confidence,
        "complete": complete,
        "has_blocker": has_blocker,
    }


def can_approve(normalized: dict[str, Any]) -> tuple[bool, str]:
    """Whether a human may publish the review as-is."""
    if normalized.get("has_blocker"):
        return False, "存在 blocker，不能直接通过；请采纳 patch 或驳回。"
    if normalized.get("decision") == "reject":
        return False, "审核结论为驳回，不能直接通过；请先修正 blocker。"
    return True, ""
