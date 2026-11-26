"""案例矩阵与增量候选：把技能结果按根因/版本/证据覆盖聚合成可签名快照。

矩阵只统计已执行完成的结果；签名绑定技能、构建、结果集与兼容范围，
任一变化都会让旧签名失效。专家改判生成候选，不直接改技能。
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from .supply import sign_payload, verify_payload
from .util import new_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "{}") if isinstance(value, str) else (value or default)
    except (TypeError, ValueError):
        return default


def _hash(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def build_matrix(db: Any, skill_name: str = "", build_sha: str = "",
                 compatibility: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = db.all(
        "SELECT ts.task_id,ts.skill_name,ts.skill_version,ts.status,ts.score,"
        "ts.result_json, t.options_json FROM task_skills ts "
        "JOIN tasks t ON t.id=ts.task_id WHERE ts.status NOT IN ('running','failed','incompatible') "
        "AND ts.invalidated=0 "
        + ("AND ts.skill_name=? " if skill_name else "")
        + "ORDER BY ts.finished_at DESC LIMIT 2000",
        (skill_name,) if skill_name else ())
    cells: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, str]] = []
    for row in rows:
        data = _loads(row.get("result_json"), {}) or {}
        verdict = data.get("verdict") or {}
        root = str(verdict.get("root_cause_name") or verdict.get("primary") or "__unmatched__")
        quality = data.get("evidence_quality") or {}
        key = f"{row['skill_name']}|{root}|{row['skill_version']}"
        cell = cells.setdefault(key, {
            "skill": row["skill_name"], "root_cause": root,
            "skill_version": row["skill_version"], "cases": 0,
            "matched": 0, "confidence": Counter(), "quality_total": 0,
            "evidence_rules": Counter(), "task_ids": [],
        })
        cell["cases"] += 1
        cell["matched"] += int(root != "__unmatched__")
        cell["confidence"][str(verdict.get("confidence") or "low")] += 1
        cell["quality_total"] += float(quality.get("quality_score") or 0)
        for ev in (data.get("evidence") or []):
            rule = str(ev.get("rule_id") or "")
            if rule:
                cell["evidence_rules"][rule] += 1
        if len(cell["task_ids"]) < 8:
            cell["task_ids"].append(row["task_id"])
        sources.append({"task_id": row["task_id"], "skill": row["skill_name"],
                        "skill_version": row["skill_version"], "root_cause": root})
    tiles = []
    for key, cell in cells.items():
        tiles.append({
            **{k: v for k, v in cell.items() if k not in ("confidence", "evidence_rules")},
            "confidence": dict(cell["confidence"]),
            "avg_quality": round(cell["quality_total"] / max(1, cell["cases"]), 1),
            "top_evidence": dict(cell["evidence_rules"].most_common(6)),
        })
    tiles.sort(key=lambda x: (-x["cases"], x["root_cause"]))
    payload = {
        "version": "bbu-case-matrix/1",
        "generated_at": _now(),
        "skill_name": skill_name,
        "build_sha": build_sha,
        "compatibility": compatibility or {},
        "summary": {"cells": len(tiles), "cases": len(sources),
                    "roots": sum(1 for x in tiles if x["root_cause"] != "__unmatched__")},
        "tiles": tiles,
        "sources": sources[:500],
    }
    payload["content_sha256"] = _hash(payload)
    return payload


def save_matrix(db: Any, data_dir: Any, payload: dict[str, Any], sign: bool = True) -> dict[str, Any]:
    if sign:
        payload["signature"] = sign_payload(data_dir, {
            k: v for k, v in payload.items() if k != "signature"})
    db.execute("INSERT INTO case_matrix_snapshots(skill_name,payload_json,created_at) "
               "VALUES(?,?,?)",
               (str(payload.get("skill_name") or ""),
                json.dumps(payload, ensure_ascii=False), _now()))
    return payload


def latest_matrix(db: Any, skill_name: str = "") -> dict[str, Any] | None:
    row = db.one("SELECT payload_json FROM case_matrix_snapshots "
                 + ("WHERE skill_name=? " if skill_name else "")
                 + "ORDER BY id DESC LIMIT 1", (skill_name,) if skill_name else ())
    return _loads(row["payload_json"], None) if row else None


def verify_matrix(data_dir: Any, payload: dict[str, Any]) -> dict[str, Any]:
    signature = payload.get("signature") or {}
    body = {k: v for k, v in payload.items() if k != "signature"}
    content_body = {k: v for k, v in body.items() if k != "content_sha256"}
    recomputed = _hash(content_body)
    if payload.get("content_sha256") and payload["content_sha256"] != recomputed:
        return {"ok": False, "reason": "content_tampered"}
    if not signature:
        return {"ok": False, "reason": "unsigned"}
    return {"ok": verify_payload(data_dir, body, signature),
            "reason": "verified" if verify_payload(data_dir, body, signature) else "bad_signature"}


def record_candidate(db: Any, *, task_id: str, skill_name: str, root_cause: str,
                     old_root: str, decision: str, note: str = "",
                     payload: dict[str, Any] | None = None,
                     kind: str = "") -> dict[str, Any]:
    input_hash = _hash({"task": task_id, "skill": skill_name,
                        "root": root_cause, "old": old_root, "decision": decision})
    row = db.one("SELECT * FROM experience_candidates WHERE input_hash=? AND skill_name=? "
                 "AND root_cause_name=?", (input_hash, skill_name, root_cause))
    if row:
        return {**row, "payload": _loads(row.get("payload_json"), {})}
    cid = new_id("EC")
    candidate_kind = kind or ("correction" if decision == "correct" else "rejection")
    body = {"version": "bbu-experience-candidate/1", "task_id": task_id,
            "skill": skill_name, "old_root": old_root, "new_root": root_cause,
            "decision": decision, "kind": candidate_kind, "note": note,
            "payload": payload or {}}
    db.execute("INSERT INTO experience_candidates(id,input_hash,skill_name,root_cause_name,"
               "task_id,kind,payload_json,status,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               (cid, input_hash, skill_name, root_cause, task_id,
                candidate_kind,
                json.dumps(body, ensure_ascii=False), "pending", _now()))
    return {"id": cid, "input_hash": input_hash, "skill_name": skill_name,
            "root_cause_name": root_cause, "task_id": task_id, "status": "pending",
            "kind": candidate_kind, "payload": body}


def decide_candidate(db: Any, candidate_id: str, status: str, reviewer: str,
                     note: str = "") -> dict[str, Any]:
    if status not in ("approved", "rejected"):
        raise ValueError("status 必须为 approved/rejected")
    row = db.one("SELECT * FROM experience_candidates WHERE id=?", (candidate_id,))
    if not row:
        raise ValueError("候选不存在")
    db.execute("UPDATE experience_candidates SET status=?,reviewed_by=?,review_note=?,"
               "reviewed_at=? WHERE id=?", (status, reviewer, note[:2000], _now(), candidate_id))
    return {**row, "status": status, "reviewed_by": reviewer}
