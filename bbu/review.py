"""专家复核、经验隔离和真实案例回流。

- 未复核经验不进入 planner；
- 确认/改判才生成 verified 经验；
- 驳回标记 deprecated，并可作为阴性样本回流；
- 所有复核操作留审计记录，评测量化改判率和置信度校准。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database
from .evaluation import RealCaseStore
from .util import iso_now, new_id

DECISIONS = {"confirm", "correct", "reject"}
ACTION_RESULTS = {"effective", "ineffective", "unknown"}


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) and value else (
            value if value is not None else default)
    except (TypeError, json.JSONDecodeError):
        return default


def _task_roots(result: dict[str, Any], verdict: dict[str, Any]) -> list[str]:
    arbitration = result.get("arbitration") or {}
    roots = arbitration.get("roots") or []
    names = [str((r or {}).get("name") or r).strip() for r in roots if (r or {}).get("name") or r]
    if names:
        return names[:6]
    name = (verdict.get("root_cause_name") or verdict.get("primary")
            or arbitration.get("root_cause_name") or "")
    return [str(name).strip()] if str(name).strip() not in ("", "NONE", "None", "none") else []


class ReviewService:
    def __init__(self, db: Database, data_dir: Path) -> None:
        self.db = db
        self.store = RealCaseStore(db, data_dir)

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM task_reviews WHERE task_id=?", (task_id,))
        return self._decode(row) if row else None

    def submit(self, task: dict[str, Any], user: dict[str, Any], payload: dict[str, Any],
               ip: str = "") -> dict[str, Any]:
        if task.get("status") not in ("done", "needs_data"):
            raise ValueError("任务尚未进入可复核终态")
        decision = str(payload.get("decision") or "").strip()
        if decision not in DECISIONS:
            raise ValueError("decision 必须为 confirm/correct/reject")
        result = _loads(task.get("result_json"), {}) or {}
        verdict = _loads(task.get("verdict_json"), {}) or result.get("verdict") or {}
        arbitration = result.get("arbitration") or {}
        original_roots = _task_roots(result, verdict)
        original_root = "|".join(original_roots)
        original_skill = str(arbitration.get("chosen_skill") or "")
        skill_row = self.db.one("SELECT skill_version FROM task_skills WHERE task_id=? AND skill_name=?",
                                (task["id"], original_skill)) if original_skill else None
        original_version = str((skill_row or {}).get("skill_version") or "")
        corrected_root = str(payload.get("corrected_root") or "").strip()
        corrected_skill = str(payload.get("corrected_skill") or "").strip()[:120]
        comment = str(payload.get("comment") or "").strip()[:4000]
        action_result = str(payload.get("action_result") or "unknown")
        if action_result not in ACTION_RESULTS:
            raise ValueError("action_result 必须为 effective/ineffective/unknown")
        if decision == "confirm" and not original_roots:
            raise ValueError("当前结论没有根因，不能确认；请改判或驳回")
        if decision == "correct" and not corrected_root:
            raise ValueError("改判必须填写修正后的根因")
        if decision == "confirm":
            corrected_root = original_root
            corrected_skill = corrected_skill or original_skill
        promote = bool(payload.get("promote_to_case"))
        if promote and (task.get("inputs_state") or "available") != "available":
            raise ValueError("原始输入未随迁移包导出，不能回流为真实案例；请取消勾选后只保存复核结论")
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        existing = self.get(task["id"])
        now = iso_now()
        review_id = existing["id"] if existing else new_id("RV")
        case_error = ""
        real_case_id = existing.get("real_case_id") if existing else None
        if promote:
            try:
                inputs = [Path(x["stored_path"]) for x in _loads(task.get("input_json"), [])
                          if x.get("stored_path") and Path(x["stored_path"]).exists()]
                if not inputs:
                    raise ValueError("任务输入已被清理，无法回流为真实案例")
                expected = [corrected_root] if decision in ("confirm", "correct") else []
                excluded = original_roots if decision == "reject" else []
                status = "labeled"
                case = self.store.ingest(
                    inputs, name=f"review-{task['id']}",
                    title=f"专家复核：{task['title']}"[:100],
                    description=task["description"],
                    split="dev", status=status, created_by=user["username"],
                    expected_roots=expected, excluded_roots=excluded, origin="review",
                    notes=(f"由任务 {task['id']} 复核生成；decision={decision}；"
                           f"action_result={action_result}；{comment}；"
                           f"labels={json.dumps({'source': 'task_review', 'review_id': review_id, 'decision': decision, 'action_result': action_result}, ensure_ascii=False)}")[:4000])
                real_case_id = case["id"]
            except Exception as exc:  # noqa: BLE001 - 回流失败不能让复核丢失
                case_error = f"{type(exc).__name__}: {exc}"
        values = {
            "reviewer_id": user["id"], "reviewer_name": user["username"],
            "decision": decision, "original_root": original_root,
            "original_skill": original_skill, "original_skill_version": original_version,
            "corrected_root": corrected_root, "corrected_skill": corrected_skill,
            "comment": comment, "action_result": action_result,
            "evidence_json": json.dumps(evidence[:50], ensure_ascii=False, default=str),
            "status": "candidate", "real_case_id": real_case_id,
            "updated_at": now,
        }
        if existing:
            sets = ",".join(f"{k}=?" for k in values)
            self.db.execute(f"UPDATE task_reviews SET {sets} WHERE id=?",
                            (*values.values(), review_id))
        else:
            self.db.execute(
                "INSERT INTO task_reviews(id,task_id,reviewer_id,reviewer_name,decision,"
                "original_root,original_skill,original_skill_version,corrected_root,"
                "corrected_skill,comment,action_result,evidence_json,status,real_case_id,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, task["id"], user["id"], user["username"], decision,
                 original_root, original_skill, original_version, corrected_root,
                 corrected_skill, comment, action_result,
                 json.dumps(evidence[:50], ensure_ascii=False, default=str),
                 "candidate", real_case_id, now, now))
        self._apply_experience(task["id"], review_id, decision, original_root,
                               corrected_root, now)
        if real_case_id and promote:
            self.db.execute("UPDATE task_reviews SET promoted_at=COALESCE(promoted_at,?) WHERE id=?",
                            (now, review_id))
        self.db.audit(user["id"], user["username"], f"task.review.{decision}", task["id"],
                      {"review_id": review_id, "corrected_root": corrected_root,
                       "action_result": action_result, "promoted": bool(real_case_id)}, ip)
        review = self.get(task["id"]) or {}
        if case_error:
            review["case_error"] = case_error
        return review

    def _apply_experience(self, task_id: str, review_id: str, decision: str,
                          original_root: str, corrected_root: str, now: str) -> None:
        trust = "deprecated" if decision == "reject" else "verified"
        row = self.db.one("SELECT id FROM plan_experiences WHERE task_id=?", (task_id,))
        if not row:
            return
        if decision == "reject":
            self.db.execute(
                "UPDATE plan_experiences SET trust_level='deprecated',review_id=?,"
                "corrected_root_name=?,reviewed_at=? WHERE task_id=?",
                (review_id, original_root, now, task_id))
        else:
            self.db.execute(
                "UPDATE plan_experiences SET trust_level='verified',review_id=?,"
                "corrected_root_name=?,root_cause_name=?,reviewed_at=? WHERE task_id=?",
                (review_id, corrected_root, corrected_root, now, task_id))

    def _decode(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**{k: row[k] for k in row.keys()},
                "evidence": _loads(row.get("evidence_json"), [])}

    def quality(self) -> dict[str, Any]:
        terminal = self.db.one(
            "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('done','needs_data','failed')")["n"]
        done = self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE status='done'")["n"]
        reviews = self.db.all("SELECT * FROM task_reviews ORDER BY updated_at DESC")
        reviewed = len(reviews)
        decisions = {d: sum(1 for r in reviews if r["decision"] == d)
                     for d in ("confirm", "correct", "reject")}
        pending = max(0, done - len({r["task_id"] for r in reviews if r["decision"]}))
        correction_rate = ((decisions["correct"] + decisions["reject"]) / reviewed
                           if reviewed else 0.0)
        actionable = [r for r in reviews if r["action_result"] in ("effective", "ineffective")]
        effective_rate = (sum(1 for r in actionable if r["action_result"] == "effective")
                          / len(actionable) if actionable else None)
        # 置信度校准只使用已复核任务；确认=原结论正确，改判/驳回=原结论错误。
        buckets: dict[str, dict[str, int]] = {k: {"total": 0, "correct": 0}
                                             for k in ("high", "medium", "low")}
        for review in reviews:
            task = self.db.one("SELECT verdict_json FROM tasks WHERE id=?", (review["task_id"],))
            verdict = _loads((task or {}).get("verdict_json"), {}) or {}
            conf = str(verdict.get("confidence") or "low").lower()
            bucket = buckets.setdefault(conf, {"total": 0, "correct": 0})
            bucket["total"] += 1
            bucket["correct"] += 1 if review["decision"] == "confirm" else 0
        calibration = {k: {**v, "accuracy": round(v["correct"] / v["total"], 3) if v["total"] else None}
                       for k, v in buckets.items()}
        unknown = self.db.one(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='done' AND verdict_json LIKE '%unknown_fault%'")["n"]
        unmatched = self.db.one(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='done' AND verdict_json LIKE '%unmatched%'")["n"]
        skills: list[dict[str, Any]] = []
        for row in self.db.all(
                "SELECT original_skill,original_skill_version,COUNT(*) AS n,"
                "SUM(CASE WHEN decision='confirm' THEN 1 ELSE 0 END) AS confirmed,"
                "SUM(CASE WHEN decision='correct' THEN 1 ELSE 0 END) AS corrected,"
                "SUM(CASE WHEN decision='reject' THEN 1 ELSE 0 END) AS rejected "
                "FROM task_reviews WHERE original_skill<>'' GROUP BY original_skill,original_skill_version "
                "ORDER BY n DESC LIMIT 12"):
            skills.append({**row,
                           "confirm_rate": round(row["confirmed"] / row["n"], 3) if row["n"] else 0})
        cases = self.db.all(
            "SELECT status,split,origin,COUNT(*) AS n FROM real_cases "
            "GROUP BY status,split,origin")
        case_counts = {f"{r['origin']}/{r['status']}/{r['split']}": r["n"] for r in cases}
        latest_eval = self.db.one(
            "SELECT id,name,split,summary_json,created_at FROM evaluations WHERE status='done' "
            "ORDER BY created_at DESC LIMIT 1")
        latest = ({**{k: latest_eval[k] for k in ("id", "name", "split", "created_at")},
                   "summary": _loads(latest_eval.get("summary_json"), {})}
                  if latest_eval else None)
        golden = sum(r["n"] for r in cases
                     if r["status"] == "golden" and r["origin"] in ("real", "review"))
        case_review = {
            k: self.db.one("SELECT COUNT(*) AS n FROM real_cases WHERE review_status=?", (k,))["n"]
            for k in ("unassigned", "pending", "confirmed", "rejected")}
        test_cases = sum(r["n"] for r in cases
                         if r["split"] == "test" and r["status"] in ("labeled", "golden")
                         and r["origin"] in ("real", "review"))
        real_cases = {
            "labeled": self.db.one("SELECT COUNT(*) AS n FROM real_cases WHERE status IN ('labeled','golden') AND origin IN ('real','review')")["n"],
            "golden": golden,
            "test": test_cases,
            "unlabeled": self.db.one("SELECT COUNT(*) AS n FROM real_cases WHERE status='unlabeled' AND origin='real'")["n"],
        }
        eval_summary = (latest or {}).get("summary") or {}
        exact_rate = float(eval_summary.get("exact_rate") or 0)
        gates = [
            {"name": "已复核真实任务", "current": reviewed, "target": 30,
             "passed": reviewed >= 30, "note": "确认/改判/驳回均计入"},
            {"name": "golden 真实案例", "current": golden, "target": 20,
             "passed": golden >= 20, "note": "需专家确认根因，不能是合成数据"},
            {"name": "独立测试集案例", "current": test_cases, "target": 10,
             "passed": test_cases >= 10, "note": "test/hidden 不参与规则调参"},
            {"name": "真实评测完全命中率", "current": exact_rate, "target": 0.8,
             "passed": bool(latest) and exact_rate >= 0.8, "note": "样本不足时不能对外宣称准确率"},
        ]
        queue_rows = self.db.all(
            "SELECT id,title,status,verdict_json,created_at FROM tasks WHERE status IN ('done','needs_data') "
            "AND id NOT IN (SELECT task_id FROM task_reviews) ORDER BY finished_at DESC LIMIT 12")
        queue = [{"id": r["id"], "title": r["title"], "status": r["status"],
                  "verdict": _loads(r["verdict_json"], {}), "created_at": r["created_at"]}
                 for r in queue_rows]
        return {
            "tasks": {"terminal": terminal, "done": done},
            "reviews": {"total": reviewed, "pending": pending, **decisions,
                        "correction_rate": round(correction_rate, 3)},
            "action_effectiveness": effective_rate,
            "unknown": {"unknown_fault": unknown, "unmatched": unmatched},
            "confidence_calibration": calibration,
            "skills": skills,
            "cases": case_counts,
            "case_review": case_review,
            "real_cases": real_cases,
            "latest_evaluation": latest,
            "maturity_gate": gates,
            "ready_for_real_data": all(g["passed"] for g in gates),
            "review_queue": queue,
            "recent_reviews": reviews[:12],
            "limitations": [
                "质量页只使用已复核任务；未复核结果不计入准确率。",
                "真实案例未达到门槛时，合成回归通过不等于现网成熟。",
                "行动有效性来自人工填写，需与现场恢复记录交叉核对。",
            ],
        }
