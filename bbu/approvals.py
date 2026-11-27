"""O11 版本 diff、双人审批与紧急发布门禁。"""
from __future__ import annotations

import json
import re
from typing import Any

from .db import Database
from .util import iso_now

SCOPE_RANK = {"associate": 0, "site": 1, "all": 2}
RULE_FIELDS = ("globs", "all_of", "any_of", "none_of", "min_matches", "min_files",
               "file_scope", "scope", "critical")
ROOT_FIELDS = ("condition", "scope", "associate_by", "confidence")


def _rules(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for step in (profile.get("analysis") or {}).get("steps") or []:
        for rule in step.get("rules") or []:
            if isinstance(rule, dict) and rule.get("id"):
                out[str(rule["id"])] = rule
    return out


def _roots(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(x.get("id")): x for x in profile.get("root_causes") or []
            if isinstance(x, dict) and x.get("id")}


def _condition_refs(condition: str) -> tuple[set[str], set[str]]:
    positive = set(re.findall(r"(?<!not )\b[A-Z]\d+\b", condition or ""))
    negative = set(re.findall(r"\bnot\s+([A-Z]\d+)\b", condition or ""))
    return positive, negative


def diff_profiles(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_rules, new_rules = _rules(old), _rules(new)
    removed = sorted(set(old_rules) - set(new_rules))
    added = sorted(set(new_rules) - set(old_rules))
    changed = []
    for rid in sorted(set(old_rules) & set(new_rules)):
        fields = {}
        for key in RULE_FIELDS:
            before, after = old_rules[rid].get(key), new_rules[rid].get(key)
            if before != after:
                fields[key] = {"old": before, "new": after}
        if fields:
            changed.append({"rule_id": rid, "fields": fields})
    old_roots, new_roots = _roots(old), _roots(new)
    root_changes = []
    for rid in sorted(set(old_roots) | set(new_roots)):
        if rid not in old_roots:
            root_changes.append({"root_id": rid, "change": "added",
                                 "condition": new_roots[rid].get("condition"),
                                 "scope": new_roots[rid].get("scope") or "all"})
            continue
        if rid not in new_roots:
            root_changes.append({"root_id": rid, "change": "removed"})
            continue
        fields = {}
        for key in ROOT_FIELDS:
            before, after = old_roots[rid].get(key), new_roots[rid].get(key)
            if before != after:
                fields[key] = {"old": before, "new": after}
        if fields:
            root_changes.append({"root_id": rid, "change": "changed", "fields": fields})
    temporal = {"old": old.get("temporal") or [], "new": new.get("temporal") or []}
    sources = {"old": old.get("required_sources") or [], "new": new.get("required_sources") or []}
    risks = []
    for rid in added:
        patterns = [str(x) for x in (new_rules[rid].get("all_of") or []) +
                    (new_rules[rid].get("any_of") or [])]
        if any(p.strip() in ("", ".*", "^.*$", ".+", "^.+$") or len(p.strip()) < 3
               for p in patterns):
            risks.append({"level": "red", "code": "ADDED_OVERBROAD_RULE", "rule_id": rid,
                          "message": "新增规则过宽或模式过短，可能误命中"})
    removed_refs: set[str] = set()
    for root in old_roots.values():
        removed_refs |= _condition_refs(str(root.get("condition") or ""))[1]
    for rid in removed:
        if rid in removed_refs:
            risks.append({"level": "red", "code": "REMOVED_NEGATIVE_RULE", "rule_id": rid,
                          "message": "删除了旧根因依赖的反证规则"})
    for change in root_changes:
        old_scope = (old_roots.get(change["root_id"]) or {}).get("scope") or "all"
        new_scope = (new_roots.get(change["root_id"]) or {}).get("scope") or "all"
        if SCOPE_RANK.get(new_scope, 2) > SCOPE_RANK.get(old_scope, 2):
            risks.append({"level": "red", "code": "SCOPE_WIDENED", "root_id": change["root_id"],
                          "message": f"根因作用域从 {old_scope} 扩大到 {new_scope}"})
    level = "red" if any(x["level"] == "red" for x in risks) else (
        "yellow" if risks else "green")
    return {"ok": True, "rules": {"added": added, "removed": removed, "changed": changed},
            "root_causes": root_changes, "temporal": temporal,
            "required_sources": sources, "risks": risks, "level": level}


class ApprovalStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, skill: str, version: str, approver_id: int, approver_name: str,
               decision: str = "approve", reason: str = "") -> dict[str, Any]:
        if decision not in ("approve", "reject"):
            raise ValueError("decision 必须是 approve/reject")
        if decision == "reject" and not str(reason).strip():
            raise ValueError("拒绝必须填写理由")
        self.db.execute(
            "INSERT INTO skill_approvals(skill,version,approver_id,approver_name,decision,"
            "reason,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(skill,version,approver_id) "
            "DO UPDATE SET decision=excluded.decision,reason=excluded.reason,"
            "created_at=excluded.created_at",
            (skill, version, approver_id, approver_name, decision, reason, iso_now()))
        return {"ok": True, "skill": skill, "version": version,
                "approver_id": approver_id, "decision": decision}

    def status(self, skill: str, version: str, required: int = 2) -> dict[str, Any]:
        rows = self.db.all(
            "SELECT * FROM skill_approvals WHERE skill=? AND version=? ORDER BY created_at",
            (skill, version))
        approves = [r for r in rows if r["decision"] == "approve"]
        rejects = [r for r in rows if r["decision"] == "reject"]
        distinct = len({int(r["approver_id"]) for r in approves})
        return {"skill": skill, "version": version, "required": required,
                "approvals": len(approves), "distinct": distinct,
                "rejects": len(rejects), "records": rows,
                "ok": not rejects and distinct >= required}


def publish_gate(*, channel: str = "stable", approvals: list[dict[str, Any]] | None = None,
                 emergency_reason: str = "", actor: str = "") -> dict[str, Any]:
    approvals = approvals or []
    distinct = len({str(x.get("approver_id") or x.get("username") or "") for x in approvals})
    reason = str(emergency_reason or "").strip()
    if channel == "stable" and distinct < 2:
        if not reason:
            return {"ok": False, "status": 409, "code": "APPROVALS_REQUIRED",
                    "message": "stable 渠道发布至少需要 2 名不同管理员批准"}
        if len(reason) < 20:
            return {"ok": False, "status": 400, "code": "EMERGENCY_REASON_SHORT",
                    "message": "紧急发布理由至少 20 字"}
        return {"ok": True, "emergency": True, "actor": actor,
                "follow_up_within_hours": 24}
    if channel not in ("stable", "canary"):
        return {"ok": False, "status": 400, "code": "BAD_CHANNEL",
                "message": "channel 必须是 stable/canary"}
    return {"ok": True, "emergency": False, "approvals": distinct}
