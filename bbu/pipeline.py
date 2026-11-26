"""诊断编排：受理 → 盘点 → AI 规划 → 按计划波次执行 → 可选补规划 → 证据仲裁 → 报告。

这是“不会一直循环”的核心：一次规划 + 最多一次补规划 + 明确停止条件 + 任务级预算。
LLM 只出现在四处：规划（1 次）、补规划（最多 1 次）、冲突仲裁（最多 1 次）、
报告研判（1 次），任何一步失败都回落到确定性逻辑。
"""
from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .ai import AIGateway, AIError, Budget, BudgetExceeded, NoModelError
from .data_egress import DataEgressPolicy
from .db import Database
from .evidence import assess_evidence
from .agent import BoundedAgent
from .calibration import CalibrationStore
from .decision_cards import build_decision_cards, decision_summary
from .claims import (build_claims, claims_markdown, validate_claims,
                     validate_narrative)
from .evidence_store import EvidenceStore
from .learning import ExperienceStore
from .notifications import create_notification
from .planner import (GATE_LABELS, compact_plan, deterministic_plan, gate_allows, needs_replan,
                      normalize_plan, plan_candidates, plan_skill_limit, plan_skill_names)
from .prompts import (ARBITER_SCHEMA, ARBITER_SYSTEM, PLAN_SCHEMA, PLANNER_SYSTEM,
                      REPLANNER_SYSTEM, REPORT_SCHEMA, REPORT_SYSTEM, arbiter_user,
                      planner_user, replanner_user, report_user)
from .reports import (build_narrative_facts, compose_markdown, evidence_for_prompt,
                      render_html, validate_citations)
from .skills import Matcher, Skill, SkillRegistry, SkillRunner, inventory_inputs, shallow_archive_text
from .session import DiagnosticSession
from .tool_contract import ToolPolicy
from .util import iso_now, read_json, truncate, write_json, write_text

def _json_load(value: Any, default: Any) -> Any:
    try:
        text = value if isinstance(value, str) else json.dumps(value or {})
        return json.loads(text or "{}") if text else default
    except (TypeError, ValueError):
        return default


def _skill_source_hints(skill: Any) -> list[str]:
    """从子 Skill profile 抽取声明的日志来源，供 HypothesisState 记录适用条件。"""
    if not skill:
        return []
    hints: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 7:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("globs", "paths", "name") and isinstance(
                        value, (str, list)) and len(hints) < 6:
                    rows = value if isinstance(value, list) else [value]
                    hints.extend(str(x) for x in rows[:3] if str(x).strip())
                elif isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(getattr(skill, "profile", {}) or {})
    return list(dict.fromkeys(hints))[:6]


STAGES = {
    "queued": "排队",
    "intake": "日志盘点",
    "planning": "AI 规划",
    "fanout": "按计划执行",
    "replanning": "补规划",
    "arbitration": "证据仲裁",
    "reporting": "报告生成",
    "done": "完成",
}
CONF_RANK = {"low": 1, "medium": 2, "high": 3}


