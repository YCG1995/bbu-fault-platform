"""启动前应用 Windows 待激活的工作目录切换/覆盖恢复。

Linux 由 root/systemd 助手完成同样动作；Windows 手动运行没有特权助手，
因此在打开数据库和启动 App 之前完成文件切换，失败时恢复旧目录。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .util import iso_now

PENDING_NAME = "pending_maintenance.json"


def _read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _config_path(root: Path) -> Path:
    return root / "config.json"


def _status_path(root: Path, job_id: str) -> Path:
    return root / "updates" / job_id / "status.json"


def _mark(root: Path, job_id: str, phase: str, message: str,
          progress: int = 100, **extra: Any) -> None:
    path = _status_path(root, job_id)
    data = _read(path, {}) or {}
    data.update({"phase": phase, "message": message, "progress": progress,
                 "updated_at": iso_now()})
    data.update(extra)
    if path.parent.is_dir():
        _write(path, data)


def _verify_stage(manifest_path: Path, stage: Path) -> dict[str, Any]:
    from bbu.migration import verify_workspace
    return verify_workspace(stage, manifest_path)


def _apply_workspace(root: Path, pending: dict[str, Any]) -> None:
    job_id = str(pending["job_id"])
    stage = Path(str(pending["stage_dir"])).resolve()
    manifest = Path(str(pending["manifest_path"])).resolve()
    target = Path(str(pending["target_root"])).resolve()
    _verify_stage(manifest, stage)
    from .config import Config
    current_work = Config(root).work_dir.resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("目标工作目录不为空")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.staging-{job_id}"
    if temp.exists():
        shutil.rmtree(temp)
    shutil.copytree(stage, temp, symlinks=False)
    if target.exists():
        target.rmdir()
    os.replace(temp, target)
    (target / "updates").mkdir(exist_ok=True)
    (target / "backups").mkdir(exist_ok=True)

    config_path = _config_path(root)
    original = _read(config_path, {}) or {}
    backup = config_path.with_name(f"config.json.before-{job_id}")
    if config_path.is_file():
        shutil.copy2(config_path, backup)
    config = dict(original)
    config.update({
        "work_dir": target.resolve().as_posix(),
        "data_dir": (target / "data").resolve().as_posix(),
        "skills_dir": (target / "skills").resolve().as_posix(),
        "cases_dir": (target / "cases").resolve().as_posix(),
    })
    _write(config_path, config)
    # 把任务状态复制到目标工作区，重启后管理页仍能看到成功记录。
    status = _read(_status_path(current_work, job_id), {}) or {}
    status.update({"phase": "succeeded", "message": "工作目录切换成功",
                   "progress": 100, "requires_restart": False, "error": ""})
    source_status = _status_path(current_work, job_id)
    if source_status.parent.is_dir():
        _write(source_status, status)
    target_status = _status_path(target, job_id)
    target_status.parent.mkdir(parents=True, exist_ok=True)
    _write(target_status, status)
    (root / PENDING_NAME).unlink(missing_ok=True)


def _apply_import(root: Path, pending: dict[str, Any]) -> None:
    job_id = str(pending["job_id"])
    stage = Path(str(pending["stage_dir"])).resolve()
    manifest = Path(str(pending["manifest_path"])).resolve()
    _verify_stage(manifest, stage)
    from .config import Config
    config = Config(root)
    data = config.data_dir.resolve()
    work = config.work_dir.resolve()
    backup_root = work / "backups" / f"import-{job_id}"
    backup_root.mkdir(parents=True, exist_ok=False)
    moved_old: list[str] = []
    moved_new: list[str] = []
    try:
        for name in ("data", "skills", "cases"):
            current = work / name
            if current.exists():
                os.replace(current, backup_root / name)
                moved_old.append(name)
        for name in ("data", "skills", "cases"):
            source = stage / name
            if not source.is_dir():
                raise ValueError(f"迁移工作区缺少 {name}")
            os.replace(source, work / name)
            moved_new.append(name)
        (root / PENDING_NAME).unlink(missing_ok=True)
        _mark(work, job_id, "succeeded", "迁移数据恢复成功，旧数据已备份", 100,
              backup_dir=str(backup_root), requires_restart=False, error="")
    except Exception as exc:
        # 恢复旧目录，避免启动到半套数据。
        for name in moved_new:
            current = work / name
            if current.exists():
                failed = backup_root / f"failed-new-{name}"
                if failed.exists():
                    shutil.rmtree(failed)
                os.replace(current, failed)
        for name in moved_old:
            old = backup_root / name
            if old.exists():
                os.replace(old, work / name)
        raise exc


def apply_pending(root: Path) -> dict[str, Any] | None:
    root = Path(root).resolve()
    path = root / PENDING_NAME
    data = _read(path, None)
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    job_id = str(data.get("job_id") or "")
    try:
        if action == "workspace":
            _apply_workspace(root, data)
        elif action == "import":
            _apply_import(root, data)
        else:
            raise ValueError("未知的待激活维护动作")
    except Exception as exc:
        try:
            from .config import Config
            source_root = Config(root).work_dir
            _mark(source_root, job_id, "failed", "重启激活失败，已继续使用原数据",
                  100, error=f"{type(exc).__name__}: {exc}", requires_restart=False)
        except Exception:
            pass
        path.unlink(missing_ok=True)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "action": action, "job_id": job_id}
