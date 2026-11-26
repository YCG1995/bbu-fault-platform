"""任务队列与工作线程池：多任务并行 + 单任务内多 Skill 扇出。"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .db import Database
from .learning import LearningService
from .pipeline import TaskPipeline
from .util import iso_now


class TaskWorker:
    def __init__(self, db: Database, config: Any, pipeline: TaskPipeline,
                 learning: LearningService | None = None) -> None:
        self.db = db
        self.config = config
        self.pipeline = pipeline
        self.learning = learning
        self.max_workers = max(1, int(config.get("max_workers", 4)))
        self.max_queue = max(1, int(config.get("max_queue", 32)))
        self.max_resume = max(0, int(config.get("max_task_resume", 1)))
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=self.max_queue)
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers,
                                            thread_name_prefix="task")
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._dispatcher = threading.Thread(target=self._dispatch, name="dispatcher", daemon=True)
        self._stopped = threading.Event()

    def start(self) -> None:
        self._recover()
        self._dispatcher.start()

    def _recover(self) -> None:
        # 服务重启后：running 任务可凭任务目录中的 plan/routing/task_skills 断点恢复；
        # 超过恢复次数的才标记失败，避免坏任务无限重启。
        enqueued: set[str] = set()
        for row in self.db.all("SELECT id,resume_count FROM tasks WHERE status='running'"):
            if int(row.get("resume_count") or 0) < self.max_resume:
                self.db.execute(
                    "UPDATE tasks SET status='queued', stage='queued', progress=0, "
                    "resume_count=resume_count+1, error=? WHERE id=?",
                    ("服务重启，已排队断点恢复", row["id"]))
                if self._enqueue(row["id"], recovered=True):
                    enqueued.add(row["id"])
            else:
                self.db.execute(
                    "UPDATE tasks SET status='failed', stage='done', finished_at=?, error=? WHERE id=?",
                    (iso_now(), "服务重启导致任务中断，且已达到自动恢复次数，请重新提交", row["id"]))
        for row in self.db.all("SELECT id FROM tasks WHERE status='queued'"):
            if row["id"] not in enqueued:
                self._enqueue(row["id"], recovered=True)

    def _enqueue(self, task_id: str, recovered: bool = False) -> bool:
        try:
            self._queue.put_nowait(task_id)
            return True
        except queue.Full:
            if recovered:
                self.db.execute(
                    "UPDATE tasks SET status='failed', stage='done', finished_at=?, error=? WHERE id=?",
                    (iso_now(), f"队列容量 {self.max_queue} 已满，任务未能恢复，请稍后重试", task_id))
            return False

    def submit(self, task_id: str, fail_on_full: bool = True) -> bool:
        self.db.execute("UPDATE tasks SET status='queued', stage='queued', progress=0 WHERE id=?",
                        (task_id,))
        if not self._enqueue(task_id):
            if fail_on_full:
                self.db.execute(
                    "UPDATE tasks SET status='failed', stage='done', finished_at=?, error=? WHERE id=?",
                    (iso_now(), f"队列容量 {self.max_queue} 已满，请稍后重试", task_id))
            return False
        return True

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            event = self._events.get(task_id)
        if event:
            event.set()
        task = self.db.one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not task:
            return False
        if task["status"] in ("queued", "running"):
            if task["status"] == "queued":
                self.db.execute(
                    "UPDATE tasks SET status='cancelled', stage='done', finished_at=?, error=? WHERE id=?",
                    (iso_now(), "用户取消", task_id))
                with self.db.tx() as conn:
                    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM task_events WHERE task_id=?",
                                       (task_id,)).fetchone()[0]
                    conn.execute(
                        "INSERT INTO task_events(task_id, seq, ts, level, stage, message, data_json) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (task_id, seq, iso_now(), "warning", "done", "任务已取消", "{}"))
            return True
        return False

    def _dispatch(self) -> None:
        while not self._stopped.is_set():
            try:
                task_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if task_id is None:
                break
            task = self.db.one("SELECT status FROM tasks WHERE id=?", (task_id,))
            if not task or task["status"] not in ("queued",):
                continue
            self._executor.submit(self._run, task_id)

    def _run(self, task_id: str) -> None:
        event = threading.Event()
        with self._lock:
            self._events[task_id] = event
        try:
            result = self.pipeline.run(task_id, event)
            if self.learning and isinstance(result, dict):
                self.learning.enqueue(task_id, result)
        except Exception as exc:  # noqa: BLE001 - 兜底，防止线程静默死亡
            self.db.execute(
                "UPDATE tasks SET status='failed', stage='done', progress=100, finished_at=?, error=? "
                "WHERE id=? AND status='running'",
                (iso_now(), f"编排器异常: {exc}"[:2000], task_id))
        finally:
            with self._lock:
                self._events.pop(task_id, None)

    def shutdown(self) -> None:
        self._stopped.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            running = len(self._events)
        return {"max_workers": self.max_workers, "running": running,
                "queued": self._queue.qsize(), "max_queue": self.max_queue,
                "available": max(0, self.max_queue - self._queue.qsize())}
