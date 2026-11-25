"""任务空间统计与原始输入释放。

平台只释放每个任务的原始上传日志；报告、证据、案例和数据库记录始终保留。
重跑任务会复用原任务的同一批输入路径，所以释放前必须扫描所有任务引用，
不能因为当前任务不再需要就直接删除共享文件。
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

ACTIVE_STATUSES = {"queued", "running"}
RELEASABLE_INPUT_STATES = {"available", "release_interrupted"}


class TaskStorageService:
    def __init__(self, db: Database, config: Any) -> None:
        self.db = db
        self.config = config
        self.max_workers = max(1, int(config.get("storage_max_workers", 1)))
        self.max_queue = max(self.max_workers, int(config.get("storage_max_queue", 8)))
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers,
                                           thread_name_prefix="storage-job")
        self._lock = threading.Lock()
        self._recover()

    def _recover(self) -> None:
        rows = self.db.all(
            "SELECT id, task_id FROM storage_jobs WHERE status IN ('queued','running')")
        for row in rows:
            self.db.execute(
                "UPDATE storage_jobs SET status='interrupted', stage='interrupted', "
                "message=?, error=?, finished_at=?, updated_at=? WHERE id=?",
                ("服务重启导致释放中断；可重试以完成剩余清理", "interrupted: 服务重启",
                 iso_now(), iso_now(), row["id"]))
            self.db.execute(
                "UPDATE tasks SET inputs_state='release_interrupted' WHERE id=? "
                "AND inputs_state='releasing'", (row["task_id"],))
        self.db.execute(
            "UPDATE tasks SET inputs_state='release_interrupted' WHERE inputs_state='releasing'")

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ 统计
    def _all_references(self) -> dict[str, set[str]]:
        refs: dict[str, set[str]] = {}
        for task in self.db.all(
                "SELECT id, input_json, inputs_state FROM tasks"):
            if (task.get("inputs_state") or "available") in (
                    "released", "excluded_by_migration"):
                continue
            for item in self._task_inputs(task):
                path = str(item.get("stored_path") or "")
                if path:
                    refs.setdefault(str(Path(path)), set()).add(task["id"])
        return refs

    @staticmethod
    def _task_inputs(task: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            value = json.loads(task.get("input_json") or "[]")
            return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []
        except (TypeError, json.JSONDecodeError):
            return []

    def _upload_root(self) -> Path:
        return (Path(self.config.data_dir) / "uploads").resolve()

    def _safe_input_path(self, value: str) -> Path | None:
        if not value:
            return None
        path = Path(value)
        try:
            resolved = path.resolve()
            resolved.relative_to(self._upload_root())
        except (OSError, ValueError):
            return None
        return resolved

    @staticmethod
    def _dir_size(path: Path) -> int:
        if not path.exists():
            return 0
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    def _task_detail(self, task: dict[str, Any],
                     refs: dict[str, set[str]]) -> dict[str, Any]:
        paths: dict[str, Path] = {}
        for item in self._task_inputs(task):
            path = self._safe_input_path(str(item.get("stored_path") or ""))
            if path is not None:
                paths[str(path)] = path
        raw_bytes = 0
        deletable_bytes = 0
        shared_paths = 0
        existing = 0
        for key, path in paths.items():
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            existing += 1
            raw_bytes += size
            if refs.get(key, set()) - {task["id"]}:
                shared_paths += 1
            else:
                deletable_bytes += size
        artifact_dir = Path(self.config.data_dir) / "tasks" / task["id"]
        artifacts = self._dir_size(artifact_dir)
        state = task.get("inputs_state") or "available"
        active = task.get("status") in ACTIVE_STATUSES
        can_release = (not active and state in RELEASABLE_INPUT_STATES
                       and existing > 0)
        return {
            "id": task["id"], "title": task.get("title"), "status": task.get("status"),
            "created_at": task.get("created_at"), "finished_at": task.get("finished_at"),
            "inputs_state": state, "raw_inputs_available": state == "available",
            "raw_input_bytes": raw_bytes, "artifact_bytes": artifacts,
            "total_bytes": raw_bytes + artifacts, "deletable_bytes": deletable_bytes,
            "shared_input_paths": shared_paths, "input_files": existing,
            "can_release": can_release, "active": active,
            "last_storage_job": None,
        }

    def overview(self, user_id: int | None = None, is_admin: bool = False,
                 limit: int = 1000) -> dict[str, Any]:
        refs = self._all_references()
        if is_admin or user_id is None:
            tasks = self.db.all(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (max(1, min(5000, int(limit))),))
        else:
            tasks = self.db.all(
                "SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, max(1, min(5000, int(limit)))))
        details = [self._task_detail(t, refs) for t in tasks]
        jobs = {j["task_id"]: j for j in self.db.all(
            "SELECT * FROM storage_jobs WHERE status IN ('queued','running') "
            "ORDER BY created_at DESC")}
        for detail in details:
            job = jobs.get(detail["id"])
            if job:
                detail["last_storage_job"] = self.public(job)
                detail["can_release"] = False
        raw_paths = {p for p, ids in refs.items() if ids}
        raw_total = 0
        for value in raw_paths:
            path = self._safe_input_path(value)
            try:
                if path and path.is_file() and not path.is_symlink():
                    raw_total += path.stat().st_size
            except OSError:
                continue
        artifacts_total = sum(
            self._dir_size(Path(self.config.data_dir) / "tasks" / t["id"]) for t in tasks)
        try:
            disk = shutil.disk_usage(self.config.data_dir)
            disk_info = {"total_bytes": disk.total, "free_bytes": disk.free,
                         "used_bytes": disk.used}
        except OSError:
            disk_info = {}
        return {
            "items": details,
            "summary": {
                "task_count": len(details),
                "raw_input_bytes": raw_total,
                "artifact_bytes": artifacts_total,
                "total_bytes": raw_total + artifacts_total,
                "shared_paths": sum(1 for ids in refs.values() if len(ids) > 1),
                "deletable_bytes": sum(d["deletable_bytes"] for d in details),
                "disk": disk_info,
            },
        }

    # -------------------------------------------------------------- 后台释放
    def active_for(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT * FROM storage_jobs WHERE task_id=? AND status IN ('queued','running') "
            "ORDER BY created_at DESC LIMIT 1", (task_id,))
        return self.public(row) if row else None

    def create(self, task_id: str, user_id: int) -> dict[str, Any]:
        with self._lock:
            active = self.active_for(task_id)
            if active:
                return active
            task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            if not task:
                raise ValueError("任务不存在")
            if task.get("status") in ACTIVE_STATUSES:
                raise ValueError("任务正在运行，结束后才能释放原始输入")
            state = task.get("inputs_state") or "available"
            if state == "excluded_by_migration":
                raise ValueError("原始输入未随迁移包导出，没有可释放的本地文件")
            if state == "released":
                raise ValueError("该任务的原始输入已释放")
            if state not in RELEASABLE_INPUT_STATES:
                raise ValueError(f"当前输入状态 {state} 不允许释放，请先检查上次中断的释放任务")
            queued = self.db.one(
                "SELECT COUNT(*) AS n FROM storage_jobs "
                "WHERE status IN ('queued','running')")["n"]
            if queued >= self.max_queue:
                raise ValueError(f"空间释放队列已满（{queued}/{self.max_queue}），请稍后重试")
            job_id = new_id("SJ", with_time=True)
            now = iso_now()
            self.db.execute(
                "INSERT INTO storage_jobs(id,task_id,user_id,status,stage,progress,message,"
                "options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, task_id, user_id, "queued", "queued", 0, "已进入释放队列",
                 json.dumps({"mode": "raw_inputs"}, ensure_ascii=False), now, now))
        self.executor.submit(self._run, job_id)
        return self.get(job_id)

    def _run(self, job_id: str) -> None:
        job = self.db.one("SELECT * FROM storage_jobs WHERE id=?", (job_id,))
        if not job:
            return
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (job["task_id"],))
        if not task:
            self._fail(job_id, "任务不存在")
            return
        self._update(job_id, status="running", stage="scanning", progress=5,
                     message="正在扫描共享输入引用", started_at=iso_now())
        try:
            if task.get("status") in ACTIVE_STATUSES:
                raise ValueError("任务正在运行，不能释放原始输入")
            self.db.execute(
                "UPDATE tasks SET inputs_state='releasing' WHERE id=?", (task["id"],))
            self._update(job_id, stage="scanning", progress=15,
                         message="正在检查其他任务是否复用同一批原始文件")
            refs = self._all_references()
            paths: dict[str, Path] = {}
            for item in self._task_inputs(task):
                path = self._safe_input_path(str(item.get("stored_path") or ""))
                if path is not None:
                    paths[str(path)] = path
            files = 0
            freed = 0
            shared = 0
            total = len(paths) or 1
            for index, (key, path) in enumerate(paths.items(), start=1):
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    if refs.get(key, set()) - {task["id"]}:
                        shared += 1
                        continue
                    size = path.stat().st_size
                    path.unlink()
                    files += 1
                    freed += size
                except OSError:
                    continue
                self._update(job_id, stage="deleting", progress=15 + int(index / total * 75),
                             message=f"已释放 {files} 个文件 / {freed} 字节")
            upload_dir = self._upload_root() / task["id"]
            if upload_dir.is_dir():
                try:
                    if not any(upload_dir.iterdir()):
                        upload_dir.rmdir()
                except OSError:
                    pass
            self._update(job_id, stage="finalizing", progress=95,
                         message="正在更新任务输入状态")
            self.db.execute(
                "UPDATE tasks SET inputs_state='released' WHERE id=?", (task["id"],))
            self._update(
                job_id, status="done", stage="done", progress=100, message="原始输入已释放",
                summary={"files": files, "freed_bytes": freed, "shared_paths": shared},
                freed_bytes=freed, finished_at=iso_now())
        except Exception as exc:  # noqa: BLE001 - 失败状态必须落库
            self._fail(job_id, str(exc))

    def _fail(self, job_id: str, message: str) -> None:
        job = self.db.one("SELECT task_id FROM storage_jobs WHERE id=?", (job_id,)) or {}
        self._update(job_id, status="failed", stage="failed", progress=100,
                     message="释放失败", error=str(message)[:1200], finished_at=iso_now())
        if job.get("task_id"):
            self.db.execute(
                "UPDATE tasks SET inputs_state='release_interrupted' WHERE id=? "
                "AND inputs_state='releasing'", (job["task_id"],))

    def _update(self, job_id: str, *, summary: Any = None, **fields: Any) -> None:
        fields["updated_at"] = iso_now()
        if summary is not None:
            fields["summary_json"] = json.dumps(summary, ensure_ascii=False)
        names = list(fields)
        values = [fields[n] for n in names]
        self.db.execute(
            "UPDATE storage_jobs SET " + ", ".join(f"{n}=?" for n in names) + " WHERE id=?",
            (*values, job_id))

    def get(self, job_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM storage_jobs WHERE id=?", (job_id,))
        if not row:
            raise ValueError("空间释放任务不存在")
        return self.public(row)

    def list_for_task(self, task_id: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM storage_jobs WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
            (task_id, max(1, min(50, int(limit)))))
        return [self.public(row) for row in rows]

    def public(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            summary = json.loads(row.get("summary_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            summary = {}
        return {
            "id": row["id"], "task_id": row["task_id"], "status": row["status"],
            "stage": row["stage"], "progress": row["progress"], "message": row["message"],
            "error": row.get("error"), "summary": summary,
            "freed_bytes": row.get("freed_bytes") or 0,
            "created_at": row["created_at"], "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"), "updated_at": row["updated_at"],
        }
