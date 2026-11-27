"""故障知识库统计：库存、覆盖、使用、审核、变更、导入、索引七组指标。

直接用 SQLite 聚合计算“当前真值”，同时把当天快照写入 kb_stats_daily，
便于页面画 7/30/90 天趋势和后续对账；不引入图表或分析库。
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _pct(part: int, total: int) -> float:
    return round((part / total) * 100, 2) if total else 0.0


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round(len(values) * 0.95)) - 1))
    return int(values[index])


def _fmt(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_stats(db, config, *, days: int = 90, kb_id: int | None = None) -> dict[str, Any]:
    days = max(1, min(365, int(days or 90)))
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_start = now.strftime("%Y-%m-%d")
    kb_id = int(kb_id) if kb_id else None
    kb_and = " AND kb_id=?" if kb_id else ""
    kb_where = " WHERE kb_id=?" if kb_id else ""
    kb_args: tuple[Any, ...] = (kb_id,) if kb_id else ()

    # 库存
    doc_rows = db.all("SELECT id,status,doc_type,source_type,quality,created_at FROM kb_documents "
                      "WHERE deleted_at IS NULL" + kb_and, kb_args)
    case_rows = db.all("SELECT id,doc_id,status,quality,cause,solution,prevention,symptom,"
                       "title_clean,hit_count,citation_count,helpful_count,unhelpful_count,"
                       "last_hit_at,created_at FROM kb_cases" + kb_where, kb_args)
    drafts = db.all("SELECT status,COUNT(*) AS n FROM kb_drafts" +
                    (" WHERE kb_id=?" if kb_id else "") + " GROUP BY status",
                    kb_args if kb_id else ())
    doc_status = Counter(str(r.get("status") or "active") for r in doc_rows)
    doc_types = Counter(str(r.get("doc_type") or "fault") for r in doc_rows)
    sources = Counter(str(r.get("source_type") or "unknown") for r in doc_rows)
    quality_buckets = Counter()
    for row in doc_rows:
        q = float(row.get("quality") or 0)
        quality_buckets["0-59" if q < 60 else "60-79" if q < 80 else "80-100"] += 1
    inventory = {
        "documents": len(doc_rows), "cases": len(case_rows),
        "active_documents": doc_status.get("active", 0),
        "disabled_documents": doc_status.get("disabled", 0),
        "active_cases": sum(1 for r in case_rows if str(r.get("status") or "") == "active"),
        "disabled_cases": sum(1 for r in case_rows if str(r.get("status") or "") == "disabled"),
        "drafts": sum(int(r.get("n") or 0) for r in drafts if str(r.get("status")) == "reviewing"),
        "by_status": dict(doc_status), "by_doc_type": dict(doc_types),
        "by_source": dict(sources), "quality_buckets": dict(quality_buckets),
    }

    # 覆盖：板卡×领域矩阵、关键字段缺失
    tags = defaultdict(lambda: {"board": [], "domain": []})
    tag_sql = "SELECT t.case_id,t.kind,t.value FROM kb_case_tags t"
    if kb_id:
        tag_sql += " JOIN kb_cases c ON c.id=t.case_id WHERE c.kb_id=?"
    for row in db.all(tag_sql, kb_args):
        if row.get("kind") in {"board", "domain"}:
            tags[str(row["case_id"])][str(row["kind"])].append(str(row["value"]))
    matrix = Counter()
    for case_id, item in tags.items():
        for board in set(item["board"]):
            for domain in set(item["domain"]):
                matrix[(board, domain)] += 1
    missing = {}
    for field in ("symptom", "cause", "solution", "prevention"):
        missing[field] = sum(1 for r in case_rows if not str(r.get(field) or "").strip())
    duplicates = db.all(
        "SELECT title_clean,COUNT(*) AS n FROM kb_cases WHERE status='active'" + kb_and +
        " GROUP BY title_clean HAVING n>1 ORDER BY n DESC LIMIT 20", kb_args)
    low_quality = sum(1 for r in case_rows if float(r.get("quality") or 0) < 60)
    coverage = {
        "board_domain_matrix": [
            {"board": b, "domain": d, "count": n}
            for (b, d), n in matrix.most_common(30)],
        "cause_coverage": _pct(len(case_rows) - missing["cause"], len(case_rows)),
        "solution_coverage": _pct(len(case_rows) - missing["solution"], len(case_rows)),
        "missing_field_top": sorted(missing.items(), key=lambda x: -x[1])[:4],
        "low_quality": low_quality,
        "duplicate_candidates": [dict(r) for r in duplicates],
    }

    # 使用
    query_rows = db.all(
        "SELECT id,user_id,query,latency_ms,zero_result,top_ids_json,"
        "created_at FROM kb_query_logs WHERE created_at>=?" + kb_and,
        (since, *kb_args))
    if kb_id:
        feedback = db.all(
            "SELECT f.helpful,COUNT(*) AS n FROM kb_feedback f "
            "JOIN kb_query_logs q ON q.id=f.query_id WHERE f.created_at>=? AND q.kb_id=? "
            "GROUP BY f.helpful", (since, kb_id))
    else:
        feedback = db.all(
            "SELECT helpful,COUNT(*) AS n FROM kb_feedback WHERE created_at>=? "
            "GROUP BY helpful", (since,))
    top_queries = db.all(
        "SELECT query,COUNT(*) AS n FROM kb_query_logs WHERE created_at>=?" + kb_and +
        " GROUP BY query ORDER BY n DESC LIMIT 20", (since, *kb_args))
    cited = db.all("SELECT id,title,citation_count,hit_count,helpful_count,"
                   "unhelpful_count,last_hit_at FROM kb_cases "
                   "WHERE (citation_count>0 OR hit_count>0)" + kb_and +
                   " ORDER BY citation_count DESC,hit_count DESC LIMIT 20", kb_args)
    helpful = sum(int(r.get("n") or 0) for r in feedback if int(r.get("helpful") or 0))
    unhelpful = sum(int(r.get("n") or 0) for r in feedback if not int(r.get("helpful") or 0))
    usage = {
        "queries": len(query_rows),
        "unique_users": len({r.get("user_id") for r in query_rows}),
        "zero_results": sum(1 for r in query_rows if int(r.get("zero_result") or 0)),
        "zero_result_rate": _pct(sum(1 for r in query_rows if int(r.get("zero_result") or 0)),
                                 len(query_rows)),
        "p95_latency_ms": _p95([int(r.get("latency_ms") or 0) for r in query_rows]),
        "top_queries": [dict(r) for r in top_queries],
        "cited_cases": [dict(r) for r in cited],
        "feedback": {"helpful": helpful, "unhelpful": unhelpful,
                     "helpful_rate": _pct(helpful, helpful + unhelpful)},
    }

    # 审核
    review_rows = db.all("SELECT * FROM kb_reviews" + kb_where, kb_args)
    status = Counter(str(r.get("status") or "") for r in review_rows)
    decision = Counter(str(r.get("decision") or "") for r in review_rows)
    human = Counter(str(r.get("human_decision") or "") for r in review_rows)
    pending = [r for r in review_rows if str(r.get("status") or "") in
               {"queued", "running", "pending", "manual", "failed"}]
    oldest = min((str(r.get("created_at") or "") for r in pending), default="")
    human_decided = sum(1 for r in review_rows if str(r.get("human_decision") or ""))
    ai_done = [r for r in review_rows if str(r.get("status") or "") == "done"]
    fallback = [r for r in review_rows if str(r.get("status") or "") in {"manual", "failed"}]
    review = {
        "total": len(review_rows), "pending": len(pending), "oldest_pending": oldest,
        "by_status": dict(status), "by_decision": dict(decision),
        "ai_done": len(ai_done),
        "ai_coverage": _pct(len(ai_done), len(review_rows)),
        "ai_pass": sum(1 for r in ai_done if str(r.get("decision") or "") == "pass"),
        "ai_needs_review": sum(1 for r in ai_done if str(r.get("decision") or "") == "needs_review"),
        "ai_reject": sum(1 for r in ai_done if str(r.get("decision") or "") == "reject"),
        "human_approval_rate": _pct(human.get("approve", 0), human_decided),
        "human_overrides": sum(1 for r in review_rows
                               if str(r.get("decision") or "") and str(r.get("human_decision") or "")
                               and str(r.get("decision")) != str(r.get("human_decision"))),
        "fallback_manual": len(fallback),
    }

    # 变更
    rev_sql = ("SELECT r.id,r.case_id,r.action,r.actor_name,r.actor_type,r.created_at "
               "FROM kb_case_revisions r")
    if kb_id:
        rev_sql += " JOIN kb_cases c ON c.id=r.case_id WHERE r.created_at>=? AND c.kb_id=?"
        rev_args: tuple[Any, ...] = (since, kb_id)
    else:
        rev_sql += " WHERE r.created_at>=?"
        rev_args = (since,)
    rev_rows = db.all(rev_sql, rev_args)
    rev_daily = Counter(_day(r.get("created_at")) for r in rev_rows)
    rev_by_case = Counter(str(r.get("case_id")) for r in rev_rows)
    case_titles = {str(r["id"]): str(r.get("title") or "")
                   for r in db.all("SELECT id,title FROM kb_cases" + kb_where, kb_args)}
    rev_actor = Counter(str(r.get("actor_name") or r.get("actor_type") or "") for r in rev_rows)
    changes = {
        "revisions": len(rev_rows),
        "restores": sum(1 for r in rev_rows if str(r.get("action")) == "restore"),
        "by_action": dict(Counter(str(r.get("action")) for r in rev_rows)),
        "daily": dict(sorted(rev_daily.items())),
        "top_cases": [{"case_id": k, "title": case_titles.get(k, ""), "revisions": v}
                      for k, v in rev_by_case.most_common(20)],
        "actors": [{"actor": k, "count": v} for k, v in rev_actor.most_common(20)],
    }

    # 导入
    jobs = db.all("SELECT * FROM kb_import_jobs WHERE created_at>=?" + kb_and,
                  (since, *kb_args))
    files = db.all("SELECT name,status,size,error FROM kb_import_files WHERE job_id IN "
                   "(SELECT id FROM kb_import_jobs WHERE created_at>=?" +
                   (" AND kb_id=?" if kb_id else "") + ")", (since, *kb_args))
    formats = Counter()
    failures = Counter()
    for row in files:
        ext = str(row.get("name") or "").rsplit(".", 1)[-1].lower() if "." in str(row.get("name") or "") else "unknown"
        formats[ext] += 1
        if str(row.get("status")) == "failed":
            failures[ext] += 1
    imports = {
        "jobs": len(jobs),
        "completed": sum(1 for r in jobs if str(r.get("status")) == "completed"),
        "partial": sum(1 for r in jobs if str(r.get("status")) == "partial"),
        "failed": sum(1 for r in jobs if str(r.get("status")) == "failed"),
        "success_rate": _pct(sum(1 for r in jobs if str(r.get("status")) in {"completed", "partial"}), len(jobs)),
        "files": len(files),
        "file_failures": sum(1 for r in files if str(r.get("status")) == "failed"),
        "formats": dict(formats), "failure_formats": dict(failures),
        "bytes": sum(int(r.get("size") or 0) for r in files),
    }

    meta = (db.one("SELECT * FROM kb_library_index_meta WHERE kb_id=?", (kb_id,))
            if kb_id else db.one("SELECT * FROM kb_index_meta WHERE id=1")) or {}
    index = {
        "status": meta.get("status") or "empty", "documents": int(meta.get("doc_count") or 0),
        "cases": int(meta.get("case_count") or 0), "terms": int(meta.get("terms") or 0),
        "bytes": int(meta.get("bytes") or 0), "build_ms": int(meta.get("build_ms") or 0),
        "built_at": meta.get("built_at") or "", "degraded": bool(meta.get("error")),
        "checksum": meta.get("checksum") or "",
    }

    knowledge = {
        "top_hit": [dict(r) for r in sorted(
            case_rows, key=lambda r: int(r.get("hit_count") or 0), reverse=True)[:20]],
        "top_helpful": [dict(r) for r in sorted(
            case_rows, key=lambda r: int(r.get("helpful_count") or 0), reverse=True)[:10]],
    }

    # 趋势：按天补齐，便于页面直接画没有图表库的柱状条。
    days_list = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    q_daily = Counter(_day(r.get("created_at")) for r in query_rows)
    zero_daily = Counter(_day(r.get("created_at")) for r in query_rows if int(r.get("zero_result") or 0))
    rev_daily_all = Counter(_day(r.get("created_at")) for r in rev_rows)
    review_daily = Counter(_day(r.get("created_at")) for r in review_rows)
    import_daily = Counter(_day(r.get("created_at")) for r in jobs)
    trend = [{"day": d, "queries": q_daily.get(d, 0), "zero_results": zero_daily.get(d, 0),
              "revisions": rev_daily_all.get(d, 0), "reviews": review_daily.get(d, 0),
              "imports": import_daily.get(d, 0)} for d in days_list]

    payload = {
        "days": days, "kb_id": kb_id or 0,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inventory": inventory, "coverage": coverage, "usage": usage, "review": review,
        "changes": changes, "imports": imports, "index": index, "knowledge": knowledge,
        "trend": trend, "cards": {
            "documents": inventory["active_documents"], "cases": inventory["active_cases"],
            "pending_reviews": review["pending"], "queries": usage["queries"],
            "zero_result_rate": usage["zero_result_rate"], "revisions": changes["revisions"],
        },
    }
    rollup(db, payload, day_start, kb_id=kb_id)
    return payload


def rollup(db, payload: dict[str, Any], day: str, *, kb_id: int | None = None) -> None:
    """把当日关键指标落在 kb_stats_daily；失败不影响主查询。"""
    metrics = {
        "inventory.documents": payload["inventory"]["documents"],
        "inventory.cases": payload["inventory"]["cases"],
        "usage.queries": payload["usage"]["queries"],
        "usage.zero_results": payload["usage"]["zero_results"],
        "review.pending": payload["review"]["pending"],
        "changes.revisions": payload["changes"]["revisions"],
        "imports.jobs": payload["imports"]["jobs"],
        "index.cases": payload["index"]["cases"],
    }
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for metric, value in metrics.items():
            db.execute(
                "INSERT INTO kb_stats_daily(day,metric,dim_key,dim_json,value,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(day,metric,dim_key) DO UPDATE SET "
                "value=excluded.value, dim_json=excluded.dim_json, updated_at=excluded.updated_at",
                (day, metric, f"kb:{kb_id}" if kb_id else "", "{}", int(value or 0), now))
    except Exception:
        pass


def stats_csv(payload: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["group", "metric", "value", "detail"])
    inv = payload.get("inventory", {})
    for key in ("documents", "cases", "active_documents", "active_cases", "disabled_documents",
                "disabled_cases", "drafts"):
        writer.writerow(["inventory", key, inv.get(key, 0), ""])
    for key in ("queries", "unique_users", "zero_results", "zero_result_rate", "p95_latency_ms"):
        writer.writerow(["usage", key, payload.get("usage", {}).get(key, 0), ""])
    for key in ("total", "pending", "ai_done", "ai_coverage", "ai_pass", "ai_needs_review",
                "ai_reject", "human_approval_rate", "human_overrides", "fallback_manual"):
        writer.writerow(["review", key, payload.get("review", {}).get(key, 0), ""])
    for key in ("revisions", "restores"):
        writer.writerow(["changes", key, payload.get("changes", {}).get(key, 0), ""])
    for key in ("documents", "cases", "terms", "bytes", "build_ms", "status"):
        writer.writerow(["index", key, payload.get("index", {}).get(key, 0), ""])
    for row in payload.get("trend", []):
        writer.writerow(["trend", row.get("day"), row.get("queries"), _fmt(row)])
    for row in payload.get("usage", {}).get("top_queries", []):
        writer.writerow(["usage.top_query", row.get("query"), row.get("n"), ""])
    return buf.getvalue()
