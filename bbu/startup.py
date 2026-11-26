"""启动前应用待激活的工作目录切换、覆盖恢复与 Windows 程序更新。

Linux 升级由 root/systemd 助手完成；Windows 手动运行没有特权助手，
因此在打开数据库和启动 App 之前完成文件切换/代码替换，失败时恢复旧目录。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .util import iso_now, within_directory

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


UPDATE_EXCLUDES = {
    "data", "skills", "cases", "updates", "backups", "config.json",
    "pending_update.json", "pending_maintenance.json", ".git", "__pycache__", ".pytest_cache",
    "venv", ".venv", ".update-import", ".update-stage",
}


def _mark_job(job: Path, phase: str, message: str, progress: int = 100,
              **extra: Any) -> None:
    path = job / "status.json"
    if not path.parent.is_dir():
        return
    data = _read(path, {}) or {}
    data.update({"phase": phase, "message": message, "progress": progress,
                 "updated_at": iso_now()})
    data.update(extra)
    _write(path, data)


def _runtime_names(root: Path, config) -> set[str]:
    names = set(UPDATE_EXCLUDES)
    for value in (config.data_dir, config.skills_dir, config.cases_dir,
                  config.data_dir.parent / "updates", config.data_dir.parent / "backups"):
        try:
            path = Path(value).resolve()
        except OSError:
            continue
        if path.parent == root:
            names.add(path.name)
    return names


def _copy_tree_entry(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def _remove_code_entries(root: Path, names: set[str]) -> None:
    for item in root.iterdir():
        if item.name in names or item.name.startswith(".update-stage"):
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink(missing_ok=True)


def _restore_code(root: Path, backup_code: Path, names: set[str]) -> None:
    _remove_code_entries(root, names)
    for item in backup_code.iterdir():
        _copy_tree_entry(item, root / item.name)


def _apply_update(root: Path, pending: dict[str, Any]) -> None:
    from .config import Config

    job = Path(str(pending.get("job_dir") or "")).resolve()
    source = Path(str(pending.get("source_dir") or "")).resolve()
    job_id = str(pending.get("job_id") or "")
    if not job_id or not job.is_dir() or not (job / "status.json").is_file():
        raise ValueError("更新任务目录不存在")
    if not source.is_dir() or not within_directory(job.resolve() / "release", source):
        raise ValueError("更新包解压目录越界或不存在")
    if not (source / "run.py").is_file() or not (source / "bbu" / "__init__.py").is_file():
        raise ValueError("更新包缺少程序文件")

    config = Config(root)
    names = _runtime_names(root, config)
    backup_root = config.data_dir.parent / "backups" / f"update-{job_id}"
    backup_code = backup_root / "code"
    if backup_root.exists():
        raise ValueError("更新备份目录已存在，请检查上次维护结果")
    backup_root.mkdir(parents=True, exist_ok=False)
    backup_code.mkdir(parents=True, exist_ok=False)
    try:
        for item in root.iterdir():
            if item.name in names or item.name.startswith(".update-stage"):
                continue
            _copy_tree_entry(item, backup_code / item.name)
        config_file = root / "config.json"
        if config_file.is_file():
            shutil.copy2(config_file, backup_root / "config.json")
        _write(backup_root / "backup.json", {
            "job_id": job_id,
            "target_version": str(pending.get("target_version") or ""),
            "old_version": str(_read(job / "status.json", {}).get("current_version") or ""),
            "created_at": iso_now(),
            "prefix": str(root),
            "type": "windows-restart-update",
        })
        _remove_code_entries(root, names)
        for item in source.iterdir():
            if item.name in names or item.name.startswith(".update-stage"):
                continue
            _copy_tree_entry(item, root / item.name)
    except Exception:
        try:
            _restore_code(root, backup_code, names)
        except Exception:
            pass
        raise

    _mark_job(job, "succeeded",
              f"代码已替换为 v{pending.get('target_version')}，正在启动新进程", 100,
              backup_dir=str(backup_root), requires_restart=False, error="")
    (root / PENDING_NAME).unlink(missing_ok=True)


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
        elif action == "update":
            _apply_update(root, data)
        else:
            raise ValueError("未知的待激活维护动作")
    except Exception as exc:
        try:
            job_dir = Path(str(data.get("job_dir") or "")) if action == "update" else None
            if job_dir:
                _mark_job(job_dir, "failed", "重启激活失败，已继续使用原程序",
                          100, error=f"{type(exc).__name__}: {exc}", requires_restart=False)
            else:
                from .config import Config
                source_root = Config(root).work_dir
                _mark(source_root, job_id, "failed", "重启激活失败，已继续使用原数据",
                      100, error=f"{type(exc).__name__}: {exc}", requires_restart=False)
        except Exception:
            pass
        path.unlink(missing_ok=True)
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "action": action, "job_id": job_id}
