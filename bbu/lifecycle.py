"""O9 任务数据生命周期：到期归档、引用保护、SHA256 与可恢复解档。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import Database
from .util import iso_now


def _parse_time(value: Any) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LifecycleService:
    def __init__(self, db: Database, config: Any) -> None:
        self.db = db
        self.config = config

    @property
    def archive_dir(self) -> Path:
        path = Path(self.config.data_dir) / "task_archives"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _upload_root(self) -> Path:
        return (Path(self.config.data_dir) / "uploads").resolve()

    def _inputs(self, task: dict[str, Any]) -> list[Path]:
        try:
            values = json.loads(task.get("input_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        out = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("stored_path") or "")
            try:
                path = Path(raw).resolve()
                path.relative_to(self._upload_root())
            except (OSError, ValueError):
                continue
            if path.is_file() and not path.is_symlink():
                out.append(path)
        return out

    def _references(self) -> dict[str, set[str]]:
        refs: dict[str, set[str]] = {}
        for row in self.db.all("SELECT id, input_paths_json FROM real_cases"):
            try:
                paths = json.loads(row.get("input_paths_json") or "[]")
            except json.JSONDecodeError:
                paths = []
            for value in paths if isinstance(paths, list) else []:
                refs.setdefault(str(value), set()).add(str(row["id"]))
        for task in self.db.all("SELECT id, input_json, inputs_state FROM tasks"):
            if (task.get("inputs_state") or "available") == "archived":
                continue
            try:
                paths = json.loads(task.get("input_json") or "[]")
            except json.JSONDecodeError:
                paths = []
            for item in paths if isinstance(paths, list) else []:
                if isinstance(item, dict) and item.get("stored_path"):
                    refs.setdefault(str(item["stored_path"]), set()).add(str(task["id"]))
        return refs

    def _referenced_by(self, task: dict[str, Any], refs: dict[str, set[str]]) -> list[str]:
        owners: set[str] = set()
        for path in self._inputs(task):
            owners |= refs.get(str(path), set()) - {str(task["id"])}
        return sorted(owners)

    def _age_days(self, task: dict[str, Any], now: datetime) -> int:
        return max(0, (now - _parse_time(task.get("finished_at") or task.get("created_at"))).days)

    def overview(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        refs = self._references()
        archive_after = int(self.config.get("task_archive_after_days", 90))
        raw_retention = int(self.config.get("task_raw_retention_days", 180))
        items = []
        for task in self.db.all("SELECT * FROM tasks ORDER BY created_at DESC"):
            age = self._age_days(task, now)
            inputs = self._inputs(task)
            refs_by = self._referenced_by(task, refs)
            state = str(task.get("retention_state") or "available")
            if refs_by:
                state = "referenced"
            elif task.get("archive_path"):
                state = "archived"
            elif age >= archive_after:
                state = "archivable"
            items.append({
                "id": task["id"], "title": task.get("title"), "age_days": age,
                "input_files": len(inputs), "input_bytes": sum(p.stat().st_size for p in inputs),
                "state": state, "archive_path": task.get("archive_path") or "",
                "archive_sha256": task.get("archive_sha256") or "",
                "retained_reason": task.get("retained_reason") or (
                    f"被真实案例/评测引用：{', '.join(refs_by)}" if refs_by else ""),
                "raw_retention_days": raw_retention, "archive_after_days": archive_after,
            })
        return {"items": items, "summary": {
            "tasks": len(items),
            "archivable": sum(1 for x in items if x["state"] == "archivable"),
            "archived": sum(1 for x in items if x["state"] == "archived"),
            "referenced": sum(1 for x in items if x["state"] == "referenced"),
            "bytes": sum(x["input_bytes"] for x in items)}}

    def archive_task(self, task_id: str, *, now: datetime | None = None,
                     force: bool = False) -> dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise ValueError("任务不存在")
        refs = self._references()
        owners = self._referenced_by(task, refs)
        if owners and not force:
            reason = "被真实案例/评测引用：" + ", ".join(owners)
            self.db.execute("UPDATE tasks SET retention_state='referenced', retained_reason=? "
                            "WHERE id=?", (reason, task_id))
            return {"ok": False, "retained_reason": reason, "task_id": task_id}
        inputs = self._inputs(task)
        if not inputs:
            raise ValueError("任务没有可归档的原始输入")
        target = self.archive_dir / f"{task_id}.tar.gz"
        tmp = target.with_suffix(".tar.gz.tmp")
        manifest = {"task_id": task_id, "archived_at": iso_now(),
                    "files": [{"name": p.name, "size": p.stat().st_size} for p in inputs]}
        with tarfile.open(tmp, "w:gz") as tf:
            for path in inputs:
                tf.add(path, arcname=f"uploads/{path.name}")
            info = tarfile.TarInfo("manifest.json")
            payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            info.size = len(payload)
            import io
            tf.addfile(info, io.BytesIO(payload))
        os.replace(tmp, target)
        digest = _sha256(target)
        for path in inputs:
            try:
                path.unlink()
            except OSError:
                pass
        self.db.execute(
            "UPDATE tasks SET inputs_state='archived', retention_state='archived', "
            "archive_path=?, archive_sha256=?, archived_at=?, retained_reason='' WHERE id=?",
            (str(target), digest, iso_now(), task_id))
        return {"ok": True, "task_id": task_id, "archive_path": str(target),
                "sha256": digest, "files": len(inputs)}

    def archive_due(self, *, now: datetime | None = None, limit: int = 50,
                    dry_run: bool = False) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        after = int(self.config.get("task_archive_after_days", 90))
        refs = self._references()
        due = []
        for task in self.db.all("SELECT * FROM tasks ORDER BY created_at"):
            if task.get("inputs_state") == "archived" or task.get("archive_path"):
                continue
            if self._age_days(task, now) < after:
                continue
            owners = self._referenced_by(task, refs)
            if owners:
                self.db.execute("UPDATE tasks SET retention_state='referenced', retained_reason=? "
                                "WHERE id=?", ("被真实案例/评测引用：" + ", ".join(owners),
                                               task["id"]))
                continue
            if not self._inputs(task):
                continue
            if len(due) >= limit:
                break
            due.append(task["id"])
        if dry_run:
            return {"ok": True, "due": due, "count": len(due)}
        results = [self.archive_task(task_id, now=now) for task_id in due]
        return {"ok": True, "count": len(results), "results": results}

    def restore(self, task_id: str, target: Path) -> dict[str, Any]:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task or not task.get("archive_path"):
            raise ValueError("任务没有归档")
        archive = Path(task["archive_path"])
        if not archive.is_file():
            raise ValueError("归档文件不存在")
        expected = str(task.get("archive_sha256") or "")
        actual = _sha256(archive)
        if expected and actual != expected:
            raise ValueError("归档 SHA256 校验失败，归档可能已损坏")
        target = Path(target).resolve()
        if target.exists() and any(target.iterdir()):
            raise ValueError("恢复目标目录非空")
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                dest = (target / member.name).resolve()
                if not str(dest).startswith(str(target) + os.sep):
                    raise ValueError("归档包含越界路径")
            tf.extractall(target, filter="data")
        files = [str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()]
        return {"ok": True, "task_id": task_id, "target": str(target),
                "files": files, "sha256": actual}
