"""历史规划经验与成功任务自动沉淀。

这里不做模型权重训练，只做两件可在内网落地的事：
1. 把每次终态任务的“计划、证据缺口、技能表现、最终结论”落库，下次相似故障
   通过字面/术语重叠检索出来，作为 planner 的参考，避免重复踩坑。
2. 成功任务在后台创建工坊草稿，复制/硬链日志包，人工在工坊确认后再发布成
   新的子 Skill；自动沉淀绝不直接发布。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from .curated import seed_curated_experiences
from .db import Database
from .planner import compact_plan
from .util import iso_now, unique_path
from .workshop import WorkshopService

_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9_.:#-]{1,}", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_STOP = {
    "的", "了", "是", "在", "和", "与", "及", "或", "对", "为", "从", "到", "有", "无",
    "上报", "出现", "发生", "导致", "目前", "现在", "问题", "故障", "异常", "告警",
    "ne", "the", "and", "with", "from", "this", "that", "task", "bbu",
}


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    out: set[str] = set()
    for raw in _ASCII_RE.findall(text):
        token = raw.strip("._:#-")
        if len(token) >= 2 and token not in _STOP:
            out.add(token)
    for run in _CJK_RE.findall(text):
        if run not in _STOP and 2 <= len(run) <= 12:
            out.add(run)
        if 3 <= len(run) <= 40:
            for i in range(len(run) - 1):
                gram = run[i:i + 2]
                if gram not in _STOP:
                    out.add(gram)
    return out


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else (value if value is not None else default)
    except (TypeError, json.JSONDecodeError):
        return default


class ExperienceStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, task: dict[str, Any], result: dict[str, Any]) -> None:
        verdict = result.get("verdict") or {}
        arbitration = result.get("arbitration") or {}
        skill_name = arbitration.get("chosen_skill") or verdict.get("skill") or ""
        root_name = arbitration.get("root_cause_name") or verdict.get("root_cause_name") \
            or verdict.get("primary") or ""
        confidence = arbitration.get("confidence") or verdict.get("confidence") or ""
        hints = (result.get("inventory") or {}).get("detected_hints") or []
        skills = []
        for item in (result.get("skills") or [])[:12]:
            skills.append({
                "skill": item.get("skill_name"),
                "status": item.get("status"),
                "root_cause": (item.get("root_cause") or {}).get("name")
                if isinstance(item.get("root_cause"), dict) else item.get("root_cause"),
                "score": item.get("score"),
                "evidence": item.get("evidence"),
                "error": item.get("error"),
            })
        outcome = {
            "status": verdict.get("status") or task.get("status"),
            "confidence": confidence,
            "chosen_skill": skill_name,
            "root_cause": root_name,
            "reason": arbitration.get("reason") or "",
            "missing_info": arbitration.get("missing_info") or [],
            "skills": skills,
            "duration_sec": (result.get("stats") or {}).get("duration_sec"),
            "llm_calls": (result.get("stats") or {}).get("llm_calls"),
        }
        plan = compact_plan(result.get("plan") or {})
        if result.get("replan"):
            plan["replan"] = result["replan"].get("summary")
        self.db.execute(
            "INSERT INTO plan_experiences(task_id,skill_name,root_cause_name,status,confidence,"
            "description,hints_json,plan_json,outcome_json,created_at,trust_level) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET skill_name=excluded.skill_name,"
            "root_cause_name=excluded.root_cause_name,status=excluded.status,"
            "confidence=excluded.confidence,description=excluded.description,"
            "hints_json=excluded.hints_json,plan_json=excluded.plan_json,"
            "outcome_json=excluded.outcome_json,created_at=excluded.created_at",
            (task["id"], skill_name, root_name, "done", confidence,
             (task.get("description") or "")[:6000],
             json.dumps(hints, ensure_ascii=False),
             json.dumps(plan, ensure_ascii=False, default=str),
             json.dumps(outcome, ensure_ascii=False, default=str), iso_now(),
             "unreviewed"))

    def search(self, description: str, hints: list[str] | None = None,
               skill_name: str = "", limit: int = 3,
               include_unreviewed: bool = False) -> list[dict[str, Any]]:
        query = _tokens((description or "") + " " + " ".join(hints or []) + " " + (skill_name or ""))
        trust_sql = "" if include_unreviewed else \
            " AND trust_level IN ('verified','golden','curated') "
        rows = self.db.all(
            "SELECT * FROM plan_experiences WHERE status IN ('done','needs_data','matched','unmatched','temporal_conflict','insufficient') "
            + trust_sql +
            "ORDER BY id DESC LIMIT 200")
        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            doc = _tokens((row.get("description") or "") + " " +
                          (row.get("root_cause_name") or "") + " " +
                          (row.get("skill_name") or "") + " " +
                          " ".join(_loads(row.get("hints_json"), [])))
            if not query or not doc:
                continue
            overlap = query & doc
            score = len(overlap) / max(1, len(query))
            if row.get("skill_name") and row["skill_name"] == skill_name:
                score += 0.12
            if row.get("root_cause_name") and row["root_cause_name"] in (description or ""):
                score += 0.08
            if score < 0.06:
                continue
            ranked.append((score, {
                "task_id": row["task_id"],
                "fault": (row.get("description") or "")[:220],
                "root_cause": row.get("root_cause_name") or "",
                "skill": row.get("skill_name") or "",
                "status": row.get("status") or "",
                "confidence": row.get("confidence") or "",
                "trust_level": row.get("trust_level") or "unreviewed",
                "plan": _loads(row.get("plan_json"), {}),
                "outcome": _loads(row.get("outcome_json"), {}),
                "match_score": round(score, 3),
            }))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in ranked[:max(0, min(limit, 10))]]

    def stats(self) -> dict[str, Any]:
        rows = self.db.all(
            "SELECT root_cause_name, COUNT(*) AS n FROM plan_experiences "
            "WHERE root_cause_name<>'' GROUP BY root_cause_name ORDER BY n DESC LIMIT 8")
        total = self.db.one("SELECT COUNT(*) AS n FROM plan_experiences")["n"]
        return {"experiences": total, "top_root_causes": rows}


class LearningService:
    def __init__(self, db: Database, config: Any, registry: Any,
                 workshop: WorkshopService) -> None:
        self.db = db
        self.config = config
        self.registry = registry
        self.workshop = workshop
        self.experiences = ExperienceStore(db)
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="auto-distill", daemon=True)

    def start(self) -> None:
        try:
            self.curated_seeded = seed_curated_experiences(
                self.db, Path(self.config.root) / "knowledge/reasoning_playbooks.json")
        except Exception:  # 种子经验失败不能阻塞平台启动
            self.curated_seeded = 0
        if self.config.get("auto_distill", True) and not self._thread.is_alive():
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def busy(self) -> bool:
        """维护任务使用：自动沉淀队列有积压时不允许迁移/切换。"""
        return bool(self._queue.qsize())

    def enqueue(self, task_id: str, result: dict[str, Any] | None = None) -> None:
        if not self.config.get("auto_distill", True):
            return
        if result is not None and not self._eligible(result):
            return
        try:
            self._queue.put_nowait(task_id)
        except queue.Full:
            self._event(task_id, "warning", "自动沉淀队列已满，本条任务跳过", "done")

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if task_id is None:
                break
            try:
                self.after_task(task_id)
            except Exception as exc:  # noqa: BLE001 - 学习失败不能影响诊断
                try:
                    self._event(task_id, "warning", f"自动沉淀失败：{type(exc).__name__}: {exc}", "done")
                except Exception:
                    pass

    @staticmethod
    def _eligible(result: dict[str, Any]) -> bool:
        verdict = result.get("verdict") or {}
        return (verdict.get("status") == "matched"
                and (verdict.get("confidence") or "").lower() in ("high", "medium")
                and bool(verdict.get("primary") or verdict.get("root_cause_name")))

    def after_task(self, task_id: str) -> dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return {"status": "missing"}
        result = _loads(task.get("result_json"), {})
        if task.get("status") != "done" or not self._eligible(result):
            return self._mark(task, {"status": "skipped",
                                      "reason": "任务未形成高/中置信匹配结论"})
        root = result.get("verdict") or {}
        root_name = root.get("root_cause_name") or root.get("primary") or "未命名根因"
        skill_name = (result.get("arbitration") or {}).get("chosen_skill") or ""
        existing = self.db.one(
            "SELECT id FROM workshop_drafts WHERE source_task_id=?", (task_id,))
        if existing:
            return self._mark(task, {"status": "existing", "draft_id": existing["id"],
                                      "reason": "该任务已沉淀过草稿"})
        # 同一根因只保留一份自学习草稿，避免每个任务都刷一份重复技能
        same_root = self.db.one(
            "SELECT id, name FROM workshop_drafts WHERE fault_name=? AND category='自学习' "
            "ORDER BY id DESC LIMIT 1", (str(root_name)[:80],))
        if same_root:
            return self._mark(task, {"status": "duplicate_root", "draft_id": same_root["id"],
                                      "reason": f"已有同根因自学习草稿 {same_root['name']}，不再重复创建"})
        if not task.get("input_json") or not _loads(task["input_json"], []):
            return self._mark(task, {"status": "skipped", "reason": "任务没有可复用的日志包"})
        digest = hashlib.sha1(f"{task_id}|{root_name}|{skill_name}".encode("utf-8")).hexdigest()[:10]
        name = f"bbu-fault-learn-{digest}"
        if self.db.one("SELECT id FROM workshop_drafts WHERE name=?", (name,)):
            return self._mark(task, {"status": "named_conflict", "name": name,
                                      "reason": "同名自学习草稿已存在"})
        title = f"自学习：{str(root_name)[:42]}"
        description = (
            f"来源任务 {task_id}\n采信技能：{skill_name or '-'}\n"
            f"自动沉淀，待人工在工坊确认规则与根因后发布。\n\n"
            f"现场描述：\n{(task.get('description') or '')[:3000]}"
        )
        try:
            draft = self.workshop.create_draft(task["user_id"] or 0, {
                "name": name, "title": title, "fault_name": str(root_name)[:80],
                "category": "自学习", "description": description,
                "keywords": [str(root_name)[:40], skill_name] if skill_name else [str(root_name)[:40]],
                "root_cause_name": str(root_name)[:80], "verified": False,
            })
        except Exception as exc:  # noqa: BLE001
            return self._mark(task, {"status": "failed", "reason": f"创建草稿失败: {exc}"})

        draft_id = draft["id"]
        try:
            self.db.execute(
                "UPDATE workshop_drafts SET source_task_id=?, updated_at=? WHERE id=?",
                (task_id, iso_now(), draft_id))
        except Exception:
            # 部分索引冲突（极端并发）时保留草稿，不覆盖已有映射
            pass
        files = self._stage_inputs(task_id, _loads(task["input_json"], []))
        if not files:
            self._mark(task, {"status": "draft_created", "draft_id": draft_id,
                              "name": name, "reason": "草稿已建，但日志不可复用"})
            return {"status": "draft_created", "draft_id": draft_id, "name": name}
        try:
            self.workshop.attach_logs(draft_id, files)
        except Exception as exc:  # noqa: BLE001 - 草稿保留，人工可重试
            self._mark(task, {"status": "draft_created_analysis_failed", "draft_id": draft_id,
                              "name": name, "reason": f"日志分析失败: {type(exc).__name__}: {exc}"})
            return {"status": "draft_created_analysis_failed", "draft_id": draft_id, "name": name}
        learning = {"status": "draft_created", "draft_id": draft_id, "name": name,
                    "reason": "后台已创建工坊草稿，等待人工确认"}
        self._mark(task, learning)
        self.db.audit(task.get("user_id"), None, "learning.draft", draft_id,
                      {"task_id": task_id, "root_cause": root_name, "skill": skill_name})
        return learning

    def _stage_inputs(self, task_id: str, inputs: list[dict[str, Any]]) -> list[dict[str, str]]:
        """把任务原始日志硬链/复制到临时目录，交给工坊（不移动用户原始上传）。"""
        tmp = self.config.data_dir / "tmp" / "learning" / task_id
        tmp.mkdir(parents=True, exist_ok=True)
        out: list[dict[str, str]] = []
        for item in inputs:
            src = Path(str(item.get("stored_path") or ""))
            if not src.exists() or not src.is_file():
                continue
            target = unique_path(tmp, src.name)
            try:
                os.link(src, target)
            except OSError:
                shutil.copy2(src, target)
            out.append({"stored_path": str(target), "name": src.name})
        return out

    def _mark(self, task: dict[str, Any], learning: dict[str, Any]) -> dict[str, Any]:
        row = self.db.one("SELECT result_json FROM tasks WHERE id=?", (task["id"],))
        result = _loads((row or {}).get("result_json"), {})
        result["learning"] = learning
        self.db.execute("UPDATE tasks SET result_json=? WHERE id=?",
                        (json.dumps(result, ensure_ascii=False, default=str), task["id"]))
        self._event(task["id"], "info" if learning.get("status") != "failed" else "warning",
                    "自学习：" + str(learning.get("reason") or learning.get("status")), "done",
                    {"learning": learning})
        return learning

    def _event(self, task_id: str, level: str, message: str, stage: str,
               data: dict[str, Any] | None = None) -> None:
        with self.db.tx() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM task_events WHERE task_id=?",
                (task_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO task_events(task_id, seq, ts, level, stage, message, data_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (task_id, seq, iso_now(), level, stage, message,
                 json.dumps(data or {}, ensure_ascii=False, default=str)))
