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
from .case_matrix import record_candidate
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


def _vector(text: str, dim: int = 64) -> dict[int, float]:
    """本地稀疏向量：词 + 字符三元组哈希，避免引入外部 embedding 依赖。"""
    values: dict[int, float] = {}
    tokens = _tokens(text)
    for token in tokens:
        bucket = int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:8], 16) % dim
        values[bucket] = values.get(bucket, 0.0) + 1.0
    clean = re.sub(r"\s+", "", (text or "").lower())
    for i in range(max(0, len(clean) - 2)):
        gram = clean[i:i + 3]
        bucket = int(hashlib.sha1(gram.encode("utf-8")).hexdigest()[:8], 16) % dim
        values[bucket] = values.get(bucket, 0.0) + 0.35
    return values


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(v * right.get(k, 0.0) for k, v in left.items())
    norm = ((sum(v * v for v in left.values()) ** 0.5) *
            (sum(v * v for v in right.values()) ** 0.5))
    return dot / norm if norm else 0.0


_ALIAS_GROUPS = [
    {"失锁", "unlocked", "unlock", "pll", "clock"},
    {"光模块", "光功率", "optical", "rx", "receive"},
    {"驻波", "vswr", "swr", "天馈"},
    {"复位", "reboot", "reset", "restart", "watchdog"},
    {"电源", "power", "voltage", "电压"},
    {"ssd", "硬盘", "disk", "smart", "寿命"},
    {"小区", "cell", "扇区", "sector"},
    {"单板", "board", "slot", "槽位"},
    {"告警", "alarm", "alert", "fm"},
]


def _alias_score(query: str, doc: str) -> float:
    q = (query or "").lower()
    d = (doc or "").lower()
    hits = 0
    for group in _ALIAS_GROUPS:
        q_hit = [x for x in group if x in q]
        d_hit = [x for x in group if x in d]
        if q_hit and d_hit and set(q_hit) != set(d_hit):
            hits += 1
    return min(1.0, hits / max(1, len(_ALIAS_GROUPS)) * 2.0)


def _bm25(query_tokens: set[str], doc_tf: dict[str, float], doc_len: float,
          df: dict[str, int], n_docs: int, avgdl: float) -> float:
    k1, b = 1.2, 0.75
    if not query_tokens or not doc_tf:
        return 0.0
    total = 0.0
    for token in query_tokens:
        tf = doc_tf.get(token, 0.0)
        if not tf:
            continue
        idf = __import__("math").log((n_docs - df.get(token, 0) + 0.5) /
                                     (df.get(token, 0) + 0.5) + 1.0)
        total += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / max(1.0, avgdl)))
    return total


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else (value if value is not None else default)
    except (TypeError, json.JSONDecodeError):
        return default


