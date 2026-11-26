"""只读恢复验证：现场回传前后状态，不改设备、不发命令。

恢复结论必须同时满足：同一任务、同一故障对象、前后证据可对照、状态分类为
recovered，且有独立恢复证据和专家复核；否则学习门禁不通过。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .evidence_model import EvidenceEvent, content_hash
from .evidence_store import EvidenceStore
from .util import iso_now, new_id

RESULTS = ("recovered", "still_fault", "new_fault", "inconclusive")


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "{}") if isinstance(value, str) else (value or default)
    except (TypeError, ValueError):
        return default


def _root(payload: dict[str, Any]) -> str:
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else payload
    return str(verdict.get("root_cause_name") or verdict.get("primary") or "")


def _evidence(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    return [x for x in rows if isinstance(x, dict)]


def _identity(payload: dict[str, Any]) -> tuple[str, str]:
    task_id = str(payload.get("task_id") or "")
    object_id = str(payload.get("device_id") or payload.get("case_id")
                    or payload.get("object_id") or "")
    return task_id, object_id


def _independent_files(payload: dict[str, Any]) -> set[str]:
    rows = _evidence(payload)
    return {str(x.get("file") or x.get("source") or "")
            for x in rows if x.get("file") or x.get("source")}


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """确定性前后对照；恢复必须由恢复后的独立证据支撑。"""
    b_root, a_root = _root(before), _root(after)
    b_ev = {str(x.get("rule_id")): x for x in _evidence(before) if x.get("rule_id")}
    a_ev = {str(x.get("rule_id")): x for x in _evidence(after) if x.get("rule_id")}
    b_task, b_object = _identity(before)
    a_task, a_object = _identity(after)
    if b_task or a_task:
        same_case = bool(b_task and a_task and b_task == a_task)
    elif b_object or a_object:
        same_case = bool(b_object and a_object and b_object == a_object)
    else:
        # compare 的调用方已把前后观测绑定到同一恢复任务；没有显式对象 ID 时
        # 只能依赖该调用上下文，不能假装拿到了对象一致性证明。
        same_case = bool(before and after)
    before_files = _independent_files(before)
    after_files = _independent_files(after)
    new_files = sorted(after_files - before_files)
    independent_after = sum(
        1 for item in _evidence(after)
        if str(item.get("file") or item.get("source") or "") in new_files)
    if not before or not after:
        status = "inconclusive"
    elif b_root and a_root and b_root == a_root:
        status = "still_fault"
    elif b_root and not a_root:
        status = ("recovered"
                  if same_case and independent_after >= 1 else "inconclusive")
    elif not b_root and a_root:
        status = "new_fault" if same_case else "inconclusive"
    else:
        status = "inconclusive"
    changed = []
    for rid in sorted(set(b_ev) | set(a_ev)):
        b = b_ev.get(rid) or {}
        a = a_ev.get(rid) or {}
        if bool(b.get("hit")) != bool(a.get("hit")) or str(b.get("quality")) != str(a.get("quality")):
            changed.append({"rule_id": rid, "before": b.get("hit"), "after": a.get("hit")})
    return {"status": status, "before_root": b_root, "after_root": a_root,
            "before_evidence": len(b_ev), "after_evidence": len(a_ev),
            "changed": changed[:50],
            "after_independent_evidence": independent_after,
            "after_independent_files": new_files[:20],
            "same_case": same_case,
            "same_case_source": "explicit" if (b_task or a_task) else "caller_task",
            "version": "bbu-recovery-compare/1"}


def _recovery_event(db: Any, run_id: str, task_id: str,
                    comparison: dict[str, Any]) -> str:
    """把一次恢复后对照写成平台生成的 EvidenceEvent，可审计、可重建。"""
    excerpt = ("恢复验证："
               f"status={comparison.get('status')}；"
               f"before_root={comparison.get('before_root') or '-'}；"
               f"after_root={comparison.get('after_root') or '-'}；"
               f"independent_files={comparison.get('after_independent_evidence', 0)}")
    digest = content_hash(excerpt)
    event_id = "EV-" + hashlib.sha256(
        f"{task_id}|{run_id}|{digest}".encode("utf-8")).hexdigest()[:24]
    source_path = str((comparison.get("after_independent_files") or [""])[0])
    event = EvidenceEvent(
        evidence_id=event_id, session_id=task_id,
        source={"tool": "recovery.verifier", "tool_version": "1",
                "path": source_path, "path_basename": source_path.rsplit("/", 1)[-1],
                "line_start": None, "line_end": None, "file_sha256": "",
                "archive_member": ""},
        entity={"component": "recovery", "device_type": ""},
        event_time={"value": iso_now(), "inferred": False},
        kind="manual", name="recovery.verification",
        value={"status": comparison.get("status"),
               "before_root": comparison.get("before_root"),
               "after_root": comparison.get("after_root"),
               "independent_files": comparison.get("after_independent_files") or []},
        excerpt=excerpt,
        parser={"name": "recovery-compare", "version": "1", "confidence": 1.0},
        trust={"origin": "platform_validation", "contains_instructions": False},
        content_sha256=digest,
    )
    EvidenceStore(db).append(event)
    return event_id



def create_run(db: Any, task_id: str, before: dict[str, Any], requested_by: str,
               source: str = "readonly_receipt") -> dict[str, Any]:
    run_id = new_id("RR")
    before = dict(before or {})
    before.setdefault("task_id", task_id)
    db.execute("INSERT INTO recovery_runs(id,task_id,status,before_json,source,requested_by,created_at) "
               "VALUES(?,?,?,?,?,?,?)",
               (run_id, task_id, "pending", json.dumps(before, ensure_ascii=False),
                source[:60], requested_by, iso_now()))
    return get_run(db, run_id)


def get_run(db: Any, run_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM recovery_runs WHERE id=?", (run_id,))
    if not row:
        raise ValueError("恢复验证不存在")
    return {**row, "before": _loads(row.get("before_json"), {}),
            "after": _loads(row.get("after_json"), {}),
            "comparison": _loads(row.get("comparison_json"), {})}


def observe(db: Any, run_id: str, after: dict[str, Any], reviewer: str = "") -> dict[str, Any]:
    run = get_run(db, run_id)
    if run["status"] == "reviewed":
        raise ValueError("已复核的恢复验证不可覆盖")
    after = dict(after or {})
    after.setdefault("task_id", run["task_id"])
    comparison = compare(run["before"], after)
    comparison["evidence_event_id"] = _recovery_event(db, run_id, run["task_id"], comparison)
    status = "observed"
    db.execute("UPDATE recovery_runs SET status=?,after_json=?,comparison_json=?,observed_at=? "
               "WHERE id=?", (status, json.dumps(after, ensure_ascii=False),
                              json.dumps(comparison, ensure_ascii=False), iso_now(), run_id))
    return get_run(db, run_id)


def review(db: Any, run_id: str, reviewer: str, decision: str,
           note: str = "") -> dict[str, Any]:
    """专家确认恢复证据；只有 recovered + 同对象 + 独立证据 + 复核才允许学习。"""
    if decision not in ("confirmed", "rejected"):
        raise ValueError("decision 必须为 confirmed/rejected")
    run = get_run(db, run_id)
    comparison = run["comparison"] or {}
    independent = int(comparison.get("after_independent_evidence") or 0)
    eligible = bool(decision == "confirmed" and comparison.get("status") == "recovered"
                    and comparison.get("same_case")
                    and independent >= 1)
    status = "reviewed" if decision == "confirmed" else "rejected"
    db.execute("UPDATE recovery_runs SET status=?,reviewed_by=?,learning_eligible=?,reviewed_at=? "
               "WHERE id=?", (status, reviewer, int(eligible), iso_now(), run_id))
    result = get_run(db, run_id)
    result["review_note"] = note[:2000]
    result["learning_gate"] = {
        "eligible": eligible,
        "requires": ["status=recovered", "same_case", "independent_recovery_evidence>=1",
                     "expert_confirmed"],
        "actual": {"status": comparison.get("status"),
                   "same_case": comparison.get("same_case"),
                   "independent": independent, "decision": decision},
    }
    return result


def list_runs(db: Any, task_id: str) -> list[dict[str, Any]]:
    return [get_run(db, r["id"]) for r in
            db.all("SELECT id FROM recovery_runs WHERE task_id=? ORDER BY created_at DESC LIMIT 50",
                   (task_id,))]
