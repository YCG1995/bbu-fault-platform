"""只读运行指标：任务、队列、技能、模型健康与恢复次数。

不引入 Prometheus 客户端依赖；/api/metrics 返回 JSON，/api/metrics/prometheus
返回文本格式，可直接被采集器抓取。
"""
from __future__ import annotations

from typing import Any

from .learning import ExperienceStore
from .util import utc_now


def _percentile(values: list[float], q: float) -> float:
    """线性插值分位数；小样本时 P95 不会低于中位数。"""
    if not values:
        raise ValueError("empty")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def collect(app: Any) -> dict[str, Any]:
    worker = app.worker.stats()
    rows = app.db.all("SELECT status, stage, COUNT(*) AS n FROM tasks GROUP BY status, stage")
    counts: dict[str, int] = {}
    stages: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + row["n"]
        if row["status"] in ("running", "queued"):
            stages[row["stage"]] = stages.get(row["stage"], 0) + row["n"]
    skills = app.registry.list_skills(True)
    skill_stats = {
        "total": len(skills),
        "ready": sum(1 for s in skills if s.status == "ready"),
        "degraded": sum(1 for s in skills if s.status == "degraded"),
        "disabled": sum(1 for s in skills if s.status == "disabled"),
        "canary": sum(1 for s in skills if getattr(s, "channel", "stable") == "canary"),
    }
    done = app.db.all(
        "SELECT started_at, finished_at FROM tasks WHERE status='done' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT 50")
    durations = []
    for row in done:
        try:
            from datetime import datetime
            start = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00"))
            durations.append(max(0.0, (end - start).total_seconds()))
        except (ValueError, TypeError):
            continue
    queue_depth = worker["queued"]
    resumed = app.db.one(
        "SELECT COALESCE(SUM(resume_count),0) AS n FROM tasks") or {"n": 0}
    experiences = ExperienceStore(app.db).stats()
    db_size = app.config.db_path.stat().st_size if app.config.db_path.exists() else 0

    return {
        "ts": utc_now(),
        "queue": {**worker, "depth": queue_depth,
                  "saturation": round(queue_depth / max(1, worker["max_queue"]), 3)},
        "tasks": counts,
        "running_stages": stages,
        "skills": skill_stats,
        "duration_sec": {
            "count": len(durations),
            "p50": round(_percentile(durations, 0.5), 1) if durations else None,
            "p95": round(_percentile(durations, 0.95), 1) if durations else None,
            "max": round(max(durations), 1) if durations else None,
        },
        "recovery": {"resumed_tasks": int(resumed.get("n") or 0)},
        "llm": app.gateway.health(),
        "experiences": experiences,
        "db_bytes": db_size,
    }


def prometheus(snapshot: dict[str, Any]) -> str:
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[str, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            tag = f"{{{labels}}}" if labels else ""
            lines.append(f"{name}{tag} {value}")

    q = snapshot["queue"]
    metric("bbu_queue_depth", "gauge", "等待执行的任务数", [("", q["depth"])])
    metric("bbu_queue_capacity", "gauge", "队列容量", [("", q["max_queue"])])
    metric("bbu_workers_running", "gauge", "正在执行的任务数", [("", q["running"])])
    metric("bbu_tasks_total", "gauge", "按状态统计的任务数",
           [(f'status="{k}"', v) for k, v in snapshot["tasks"].items()])
    metric("bbu_skills_total", "gauge", "按状态统计的技能数",
           [(f'status="{k}"', v) for k, v in snapshot["skills"].items()])
    d = snapshot["duration_sec"]
    metric("bbu_task_duration_seconds", "gauge", "最近 50 个完成任务耗时",
           [('quantile="0.5"', d["p50"] or 0), ('quantile="0.95"', d["p95"] or 0),
            ('quantile="max"', d["max"] or 0)])
    llm = snapshot["llm"]
    metric("bbu_llm_calls_total", "counter", "模型调用次数", [("", llm["calls"])])
    metric("bbu_llm_failures_total", "counter", "模型调用失败次数", [("", llm["failures"])])
    metric("bbu_tasks_resumed_total", "counter", "服务重启后断点恢复次数",
           [("", snapshot["recovery"]["resumed_tasks"])])
    return "\n".join(lines) + "\n"
