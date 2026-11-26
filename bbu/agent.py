"""有界诊断 Agent 的固定动作空间与停止条件。

这里不实现开放式自主规划：动作只有“选择已注册技能、执行、补数据、一次
重规划、停止”，不暴露 shell、任意 URL、任意文件写或自动发布。新引擎先
shadow 记录决策，不改变 v1 执行；只有显式 bounded/canary 模式才作为门禁。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .util import iso_now, new_id

ACTION_SPACE = {
    "select_registered_skill", "execute_skill", "request_more_data",
    "replan_once", "stop",
}
GATE_VALUES = {"always", "on_hit", "on_miss", "evidence_sufficient", "confidence_low"}
FORBIDDEN = re.compile(r"(shell|subprocess|curl|wget|http://|https://|任意文件|自动发布|"
                       r"rm -rf|powershell|bash -c)", re.I)


class BoundedAgent:
    def __init__(self, db: Any, config: Any) -> None:
        self.db = db
        self.config = config
        self.mode = str(config.get("agent_engine_mode", "shadow")).lower()

    def _limits(self) -> tuple[int, int, int]:
        return (max(1, int(self.config.get("agent_max_steps", 4))),
                max(1, int(self.config.get("agent_max_skills", 16))),
                max(0, int(self.config.get("agent_max_replans", 1))))

    def review_plan(self, task: dict[str, Any], plan: dict[str, Any],
                    catalog: list[dict[str, Any]],
                    prior_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        max_steps, max_skills, max_replans = self._limits()
        names = {str(x.get("name") if isinstance(x, dict) else getattr(x, "name", ""))
                 for x in catalog}
        steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
        reasons: list[str] = []
        actions: list[dict[str, Any]] = []
        skill_total = 0
        if len(steps) > max_steps:
            reasons.append(f"计划波次 {len(steps)} 超过上限 {max_steps}")
        for idx, step in enumerate(steps):
            sid = str(step.get("id") or f"S{idx + 1}")
            title = str(step.get("title") or "") + " " + str(step.get("objective") or "")
            if FORBIDDEN.search(title):
                reasons.append(f"{sid} 含开放动作/外部命令")
            if str(step.get("gate") or "always") not in GATE_VALUES:
                reasons.append(f"{sid} gate 非法: {step.get('gate')}")
            skills = [str(x) for x in (step.get("skills") or [])]
            missing = [x for x in skills if x not in names]
            if missing:
                reasons.append(f"{sid} 引用未注册技能: {','.join(missing[:4])}")
            if not skills:
                reasons.append(f"{sid} 没有可执行技能")
            skill_total += len(skills)
            actions.append({"action": "select_registered_skill", "step": sid,
                            "skills": skills[:8], "gate": step.get("gate") or "always"})
            actions.append({"action": "execute_skill", "step": sid,
                            "parallel": min(len(skills), int(self.config.get("max_skill_parallel", 4)))})
        if skill_total > max_skills:
            reasons.append(f"技能总数 {skill_total} 超过上限 {max_skills}")
        if plan.get("replan") and max_replans < 1:
            reasons.append("本次不允许重规划")
        if len(plan.get("replan") or []) > max_replans:
            reasons.append("重规划次数超过上限")
        stop_reason = ""
        if prior_results:
            ok = [x for x in prior_results if x.get("ok")]
            if not ok:
                stop_reason = "no_progress"
            elif plan.get("in_scope") is False:
                stop_reason = "missing_data"
        trace = {
            "trace_id": new_id("AT"),
            "task_id": task.get("id"),
            "mode": self.mode,
            "engine": "bounded-v1",
            "action_space": sorted(ACTION_SPACE),
            "actions": actions,
            "action_count": len(actions),
            "plan_steps": len(steps),
            "skill_total": skill_total,
            "limits": {"max_steps": max_steps, "max_skills": max_skills,
                       "max_replans": max_replans},
            "violations": reasons,
            "stop_reason": stop_reason,
            "created_at": iso_now(),
        }
        self._save(trace)
        return trace

    def finalize(self, trace: dict[str, Any], results: list[dict[str, Any]],
                 arbitration: dict[str, Any], budget: Any | None = None) -> dict[str, Any]:
        reasons = list(trace.get("violations") or [])
        stop = ""
        snapshot = budget.snapshot() if hasattr(budget, "snapshot") else {}
        if snapshot.get("calls", 0) >= snapshot.get("max_calls", 10**9):
            stop = "budget_exhausted"
        elif not results:
            stop = "no_progress"
        elif arbitration.get("status") in ("matched", "multiple_causes"):
            stop = "evidence_sufficient"
        elif arbitration.get("status") in ("unmatched", "unknown_fault"):
            stop = "unknown_fault"
        elif arbitration.get("status") == "needs_data":
            stop = "missing_data"
        trace = dict(trace)
        trace["stop_reason"] = stop
        trace["final_status"] = arbitration.get("status")
        trace["violations"] = reasons
        trace["finished_at"] = iso_now()
        self._save(trace)
        return trace

    def _save(self, trace: dict[str, Any]) -> None:
        if not self.db:
            return
        try:
            self.db.execute(
                "INSERT INTO agent_traces(id,task_id,mode,action_count,stop_reason,payload_json,"
                "created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "action_count=excluded.action_count,stop_reason=excluded.stop_reason,"
                "payload_json=excluded.payload_json",
                (trace["trace_id"], str(trace.get("task_id") or ""), trace.get("mode") or "",
                 int(trace.get("action_count") or 0), trace.get("stop_reason") or "",
                 json.dumps(trace, ensure_ascii=False, default=str),
                 trace.get("created_at") or iso_now()))
        except Exception:
            # 轨迹记录失败不能覆盖诊断结果；调用方仍保留内存中的 trace。
            pass

    def latest(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT payload_json FROM agent_traces WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,))
        if not row:
            return None
        try:
            return json.loads(row.get("payload_json") or "{}")
        except json.JSONDecodeError:
            return None
