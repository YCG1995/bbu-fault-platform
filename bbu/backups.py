"""历史备份目录扫描与安全删除。

网页升级快照、迁移恢复前的旧数据备份都位于 STATE_DIR/backups。
这里只管理该目录的直接子项，不递归跟随符号链接，也不允许路径穿越。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
ACTIVE_UPDATE_PHASES = {"queued", "backup", "migrating", "restarting", "health_check", "rolling_back"}


class BackupManager:
    def __init__(self, db: Any, config: Any) -> None:
        self.db = db
        self.config = config

    @property
    def root(self) -> Path:
        return (Path(self.config.data_dir).parent / "backups").resolve()

    def _safe(self, name: str) -> Path:
        name = str(name or "").strip()
        if not BACKUP_NAME_RE.fullmatch(name) or name in (".", ".."):
            raise ValueError("备份名称非法")
        root = self.root
        raw = root / name
        if raw.is_symlink():
            raise ValueError("不允许操作符号链接")
        path = raw.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("备份路径越界") from exc
        if path == root:
            raise ValueError("不能操作备份根目录")
        return path

    @staticmethod
    def _dir_size(path: Path) -> int:
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    @staticmethod
    def _file_count(path: Path) -> int:
        count = 0
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    count += 1
            except OSError:
                continue
        return count

    def _active_references(self) -> dict[str, list[str]]:
        """扫描升级状态，返回正在使用/引用某备份的升级任务 ID。"""
        refs: dict[str, list[str]] = {}
        updates = (Path(self.config.data_dir).parent / "updates").resolve()
        if not updates.is_dir():
            return refs
        for status_file in updates.glob("*/status.json"):
            try:
                status = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            backup = str(status.get("backup_dir") or "").strip()
            if not backup:
                continue
            try:
                key = str(Path(backup).resolve())
            except OSError:
                continue
            refs.setdefault(key, []).append(str(status.get("id") or status_file.parent.name))
        return refs

    def _active_jobs(self) -> dict[str, list[str]]:
        active: dict[str, list[str]] = {}
        updates = (Path(self.config.data_dir).parent / "updates").resolve()
        if not updates.is_dir():
            return active
        for status_file in updates.glob("*/status.json"):
            try:
                status = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            backup = str(status.get("backup_dir") or "").strip()
            phase = str(status.get("phase") or "")
            if backup and phase in ACTIVE_UPDATE_PHASES:
                active.setdefault(str(Path(backup).resolve()), []).append(
                    str(status.get("id") or status_file.parent.name))
        return active

    def _meta(self, path: Path) -> dict[str, Any]:
        if not path.is_dir():
            return {}
        try:
            value = json.loads((path / "backup.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def item(self, name: str, active: dict[str, list[str]] | None = None,
             refs: dict[str, list[str]] | None = None) -> dict[str, Any]:
        path = self._safe(name)
        if not path.exists():
            raise ValueError("备份不存在")
        is_dir = path.is_dir()
        meta = self._meta(path)
        if is_dir:
            size = self._dir_size(path)
            count = self._file_count(path)
        else:
            size = path.stat().st_size
            count = 1
        created = str(meta.get("created_at") or "").strip()
        if not created:
            created = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        key = str(path)
        active_jobs = (active or {}).get(key, [])
        referenced_by = (refs or {}).get(key, [])
        old_version = str(meta.get("old_version") or "")
        new_version = str(meta.get("new_version") or "")
        if name.startswith("import-"):
            kind = "import"
        elif old_version or new_version or "-to-" in name:
            kind = "upgrade"
        else:
            kind = "manual"
        return {
            "name": name, "path": key, "type": "dir" if is_dir else "file",
            "kind": kind, "created_at": created, "size_bytes": size, "file_count": count,
            "old_version": old_version, "new_version": new_version,
            "has_state_backup": bool(meta.get("has_state_backup", False)),
            "in_use": bool(active_jobs), "active_jobs": active_jobs,
            "referenced_by": referenced_by,
        }

    def overview(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        active = self._active_jobs()
        refs = self._active_references()
        items = []
        for child in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_symlink():
                continue
            try:
                items.append(self.item(child.name, active, refs))
            except (OSError, ValueError):
                continue
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return {
            "root": str(self.root),
            "items": items,
            "summary": {
                "count": len(items),
                "total_bytes": sum(int(x.get("size_bytes") or 0) for x in items),
                "deletable_count": sum(1 for x in items if not x.get("in_use")),
            },
        }

    def delete(self, name: str) -> dict[str, Any]:
        item = self.item(name, self._active_jobs(), self._active_references())
        if item["in_use"]:
            raise ValueError("该备份正在被升级任务使用，完成或结束后再删除")
        path = self._safe(name)
        size = int(item.get("size_bytes") or 0)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return {"ok": True, "name": name, "size_bytes": size,
                "file_count": item.get("file_count") or 0}