class TaskPipeline:
    def __init__(self, db: Database, config: Any, registry: SkillRegistry,
                 gateway: AIGateway, runner: SkillRunner,
                 experiences: ExperienceStore | None = None,
                 traces: Any = None) -> None:
        self.db = db
        self.config = config
        self.registry = registry
        self.gateway = gateway
        self.runner = runner
        self.matcher = Matcher(registry)
        self.experiences = experiences or ExperienceStore(db)
        self.traces = traces
        self.egress = DataEgressPolicy.for_diagnostic(config)
        self.evidence_store = EvidenceStore(db)
        self.tools = ToolPolicy(db, config)
        self.calibration = CalibrationStore(db, config)
        self.agent = BoundedAgent(db, config)
        if getattr(runner, "policy", None) is None:
            runner.db = db
            runner.policy = self.tools

    def _audit_model_egress(self, task: dict[str, Any], stage: str,
                            original: Any, sanitized: Any) -> None:
        detail = {"stage": stage, **self.egress.payload_audit(original, sanitized)}
        self.db.audit(task.get("user_id"), task.get("username"),
                      "diagnostic_model_egress", str(task.get("id") or ""), detail)

    # ---------- 基础设施 ----------
    def _emit(self, task_id: str, level: str, message: str,
              stage: str = "", data: dict | None = None) -> None:
        now = iso_now()
        with self.db.tx() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM task_events WHERE task_id=?",
                (task_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO task_events(task_id, seq, ts, level, stage, message, data_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (task_id, seq, now, level, stage, message,
                 json.dumps(data or {}, ensure_ascii=False, default=str)))

    def _stage(self, task: dict[str, Any], stage: str, progress: int,
               message: str, level: str = "info") -> None:
        self.db.execute(
            "UPDATE tasks SET stage=?, progress=? WHERE id=?",
            (stage, progress, task["id"]))
        self._emit(task["id"], level, message, stage, {"progress": progress})
        trace = task.get("_trace")
        if trace:
            trace.append("stage", phase=stage, status="running", progress=int(progress),
                         message=message, level=level)
        task["stage"] = stage
        task["progress"] = progress

    # ---------- 主流程 ----------
    def run(self, task_id: str, cancel_event: threading.Event) -> dict[str, Any]:
        # 诊断使用任务提交者的个人模型配置；未配置时网关自动回退管理员默认模型。
        row = self.db.one("SELECT user_id,title,mode,description FROM tasks WHERE id=?", (task_id,)) or {}
        previous = self.gateway.set_user(row.get("user_id") or 0)
        previous_usage = self.gateway.set_usage_context({
            "task_id": task_id, "task_kind": "diagnosis"})
        trace = None
        try:
            if self.traces:
                trace = self.traces.start(
                    kind="diagnosis", ref_id=task_id, user_id=int(row.get("user_id") or 0),
                    meta={"title": row.get("title") or "", "mode": row.get("mode") or "",
                          "description_chars": len(row.get("description") or ""),
                          "trigger": "worker"})
            return self._run_task(task_id, cancel_event, trace)
        except Exception as exc:
            if trace:
                trace.error(phase="run", code="orchestrator_error",
                            message=f"{type(exc).__name__}: {exc}", exc=exc,
                            retryable=True)
                trace.status = "failed"
            raise
        finally:
            if trace:
                latest = self.db.one("SELECT status,error FROM tasks WHERE id=?", (task_id,)) or {}
                status = str(latest.get("status") or "")
                if trace.status == "failed":
                    trace.finish("failed", error_code="orchestrator_error",
                                 error_summary=str(latest.get("error") or "任务异常中断"))
                elif status == "done":
                    trace.finish("success")
                elif status == "needs_data":
                    trace.finish("needs_data", error_code="needs_data",
                                 error_summary=str(latest.get("error") or "需要补充数据"))
                elif status == "cancelled":
                    trace.finish("cancelled", error_code="cancelled",
                                 error_summary="用户取消")
                elif status == "failed":
                    trace.finish("failed", error_code="task_failed",
                                 error_summary=str(latest.get("error") or "任务失败"))
                else:
                    trace.finish("failed", error_code="incomplete",
                                 error_summary="任务未正常结束")
            self.gateway.set_user(previous)
            self.gateway.set_usage_context(previous_usage)

    def _run_task(self, task_id: str, cancel_event: threading.Event,
                  trace: Any = None) -> dict[str, Any]:
        started = time.time()
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise RuntimeError(f"任务不存在: {task_id}")
        if trace:
            task["_trace"] = trace
        task["username"] = (self.db.one(
            "SELECT username FROM users WHERE id=?", (task["user_id"],)) or {}).get("username")
        task_dir = self.config.data_dir / "tasks" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        stage_times: dict[str, float] = {}
        models_used: set[str] = set()
        session = DiagnosticSession(self.db, task_id)
        session_snapshot = session.ensure()
        session_round = int(session_snapshot.get("supplement_round") or 0)
        try:
            prior_budget = json.loads(task.get("session_budget_json") or "{}")
        except json.JSONDecodeError:
            prior_budget = {}
        if not isinstance(prior_budget, dict):
            prior_budget = {}

        def _budget_event(kind: str, msg: str, data: dict | None) -> None:
            self._emit(task_id, "debug", msg, task.get("stage", ""), data or {})
            try:
                self.db.execute("UPDATE tasks SET session_budget_json=? WHERE id=?",
                                (json.dumps(budget.snapshot(), ensure_ascii=False), task_id))
            except Exception as exc:  # noqa: BLE001 - 预算落库失败要留痕，但不能掩盖原错误
                self._emit(task_id, "warning", f"跨轮预算写入失败：{exc}", task.get("stage", ""))

        budget = Budget(
            max_calls=int(self.config.get("max_llm_calls", 12)),
            max_tokens=int(self.config.get("max_total_tokens", 200000)),
            deadline=time.time() + int(self.config.get("task_timeout_sec", 2400)),
            on_event=_budget_event, prior=prior_budget,
        )
        resume = bool(task.get("resume_count") or task.get("continuation_count")) \
            and (task_dir / "plan.json").exists() and (task_dir / "routing.json").exists()
        existing_results = self._load_skill_results(task_id) if resume else []
        replan_done = resume and session_round == 0 and (task_dir / "replan.json").exists()
        session.append("run_started", {
            "state": "planning" if resume else "intake",
            "supplement_round": session_round,
            "continuation": bool(task.get("continuation_count")),
            "budget": budget.snapshot(),
        }, event_key=f"run-start-{session_round}-{int(resume)}")

        def _mark(stage: str) -> None:
            stage_times[stage] = time.time()

        try:
            self.db.execute(
                "UPDATE tasks SET status='running', started_at=?, stage='intake', progress=5 WHERE id=?",
                (iso_now(), task_id))
            _mark("start")
            self._stage(task, "intake", 5, "任务开始，正在盘点输入日志")

            if cancel_event.is_set():
                return self._finish_cancelled(task, task_dir)

            # 1. 输入盘点
            inputs = [Path(x["stored_path"]) for x in json.loads(task["input_json"] or "[]")
                      if x.get("stored_path")]
            missing = [str(p) for p in inputs if not p.exists()]
            inventory = inventory_inputs([p for p in inputs if p.exists()])
            inventory["missing_files"] = missing
            write_json(task_dir / "inventory.json", inventory)
            self._emit(task_id, "info",
                       f"盘点完成：{len(inventory['files'])} 个输入，"
                       f"{inventory['total_members']} 个成员，"
                       f"解压后 {inventory['total_uncompressed_bytes'] / 1024 / 1024:.1f} MB",
                       "intake", {"inventory": {k: inventory[k] for k in (
                           "total_members", "total_uncompressed_bytes", "detected_hints")}})
            self._stage(task, "intake", 12, "日志盘点完成")
            _mark("intake")

            if not inputs:
                return self._needs_data(
                    task, task_dir, stage_times, budget,
                    ["未上传任何日志包，无法运行证据检索",
                     "请提供 BBU/ITBBU/SDR 采集包（zip/tar.gz）或已解压目录"],
                    "无日志输入", inventory)

            if resume:
                plan = read_json(task_dir / "plan.json", {}) or {}
                routing = read_json(task_dir / "routing.json", {}) or {}
                scores: dict[str, float] = {}
                all_skills: list[Skill] = []
                experiences: list[dict[str, Any]] = []
                if not plan.get("steps") or not routing.get("candidates"):
                    resume = False
                else:
                    replan_file = task_dir / "replan.json"
                    if session_round == 0 and replan_file.exists():
                        replan_plan = read_json(replan_file, {}) or {}
                        if replan_plan.get("steps"):
                            plan["steps"] = (plan.get("steps") or []) + replan_plan["steps"]
                            plan["resume_replan_merged"] = True
                    invalidated = set((session_snapshot.get("last_input") or {}).get(
                        "invalidated_skills") or [])
                    invalidated.update(row["skill_name"] for row in self.db.all(
                        "SELECT skill_name FROM task_skills WHERE task_id=? AND invalidated=1",
                        (task_id,)))
                    if invalidated:
                        for step in plan.get("steps") or []:
                            if any(name in invalidated for name in (step.get("skills") or [])):
                                step.pop("status", None)
                                step.pop("skill_results", None)
                        plan.pop("replan", None)
                    self._emit(task_id, "warning",
                               f"从检查点恢复：已有 {len(existing_results)} 个可复用技能结果，"
                               f"本轮补充失效的技能将重新执行" if session_round
                               else f"服务重启，从检查点恢复：已有 {len(existing_results)} 个技能结果，跳过重复执行",
                               "planning")
                    self._stage(task, "planning", 22,
                                f"断点恢复：{len(plan.get('steps') or [])} 个波次，"
                                f"已有 {len(existing_results)} 个技能结果")
                    _mark("planning")
            if not resume:
                # 2. AI 规划：先假设、再选技能、安排并行波次
                self._stage(task, "planning", 15, "AI 正在分析故障描述与日志结构，生成诊断计划")
                archive_texts = self._shallow_texts([p for p in inputs if p.exists()])
                # 现场采集包常只有日志、没有故障描述：把盘点命中的文件名/成员名作为弱信号，
                # 但仍要求技能规则在文件内容中命中，避免仅凭文件名猜根因。
                member_names = [p.name for p in inputs] + list(inventory.get("top_members") or [])[:120]
                match_text = "\n".join([
                    task["description"] or "",
                    " ".join(inventory.get("detected_hints") or []),
                    " ".join(member_names),
                ])
                matches = self.matcher.match(match_text, member_names, archive_texts)
                scores = {m.skill.name: m.score for m in matches}
                all_skills = [s for s in self.registry.list_skills(False)
                              if s.status in ("ready", "degraded")
                              and (getattr(s, "channel", "stable") != "canary"
                                   or bool(json.loads(task.get("options_json") or "{}").get("include_canary")))]
                all_skills.sort(key=lambda s: scores.get(s.name, 0), reverse=True)
                catalog = []
                for s in all_skills[:8]:
                    entry = s.index_card()
                    entry["rule_score"] = round(scores.get(s.name, 0), 1)
                    catalog.append(entry)
                index_chars = len(json.dumps(catalog, ensure_ascii=False))
                if trace:
                    trace.append("stage", phase="planning", status="running",
                                 message=f"技能索引卡 {len(catalog)} 张，{index_chars} 字符；按需加载完整 profile")
                options = _json_load(task.get("options_json"), {})
                selected = [str(x) for x in (options.get("skill_names") or [])]
                experiences = self.experiences.search(
                    task["description"] or match_text, inventory.get("detected_hints") or [],
                    all_skills[0].name if all_skills else "", limit=3,
                    include_unreviewed=bool(self.config.get("experience_include_unreviewed", False)),
                    device_type=str(options.get("device_type") or ""),
                    software_version=str(options.get("software_version") or ""),
                    exclude_task=str(task.get("id") or ""), with_counterexamples=True)
                if selected:
                    scores = {name: max(float(scores.get(name, 0)), 60.0) for name in selected}
                    selected_set = set(selected)
                    ordered = [s for s in all_skills if s.name in selected_set]
                    missing = [name for name in selected if name not in {s.name for s in ordered}]
                    if missing:
                        fallback = deterministic_plan([{"skill": n, "score": 60.0}
                                                       for n in selected if n not in missing],
                                                      self.config, task.get("mode") or "fast")
                        fallback["summary"] = "用户指定技能不可用：" + "、".join(missing)
                        fallback["in_scope"] = False
                        plan = fallback
                        plan_meta = {"llm_used": False, "notes": fallback["summary"], "model": ""}
                    else:
                        det_selected = [{"skill": s.name, "score": scores[s.name]}
                                        for s in ordered]
                        plan = deterministic_plan(det_selected, self.config,
                                                  task.get("mode") or "fast")
                        plan["source"] = "user_selected"
                        plan["summary"] = f"按指定技能执行：{'、'.join(selected)}"
                        plan["selected_skills"] = selected
                        plan_meta = {"llm_used": False,
                                     "notes": ["用户指定技能，跳过 AI 扩选与补规划"],
                                     "model": ""}
                else:
                    plan, plan_meta = self._plan(task, inventory, catalog, experiences, scores,
                                                 budget, models_used)
                plan["experience_refs"] = [
                    {"task_id": e.get("task_id"), "root_cause": e.get("root_cause"),
                     "skill": e.get("skill"), "match_score": e.get("match_score")}
                    for e in experiences
                ]
                plan["plan_notes"] = plan_meta.get("notes") or []
                write_json(task_dir / "plan.json", plan)
                self._apply_task_title(task, plan.get("task_title"), inventory)
                routing = self._routing_from_plan(plan, scores, all_skills, experiences,
                                                  plan_meta.get("llm_used", False),
                                                  task.get("mode") or "fast")
                write_json(task_dir / "routing.json", routing)
                self._emit(task_id, "info",
                           f"AI 计划（{plan.get('source')}）：{plan.get('summary')}；"
                           + " → ".join(
                               f"{s['id']}[{GATE_LABELS.get(s.get('gate'), s.get('gate'))}]"
                               f"({','.join(s.get('skills') or [])})"
                               for s in plan.get("steps") or [])[:900],
                           "planning", {"plan": plan, "routing": routing,
                                        "experience_refs": plan.get("experience_refs")})
                self._stage(task, "planning", 22,
                            f"计划包含 {len(plan.get('steps') or [])} 个波次、"
                            f"{len(plan_skill_names(plan))} 个技能")
                _mark("planning")

            agent_trace = self.agent.review_plan(task, plan, all_skills,
                                                 existing_results)
            write_json(task_dir / "agent-trace.json", agent_trace)
            self._emit(task_id, "info",
                       f"有界 Agent（{agent_trace['mode']}）动作 "
                       f"{agent_trace['action_count']}/{agent_trace['limits']['max_steps']} 波次；"
                       f"违规 {len(agent_trace['violations'])} 项",
                       "planning", {"agent_trace": agent_trace})
            if agent_trace.get("violations") and agent_trace["mode"] == "bounded":
                return self._needs_data(
                    task, task_dir, stage_times, budget,
                    ["有界 Agent 拒绝执行计划：" + "；".join(agent_trace["violations"][:4])],
                    "计划越过固定动作空间", inventory, routing, plan)
            if cancel_event.is_set():
                return self._finish_cancelled(task, task_dir)

            if not plan.get("in_scope") or not routing["candidates"]:
                return self._needs_data(
                    task, task_dir, stage_times, budget,
                    plan.get("missing_info") or routing.get("missing_info")
                    or ["当前技能库中没有与描述匹配的子 Skill"],
                    "未生成可执行的诊断计划", inventory, routing, plan)

            # 3. 按计划执行：波次内并行，波次间按门槛推进
            self._init_hypotheses(task, session, plan, routing)
            self._stage(task, "fanout", 25, "按 AI 计划开始执行并行技能")
            results, executed = self._execute_plan(
                task, plan, inputs, task_dir, cancel_event, budget, models_used, existing_results,
                scores=scores)
            _mark("fanout")
            if cancel_event.is_set():
                return self._finish_cancelled(task, task_dir, results)

            # 4. 一次有界补规划：只在证据不足/低置信度时触发，最多一次
            if replan_done:
                self._emit(task_id, "info", "检查点显示补规划已执行，跳过重复补规划", "replanning")
                replan = None
            else:
                replan = self._maybe_replan(task, plan, results, executed, inputs, inventory,
                                            all_skills, scores, task_dir, budget, models_used,
                                            cancel_event)
            if replan:
                # 任务结果只保留可展示的计划摘要，避免把完整 results 重复写进 plan
                plan["replan"] = {k: v for k, v in replan.items()
                                  if k not in ("results", "executed", "plan")}
                results = replan.get("results", results)
                executed = replan.get("executed", executed)
                routing["replan"] = {k: replan.get(k) for k in (
                    "status", "reason", "summary", "steps", "candidates")}
                routing["candidates"] = self._merge_candidates(
                    routing.get("candidates") or [], replan.get("candidates") or [])
                write_json(task_dir / "plan.json", plan)
                write_json(task_dir / "replan.json", replan.get("plan") or {})
                write_json(task_dir / "routing.json", routing)
            if cancel_event.is_set():
                return self._finish_cancelled(task, task_dir, results)

            self._init_hypotheses(task, session, plan, routing)
            if not results:
                return self._needs_data(
                    task, task_dir, stage_times, budget,
                    ["所有计划技能都未能执行，请检查技能健康状态"],
                    "技能执行失败", inventory, routing, plan)

            # 5. 仲裁
            self._stage(task, "arbitration", 70, "汇总计划执行结果，进行证据仲裁")
            arbitration = self._arbitrate(task, results, budget, models_used)
            self._sync_hypotheses(task, session, results, arbitration)
            chosen_for_cards = arbitration.get("chosen_skill") or ""
            if chosen_for_cards:
                skill_for_cards = self.registry.get(chosen_for_cards)
                chosen_result = next((r for r in results
                                      if r.get("skill_name") == chosen_for_cards), None)
                arbitration["decision_cards"] = build_decision_cards(
                    skill_for_cards, chosen_result) if skill_for_cards else []
                arbitration["decision_summary"] = decision_summary(
                    arbitration["decision_cards"])
            arbitration["calibration"] = self.calibration.annotate(
                str(arbitration.get("confidence") or "medium"))
            agent_trace = self.agent.finalize(agent_trace, results, arbitration, budget)
            write_json(task_dir / "arbitration.json", arbitration)
            self._emit(task_id, "info", f"仲裁结论：{arbitration['status']} / "
                       f"{arbitration.get('chosen_skill') or '-'} / {arbitration.get('root_cause') or '-'}",
                       "arbitration", {"arbitration": arbitration})
            _mark("arbitration")

            # 6. 报告
            self._stage(task, "reporting", 82, "生成统一诊断报告")
            (report_md, report_html, narrative, claims, claim_check,
             narrative_check) = self._report(
                task, results, arbitration, inventory, stage_times, budget,
                models_used, started, plan)
            _mark("reporting")
            md_path = write_text(task_dir / "report.md", report_md)
            html_path = write_text(task_dir / "report.html", render_html(
                report_md, f"{task['title']} - 诊断报告", self.config.get("brand") or "BBU 故障诊断平台"))
            stats = self._stats(started, stage_times, budget, models_used)
            status = "done" if arbitration["status"] in (
                "matched", "multiple_causes", "temporal_conflict", "unmatched", "unknown_fault") else "needs_data"
            result = {
                "verdict": {
                    "status": arbitration["status"],
                    "primary": arbitration.get("root_cause"),
                    "root_cause_name": arbitration.get("root_cause_name"),
                    "skill": arbitration.get("chosen_skill"),
                    "confidence": arbitration.get("confidence"),
                    "calibration": arbitration.get("calibration"),
                },
                "arbitration": arbitration,
                "routing": routing,
                "plan": plan,
                "replan": (plan.get("replan") if isinstance(plan.get("replan"), dict) else None),
                "experiences": plan.get("experience_refs") or [],
                "inventory": {k: inventory.get(k) for k in (
                    "total_members", "total_uncompressed_bytes", "detected_hints",
                    "total_input_bytes")},
                "skills": [{k: r.get(k) for k in (
                    "skill_name", "status", "score", "primary", "root_cause",
                    "duration_ms", "error", "limitations")} for r in results],
                "stats": stats,
                "narrative": narrative,
                "claims": claims,
                "claim_check": claim_check,
                "narrative_check": narrative_check,
                "agent_trace": agent_trace,
            }
            self.db.execute(
                "UPDATE tasks SET status=?, stage='done', progress=100, finished_at=?, "
                "verdict_json=?, result_json=?, report_md=?, report_html=?, stats_json=? WHERE id=?",
                (status, iso_now(),
                 json.dumps(result["verdict"], ensure_ascii=False, default=str),
                 json.dumps(result, ensure_ascii=False, default=str),
                 str(md_path), str(html_path),
                 json.dumps(stats, ensure_ascii=False),
                 task_id))
            self._emit(task_id, "success", "诊断完成，报告已生成", "done",
                       {"report": {"md": str(md_path), "html": str(html_path)},
                        "verdict": result["verdict"], "stats": stats})
            terminal_state = ("resolved" if arbitration["status"] in (
                "matched", "multiple_causes", "temporal_conflict")
                else "unknown_fault" if arbitration["status"] in ("unknown_fault", "unmatched")
                else status)
            self._session_note(task_id, "decision_summary", {
                "state": terminal_state, "verdict": result["verdict"],
                "reason": arbitration.get("reason"),
                "evidence_count": self.evidence_store.count(task_id),
                "budget": budget.snapshot(),
                "report": {"md": str(md_path), "html": str(html_path)},
            }, f"decision-{session_round}")
            try:
                self.experiences.record(task, result)
            except Exception as exc:  # noqa: BLE001 - 经验写失败不能覆盖诊断结果
                self._emit(task_id, "warning", f"历史经验写入失败：{exc}", "done")
            kind = "task_done" if status == "done" else "task_failed"
            title = "诊断完成" if status == "done" else "任务需要补充数据"
            summary = (result["verdict"].get("root_cause_name")
                       or result["verdict"].get("primary") or "未匹配根因")
            create_notification(self.db, task["user_id"], kind, title,
                                f"{task['title']}：{summary}", f"/#/tasks/{task_id}")
            return result

        except BudgetExceeded as exc:
            return self._fail(task, task_dir, f"预算耗尽：{exc}", stage_times, budget)
        except Exception as exc:  # noqa: BLE001 - 任务失败要留痕，不能拖垮 worker
            log = task_dir / "error.log"
            write_text(log, traceback.format_exc())
            return self._fail(task, task_dir, f"{type(exc).__name__}: {exc}", stage_times, budget)

    # ---------- AI 规划 ----------
    def _planner_role(self) -> str | None:
        """planner 优先；老部署只配了 router 时复用 router，仍使用计划 Schema。"""
        if not self.config.get("planner_enabled", True):
            return None
        try:
            if self.gateway.has_real_model("planner") and self.gateway.available("planner"):
                return "planner"
            if self.gateway.has_real_model("router") and self.gateway.available("router"):
                return "router"
        except NoModelError:
            return None
        return None

    def _det_candidates(self, catalog: list[dict[str, Any]], scores: dict[str, float],
                        mode: str) -> list[dict[str, Any]]:
        threshold = float(self.config.get("min_route_score", 30))
        ordered = []
        for entry in catalog:
            name = str(entry.get("skill") or "")
            score = float(scores.get(name, 0.0))
            if score >= threshold:
                ordered.append({"skill": name, "score": score,
                                "reason": "规则匹配分",
                                "source": "rule", "signals": {}})
        ordered.sort(key=lambda x: x["score"], reverse=True)
        k = int(self.config.get("deep_max_skills" if mode == "deep" else "fast_max_skills", 2))
        selected = ordered[:k]
        if not selected:
            fallback = sorted(
                ({"skill": str(e.get("skill") or ""), "score": float(scores.get(str(e.get("skill") or ""), 0.0)),
                  "reason": "规则弱匹配（低于阈值，仍保留一次验证机会）",
                  "source": "rule", "signals": {}}
                 for e in catalog if scores.get(str(e.get("skill") or ""), 0.0) >= max(20, threshold - 10)),
                key=lambda x: x["score"], reverse=True)
            selected = fallback[:1]
        return selected

    def _apply_task_title(self, task: dict[str, Any], ai_title: Any,
                          inventory: dict[str, Any]) -> None:
        """AI 标题只作为展示信息；失败时保留确定性标题，不影响诊断主链。"""
        if task.get("title_source") == "manual":
            return
        candidate = self._clean_task_title(ai_title)
        source = "ai" if candidate else "deterministic"
        if not candidate:
            candidate = self._fallback_task_title(task, inventory)
        if not candidate or candidate == task.get("title"):
            return
        self.db.execute("UPDATE tasks SET title=?, title_source=? WHERE id=?",
                        (candidate, source, task["id"]))
        old = task.get("title") or ""
        task["title"] = candidate
        task["title_source"] = source
        self._emit(task["id"], "info", f"任务标题已更新为“{candidate}”", "planning",
                   {"title": candidate, "previous": old, "title_source": source})

    @staticmethod
    def _clean_task_title(value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'“”‘’。；;，,")
        text = re.sub(r"^(任务标题|标题)[:：]\s*", "", text)
        if not text or len(text) < 4 or len(text) > 40:
            return ""
        generic = ("未命名", "故障分析", "诊断分析", "bbu故障", "基站故障", "日志分析")
        if text.lower() in generic or any(text == g + suffix for g in generic
                                         for suffix in ("", "报告", "诊断")):
            return ""
        return text

    @staticmethod
    def _fallback_task_title(task: dict[str, Any], inventory: dict[str, Any]) -> str:
        description = re.sub(r"\s+", " ", str(task.get("description") or "")).strip()
        first = description.split(" ")[0] if description else ""
        if len(first) >= 6 and "未命名" not in first:
            return truncate(first, 60)
        hints = [str(x).strip() for x in (inventory.get("detected_hints") or []) if str(x).strip()]
        if hints:
            return truncate(f"{hints[0]}相关现场故障诊断", 60)
        return truncate(f"{task.get('title') or 'BBU 现场故障诊断'}", 60)

    def _plan(self, task: dict[str, Any], inventory: dict[str, Any],
              catalog: list[dict[str, Any]], experiences: list[dict[str, Any]],
              scores: dict[str, float], budget: Budget,
              models_used: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        mode = task.get("mode") or "fast"
        threshold = float(self.config.get("min_route_score", 30))
        det = self._det_candidates(catalog, scores, mode)
        det_plan = deterministic_plan(det, self.config, mode)
        role = self._planner_role()
        meta: dict[str, Any] = {"llm_used": False, "notes": [], "model": ""}
        if not role:
            meta["notes"].append("未配置真实 planner/router 模型，使用确定性计划")
            self._emit(task["id"], "info", "无真实规划模型，使用确定性计划", "planning")
            return det_plan, meta

        max_skills = int(self.config.get("deep_max_skills" if mode == "deep" else "fast_max_skills", 2))
        max_steps = int(self.config.get("planner_max_steps", 4))
        prompt_inventory = {
            "files": inventory.get("files"),
            "members": inventory.get("total_members"),
            "hints": inventory.get("detected_hints"),
            "top_members": (inventory.get("top_members") or [])[:40],
        }
        try:
            source_payload = {
                "description": task.get("description") or "",
                "inventory": prompt_inventory,
                "catalog": catalog,
                "experiences": experiences,
            }
            safe_payload = self.egress.sanitize_structure(source_payload)
            self._audit_model_egress(task, f"planner:{role}", source_payload, safe_payload)
            result = self.gateway.complete(
                role, PLANNER_SYSTEM,
                planner_user(safe_payload["description"], safe_payload["inventory"],
                             safe_payload["catalog"], safe_payload["experiences"],
                             mode, max_skills, max_steps),
                json_schema=PLAN_SCHEMA, purpose="plan", budget=budget,
                model_override=task.get("model_override"),
                trace=task.get("_trace"))
            meta["llm_used"] = True
            meta["model"] = result.model_name
            models_used.add(result.model_name)
            plan, errors, warnings = normalize_plan(
                result.data, catalog, self.config, mode, source="ai", replan=False)
            meta["notes"].extend(warnings)
            if task.get("_trace"):
                task["_trace"].validation(phase="planning", name="plan_normalize",
                                           ok=not errors, errors=errors[:20],
                                           details={"warnings": warnings[:20],
                                                    "summary": plan.get("summary")})
            if errors:
                meta["notes"].extend(errors)
                self._emit(task["id"], "warning",
                           "AI 计划未通过校验，回退确定性计划：" + "；".join(errors[:3]),
                           "planning")
                fallback = deterministic_plan(det, self.config, mode)
                fallback["plan_notes"] = meta["notes"]
                return fallback, meta
            self._emit(task["id"], "info",
                       f"AI 计划已生成（{role}）：{plan.get('summary')}", "planning",
                       {"plan": compact_plan(plan)})
            plan["plan_notes"] = meta["notes"]
            plan["model"] = result.model_name
            return plan, meta
        except (AIError, NoModelError) as exc:
            meta["notes"].append(f"AI 规划失败，回退确定性计划：{exc}")
            self._emit(task["id"], "warning",
                       f"AI 规划失败，回退确定性计划：{exc}", "planning")
            fallback = deterministic_plan(det, self.config, mode)
            fallback["plan_notes"] = meta["notes"]
            return fallback, meta

    def _routing_from_plan(self, plan: dict[str, Any], scores: dict[str, float],
                           all_skills: list[Skill], experiences: list[dict[str, Any]],
                           llm_used: bool, mode: str) -> dict[str, Any]:
        threshold = float(self.config.get("min_route_score", 30))
        candidates = plan_candidates(plan, scores, threshold)
        all_scores = []
        for s in all_skills[:12]:
            all_scores.append({
                "skill": s.name, "title": s.title,
                "score": round(float(scores.get(s.name, 0.0)), 1),
                "status": s.status, "verified": s.verified,
            })
        return {
            "mode": mode,
            "summary": plan.get("summary") or "",
            "out_of_scope": not bool(plan.get("in_scope")),
            "missing_info": plan.get("missing_info") or [],
            "candidates": candidates,
            "all_scores": all_scores,
            "llm_used": llm_used,
            "plan_source": plan.get("source"),
            "experience_refs": [
                {"task_id": e.get("task_id"), "root_cause": e.get("root_cause"),
                 "skill": e.get("skill"), "match_score": e.get("match_score")}
                for e in experiences],
        }

    def _merge_candidates(self, base: list[dict[str, Any]],
                          extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged = {c.get("skill"): dict(c) for c in base if c.get("skill")}
        for c in extra:
            name = c.get("skill")
            if not name:
                continue
            if name in merged:
                if float(c.get("score") or 0) > float(merged[name].get("score") or 0):
                    merged[name]["score"] = c.get("score")
                merged[name]["reason"] = (str(merged[name].get("reason") or "")
                                          + "；补规划：" + str(c.get("reason") or ""))[:400]
            else:
                merged[name] = dict(c)
        return sorted(merged.values(), key=lambda x: float(x.get("score") or 0), reverse=True)

    def _shallow_texts(self, inputs: list[Path]) -> dict[str, str]:
        patterns: list[str] = []

        def collect(node: Any, depth: int = 0) -> None:
            if depth > 3 or not isinstance(node, dict):
                return
            raw = node.get("any_of") or node.get("contains") or node.get("tokens")
            if isinstance(raw, list):
                patterns.extend(str(x) for x in raw[:8] if str(x).strip())
            for value in node.values():
                if isinstance(value, (dict, list)):
                    if isinstance(value, dict):
                        collect(value, depth + 1)
                    else:
                        for item in value:
                            if isinstance(item, dict):
                                collect(item, depth + 1)

        for skill in self.registry.list_skills(False):
            for qs in skill.quick_signatures or []:
                patterns.extend(str(x) for x in (qs.get("any_of") or [])[:8])
            for rule in skill.rules or []:
                collect(rule)
        patterns = list(dict.fromkeys(p for p in patterns if len(p) <= 80))[:120]
        if not patterns:
            return {}
        texts: dict[str, str] = {}
        for path in inputs[:4]:
            try:
                texts.update(shallow_archive_text(path, patterns))
            except Exception:  # noqa: BLE001 - 快速签名失败不影响路由
                continue
        return texts

    # ---------- 计划执行 ----------
    def _load_skill_results(self, task_id: str) -> list[dict[str, Any]]:
        """从 task_skills 恢复已完成的技能结果，供服务重启后的断点续跑。"""
        rows = self.db.all(
            "SELECT skill_name, skill_version, score, status, result_json FROM task_skills "
            "WHERE task_id=? AND result_json IS NOT NULL AND status NOT IN ('running') "
            "AND invalidated=0 ORDER BY id", (task_id,))
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row["result_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            data.setdefault("skill_name", row["skill_name"])
            data["score"] = row["score"] or 0
            data["skill_version"] = row["skill_version"]
            data["resumed"] = True
            results.append(data)
        return results

    def _execute_plan(self, task: dict[str, Any], plan: dict[str, Any],
                      inputs: list[Path], task_dir: Path, cancel_event: threading.Event,
                      budget: Budget, models_used: set[str],
                      existing_results: list[dict[str, Any]],
                      scores: dict[str, float] | None = None,
                      stage: str = "fanout") -> tuple[list[dict[str, Any]], set[str]]:
        """波次内并行、波次间按 gate 推进；每个技能只执行一次。"""
        results = list(existing_results)
        executed = {str(r.get("skill_name") or "") for r in results if r.get("skill_name")}
        steps = plan.get("steps") or []
        for idx, step in enumerate(steps, start=1):
            if cancel_event.is_set():
                break
            if step.get("status") in ("done", "skipped"):
                continue
            if not gate_allows(str(step.get("gate") or "always"), results):
                step["status"] = "skipped"
                self._emit(task["id"], "info",
                           f"跳过 {step.get('id')}·{step.get('title')}：{step.get('gate_label') or step.get('gate')} 未触发",
                           stage, {"plan_step": step.get("id"), "gate": step.get("gate")})
                continue
            candidates = []
            for name in step.get("skills") or []:
                if name in executed:
                    continue
                score = float((scores or {}).get(name, 0.0))
                if plan.get("source") == "ai":
                    score = max(30.0, score)
                candidates.append({"skill": name,
                                   "score": score or 30.0,
                                   "reason": f"{step.get('id')}·{step.get('title')}",
                                   "source": "plan", "signals": {}, "plan_step": step.get("id")})
            if not candidates:
                step["status"] = "skipped"
                continue
            step["status"] = "running"
            self._emit(task["id"], "info",
                       f"执行 {step.get('id')}·{step.get('title')}（{len(candidates)} 个技能并行）",
                       stage, {"plan_step": step.get("id"), "skills": [c["skill"] for c in candidates]})
            step_results = self._run_wave(task, candidates, inputs, task_dir,
                                          cancel_event, budget, models_used, stage,
                                          step.get("id"))
            results.extend(step_results)
            executed.update(str(r.get("skill_name") or "") for r in step_results if r.get("skill_name"))
            step["status"] = "done" if step_results else "failed"
            step["skill_results"] = [r.get("skill_name") for r in step_results]
            if len(steps) > 1 and stage == "fanout":
                progress = min(65, 25 + int(40 * idx / len(steps)))
                self._stage(task, stage, progress,
                            f"已完成 {idx}/{len(steps)} 个计划波次")
        return results, executed

    def _run_wave(self, task: dict[str, Any], candidates: list[dict[str, Any]],
                  inputs: list[Path], task_dir: Path, cancel_event: threading.Event,
                  budget: Budget, models_used: set[str], stage: str,
                  step_id: str = "") -> list[dict[str, Any]]:
        parallel = max(1, int(self.config.get("max_skill_parallel", 4)))
        parallel = min(parallel, max(1, len(candidates)))
        results: list[dict[str, Any]] = []

        def _run_one(cand: dict[str, Any]) -> dict[str, Any]:
            skill = self.registry.get(cand["skill"])
            if not skill:
                return {"skill_name": cand["skill"], "ok": False, "status": "missing",
                        "error": "技能不存在", "score": cand["score"],
                        "evidence": [], "root_cause": None, "limitations": []}
            self._emit(task["id"], "info", f"开始执行 {skill.name}", stage,
                       {"skill": skill.name, "score": cand["score"], "plan_step": step_id})
            out = task_dir / "skills" / skill.name
            self._ensure_skill_row(task["id"], skill, cand["score"])
            role = "engineer"
            if self.db:
                user = self.db.one("SELECT role FROM users WHERE id=?", (task.get("user_id"),))
                role = str((user or {}).get("role") or "engineer")
            approval_id = ""
            try:
                manifest = self.tools.manifest(skill)
                if manifest.approval == "required":
                    args = self.tools.arguments(task["id"], inputs, task["description"],
                                                self.tools.current_revision(task["id"]))
                    approval_id = self.tools.find_approved(
                        task["id"], manifest.tool_name, manifest.version,
                        self.tools.arguments_hash(args))
            except Exception as exc:  # noqa: BLE001 - 审批查询失败按无审批处理
                self._emit(task["id"], "warning", f"工具审批查询失败：{exc}", stage)
            tool_key = f"{task['id']}-{step_id}-{skill.name}-{time.time_ns()}"
            trace = task.get("_trace")
            if trace:
                trace.tool_call(
                    phase=stage, step_id=step_id, tool=skill.name, version=skill.version or "",
                    arguments={"inputs": [str(x) for x in inputs], "description": task.get("description") or "",
                               "score": cand.get("score"), "plan_step": step_id,
                               "approval_id": approval_id},
                    decision={"approval_id": approval_id, "source": cand.get("source") or "plan"},
                    sandbox={"kind": "subprocess", "cwd": str(skill.path),
                             "timeout_sec": self.config.get("skill_timeout_sec", 120)})
            self._session_note(task["id"], "tool_started", {
                "state": "fanout", "skill": skill.name, "plan_step": step_id,
                "score": cand["score"], "approval_id": approval_id,
            }, f"tool-start-{tool_key}")
            try:
                res = self.runner.run(skill, inputs, task["description"], out,
                                      cancel_event=cancel_event, task_id=task["id"],
                                      actor_role=role, approval_id=approval_id)
            except Exception as exc:
                if trace:
                    trace.error(phase=stage, step_id=step_id, code="skill_runner_error",
                                message=f"{skill.name}: {exc}", exc=exc, retryable=True,
                                details={"tool": skill.name, "tool_key": tool_key})
                raise
            res["skill_name"] = skill.name
            res["skill_title"] = skill.title
            res["required_sources"] = list(
                ((getattr(skill, "profile", {}) or {}).get("evidence") or {}
                 ).get("required_sources") or [])
            res["score"] = cand["score"]
            res["match_reason"] = cand.get("reason", "")
            res["plan_step"] = step_id
            res["evidence_ids"] = self.evidence_store.extend_legacy(
                task["id"], skill.name, skill.version or "", res.get("evidence") or [],
                source_root=out)
            self._persist_skill(task["id"], res, skill)
            if trace:
                trace.tool_result(
                    phase=stage, step_id=step_id, tool=skill.name,
                    version=skill.version or "", tool_call_id=res.get("tool_call_id") or "",
                    status=str(res.get("status") or ("done" if res.get("ok") else "failed")),
                    exit_code=res.get("exit_code"),
                    duration_ms=int(res.get("duration_ms") or 0),
                    stdout=str(res.get("stdout_tail") or ""),
                    stderr=str(res.get("stderr_tail") or ""),
                    error=str(res.get("error") or ""),
                    artifacts=res.get("artifacts"),
                    result={"ok": res.get("ok"), "status": res.get("status"),
                            "primary": res.get("primary"),
                            "root_cause": res.get("root_cause"),
                            "evidence_count": len(res.get("evidence") or []),
                            "limitations": (res.get("limitations") or [])[:10],
                            "summary": res.get("summary"),
                            "verdict": res.get("verdict"),
                            "reports": res.get("reports")})
            self._session_note(task["id"], "tool_finished", {
                "state": "fanout", "skill": skill.name, "plan_step": step_id,
                "status": res.get("status") or ("done" if res.get("ok") else "failed"),
                "ok": bool(res.get("ok")), "tool_call_id": res.get("tool_call_id") or "",
                "duration_ms": res.get("duration_ms"),
            }, f"tool-finish-{tool_key}")
            if res.get("evidence_ids"):
                self._session_note(task["id"], "evidence_added", {
                    "state": "fanout", "skill": skill.name,
                    "evidence_ids": res["evidence_ids"][:100],
                    "count": len(res["evidence_ids"]),
                }, f"evidence-{tool_key}")
            self._emit(task["id"],
                       "success" if res.get("ok") else "error",
                       f"{skill.name} 完成：{res.get('status')}"
                       + (f"（{res['root_cause']['name']}）" if res.get("root_cause") else ""),
                       stage,
                       {"skill": skill.name, "status": res.get("status"),
                        "primary": res.get("primary"), "plan_step": step_id,
                        "duration_ms": res.get("duration_ms"),
                        "error": res.get("error")})
            return res

        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="skill") as pool:
            futures = [pool.submit(_run_one, c) for c in candidates]
            for future in as_completed(futures):
                if cancel_event.is_set():
                    continue
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    results.append({"skill_name": "unknown", "ok": False, "status": "failed",
                                    "error": str(exc), "evidence": [], "root_cause": None,
                                    "limitations": []})
        order = {c["skill"]: i for i, c in enumerate(candidates)}
        results.sort(key=lambda r: order.get(r.get("skill_name", ""), 99))
        return results

    def _ensure_skill_row(self, task_id: str, skill: Skill, score: float) -> None:
        now = iso_now()
        self.db.execute(
            "INSERT OR IGNORE INTO task_skills(task_id, skill_name, skill_version, status, "
            "score, started_at) VALUES(?,?,?,?,?,?)",
            (task_id, skill.name, skill.version, "running", score, now))
        self.db.execute(
            "UPDATE task_skills SET invalidated=0 WHERE task_id=? AND skill_name=?",
            (task_id, skill.name))

    def _persist_skill(self, task_id: str, res: dict[str, Any], skill: Skill) -> None:
        now = iso_now()
        self.db.execute(
            "UPDATE task_skills SET status=?, verdict_json=?, summary_json=?, result_json=?, "
            "report_md_path=?, report_html_path=?, error=?, started_at=COALESCE(started_at,?), "
            "finished_at=?, stats_json=?, invalidated=0 WHERE task_id=? AND skill_name=?",
            (res.get("status") or ("done" if res.get("ok") else "failed"),
             json.dumps(res.get("verdict") or {}, ensure_ascii=False, default=str),
             json.dumps(res.get("summary") or {}, ensure_ascii=False, default=str),
             json.dumps({k: res.get(k) for k in (
                 "ok", "status", "primary", "root_cause", "evidence", "match_counts",
                 "glob_fallback", "sites", "limitations", "verdict", "rule_meta", "evidence_ids",
                 "required_sources", "evidence_quality")},
                 ensure_ascii=False, default=str),
             (res.get("reports") or {}).get("md") or None,
             (res.get("reports") or {}).get("html") or None,
             res.get("error"),
             now, now,
             json.dumps({"duration_ms": res.get("duration_ms"), "tokens": res.get("tokens")},
                        ensure_ascii=False),
             task_id, skill.name),
        )

    # ---------- 一次有界补规划 ----------
    def _maybe_replan(self, task: dict[str, Any], plan: dict[str, Any],
                      results: list[dict[str, Any]], executed: set[str],
                      inputs: list[Path], inventory: dict[str, Any],
                      all_skills: list[Skill], scores: dict[str, float],
                      task_dir: Path, budget: Budget, models_used: set[str],
                      cancel_event: threading.Event) -> dict[str, Any] | None:
        if not self.config.get("planner_replan", True) or plan.get("source") != "ai":
            return None
        role = self._planner_role()
        if not role:
            return None
        remaining = [s for s in all_skills if s.name not in executed]
        need, why = needs_replan(results, [s.name for s in remaining])
        if not need:
            self._emit(task["id"], "info", f"不补规划：{why}", "fanout")
            return {"status": "not_needed", "reason": why, "steps": [], "results": results,
                    "executed": executed, "plan": {"steps": []}}
        if cancel_event.is_set():
            return None
        self._stage(task, "replanning", 65, f"证据仍有缺口，触发一次补规划：{why}")
        self._emit(task["id"], "warning", f"补规划触发：{why}", "replanning")
        catalog = []
        for s in remaining[:20]:
            entry = s.catalog_entry()
            entry["rule_score"] = round(float(scores.get(s.name, 0.0)), 1)
            catalog.append(entry)
        options = _json_load(task.get("options_json"), {})
        experiences = self.experiences.search(
            task["description"], inventory.get("detected_hints") or [],
            (results[0].get("skill_name") if results else ""), limit=3,
            include_unreviewed=bool(self.config.get("experience_include_unreviewed", False)),
            device_type=str(options.get("device_type") or ""),
            software_version=str(options.get("software_version") or ""),
            exclude_task=str(task.get("id") or ""), with_counterexamples=True)
        executed_summary = []
        for r in results:
            root = r.get("root_cause") or {}
            verdict = r.get("verdict") or {}
            executed_summary.append({
                "skill": r.get("skill_name"),
                "status": verdict.get("status") or r.get("status"),
                "ok": r.get("ok"),
                "root_cause": root.get("name") if isinstance(root, dict) else None,
                "confidence": root.get("confidence") if isinstance(root, dict) else None,
                "evidence": len(r.get("evidence") or []),
                "limitations": (r.get("limitations") or [])[:3],
                "error": r.get("error"),
            })
        mode = task.get("mode") or "fast"
        max_new = plan_skill_limit(self.config, mode, replan=True)
        max_steps = int(self.config.get("planner_max_steps", 4))
        try:
            result = self.gateway.complete(
                role, REPLANNER_SYSTEM,
                replanner_user(task["description"],
                               {"files": inventory.get("files"),
                                "members": inventory.get("total_members"),
                                "hints": inventory.get("detected_hints")},
                               executed_summary, catalog, experiences,
                               compact_plan(plan), max_new, max_steps),
                json_schema=PLAN_SCHEMA, purpose="replan", budget=budget,
                model_override=task.get("model_override"),
                trace=task.get("_trace"))
            models_used.add(result.model_name)
            new_plan, errors, warnings = normalize_plan(
                result.data, catalog, self.config, mode, executed=executed,
                source="ai", replan=True, allow_empty=True)
            if task.get("_trace"):
                task["_trace"].validation(phase="replanning", name="replan_normalize",
                                           ok=not errors, errors=errors[:20],
                                           details={"summary": new_plan.get("summary"),
                                                    "steps": new_plan.get("steps")})
            if errors:
                self._emit(task["id"], "warning",
                           "补规划未通过校验，不再追加：" + "；".join(errors[:3]), "replanning")
                return {"status": "failed", "reason": "；".join(errors[:3]),
                        "steps": [], "results": results, "executed": executed,
                        "plan": new_plan}
            if not new_plan.get("in_scope") or not new_plan.get("steps"):
                self._emit(task["id"], "info",
                           "补规划认为现有证据已足够，不再追加技能", "replanning")
                return {"status": "no_new_skills",
                        "reason": new_plan.get("summary") or "证据已足够",
                        "steps": [], "results": results, "executed": executed,
                        "plan": new_plan, "notes": warnings}
            new_plan["replan"] = True
            new_plan["replan_reason"] = why
            new_plan["experience_refs"] = [
                {"task_id": e.get("task_id"), "root_cause": e.get("root_cause"),
                 "skill": e.get("skill")} for e in experiences]
            next_results, next_executed = self._execute_plan(
                task, new_plan, inputs, task_dir, cancel_event, budget,
                models_used, results, scores=scores, stage="replanning")
            candidates = plan_candidates(new_plan, scores,
                                         float(self.config.get("min_route_score", 30)))
            self._emit(task["id"], "info",
                       f"补规划执行完成：新增 {len(candidates)} 个技能，"
                       f"累计 {len(next_results)} 个技能结果", "replanning")
            return {
                "status": "executed",
                "reason": why,
                "summary": new_plan.get("summary") or "",
                "steps": [{k: s.get(k) for k in ("id", "title", "objective", "skills",
                                                  "gate", "gate_label", "status", "reason")}
                          for s in new_plan.get("steps") or []],
                "candidates": candidates,
                "plan": new_plan,
                "results": next_results,
                "executed": next_executed,
                "notes": warnings,
            }
        except (AIError, NoModelError) as exc:
            self._emit(task["id"], "warning", f"补规划失败，不再追加：{exc}", "replanning")
            return {"status": "failed", "reason": str(exc), "steps": [],
                    "results": results, "executed": executed, "plan": {"steps": []}}

    # ---------- 仲裁 ----------
    def _arbitrate(self, task: dict[str, Any], results: list[dict[str, Any]],
                   budget: Budget, models_used: set[str]) -> dict[str, Any]:
        scored = []
        for res in results:
            if not res.get("ok"):
                continue
            quality = assess_evidence(res)
            res["evidence_quality"] = quality
            score = float(res.get("score") or 0) * 0.25
            verdict = res.get("verdict") or {}
            status = verdict.get("status") or res.get("status")
            root = res.get("root_cause") or {}
            conf = (root.get("confidence") or "medium").lower()
            if status == "matched":
                score += 40
            elif status == "temporal_conflict":
                score += 5
            else:
                score -= 25
            score += {"high": 8, "medium": 2, "low": -10}.get(conf, 0)
            # 证据质量是主信号，不再按命中条数线性加分，避免同一条日志刷分。
            score += quality["quality_score"] * 0.45
            score += min(10, max(0, len(quality["independent_files"]) - 1) * 3)
            if res.get("glob_fallback"):
                score -= 8
            if res.get("limitations") and any("triage" in x for x in res["limitations"]):
                score -= 10
            scored.append({**res, "arb_score": round(score, 1)})
        scored.sort(key=lambda r: r["arb_score"], reverse=True)
        matched = [r for r in scored if (r.get("verdict") or {}).get("status") == "matched"]

        def _candidate(row: dict[str, Any]) -> dict[str, Any]:
            quality = row.get("evidence_quality") or {}
            return {
                "skill": row["skill_name"],
                "title": row.get("skill_title"),
                "route_score": row.get("score"),
                "arb_score": row.get("arb_score"),
                "status": (row.get("verdict") or {}).get("status"),
                "primary": row.get("primary"),
                "root_cause": (row.get("root_cause") or {}).get("name"),
                "root_cause_id": (row.get("root_cause") or {}).get("id"),
                "confidence": quality.get("confidence") or (row.get("root_cause") or {}).get("confidence"),
                "static_confidence": (row.get("root_cause") or {}).get("confidence"),
                "hit_rules": (row.get("verdict") or {}).get("groups", {}).get("ALL", {}).get("hit_rules") or [],
                "match_counts": row.get("match_counts") or {},
                "temporal_ok": (row.get("verdict") or {}).get("groups", {}).get("ALL", {}).get("root_causes"),
                "glob_fallback": bool(row.get("glob_fallback")),
                "evidence_quality": {k: quality.get(k) for k in (
                    "quality_score", "confidence", "unique_clusters", "supporting_clusters",
                    "unrelated_clusters", "duplicate_occurrences", "direct_rules",
                    "independent_files", "reasons")},
                "evidence": [{k: (self.egress.sanitize_text(e.get(k), limit=240)
                                   if k == "text" else e.get(k)) for k in (
                                   "rule_id", "file", "line_no", "timestamp", "text")}
                             for e in (row.get("evidence") or [])[:4]],
                "limitations": row.get("limitations") or [],
            }

        candidates = [_candidate(r) for r in scored[:4]]

        if not matched:
            best = scored[0] if scored else None
            status = "unmatched"
            if best and (best.get("verdict") or {}).get("status") == "temporal_conflict":
                status = "temporal_conflict"
            elif best and (best.get("evidence_quality") or {}).get("raw_evidence", 0) > 0:
                # 有异常证据但没有任何已知根因条件成立：这是未知故障，不是“日志正常”。
                status = "unknown_fault"
            reason = {
                "unknown_fault": "发现异常证据，但没有任何已知根因条件成立；不做强行归因，建议补充采集或创建新技能。",
                "temporal_conflict": "规则条件成立但时序不满足，暂不归因。",
            }.get(status, "所有子技能均未匹配到已知根因；不做强行归因。" if scored
                  else "没有可用的技能结果")
            return {
                "status": status,
                "chosen_skill": "",
                "root_cause": "",
                "confidence": "low",
                "reason": reason,
                "conflicts": [f"{r['skill_name']}:{((r.get('verdict') or {}).get('status'))}" for r in scored],
                "missing_info": self._missing_hints(scored),
                "candidates": candidates,
                "llm_used": False,
            }

        top = matched[0]
        second = matched[1] if len(matched) > 1 else None
        margin = top["arb_score"] - (second["arb_score"] if second else 0)
        deterministic_ok = (
            (top.get("root_cause") or {}).get("confidence") in ("high", "medium")
            and margin >= 18
        )
        chosen = top
        if second:
            reason = (f"确定性仲裁：{top['skill_name']} 证据最强"
                      f"（综合分 {top['arb_score']:.0f}，领先 {margin:.0f}）")
        else:
            reason = f"确定性仲裁：仅 {top['skill_name']} 命中已知根因，采信其结论"
        llm_used = False
        data: dict[str, Any] = {}
        if not deterministic_ok and self.gateway.has_real_model("arbiter") and self.gateway.available("arbiter"):
            try:
                source_payload = {"description": task.get("description") or "",
                                  "candidates": candidates}
                safe_payload = self.egress.sanitize_structure(source_payload)
                self._audit_model_egress(task, "arbiter", source_payload, safe_payload)
                result = self.gateway.complete(
                    "arbiter", ARBITER_SYSTEM,
                    arbiter_user(safe_payload["description"], safe_payload["candidates"]),
                    json_schema=ARBITER_SCHEMA, purpose="adjudicate", budget=budget,
                    model_override=task.get("model_override"),
                    trace=task.get("_trace"))
                llm_used = True
                models_used.add(result.model_name)
                data = result.data or {}
                if task.get("_trace"):
                    task["_trace"].validation(phase="arbitration", name="arbiter_choice",
                                               ok=bool(data.get("decision")),
                                               errors=[] if data.get("decision") else ["模型未给出 decision"],
                                               details={"decision": data.get("decision"),
                                                        "chosen_skill": data.get("chosen_skill")})
                decision = data.get("decision")
                if decision == "choose":
                    picked = next((r for r in matched if r["skill_name"] == data.get("chosen_skill")), None)
                    if picked:
                        chosen = picked
                        reason = "LLM 仲裁：" + str(data.get("reason") or "")
                    else:
                        reason += "；LLM 指定技能不存在，沿用确定性结果"
                elif decision == "insufficient":
                    return {
                        "status": "insufficient",
                        "chosen_skill": "",
                        "root_cause": "",
                        "confidence": "low",
                        "reason": "LLM 仲裁认为证据不足以定论：" + str(data.get("reason") or ""),
                        "conflicts": data.get("conflicts") or [],
                        "missing_info": data.get("missing_info") or self._missing_hints(scored),
                        "candidates": candidates,
                        "llm_used": True,
                    }
                else:
                    reason += "；LLM 判定未匹配"
            except (AIError, NoModelError) as exc:
                self._emit(task["id"], "warning", f"LLM 仲裁失败，回退确定性结果：{exc}", "arbitration")

        # LLM 可下调置信度；取更低者，防止过度自信
        llm_conf = data.get("confidence") if llm_used else None
        root_conf = ((chosen.get("root_cause") or {}).get("confidence") or "medium").lower()
        if llm_conf in CONF_RANK and CONF_RANK[llm_conf] < CONF_RANK.get(root_conf, 2):
            root_conf = llm_conf
        # 多根因：只有不同根因各自具备独立、非低质量证据时才并列输出。
        root_groups: list[dict[str, Any]] = []
        seen_roots: set[str] = set()
        for row in matched:
            name = str((row.get("root_cause") or {}).get("name") or "").strip()
            key = re.sub(r"\s+", "", name.lower())
            if not key or key in seen_roots:
                continue
            seen_roots.add(key)
            quality = row.get("evidence_quality") or {}
            root_groups.append({
                "name": name,
                "root_cause_id": (row.get("root_cause") or {}).get("id"),
                "skill": row["skill_name"],
                "confidence": quality.get("confidence") or "low",
                "quality_score": quality.get("quality_score", 0),
                "independent_files": len(quality.get("independent_files") or []),
                "arb_score": row.get("arb_score"),
            })
        root_groups.sort(key=lambda x: float(x.get("arb_score") or 0), reverse=True)
        multi_roots = [g for g in root_groups
                       if g["confidence"] in ("high", "medium")
                       and float(g["quality_score"]) >= 50]
        is_multi = len(multi_roots) >= 2
        roots_payload = multi_roots[:4] if is_multi else root_groups[:1]
        final_status = "multiple_causes" if is_multi else "matched"
        if is_multi:
            root_conf = "high" if all(g["confidence"] == "high" for g in multi_roots[:3]) else "medium"
            reason = (f"检测到 {len(multi_roots)} 个相互独立的根因："
                      + "；".join(f"{g['name']}({g['quality_score']}/100)" for g in multi_roots[:3])
                      + "。每个根因均有独立证据支撑，需并行处置并分别验证。")
        return {
            "status": final_status,
            "chosen_skill": chosen["skill_name"],
            "root_cause": chosen.get("primary"),
            "root_cause_name": (chosen.get("root_cause") or {}).get("name"),
            "confidence": root_conf,
            "reason": reason,
            "roots": roots_payload,
            "evidence_quality": (chosen.get("evidence_quality") or {}),
            "conflicts": [f"{r['skill_name']}:{r.get('primary')}" for r in matched[1:3]],
            "missing_info": self._missing_hints(scored),
            "candidates": candidates,
            "llm_used": llm_used,
        }

    def _missing_hints(self, scored: list[dict[str, Any]]) -> list[str]:
        hints: list[str] = []
        for r in scored[:3]:
            for item in r.get("limitations") or []:
                if "glob 兜底" in item or "不受支持" in item or "triage" in item:
                    hints.append(f"{r['skill_name']}：{item}")
            quality = r.get("evidence_quality") or {}
            if quality.get("confidence") == "low" and quality.get("reasons"):
                hints.append(f"{r['skill_name']}：证据质量低（{quality['reasons'][0]}）")
        hints.append("如结论与现网不符，请补充同网元的完整采集包（含 collectinfo、告警表与对应单板日志）")
        return hints

    # ---------- 报告 ----------
    def _report(self, task: dict[str, Any], results: list[dict[str, Any]],
                arbitration: dict[str, Any], inventory: dict[str, Any],
                stage_times: dict[str, float], budget: Budget,
                models_used: set[str], started: float,
                plan: dict[str, Any] | None = None) -> tuple[
                    str, str, dict[str, Any] | None, dict[str, Any],
                    dict[str, Any], dict[str, Any]]:
        chosen_name = arbitration.get("chosen_skill")
        chosen = next((r for r in results if r["skill_name"] == chosen_name and r.get("ok")), None)
        claims = build_claims(task, results, arbitration)
        claim_check = validate_claims(
            claims, evidence=(chosen or {}).get("evidence") or [],
            arbitration=arbitration)
        narrative = None
        narrative_check: dict[str, Any] = {"ok": True, "skipped": True, "errors": []}
        if not claim_check.get("ok"):
            self._emit(task["id"], "warning",
                       "结构化 claim 校验未通过，报告只保留确定性模板：" +
                       "；".join(f"{x.get('claim')}/{x.get('code')}"
                                for x in (claim_check.get("errors") or [])[:4]),
                       "reporting")
        if chosen and claim_check.get("ok") and self.config.get("report_llm") \
                and self.gateway.has_real_model("writer") \
                and self.gateway.available("writer"):
            facts = build_narrative_facts(task, chosen, arbitration)
            facts["并行技能"] = [{"skill": r.get("skill_name"), "status": r.get("status"),
                                "root_cause": (r.get("root_cause") or {}).get("name")}
                                for r in results]
            evidence = evidence_for_prompt(chosen)
            try:
                source_payload = {"facts": facts, "evidence": evidence}
                safe_payload = self.egress.sanitize_structure(source_payload)
                self._audit_model_egress(task, "writer", source_payload, safe_payload)
                result = self.gateway.complete(
                    "writer", REPORT_SYSTEM, report_user(safe_payload["facts"], safe_payload["evidence"]),
                    json_schema=REPORT_SCHEMA, purpose="report", budget=budget,
                    model_override=task.get("model_override"),
                    trace=task.get("_trace"))
                models_used.add(result.model_name)
                data = result.data or {}
                bad = validate_citations(
                    (data.get("headline") or "") + "\n" + (data.get("analysis") or ""),
                    len(evidence))
                if bad:
                    if task.get("_trace"):
                        task["_trace"].validation(phase="reporting", name="report_citations",
                                                   ok=False, errors=bad[:20],
                                                   details={"headline": data.get("headline"),
                                                            "analysis": data.get("analysis")})
                    self._emit(task["id"], "warning",
                               "报告研判引用不存在的证据，已回退确定性模板：" + "；".join(bad[:3]),
                               "reporting")
                else:
                    narrative_check = validate_narrative(
                        data, claims, evidence=(chosen or {}).get("evidence") or [])
                    if task.get("_trace"):
                        task["_trace"].validation(
                            phase="reporting", name="report_narrative",
                            ok=bool(narrative_check.get("ok")),
                            errors=(narrative_check.get("errors") or [])[:20],
                            details={"warnings": (narrative_check.get("warnings") or [])[:20],
                                     "claims": len(claims or [])})
                    if narrative_check.get("ok"):
                        narrative = data
                    else:
                        self._emit(task["id"], "warning",
                                   "模型叙述含未支撑数字/时间或过度断言，已回退确定性模板",
                                   "reporting")
            except (AIError, NoModelError) as exc:
                self._emit(task["id"], "warning",
                           f"LLM 报告研判失败，使用确定性模板：{exc}", "reporting")

        stage_times.setdefault("reporting", time.time())
        stats = self._stats(started, stage_times, budget, models_used)
        md = compose_markdown(task, results, arbitration, inventory, stats, narrative, plan)
        md += claims_markdown(claims, claim_check, narrative_check)
        return (md, render_html(md, f"{task['title']} - 诊断报告",
                                self.config.get("brand")), narrative,
                claims, claim_check, narrative_check)

    def _stats(self, started: float, stage_times: dict[str, float],
               budget: Budget, models_used: set[str]) -> dict[str, Any]:
        now = time.time()
        ordered = ["start", "intake", "planning", "fanout", "arbitration", "reporting"]
        durations = {}
        prev = started
        for key in ordered[1:]:
            if key in stage_times:
                durations[key] = round(stage_times[key] - prev, 2)
                prev = stage_times[key]
        durations["total"] = round(now - started, 2)
        snap = budget.snapshot()
        return {
            "duration_sec": durations["total"],
            "stage_durations": durations,
            "llm_calls": snap["calls"],
            "total_tokens": snap["total_tokens"],
            "input_tokens": snap["input_tokens"],
            "output_tokens": snap["output_tokens"],
            "models": sorted(models_used),
            "budget": snap,
        }

    # ---------- 终止路径 ----------
    def _needs_data(self, task, task_dir, stage_times, budget, missing: list[str],
                    reason: str, inventory: dict[str, Any],
                    routing: dict[str, Any] | None = None,
                    plan: dict[str, Any] | None = None) -> dict[str, Any]:
        trace = task.get("_trace")
        if trace:
            trace.append("decision", status="needs_data", phase="arbitration",
                         reason=reason, missing_info=missing[:20])
        arbitration = {
            "status": "needs_data",
            "chosen_skill": "",
            "root_cause": "",
            "confidence": "low",
            "reason": reason,
            "missing_info": missing,
            "candidates": (routing or {}).get("candidates") or [],
            "llm_used": False,
        }
        stats = self._stats(time.time(), stage_times, budget, set())
        md = compose_markdown(task, [], arbitration, inventory, stats, None, plan)
        md_path = write_text(task_dir / "report.md", md)
        html_path = write_text(task_dir / "report.html",
                               render_html(md, f"{task['title']} - 诊断报告",
                                           self.config.get("brand")))
        result = {"verdict": {"status": "needs_data", "primary": None, "confidence": "low"},
                  "arbitration": arbitration, "routing": routing or {},
                  "plan": plan or {}, "stats": stats}
        self.db.execute(
            "UPDATE tasks SET status='needs_data', stage='done', progress=100, finished_at=?, "
            "verdict_json=?, result_json=?, report_md=?, report_html=?, stats_json=? WHERE id=?",
            (iso_now(), json.dumps(result["verdict"], ensure_ascii=False),
             json.dumps(result, ensure_ascii=False, default=str),
             str(md_path), str(html_path), json.dumps(stats, ensure_ascii=False), task["id"]))
        self._emit(task["id"], "warning", f"需要补充数据：{reason}", "done",
                   {"missing_info": missing, "report": {"md": str(md_path), "html": str(html_path)}})
        session = DiagnosticSession(self.db, task["id"])
        session_round = int(session.snapshot().get("supplement_round") or 0)
        questions = [{
            "question_id": f"Q{i + 1}", "question": text, "blocking": True,
            "distinguishes": [c.get("skill") for c in (routing or {}).get("candidates") or []][:3],
        } for i, text in enumerate(missing[:3])]
        self._session_note(task["id"], "gaps_updated", {
            "state": "needs_data", "gaps": questions, "reason": reason,
        }, f"gaps-{session_round}")
        self._session_note(task["id"], "decision_summary", {
            "state": "needs_data", "verdict": result["verdict"], "reason": reason,
            "evidence_count": self.evidence_store.count(task["id"]),
            "report": {"md": str(md_path), "html": str(html_path)},
        }, f"decision-{session_round}")
        create_notification(self.db, task["user_id"], "task_failed", "任务需要补充数据",
                            f"{task['title']}：{reason}", f"/#/tasks/{task['id']}")
        return result

    def _finish_cancelled(self, task, task_dir, results=None) -> dict[str, Any]:
        trace = task.get("_trace")
        if trace:
            trace.append("decision", status="cancelled", phase="run", reason="用户取消")
        self.db.execute(
            "UPDATE tasks SET status='cancelled', stage='done', progress=100, finished_at=?, error=? WHERE id=?",
            (iso_now(), "用户取消", task["id"]))
        self._emit(task["id"], "warning", "任务已取消", "done", {})
        self._session_note(task["id"], "decision_summary", {
            "state": "cancelled", "reason": "用户取消",
        }, "decision-cancelled")
        create_notification(self.db, task["user_id"], "task_failed", "任务已取消",
                            task["title"], f"/#/tasks/{task['id']}")
        return {"status": "cancelled"}

    def _session_note(self, task_id: str, event_type: str, payload: dict[str, Any],
                      event_key: str) -> None:
        try:
            DiagnosticSession(self.db, task_id).append(
                event_type, payload, event_key=event_key)
        except Exception as exc:  # noqa: BLE001 - 会话留痕失败不应覆盖已经生成的报告
            self._emit(task_id, "warning", f"诊断会话写入失败：{exc}", "done")

    @staticmethod
    def _hypothesis_id(skill_name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(skill_name or "")).strip("-") or "candidate"
        return "H-" + slug[:40]

    def _init_hypotheses(self, task: dict[str, Any], session: DiagnosticSession,
                         plan: dict[str, Any], routing: dict[str, Any]) -> None:
        """把计划候选技能变成可更新的 HypothesisState，而不是只写一句计划文本。"""
        try:
            existing = session.snapshot().get("hypotheses") or {}
        except Exception:
            existing = {}
        names = plan_skill_names(plan) or [
            str(x.get("skill")) for x in routing.get("candidates") or [] if x.get("skill")]
        options = {}
        try:
            options = json.loads(task.get("options_json") or "{}")
        except json.JSONDecodeError:
            options = {}
        for name in names[:12]:
            hid = self._hypothesis_id(name)
            if hid in existing:
                continue
            skill = self.registry.get(name)
            statement = (f"{getattr(skill, 'title', '') or name} 可能是当前故障根因"
                         if skill else f"技能 {name} 可能是当前故障根因")
            hypothesis = {
                "hypothesis_id": hid, "statement": statement, "skill": name,
                "status": "open", "state": "planned",
                "applicability": {
                    "site": options.get("site") or "",
                    "device_type": options.get("device_type") or "",
                    "required_sources": _skill_source_hints(skill),
                },
                "necessary_checks": [
                    "至少一个规则命中且证据可定位",
                    "证据来自至少两个独立来源或专家确认",
                    "反证/近似故障未被同时高置信命中",
                ],
                "prior_score": float((next(
                    (x.get("score") for x in routing.get("candidates") or []
                     if x.get("skill") == name), 30) or 30)) / 100.0,
                "supporting_evidence": [], "contradicting_evidence": [],
                "missing_data": [], "seen_count": 0,
            }
            self._session_note(task["id"], "hypothesis_updated",
                               {"hypothesis": hypothesis, "hypothesis_state": "planned"},
                               f"hyp-init-{hid}")

    def _sync_hypotheses(self, task: dict[str, Any], session: DiagnosticSession,
                         results: list[dict[str, Any]],
                         arbitration: dict[str, Any] | None = None) -> None:
        """按技能结果更新假设；同一状态和证据集不重复写事件。"""
        try:
            existing = session.snapshot().get("hypotheses") or {}
        except Exception:
            existing = {}
        chosen = str((arbitration or {}).get("chosen_skill") or "")
        for result in results:
            name = str(result.get("skill_name") or "")
            if not name:
                continue
            hid = self._hypothesis_id(name)
            old = dict(existing.get(hid) or {})
            if not old:
                continue
            root = result.get("root_cause") or {}
            evidence_ids = [str(x) for x in (result.get("evidence_ids") or []) if x]
            quality = result.get("evidence_quality") or {}
            status = str(old.get("status") or "open")
            missing = list(old.get("missing_data") or [])
            reason = ""
            if result.get("ok") and root:
                if quality.get("confidence") in ("high", "medium"):
                    status = "supported"
                    reason = f"技能命中根因 {root.get('name') or ''}，证据质量 {quality.get('confidence')}"
                else:
                    status = "weakened"
                    reason = f"技能命中候选根因，但证据质量 {quality.get('confidence') or 'low'}"
            elif result.get("status") in ("needs_data", "unmatched", "inconclusive"):
                status = "weakened" if status != "confirmed" else status
                reason = f"技能结果 {result.get('status')}，未形成确定结论"
                for item in result.get("limitations") or []:
                    if str(item) not in missing:
                        missing.append(str(item))
            elif not result.get("ok"):
                status = "refuted" if str(result.get("status")) in ("missing", "denied", "failed") \
                    else "weakened"
                reason = f"技能未完成：{result.get('status')} {result.get('error') or ''}"
            if arbitration and chosen:
                if name == chosen and arbitration.get("status") in (
                        "matched", "multiple_causes", "temporal_conflict"):
                    status = "confirmed"
                    reason = f"仲裁选择 {name} 作为最终根因"
                elif status == "supported":
                    status = "weakened"
                    reason = f"同题其他候选 {chosen} 已获仲裁采信"
            hypothesis = {
                **old,
                "status": status,
                "supporting_evidence": evidence_ids or list(old.get("supporting_evidence") or []),
                "contradicting_evidence": list(old.get("contradicting_evidence") or []),
                "missing_data": missing[:8],
                "seen_count": int(old.get("seen_count") or 0) + 1,
                "last_result": {
                    "status": result.get("status"),
                    "root_cause": root.get("name"),
                    "confidence": quality.get("confidence"),
                },
            }
            changed = any(hypothesis.get(k) != old.get(k) for k in (
                "status", "supporting_evidence", "missing_data", "seen_count", "last_result"))
            if not changed:
                continue
            digest = hashlib.sha256(
                ("|".join(evidence_ids) + "|" + status).encode("utf-8")).hexdigest()[:10]
            state = ("confirmed" if status == "confirmed" else
                     "supported" if status == "supported" else
                     "refuted" if status == "refuted" else "evaluating")
            self._session_note(task["id"], "hypothesis_updated",
                               {"hypothesis": hypothesis, "hypothesis_state": state},
                               f"hyp-{hid}-{status}-{digest}")

    def _fail(self, task, task_dir, error: str, stage_times, budget) -> dict[str, Any]:
        trace = task.get("_trace")
        if trace:
            trace.error(phase="run", code="task_failed", message=error, retryable=False)
        stats = self._stats(time.time(), stage_times, budget, set())
        self.db.execute(
            "UPDATE tasks SET status='failed', stage='done', progress=100, finished_at=?, "
            "error=?, stats_json=? WHERE id=?",
            (iso_now(), error[:2000], json.dumps(stats, ensure_ascii=False), task["id"]))
        self._emit(task["id"], "error", f"任务失败：{error}", "done", {"error": error})
        self._session_note(task["id"], "decision_summary", {
            "state": "failed", "reason": error[:300],
        }, f"decision-failed-{int(time.time())}")
        create_notification(self.db, task["user_id"], "task_failed", "任务执行失败",
                            f"{task['title']}：{error[:300]}", f"/#/tasks/{task['id']}")
        return {"status": "failed", "error": error}