def _input_digest(task: dict[str, Any]) -> str:
    try:
        inputs = json.loads(task.get("input_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        inputs = []
    files = []
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                files.append({k: item.get(k) for k in ("sha256", "name", "size")})
            else:
                files.append(str(item))
    return hashlib.sha256(
        json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


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
        try:
            options = json.loads(task.get("options_json") or "{}")
        except json.JSONDecodeError:
            options = {}
        device_type = str(options.get("device_type") or options.get("device") or "")
        software_version = str(options.get("software_version") or options.get("version") or "")
        source_revision = str((result.get("trace") or {}).get("session_revision") or "")
        outcome = {
            "status": verdict.get("status") or task.get("status"),
            "device_type": device_type,
            "software_version": software_version,
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
            "description,hints_json,plan_json,outcome_json,created_at,trust_level,"
            "device_type,software_version,source_revision,invalidated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
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
             "unreviewed", device_type, software_version, source_revision))

    def search(self, description: str, hints: list[str] | None = None,
               skill_name: str = "", limit: int = 3,
               include_unreviewed: bool = False, *, device_type: str = "",
               software_version: str = "", fault_domain: str = "",
               exclude_task: str = "", with_counterexamples: bool = False) -> list[dict[str, Any]]:
        query_text = (description or "") + " " + " ".join(hints or []) + " " + (skill_name or "")
        query_tokens = _tokens(query_text)
        query_vec = _vector(query_text)
        trust_sql = "" if include_unreviewed else \
            " AND trust_level IN ('verified','golden','curated') "
        rows = self.db.all(
            "SELECT * FROM plan_experiences WHERE status IN "
            "('done','needs_data','matched','unmatched','temporal_conflict','insufficient') "
            "AND invalidated=0 " + trust_sql +
            "ORDER BY id DESC LIMIT 300")
        docs: list[dict[str, Any]] = []
        for row in rows:
            if exclude_task and row.get("task_id") == exclude_task:
                continue
            outcome = _loads(row.get("outcome_json"), {})
            row_device = str(outcome.get("device_type") or row.get("device_type") or "")
            row_version = str(outcome.get("software_version") or row.get("software_version") or "")
            if device_type and row_device and row_device != device_type:
                continue
            if software_version and row_version and row_version != software_version:
                continue
            if fault_domain and fault_domain not in str(row.get("root_cause_name") or ""):
                continue
            fields = {
                "title": str(row.get("root_cause_name") or ""),
                "fault": str(row.get("description") or ""),
                "skill": str(row.get("skill_name") or ""),
                "body": " ".join(_loads(row.get("hints_json"), [])),
            }
            doc_text = " ".join(fields.values())
            doc_tf: dict[str, float] = {}
            for field, weight in (("title", 2.0), ("fault", 1.5), ("skill", 1.2), ("body", 1.0)):
                for token in _tokens(fields[field]):
                    doc_tf[token] = doc_tf.get(token, 0.0) + weight
            docs.append({"row": row, "outcome": outcome, "text": doc_text,
                         "tf": doc_tf, "len": sum(doc_tf.values()),
                         "doc_tokens": set(doc_tf)})
        n_docs = len(docs)
        avgdl = (sum(d["len"] for d in docs) / n_docs) if n_docs else 1.0
        df: dict[str, int] = {}
        for d in docs:
            for token in d["doc_tokens"]:
                df[token] = df.get(token, 0) + 1
        query_tokens = _tokens(query_text)
        ranked: list[tuple[float, dict[str, Any]]] = []
        all_docs: list[tuple[float, dict[str, Any]]] = []
        for d in docs:
            row, outcome = d["row"], d["outcome"]
            bm25 = _bm25(query_tokens, d["tf"], d["len"], df, n_docs, avgdl)
            bm25_norm = bm25 / (bm25 + 1.0) if bm25 else 0.0
            alias = _alias_score(query_text, d["text"])
            vector = _cosine(query_vec, _vector(d["text"]))
            score = 0.65 * bm25_norm + 0.2 * alias + 0.15 * vector
            if row.get("skill_name") and row["skill_name"] == skill_name:
                score += 0.08
            if row.get("root_cause_name") and row["root_cause_name"] in (description or ""):
                score += 0.04
            if device_type and row_device == device_type:
                score += 0.05
            if score < 0.06:
                continue
            item = {
                "task_id": row["task_id"],
                "fault": (row.get("description") or "")[:220],
                "root_cause": row.get("root_cause_name") or "",
                "skill": row.get("skill_name") or "",
                "status": row.get("status") or "",
                "confidence": row.get("confidence") or "",
                "trust_level": row.get("trust_level") or "unreviewed",
                "device_type": row_device,
                "software_version": row_version,
                "plan": _loads(row.get("plan_json"), {}),
                "outcome": outcome,
                "match_score": round(score, 3),
                "match": {"bm25": round(bm25_norm, 3), "alias": round(alias, 3),
                          "vector": round(vector, 3),
                          "structured": bool(device_type or software_version)},
            }
            ranked.append((score, item))
            all_docs.append((score, item))
        ranked.sort(key=lambda x: x[0], reverse=True)
        result = [item for _, item in ranked[:max(0, min(limit, 10))]]
        if with_counterexamples:
            for item in result:
                candidates = [
                    other for score, other in all_docs
                    if other.get("skill") == item.get("skill")
                    and other.get("root_cause") and other.get("root_cause") != item.get("root_cause")
                    and score >= 0.06
                ]
                item["counterexamples"] = [
                    {"task_id": x["task_id"], "root_cause": x["root_cause"],
                     "match_score": x["match_score"]} for x in candidates[:3]]
        return result

    def invalidate(self, task_ids: list[str], reason: str = "") -> int:
        ids = [str(x) for x in task_ids if x]
        if not ids:
            return 0
        changed = 0
        for task_id in ids:
            self.db.execute(
                "UPDATE plan_experiences SET invalidated=1 WHERE task_id=?", (task_id,))
            changed += 1
        return changed

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
        # 同一根因不重复建技能；但新日志变体必须进入候选池，供专家补例/合并。
        same_root = self.db.one(
            "SELECT id, name, source_task_id FROM workshop_drafts "
            "WHERE fault_name=? AND category='自学习' ORDER BY id DESC LIMIT 1",
            (str(root_name)[:80],))
        if same_root:
            source_task = self.db.one(
                "SELECT * FROM tasks WHERE id=?", (same_root.get("source_task_id"),)) \
                if same_root.get("source_task_id") else None
            if source_task and _input_digest(source_task) == _input_digest(task):
                return self._mark(task, {
                    "status": "duplicate_root", "draft_id": same_root["id"],
                    "reason": f"同根因且输入完全相同：已有草稿 {same_root['name']}"})
            candidate = record_candidate(
                self.db, task_id=task_id, skill_name=skill_name or same_root["name"],
                root_cause=str(root_name)[:80], old_root=str(root_name)[:80],
                decision="variant", kind="same_root_variant",
                note="同根因新日志变体，等待专家确认是否补例或合并",
                payload={"draft_id": same_root["id"], "source_task_id": task_id,
                         "input_digest": _input_digest(task),
                         "description": (task.get("description") or "")[:1200],
                         "files": (task.get("input_json") or "")[:1200]})
            return self._mark(task, {
                "status": "candidate_recorded", "draft_id": same_root["id"],
                "candidate_id": candidate["id"],
                "reason": "同根因新变体已写入经验候选，未丢弃"})
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
