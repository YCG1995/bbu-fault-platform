"""AI 诊断计划：结构化波次、门槛、校验与确定性回退。

计划只描述“下一步跑什么、为什么、什么时候停”，不携带任何未经日志验证的根因。
真正的根因仍然只能由子 Skill 的规则与证据链给出。
"""
from __future__ import annotations

import re
from typing import Any

GATE_LABELS = {
    "always": "无条件",
    "previous_unmatched": "前序未命中",
    "previous_conflict": "前序结论冲突",
    "previous_low_confidence": "前序置信度不足",
    "previous_gap": "前序存在证据缺口",
}
GATES = set(GATE_LABELS)
STEP_ID_RE = re.compile(r"^[A-Za-z]?[0-9]{1,2}$")


def _clip(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _strings(value: Any, limit: int, text_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _clip(item, text_limit)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def plan_skill_limit(config: Any, mode: str, replan: bool = False) -> int:
    if replan:
        return max(1, int(config.get("planner_replan_max_skills",
                                      config.get("deep_max_skills", 4))))
    key = "deep_max_skills" if mode == "deep" else "fast_max_skills"
    return max(1, int(config.get(key, 2)))


def normalize_plan(data: Any, catalog: list[dict[str, Any]], config: Any, mode: str,
                   *, executed: set[str] | None = None, source: str = "ai",
                   replan: bool = False, allow_empty: bool = False) -> tuple[dict[str, Any], list[str], list[str]]:
    """校验并收敛模型计划；返回 (plan, fatal_errors, warnings)。

    - 未知技能、重复技能、超预算步骤会被删除并记录 warning；
    - 一个可执行技能都不剩时返回 fatal error，由编排器回退确定性计划；
    - 所有裁剪都是确定性的，绝不再次调用模型修复计划。
    """
    executed = set(executed or set())
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        return _empty_plan(source), ["计划不是 JSON 对象"], warnings

    in_scope = data.get("in_scope") is not False
    summary = _clip(data.get("summary"), 260)
    hypothesis = _strings(data.get("hypothesis"), 3, 220)
    missing = _strings(data.get("missing_info"), 8, 240)
    raw_steps = data.get("steps")
    if raw_steps is None:
        raw_steps = []
    if not isinstance(raw_steps, list):
        errors.append("steps 必须是数组")
        raw_steps = []

    max_steps = max(1, int(config.get("planner_max_steps", 4)))
    max_skills = plan_skill_limit(config, mode, replan=replan)
    max_total = max(max_skills, int(config.get("planner_max_total_skills", 8)))
    max_per_step = max(1, int(config.get("max_skill_parallel", 4)))
    if replan:
        max_total = min(max_total, max_skills + max_per_step)

    names = {str(c.get("skill") or "") for c in catalog if c.get("skill")}
    steps: list[dict[str, Any]] = []
    used: set[str] = set()
    seen_ids: set[str] = set()
    for idx, raw in enumerate(raw_steps[:max_steps], start=1):
        if not isinstance(raw, dict):
            warnings.append(f"第 {idx} 步不是对象，已丢弃")
            continue
        sid = _clip(raw.get("id"), 10) or f"P{idx}"
        if not STEP_ID_RE.fullmatch(sid) or sid in seen_ids:
            sid = f"P{idx}"
        seen_ids.add(sid)
        gate = str(raw.get("gate") or "always")
        if gate not in GATES:
            warnings.append(f"{sid} 的 gate={gate!r} 非法，按 always 处理")
            gate = "always"
        if not replan and not steps and gate != "always":
            warnings.append(f"{sid} 是首步，gate 强制改为 always")
            gate = "always"

        raw_skills = raw.get("skills") if isinstance(raw.get("skills"), list) else []
        step_skills: list[str] = []
        for item in raw_skills:
            name = str(item or "").strip()
            if not name:
                continue
            if name not in names:
                warnings.append(f"忽略目录外技能：{name}")
                continue
            if name in executed:
                warnings.append(f"忽略已执行技能：{name}")
                continue
            if name in used:
                warnings.append(f"忽略重复技能：{name}")
                continue
            if name in step_skills:
                continue
            if len(step_skills) >= max_per_step:
                warnings.append(f"{sid} 超过单步并行上限 {max_per_step}，忽略 {name}")
                continue
            if len(used) >= max_total:
                warnings.append(f"计划超过总技能上限 {max_total}，忽略 {name}")
                continue
            step_skills.append(name)
            used.add(name)
        if not step_skills:
            warnings.append(f"{sid} 没有可用技能，已跳过")
            continue
        steps.append({
            "id": sid,
            "title": _clip(raw.get("title"), 80) or f"步骤 {idx}",
            "objective": _clip(raw.get("objective"), 220),
            "skills": step_skills,
            "gate": gate,
            "gate_label": GATE_LABELS[gate],
            "reason": _clip(raw.get("reason"), 260),
        })

    if len(raw_steps) > max_steps:
        warnings.append(f"计划步骤超过上限 {max_steps}，已截断")
    if in_scope and not steps and not allow_empty:
        errors.append("计划未包含任何可执行技能")
    if not in_scope:
        steps = []
        used.clear()
        if not missing:
            missing = ["当前技能库中没有匹配本次故障的子 Skill，请补充日志或先构建技能"]
    if not summary:
        summary = "AI 未给出计划摘要" if in_scope else "AI 判断超出当前技能覆盖范围"
    return ({
        "source": source,
        "task_title": _clip(data.get("task_title"), 40),
        "in_scope": in_scope,
        "summary": summary,
        "hypothesis": hypothesis,
        "steps": steps,
        "missing_info": missing,
        "replan": False,
    }, errors, warnings)


def _empty_plan(source: str = "ai") -> dict[str, Any]:
    return {"source": source, "in_scope": False, "summary": "计划为空",
            "task_title": "", "hypothesis": [], "steps": [], "missing_info": [], "replan": False}


def deterministic_plan(candidates: list[dict[str, Any]], config: Any,
                       mode: str) -> dict[str, Any]:
    """无真实 planner 模型 / 计划非法时的保底：所有候选技能一波并行。"""
    limit = plan_skill_limit(config, mode)
    names: list[str] = []
    for cand in candidates:
        name = str(cand.get("skill") or "")
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    if not names:
        return _empty_plan("deterministic")
    return {
        "source": "deterministic",
        "in_scope": True,
        "summary": f"确定性回退：并行验证 {len(names)} 个候选技能",
        "hypothesis": ["按规则匹配分与日志签名选择候选技能"],
        "steps": [{
            "id": "P1",
            "title": "候选技能并行验证",
            "objective": "在不扩大调用预算的前提下，用现有技能规则验证最可能的故障方向",
            "skills": names,
            "gate": "always",
            "gate_label": GATE_LABELS["always"],
            "reason": "未使用 AI 计划，采用确定性路由结果",
        }],
        "missing_info": [],
        "replan": False,
    }


def plan_candidates(plan: dict[str, Any], scores: dict[str, float],
                    threshold: float = 30.0) -> list[dict[str, Any]]:
    """把计划展开为候选列表；同一技能只保留一次。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    source = "planner" if plan.get("source") == "ai" else "deterministic"
    for step in plan.get("steps") or []:
        for name in step.get("skills") or []:
            if name in seen:
                continue
            seen.add(name)
            score = float(scores.get(name, 0.0))
            if source == "planner" and score < threshold:
                score = threshold
            out.append({
                "skill": name,
                "score": score,
                "reason": f"{step.get('id')}·{step.get('title')}：{step.get('reason') or ''}"[:300],
                "source": source,
                "signals": {},
                "plan_step": step.get("id"),
                "gate": step.get("gate"),
            })
    return out


def plan_skill_names(plan: dict[str, Any]) -> list[str]:
    return [n for step in plan.get("steps") or [] for n in (step.get("skills") or [])]


def gate_allows(gate: str, results: list[dict[str, Any]]) -> bool:
    if gate == "always" or not gate:
        return True
    if not results:
        return gate in ("previous_unmatched", "previous_low_confidence")
    matched = []
    for res in results:
        verdict = res.get("verdict") or {}
        status = verdict.get("status") or res.get("status")
        if status == "matched":
            matched.append(res)
    if gate == "previous_unmatched":
        return any(((r.get("verdict") or {}).get("status") or r.get("status")) != "matched"
                   for r in results)
    if gate == "previous_conflict":
        roots = {((r.get("root_cause") or {}).get("name") or "") for r in matched}
        roots.discard("")
        return len(roots) > 1 or any(
            ((r.get("verdict") or {}).get("status") or "") == "temporal_conflict"
            for r in results)
    if gate == "previous_low_confidence":
        if not matched:
            return True
        ranks = {"high": 3, "medium": 2, "low": 1}
        best = max(ranks.get(str((r.get("root_cause") or {}).get("confidence") or "low").lower(), 0)
                   for r in matched)
        return best < 3
    if gate == "previous_gap":
        for res in results:
            status = str((res.get("verdict") or {}).get("status") or res.get("status") or "")
            if not res.get("ok") or status in (
                    "failed", "missing", "no_evidence", "timeout", "incompatible",
                    "unmatched", "error"):
                return True
            if res.get("glob_fallback") or res.get("limitations"):
                return True
        return False
    return False


def needs_replan(results: list[dict[str, Any]],
                 remaining_skills: list[str]) -> tuple[bool, str]:
    """只在“确实定不了”时补规划；多根因并存交给仲裁，不在这里循环。"""
    if not remaining_skills:
        return False, "没有尚未执行的技能，补规划无收益"
    if not results:
        return True, "所有计划步骤都没有产出结果"
    matched = [r for r in results if ((r.get("verdict") or {}).get("status")
                                      or r.get("status")) == "matched"]
    if not matched:
        return True, "前序没有任何技能命中已知根因"
    roots = {((r.get("root_cause") or {}).get("name") or "") for r in matched}
    roots.discard("")
    if len(roots) > 1:
        return False, "存在多个不同根因，交由仲裁比较证据强弱"
    ranks = {"high": 3, "medium": 2, "low": 1}
    best = max(ranks.get(str((r.get("root_cause") or {}).get("confidence") or "low").lower(), 0)
               for r in matched)
    if best < 3:
        return True, "最高置信度不是 high，补充一个能独立区分的技能"
    return False, "已出现唯一高置信根因，停止扩展"


def compact_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """给经验库/提示词使用的短计划，避免把长文本反复塞进上下文。"""
    return {
        "summary": plan.get("summary"),
        "hypothesis": (plan.get("hypothesis") or [])[:3],
        "steps": [{k: s.get(k) for k in ("id", "title", "skills", "gate", "objective")}
                  for s in (plan.get("steps") or [])],
        "source": plan.get("source"),
    }
