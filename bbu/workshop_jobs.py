"""技能工坊后台任务：持久化状态、真实阶段进度、重启中断恢复。

工坊的 AI 提取、评审、日志解包和构建都可能远超一次 HTTP 请求的合理等待时间。
这里不复用诊断 TaskWorker，避免长工坊任务占满诊断队列。
"""
from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .db import Database
from .util import iso_now, new_id


class WorkshopJobService:
    def __init__(self, db: Database, config: Any, workshop: Any, gateway: Any,
                 traces: Any = None) -> None:
        self.db = db
        self.config = config
        self.workshop = workshop
        self.gateway = gateway
        self.traces = traces
        self.max_workers = max(1, int(config.get("workshop_max_workers", 1)))
        self.max_queue = max(self.max_workers, int(config.get("workshop_max_queue", 16)))
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers,
                                           thread_name_prefix="workshop-job")
        self._lock = threading.Lock()
        self._recover()

    def _recover(self) -> None:
        for row in self.db.all(
                "SELECT id, draft_id FROM workshop_jobs WHERE status IN ('queued','running')"):
            self.db.execute(
                "UPDATE workshop_jobs SET status='interrupted', stage='interrupted', "
                "message=?, error=?, finished_at=?, updated_at=? WHERE id=?",
                ("服务重启导致任务中断，请重试", "interrupted: 服务重启", iso_now(), iso_now(),
                 row["id"]))
            self.db.execute(
                "UPDATE workshop_drafts SET status='needs_confirmation', last_error=?, updated_at=? "
                "WHERE id=? AND status IN ('generating','analyzing')",
                ("上次工坊任务被服务中断，请重试当前步骤", iso_now(), row["draft_id"]))

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def active_for(self, draft_id: str, kind: str) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT * FROM workshop_jobs WHERE draft_id=? AND kind=? "
            "AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
            (draft_id, kind))
        return self.public(row) if row else None

    def create(self, user_id: int, draft_id: str, kind: str,
               args: dict[str, Any] | None = None, *, model_override: int | None = None,
               uploads: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        with self._lock:
            active = self.active_for(draft_id, kind)
            if active:
                return active
            queued = self.db.one(
                "SELECT COUNT(*) AS n FROM workshop_jobs WHERE status IN ('queued','running')")["n"]
            if queued >= self.max_queue:
                raise ValueError(f"工坊任务队列已满（{queued}/{self.max_queue}），请稍后重试")
            job_id = new_id("WJ", with_time=True)
            job_args = dict(args or {})
            if model_override:
                job_args["model_override"] = int(model_override)
            if uploads:
                job_args["files"] = self._stage_uploads(draft_id, job_id, uploads)
            now = iso_now()
            self.db.execute(
                "INSERT INTO workshop_jobs(id,draft_id,user_id,kind,status,stage,progress,message,"
                "args_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, draft_id, user_id, kind, "queued", "queued", 0, "已进入工坊队列",
                 json.dumps(job_args, ensure_ascii=False, default=str), now, now))
        self.executor.submit(self._run, job_id)
        return self.get(job_id)

    def _stage_uploads(self, draft_id: str, job_id: str,
                       uploads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        root = Path(self.config.data_dir) / "workshop" / draft_id / "job_uploads" / job_id
        root.mkdir(parents=True, exist_ok=True)
        staged = []
        for item in uploads:
            src = Path(str(item.get("stored_path") or ""))
            if not src.is_file():
                raise ValueError("上传文件已失效，请重新上传")
            name = str(item.get("name") or src.name)
            dest = root / Path(name).name
            shutil.copy2(src, dest)
            staged.append({"stored_path": str(dest), "name": name,
                           "size": dest.stat().st_size})
        return staged

    def _run(self, job_id: str) -> None:
        job = self.db.one("SELECT * FROM workshop_jobs WHERE id=?", (job_id,))
        if not job:
            return
        args = self._loads(job.get("args_json"), {})
        self._update(job_id, status="running", stage="starting", progress=3,
                     message="正在启动工坊任务", started_at=iso_now())
        trace = None
        if self.traces:
            trace = self.traces.start(
                kind="workshop", ref_id=job_id, user_id=int(job.get("user_id") or 0),
                meta={"draft_id": job.get("draft_id"), "job_kind": job.get("kind"),
                      "trigger": "job", "args_keys": sorted(args.keys())})
        result: Any = None
        try:
            kind = job["kind"]
            draft_id = job["draft_id"]
            model_override = args.get("model_override")
            with self.gateway.usage_context(
                    int(job["user_id"]), task_id=job_id, task_kind="workshop"):
                if kind == "analyze":
                    result = self.workshop.analyze(
                        draft_id, progress=self._callback(job_id, trace),
                        model_override=model_override, trace=trace)
                elif kind == "logs":
                    result = self.workshop.attach_logs(
                        draft_id, args.get("files") or [], progress=self._callback(job_id, trace),
                        model_override=model_override, trace=trace)
                elif kind == "refine":
                    result = self.workshop.refine(
                        draft_id, str(args.get("text") or ""), progress=self._callback(job_id, trace),
                        model_override=model_override, trace=trace)
                elif kind == "case":
                    result = self.workshop.attach_case(
                        draft_id, args.get("files") or [], args.get("expected") or {})
                elif kind == "build":
                    result = self.workshop.build(draft_id)
                else:
                    raise ValueError(f"未知工坊任务类型: {kind}")
            self._update(job_id, status="done", stage="done", progress=100,
                         message="任务完成", result=result, finished_at=iso_now())
            if trace:
                ok = result.get("ok", True) if isinstance(result, dict) else True
                trace.finish("success" if ok else "failed",
                             error_code="" if ok else "workshop_result_failed",
                             error_summary="" if ok else str((result or {}).get("error") or "工坊结果未通过"))
        except Exception as exc:  # noqa: BLE001 - 任务状态必须完整落库
            if trace:
                trace.error(phase="workshop", code="workshop_job_failed",
                            message=f"{kind if 'kind' in locals() else ''}: {exc}",
                            exc=exc, retryable=True,
                            details={"job_id": job_id, "draft_id": job.get("draft_id")})
                trace.finish("failed", error_code="workshop_job_failed",
                             error_summary=str(exc)[:4000])
            self._update(job_id, status="failed", stage="failed", progress=100,
                         message="任务失败", error=str(exc)[:1200], finished_at=iso_now())
            try:
                self.db.execute(
                    "UPDATE workshop_drafts SET status='needs_confirmation', last_error=?, "
                    "updated_at=? WHERE id=?",
                    (f"工坊任务失败：{str(exc)[:800]}", iso_now(), job["draft_id"]))
            except Exception:
                pass

    def _callback(self, job_id: str, trace: Any = None):
        def report(stage: str, progress: int, message: str) -> None:
            progress = max(0, min(99, int(progress)))
            self._update(job_id, stage=stage, progress=progress, message=message)
            if trace:
                trace.append("stage", phase=stage, status="running", progress=progress,
                             message=message)
        return report

    def _update(self, job_id: str, *, result: Any = None, **fields: Any) -> None:
        fields["updated_at"] = iso_now()
        if result is not None:
            fields["result_json"] = json.dumps(result, ensure_ascii=False, default=str)
        names = list(fields)
        values = [fields[n] for n in names]
        self.db.execute(
            "UPDATE workshop_jobs SET " + ", ".join(f"{n}=?" for n in names) + " WHERE id=?",
            (*values, job_id))

    def get(self, job_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM workshop_jobs WHERE id=?", (job_id,))
        if not row:
            raise ValueError("工坊任务不存在")
        return self.public(row)

    def list_for_draft(self, draft_id: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM workshop_jobs WHERE draft_id=? ORDER BY created_at DESC LIMIT ?",
            (draft_id, max(1, min(50, limit))))
        return [self.public(r) for r in rows]

    def public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "draft_id": row["draft_id"], "kind": row["kind"],
            "status": row["status"], "stage": row["stage"], "progress": row["progress"],
            "message": row["message"], "error": row["error"],
            "result": self._loads(row.get("result_json"), {}),
            "created_at": row["created_at"], "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"), "updated_at": row["updated_at"],
        }

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return default
