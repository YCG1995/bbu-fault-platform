"""模型 / 任务 / 技能用量统计。

采集层只落“调用事实”，聚合层按本地时区切日。个人模型与管理员模型通过
scope 区分；普通用户只读取自己的事件，管理员可读取全部或指定用户。
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import Any

from .util import iso_now


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return default


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class UsageStore:
    def __init__(self, db: Any, config: Any = None) -> None:
        self.db = db
        self.config = config

    # ---------- 采集 ----------
    def record(self, event: dict[str, Any]) -> str:
        usage_id = "U" + uuid.uuid4().hex[:20]
        self.db.execute(
            "INSERT INTO llm_usage_events(id,user_id,task_id,task_kind,skill_name,model_id,"
            "model_name,provider,model,scope,purpose,stage,status,input_tokens,output_tokens,"
            "total_tokens,estimated_input_tokens,latency_ms,error,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (usage_id, _int(event.get("user_id")), str(event.get("task_id") or ""),
             str(event.get("task_kind") or "direct"), str(event.get("skill_name") or ""),
             str(event.get("model_id") or ""), str(event.get("model_name") or ""),
             str(event.get("provider") or ""), str(event.get("model") or ""),
             str(event.get("scope") or "admin"), str(event.get("purpose") or ""),
             str(event.get("stage") or ""), str(event.get("status") or "success"),
             _int(event.get("input_tokens")), _int(event.get("output_tokens")),
             _int(event.get("total_tokens")),
             _int(event.get("estimated_input_tokens")), _int(event.get("latency_ms")),
             str(event.get("error") or "")[:400], str(event.get("created_at") or iso_now())))
        return usage_id

    # ---------- 时间范围 ----------
    def _tz(self):
        offset = 8
        try:
            offset = int(getattr(self.config, "get", lambda *_: 8)("local_tz_offset", 8))
        except (TypeError, ValueError):
            pass
        return timezone(timedelta(hours=max(-12, min(14, offset))))

    def _range(self, days: int, now: datetime | None = None):
        days = max(1, min(365, int(days or 30)))
        tz = self._tz()
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_local = now.astimezone(tz)
        start_day = now_local.date() - timedelta(days=days - 1)
        start_local = datetime.combine(start_day, time.min, tzinfo=tz)
        start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        dates = [(start_day + timedelta(days=i)).isoformat()
                 for i in range((now_local.date() - start_day).days + 1)]
        offset = tz.utcoffset(now_local)
        hours = int(offset.total_seconds() // 3600) if offset else 0
        return {
            "days": days, "start": start_day.isoformat(), "end": now_local.date().isoformat(),
            "start_utc": start_utc, "end_utc": end_utc, "dates": dates, "tz": tz,
            "tz_label": f"UTC{hours:+d}",
        }

    def _local_day(self, stamp: str) -> str:
        try:
            value = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            try:
                value = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return ""
        return value.astimezone(self._tz()).date().isoformat()

    # ---------- 聚合 ----------
    def overview(self, days: int = 30, user_id: int | None = None,
                 all_users: bool = False, include_directory: bool = False,
                 now: datetime | None = None) -> dict[str, Any]:
        rng = self._range(days, now)
        where = "created_at>=? AND created_at<=? AND task_kind<>'model_test'"
        args: list[Any] = [rng["start_utc"], rng["end_utc"]]
        if not all_users:
            where += " AND user_id=?"
            args.append(int(user_id or 0))
        events = [dict(r) for r in self.db.all(
            "SELECT * FROM llm_usage_events WHERE " + where + " ORDER BY created_at", tuple(args))]

        totals = {"calls": 0, "failed_calls": 0, "input_tokens": 0, "output_tokens": 0,
                  "total_tokens": 0, "tasks": 0, "skills": 0, "users": 0}
        daily = {d: {"date": d, "calls": 0, "failed_calls": 0, "input_tokens": 0,
                     "output_tokens": 0, "total_tokens": 0, "models": []} for d in rng["dates"]}
        day_models: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        models: dict[str, dict[str, Any]] = {}
        users: dict[str, dict[str, Any]] = {}
        user_day: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        task_ids: set[str] = set()
        skill_names: set[str] = set()
        for e in events:
            ok = str(e.get("status")) == "success"
            tin, tout = _int(e.get("input_tokens")), _int(e.get("output_tokens"))
            ttotal = _int(e.get("total_tokens")) or tin + tout
            totals["calls"] += 1
            totals["failed_calls"] += 0 if ok else 1
            totals["input_tokens"] += tin
            totals["output_tokens"] += tout
            totals["total_tokens"] += ttotal
            if e.get("task_id"):
                task_ids.add(str(e["task_id"]))
            if e.get("skill_name"):
                skill_names.add(str(e["skill_name"]))
            date_key = self._local_day(e.get("created_at") or "") or rng["end"]
            if date_key not in daily:
                daily[date_key] = {"date": date_key, "calls": 0, "failed_calls": 0,
                                   "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                                   "models": []}
            row = daily[date_key]
            row["calls"] += 1
            row["failed_calls"] += 0 if ok else 1
            row["input_tokens"] += tin
            row["output_tokens"] += tout
            row["total_tokens"] += ttotal
            key = "|".join((str(e.get("model_id") or ""), str(e.get("model_name") or ""),
                            str(e.get("scope") or "admin")))
            dm = day_models[date_key].setdefault(key, {
                "key": key, "model_name": str(e.get("model_name") or ""),
                "scope": str(e.get("scope") or "admin"), "calls": 0,
                "failed_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
            dm["calls"] += 1
            dm["failed_calls"] += 0 if ok else 1
            dm["input_tokens"] += tin
            dm["output_tokens"] += tout
            dm["total_tokens"] += ttotal
            m = models.setdefault(key, {
                "key": key, "model_id": str(e.get("model_id") or ""),
                "model_name": str(e.get("model_name") or ""),
                "provider": str(e.get("provider") or ""), "model": str(e.get("model") or ""),
                "scope": str(e.get("scope") or "admin"), "calls": 0, "failed_calls": 0,
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "latency_total": 0, "latency_n": 0, "users": set(),
            })
            m["calls"] += 1
            m["failed_calls"] += 0 if ok else 1
            m["input_tokens"] += tin
            m["output_tokens"] += tout
            m["total_tokens"] += ttotal
            if _int(e.get("latency_ms")):
                m["latency_total"] += _int(e.get("latency_ms"))
                m["latency_n"] += 1
            if e.get("user_id"):
                uid = int(e["user_id"])
                m["users"].add(uid)
                u = users.setdefault(str(uid), {
                    "user_id": uid, "username": "", "calls": 0,
                    "failed_calls": 0, "total_tokens": 0})
                u["calls"] += 1
                u["failed_calls"] += 0 if ok else 1
                u["total_tokens"] += ttotal
                ud = user_day[date_key].setdefault(uid, {
                    "user_id": uid, "user_name": "", "calls": 0, "failed_calls": 0,
                    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                ud["calls"] += 1
                ud["failed_calls"] += 0 if ok else 1
                ud["input_tokens"] += tin
                ud["output_tokens"] += tout
                ud["total_tokens"] += ttotal
        totals["tasks"] = len(task_ids)
        totals["skills"] = len(skill_names)
        totals["users"] = len(users)
        for u in users.values():
            row = self.db.one("SELECT username,display_name FROM users WHERE id=?", (u["user_id"],))
            if row:
                u["username"] = row.get("username") or ""
                u["display_name"] = row.get("display_name") or ""
        for m in models.values():
            m["users"] = len(m["users"])
            m["latency_ms_avg"] = round(m["latency_total"] / m["latency_n"]) if m["latency_n"] else 0
            m.pop("latency_total", None); m.pop("latency_n", None)
        for d in daily.values():
            if not d:
                continue
            d["models"] = sorted(day_models[d["date"]].values(),
                                key=lambda x: (-x["total_tokens"], -x["calls"], x["key"]))
        model_rows = sorted(models.values(), key=lambda x: (-x["total_tokens"], -x["calls"], x["key"]))
        user_daily: list[dict[str, Any]] = []
        for date_key in rng["dates"]:
            day_rows = user_day.get(date_key)
            if not day_rows:
                continue
            payload: list[dict[str, Any]] = []
            for uid, item in sorted(day_rows.items(),
                                    key=lambda kv: (-kv[1]["total_tokens"], -kv[1]["calls"], kv[0])):
                meta = users.get(str(uid), {})
                item["user_name"] = (meta.get("display_name") or meta.get("username")
                                     or f"用户 #{uid}")
                payload.append(item)
            user_daily.append({
                "date": date_key,
                "calls": sum(x["calls"] for x in payload),
                "failed_calls": sum(x["failed_calls"] for x in payload),
                "total_tokens": sum(x["total_tokens"] for x in payload),
                "users": payload,
            })
        skill_rows = self._skills(rng, None if all_users else user_id, all_users)
        directory: list[dict[str, Any]] = []
        if include_directory:
            for row in self.db.all(
                    "SELECT e.user_id, COALESCE(u.display_name, u.username, '') AS user_name, "
                    "COUNT(*) AS calls, SUM(e.total_tokens) AS total_tokens "
                    "FROM llm_usage_events e LEFT JOIN users u ON u.id=e.user_id "
                    "WHERE e.created_at>=? AND e.created_at<=? AND e.task_kind<>'model_test' "
                    "GROUP BY e.user_id ORDER BY total_tokens DESC",
                    (rng["start_utc"], rng["end_utc"])):
                directory.append({"user_id": int(row["user_id"]),
                                  "user_name": str(row["user_name"] or ""),
                                  "calls": int(row["calls"] or 0),
                                  "total_tokens": int(row["total_tokens"] or 0)})
        return {
            "range": {k: rng[k] for k in ("days", "start", "end")} | {"timezone": rng["tz_label"]},
            "scope": {"mode": "all" if all_users else "self", "user_id": None if all_users else user_id},
            "totals": totals,
            "daily": [daily[d] for d in rng["dates"]],
            "user_daily": user_daily,
            "models": model_rows,
            "skills": skill_rows,
            "users": sorted(users.values(), key=lambda x: (-x["total_tokens"], x["user_id"])),
            "directory": directory,
        }

    def _skills(self, rng: dict[str, Any], user_id: int | None,
                all_users: bool) -> list[dict[str, Any]]:
        where = "t.created_at>=? AND t.created_at<=? AND CAST(ts.invalidated AS INTEGER)=0"
        args: list[Any] = [rng["start_utc"], rng["end_utc"]]
        if not all_users:
            where += " AND t.user_id=?"
            args.append(int(user_id or 0))
        rows = self.db.all(
            "SELECT ts.skill_name,ts.status,ts.stats_json,ts.result_json,ts.finished_at,"
            "t.id AS task_id,t.input_json,t.created_at FROM task_skills ts "
            "JOIN tasks t ON t.id=ts.task_id WHERE " + where
            + " ORDER BY t.created_at DESC", tuple(args))
        tool_rows = self.db.all(
            "SELECT tc.tool_name,COUNT(*) AS n FROM tool_calls tc JOIN tasks t ON t.id=tc.task_id "
            "WHERE t.created_at>=? AND t.created_at<=?" +
            ("" if all_users else " AND t.user_id=?") + " GROUP BY tc.tool_name",
            tuple([rng["start_utc"], rng["end_utc"]] + ([] if all_users else [int(user_id or 0)])))
        tools = {str(r["tool_name"]): int(r["n"] or 0) for r in tool_rows}
        # 同技能在多个任务中执行时，关联输入规模按任务去重后累加（同一任务只计一次）。
        agg: dict[str, dict[str, Any]] = {}
        task_inputs: dict[str, int] = {}
        for row in rows:
            name = str(row["skill_name"] or "")
            if not name:
                continue
            stats = _loads(row.get("stats_json"), {}) or {}
            result = _loads(row.get("result_json"), {}) or {}
            duration = _int(stats.get("duration_ms"))
            tokens = stats.get("tokens") if isinstance(stats.get("tokens"), dict) else {}
            total = _int(tokens.get("total_tokens") if tokens else 0) or (
                _int(tokens.get("input_tokens")) + _int(tokens.get("output_tokens"))
                if tokens else 0)
            evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
            tid = str(row.get("task_id") or "")
            if tid and tid not in task_inputs:
                task_inputs[tid] = sum(_int(x.get("size")) for x in _loads(row.get("input_json"), [])
                                        if isinstance(x, dict))
            item = agg.setdefault(name, {
                "skill_name": name, "executions": 0, "failed": 0, "duration_ms": 0,
                "tokens": 0, "evidence_count": 0, "tool_calls": 0,
                "input_bytes": 0, "task_ids": set(), "last_at": "",
            })
            item["executions"] += 1
            item["duration_ms"] += duration
            item["tokens"] += total
            item["evidence_count"] += len(evidence)
            item["task_ids"].add(tid)
            item["last_at"] = max(item["last_at"], str(row.get("finished_at") or row.get("created_at") or ""))
            status = str(row.get("status") or "")
            if status not in ("done", "matched", "completed", "ok"):
                item["failed"] += 1
        # 技能自身直接调用模型的用量：若已落 llm_usage_events，按技能名并入。
        event_rows = self.db.all(
            "SELECT skill_name,SUM(total_tokens) AS tokens,SUM(input_tokens) AS tin,"
            "SUM(output_tokens) AS tout,COUNT(*) AS calls FROM llm_usage_events "
            "WHERE created_at>=? AND created_at<=? AND skill_name<>''" +
            ("" if all_users else " AND user_id=?") + " GROUP BY skill_name",
            tuple([rng["start_utc"], rng["end_utc"]] + ([] if all_users else [int(user_id or 0)])))
        for e in event_rows:
            name = str(e["skill_name"] or "")
            if not name or name not in agg:
                # 没有执行记录时不要凭空生成“0 次执行却有 Token”的技能行。
                continue
            agg[name]["tokens"] = max(agg[name]["tokens"], _int(e.get("tokens")))
        for item in agg.values():
            item["tool_calls"] = tools.get(item["skill_name"], 0)
            ids = item.pop("task_ids")
            item["task_count"] = len(ids)
            item["input_bytes"] = sum(task_inputs.get(tid, 0) for tid in ids)
            # 同一任务多技能会重复计入关联输入；字段名保留“关联”语义，页面注明。
        return sorted(agg.values(), key=lambda x: (-x["executions"], -x["tokens"], x["skill_name"]))

    def task_rows(self, days: int = 30, user_id: int | None = None, all_users: bool = False,
                  page: int = 1, page_size: int = 20,
                  now: datetime | None = None) -> dict[str, Any]:
        rng = self._range(days, now)
        page = max(1, int(page or 1)); page_size = max(1, min(100, int(page_size or 20)))
        where = "t.created_at>=? AND t.created_at<=?"
        args: list[Any] = [rng["start_utc"], rng["end_utc"]]
        if not all_users:
            where += " AND t.user_id=?"
            args.append(int(user_id or 0))
        total = self.db.one("SELECT COUNT(*) AS n FROM tasks t WHERE " + where, tuple(args))
        tasks = self.db.all(
            "SELECT t.*,u.username FROM tasks t LEFT JOIN users u ON u.id=t.user_id WHERE " + where
            + " ORDER BY t.created_at DESC LIMIT ? OFFSET ?",
            tuple(args + [page_size, (page - 1) * page_size]))
        ids = [str(t["id"]) for t in tasks]
        events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if ids:
            rows = self.db.all(
                "SELECT * FROM llm_usage_events WHERE task_id IN ("
                + ",".join("?" for _ in ids) + ") AND task_kind='diagnosis' ORDER BY created_at",
                tuple(ids))
            for r in rows:
                events[str(r["task_id"])].append(dict(r))
        items = []
        for t in tasks:
            stats = _loads(t.get("stats_json"), {}) or {}
            evs = events.get(str(t["id"]), [])
            calls = len(evs)
            tin = sum(_int(e.get("input_tokens")) for e in evs)
            tout = sum(_int(e.get("output_tokens")) for e in evs)
            tks = sum((_int(e.get("total_tokens")) or
                       _int(e.get("input_tokens")) + _int(e.get("output_tokens"))) for e in evs)
            if not evs:
                calls = _int(stats.get("llm_calls"))
                tin = _int(stats.get("input_tokens"))
                tout = _int(stats.get("output_tokens"))
                tks = _int(stats.get("total_tokens"))
            items.append({
                "task_id": t["id"], "title": t.get("title") or "",
                "status": t.get("status") or "", "mode": t.get("mode") or "",
                "user_id": t.get("user_id"), "username": t.get("username") or "",
                "created_at": t.get("created_at"), "finished_at": t.get("finished_at"),
                "calls": calls, "input_tokens": tin, "output_tokens": tout, "total_tokens": tks,
                "has_events": bool(evs),
                "models": sorted({str(e.get("model_name") or "") for e in evs if e.get("model_name")}),
            })
        return {"range": {k: rng[k] for k in ("days", "start", "end")}, "page": page,
                "page_size": page_size, "total": int((total or {}).get("n") or 0), "items": items}
