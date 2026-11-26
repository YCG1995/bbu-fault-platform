"""根因决策卡：必要证据、反证、近邻鉴别和适用域。

缺扫描 ≠ 反证：只有“已扫描到该日志源但规则阴性/none_of 命中”才记为反证；
没有对应日志源时统一为 missing_data。卡片只解释已执行技能的证据，不替代仲裁。
"""
from __future__ import annotations

import re
from typing import Any


def _rule_ids(condition: str) -> tuple[list[str], list[str]]:
    positive = re.findall(r"(?<!not )\bE\d+\b", condition or "")
    negative = re.findall(r"\bnot\s+(E\d+)\b", condition or "")
    return list(dict.fromkeys(positive)), list(dict.fromkeys(negative))


def build_decision_cards(skill: Any, result: dict[str, Any] | None) -> list[dict[str, Any]]:
    profile = getattr(skill, "profile", {}) or {}
    result = result or {}
    evidence = result.get("evidence") or []
    present = {str(x.get("rule_id") or "") for x in evidence if x.get("rule_id")}
    rules = {str(r.get("id") or ""): r for r in profile.get("evidence_rules") or []}

    def site_of(ev: dict[str, Any]) -> str:
        path = str((ev.get("source") or {}).get("path") or ev.get("file") or "").replace("\\", "/")
        return path.split("/", 1)[0] if "/" in path else "default"

    by_rule_site: dict[str, set[str]] = {}
    for ev in evidence:
        rid = str(ev.get("rule_id") or "")
        if rid:
            by_rule_site.setdefault(rid, set()).add(site_of(ev))
    cards: list[dict[str, Any]] = []
    for root in profile.get("root_causes") or []:
        condition = str(root.get("condition") or "")
        positive, negative = _rule_ids(condition)
        missing = [x for x in positive if x not in present]
        contradicted = [x for x in negative if x in present]
        state = "supported" if positive and not missing and not contradicted \
            else ("contradicted" if contradicted else
                  ("missing_data" if missing else "unknown"))
        site_matrix = []
        sites = sorted({site for rule_sites in by_rule_site.values() for site in rule_sites})
        for site in sites:
            hits = [rid for rid in positive if site in by_rule_site.get(rid, set())]
            missing_site = [rid for rid in positive if rid not in hits]
            site_matrix.append({"site": site, "hit_rules": hits,
                                "missing_rules": missing_site,
                                "state": "supported" if hits and not missing_site
                                else ("partial" if hits else "missing_data")})
        cards.append({
            "root_id": root.get("id"), "root_cause": root.get("name"),
            "scope": root.get("scope") or "all",
            "associate_by": root.get("associate_by") or [],
            "site_evidence": site_matrix,
            "condition": condition,
            "necessary_evidence": [{"rule": rid, "present": rid in present,
                                    "description": (rules.get(rid) or {}).get("description", "")}
                                   for rid in positive],
            "counter_evidence": [{"rule": rid, "present": rid in present,
                                  "description": (rules.get(rid) or {}).get("description", "")}
                                 for rid in negative],
            "near_miss": [
                {"fault": x.get("fault"), "differentiator": x.get("differentiator")}
                for x in (profile.get("symptoms") or {}).get("lookalikes") or []
                if isinstance(x, dict)
            ][:4],
            "applicability": {
                "device_types": (profile.get("domain") or {}).get("device_types") or [],
                "version_scope": (profile.get("domain") or {}).get("version_scope") or "待补充",
            },
            "state": state,
            "missing_evidence": missing,
            "contradicted_by": contradicted,
            "limitations": (["缺少日志源/扫描覆盖不足时，不得把 missing_data 当作反证"]
                            if missing else []),
        })
    return cards


def decision_summary(cards: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cards": len(cards),
        "supported": sum(1 for c in cards if c.get("state") == "supported"),
        "contradicted": sum(1 for c in cards if c.get("state") == "contradicted"),
        "missing_data": sum(1 for c in cards if c.get("state") == "missing_data"),
    }
