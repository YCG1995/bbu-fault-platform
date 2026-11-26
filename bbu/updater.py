"""网页离线升级：预检、状态持久化与 root/systemd 一次性助手调用。

安全边界：
- Web 进程只写入 STATE_DIR/updates/，不执行 sudo 之外的任意命令；
- 默认通过固定 unit `bbu-upgrade-helper@<job>.service` 调用 root 助手；
- 助手只读取 job 目录中的 request.json，路径与脚本由 root 配置固定。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .filelock import exclusive_lock
from .util import iso_now, new_id, sha256_file, within_directory

JOB_ID_RE = re.compile(r"^U[A-Za-z0-9_-]{6,40}$")
TERMINAL_PHASES = {"succeeded", "failed", "rolled_back"}
EXECUTION_PHASES = {"queued", "backup", "migrating", "restarting", "health_check", "rolling_back"}
ALL_PHASES = EXECUTION_PHASES | {"uploaded", "verifying", "ready"} | TERMINAL_PHASES
REQUIRED_FILES = {
    "run.py", "bbu/__init__.py", "web/index.html",
    "vendor/yaml/__init__.py", "deploy/upgrade_offline.sh",
    "deploy/rollback_offline.sh",
}
MAX_MEMBERS = 30000
MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024


def supported(app=None) -> bool:
    """Windows 默认不支持 root/systemd 网页升级；配置自定义助手命令后可启用。"""
    if os.name != "nt":
        return True
    return bool(app is not None and str(app.config.get("upgrade_helper_cmd") or "").strip())


class UpdateError(Exception):
    """可安全返回给管理员的预检/状态错误。"""


class UpdateConflict(UpdateError):
    """当前环境不满足执行条件（409）。"""


def _updates_root(app) -> Path:
    root = app.config.data_dir.parent / "updates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_dir(app, job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id or ""):
        raise UpdateError("升级任务 ID 非法")
    path = _updates_root(app) / job_id
    if not path.is_dir() or not within_directory(_updates_root(app), path):
        raise UpdateError("升级任务不存在")
    return path


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


@contextmanager
def _lock(app):
    """短事务锁，避免并发上传/提交产生两个执行中的升级。

    Windows 没有 fcntl，统一走 bbu.filelock 的 msvcrt 实现。
    """
    with exclusive_lock(_updates_root(app) / ".web.lock"):
        yield


def status_of(app, job_id: str) -> dict[str, Any]:
    job = _job_dir(app, job_id)
    status = _read_json(job / "status.json", None)
    if not status:
        raise UpdateError("升级任务状态不存在")
    status.setdefault("id", job_id)
    status["log_tail"] = _tail(job / "helper.log", 1800)
    return status


def _tail(path: Path, limit: int) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
        return data[-limit:]
    except OSError:
        return ""


def _phase(status: dict[str, Any], phase: str, message: str, progress: int | None = None,
           **extra: Any) -> dict[str, Any]:
    status.update({"phase": phase, "message": message, "updated_at": iso_now()})
    if progress is not None:
        status["progress"] = max(0, min(100, int(progress)))
    status.update(extra)
    return status


def _save_status(app, job_id: str, status: dict[str, Any]) -> dict[str, Any]:
    _write_json(_job_dir(app, job_id) / "status.json", status)
    return status


def current_version() -> str:
    return __version__


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value or "")
    if not parts:
        raise UpdateError("版本号非法")
    return tuple(int(p) for p in parts[:4])


def version_newer(target: str, current: str) -> bool:
    a, b = _version_tuple(target), _version_tuple(current)
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


def _safe_member_name(name: str) -> tuple[str, ...]:
    if name.startswith("/") or "\\" in name:
        raise UpdateError("压缩包包含非法绝对路径或反斜杠")
    parts = tuple(p for p in PurePosixPath(name).parts if p not in ("", "."))
    if not parts or any(p == ".." for p in parts):
        raise UpdateError("压缩包包含路径穿越条目")
    return parts


def inspect_archive(path: Path, release_dir: Path, current: str,
                    expected_sha256: str = "", allow_same: bool = False) -> dict[str, Any]:
    """校验包结构/哈希/版本并安全解压；返回目标版本、SHA、说明。"""
    if not path.is_file():
        raise UpdateError("升级包不存在")
    size = path.stat().st_size
    if size <= 0:
        raise UpdateError("升级包为空")
    actual = sha256_file(path)
    expected = (expected_sha256 or "").strip().lower()
    if expected and expected != actual:
        raise UpdateError("SHA256 不匹配，升级包可能被篡改")
    top_levels: set[str] = set()
    found: set[str] = set()
    total = 0
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS:
            raise UpdateError(f"压缩包条目过多（>{MAX_MEMBERS}）")
        for member in members:
            parts = _safe_member_name(member.name)
            top_levels.add(parts[0])
            if member.issym() or member.islnk():
                raise UpdateError("压缩包包含符号链接或硬链接，已拒绝")
            if not (member.isfile() or member.isdir()):
                raise UpdateError("压缩包包含设备/特殊文件，已拒绝")
            total += max(0, member.size)
            if total > MAX_UNCOMPRESSED:
                raise UpdateError("压缩包解压后体积超过上限")
            rel = "/".join(parts[1:])
            if rel in REQUIRED_FILES:
                found.add(rel)
        if len(top_levels) != 1:
            raise UpdateError("升级包必须只有一个顶层目录")
        missing = REQUIRED_FILES - found
        if missing:
            raise UpdateError("升级包缺少必需文件：" + "、".join(sorted(missing)))
        release_dir.mkdir(parents=True, exist_ok=True)
        tar.extractall(release_dir, filter="data")
    root = release_dir / next(iter(top_levels))
    if not root.is_dir():
        raise UpdateError("升级包顶层目录不存在")
    version_file = root / "bbu" / "__init__.py"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', version_file.read_text(encoding="utf-8"))
    if not match:
        raise UpdateError("无法从 bbu/__init__.py 读取版本")
    target = match.group(1)
    _version_tuple(target)
    if not version_newer(target, current) and not (allow_same and target == current):
        raise UpdateError(f"只允许升级到更高版本：当前 {current}，目标 {target}")
    notes = ""
    for note in sorted((root / "docs").glob(f"版本说明-{target}.md")):
        notes = note.read_text(encoding="utf-8", errors="replace")[:6000]
        break
    return {"target_version": target, "sha256": actual, "size": size,
            "source_dir": str(root), "release_notes": notes,
            "entries": len(members), "uncompressed": total}


def create_job(app, uploaded_path: Path, filename: str, expected_sha256: str = "",
               allow_same: bool = False) -> dict[str, Any]:
    if not supported(app):
        raise UpdateError("Windows 版默认不支持网页自动升级；请停止服务后覆盖程序目录并保留 data/config.json")
    suffix = Path(filename or "").name.lower()
    if not suffix.endswith(".tar.gz"):
        raise UpdateError("仅支持 .tar.gz 离线升级包")
    max_mb = int(app.config.get("max_update_mb", 512))
    if uploaded_path.stat().st_size > max_mb * 1024 * 1024:
        raise UpdateError(f"升级包超过上限（{max_mb} MB）")
    with _lock(app):
        job_id = new_id("U")
        job = _updates_root(app) / job_id
        job.mkdir(parents=True, exist_ok=False)
        package = job / "package.tar.gz"
        os.replace(uploaded_path, package)
        status = {
            "id": job_id, "phase": "uploaded", "message": "升级包已上传，等待预检",
            "progress": 5, "current_version": current_version(), "target_version": "",
            "package_sha256": "", "size": package.stat().st_size, "backup_dir": "",
            "error": "", "created_at": iso_now(), "updated_at": iso_now(),
            "release_notes": "", "action": "upgrade", "source_dir": "",
        }
        _write_json(job / "status.json", status)
        try:
            result = inspect_archive(package, job / "release", current_version(),
                                     expected_sha256, allow_same)
            status = _phase(status, "ready", "预检通过，可执行升级", 100,
                            target_version=result["target_version"],
                            package_sha256=result["sha256"],
                            source_dir=result["source_dir"],
                            release_notes=result["release_notes"],
                            size=result["size"])
            _write_json(job / "job.json", {"filename": Path(filename).name, **result})
        except Exception as exc:
            status = _phase(status, "failed", "预检失败", 100, error=str(exc))
            _write_json(job / "status.json", status)
            raise
        _save_status(app, job_id, status)
        return status


def verify_job(app, job_id: str, expected_sha256: str = "", allow_same: bool = False) -> dict[str, Any]:
    job = _job_dir(app, job_id)
    status = status_of(app, job_id)
    if status["phase"] in EXECUTION_PHASES:
        raise UpdateError("升级正在执行，不能重复预检")
    package = job / "package.tar.gz"
    shutil.rmtree(job / "release", ignore_errors=True)
    status = _phase(status, "verifying", "正在重新校验升级包", 20)
    _save_status(app, job_id, status)
    # 不允许重新预检时用另一个包替换已登记 SHA 的文件。
    expected = (expected_sha256 or "").strip() or str(status.get("package_sha256") or "")
    try:
        result = inspect_archive(package, job / "release", current_version(),
                                 expected, allow_same)
    except Exception as exc:
        status = _phase(status, "failed", "预检失败", 100, error=str(exc))
        _save_status(app, job_id, status)
        raise
    status = _phase(status, "ready", "预检通过，可执行升级", 100,
                    target_version=result["target_version"],
                    package_sha256=result["sha256"], source_dir=result["source_dir"],
                    release_notes=result["release_notes"], size=result["size"], error="")
    _write_json(job / "job.json", {"filename": "package.tar.gz", **result})
    return _save_status(app, job_id, status)


def active_execution(app) -> dict[str, Any] | None:
    for item in list_jobs(app, include_terminal=False):
        if item.get("action") in ("export", "workspace", "import"):
            continue
        if item.get("phase") in EXECUTION_PHASES:
            return item
    return None


def list_jobs(app, include_terminal: bool = True) -> list[dict[str, Any]]:
    items = []
    for job in sorted(_updates_root(app).iterdir(), key=lambda p: p.name, reverse=True):
        if not job.is_dir() or not JOB_ID_RE.match(job.name):
            continue
        status = _read_json(job / "status.json", None)
        if not status:
            continue
        if status.get("action") in ("export", "workspace", "import"):
            continue
        status.setdefault("id", job.name)
        if include_terminal or status.get("phase") not in TERMINAL_PHASES:
            status.pop("log_tail", None)
            items.append(status)
    return items[:40]


def running_tasks(app) -> int:
    return int(app.db.one(
        "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','running')")["n"])


def disk_free(app) -> int:
    return shutil.disk_usage(app.config.data_dir).free


def assert_can_execute(app, package_size: int = 0) -> None:
    active = active_execution(app)
    if active:
        raise UpdateConflict(f"已有升级任务正在执行：{active['id']}")
    running = running_tasks(app)
    if running:
        raise UpdateConflict(f"当前有 {running} 个排队/运行中的诊断任务，请等待完成后再升级")
    need = max(512 * 1024 * 1024, int(package_size) * 4)
    if disk_free(app) < need:
        raise UpdateConflict(f"磁盘空间不足：至少需要 {need // (1024*1024)} MB 可用")


def _helper_command(app, job_id: str) -> list[str]:
    custom = str(app.config.get("upgrade_helper_cmd") or "").strip()
    if custom:
        return shlex.split(custom) + [job_id]
    trigger = str(app.config.get("upgrade_helper_trigger") or "/usr/local/bin/bbu-upgrade-helper-trigger")
    return ["sudo", "-n", trigger, job_id]


def _request_base(app, job_id: str, action: str) -> dict[str, Any]:
    return {
        "job_id": job_id, "action": action, "update_dir": str(_job_dir(app, job_id)),
        "prefix": str(app.config.get("_root") or ""), "state_dir": str(app.config.data_dir.parent),
        "service_name": str(app.config.get("upgrade_service_name", "bbu-fault-platform")),
        "service_user": str(app.config.get("upgrade_service_user", "bbu")),
        "health_url": f"http://127.0.0.1:{int(app.config.get('port', 8080))}/api/health",
        "created_at": iso_now(),
    }


def _launch_failure_detail(detail: str) -> str:
    """Attach a NoNewPrivileges hint for both English and Chinese sudo errors."""
    lowered = detail.lower()
    compact = "".join(detail.split()).lower()
    if ("effective uid is not 0" in lowered
            or "no new privileges" in lowered
            or "有效用户id不是0" in compact):
        detail += ("；请检查主服务 systemd 单元 NoNewPrivileges 是否为 true，"
                   "并在服务器 root 控制台运行 deploy/fix_upgrade_helper.sh --diagnose")
    return detail


def _launch(app, job_id: str, request: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    _write_json(_job_dir(app, job_id) / "request.json", request)
    status = _phase(status, "queued", "已提交给 root 助手，等待执行", 10, error="")
    _save_status(app, job_id, status)
    try:
        result = subprocess.run(_helper_command(app, job_id), capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            detail = _launch_failure_detail(
                (result.stderr or result.stdout or "未知错误").strip()[-500:])
            status = _phase(status, "failed", "无法启动 root 升级助手", 100,
                            error=f"助手启动失败：{detail}")
            _save_status(app, job_id, status)
            raise UpdateError(f"无法启动 root 升级助手：{detail}")
    except subprocess.TimeoutExpired as exc:
        status = _phase(status, "failed", "root 升级助手启动超时", 100, error="助手启动超时")
        _save_status(app, job_id, status)
        raise UpdateError("root 升级助手启动超时") from exc
    return status


def apply_job(app, job_id: str) -> dict[str, Any]:
    if not supported(app):
        raise UpdateError("Windows 版默认不支持网页自动升级")
    with _lock(app):
        job = _job_dir(app, job_id)
        status = status_of(app, job_id)
        if status.get("phase") != "ready":
            raise UpdateError("升级包尚未通过预检，不能执行")
        request = _request_base(app, job_id, "upgrade")
        request.update({
            "source_dir": status.get("source_dir", ""),
            "package_sha256": status.get("package_sha256", ""),
            "target_version": status.get("target_version", ""),
            "backup_parent": str(app.config.data_dir.parent / "backups"),
        })
        if not within_directory(_job_dir(app, job_id) / "release", Path(request["source_dir"])):
            raise UpdateError("升级包解压目录越界")
        assert_can_execute(app, int(status.get("size") or 0))
        status = _phase(status, "queued", "已通过预检，准备备份并升级", 10)
        return _launch(app, job_id, request, status)


def _valid_backup(path: Path, state_dir: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir() or not within_directory((state_dir / "backups").resolve(), resolved):
        raise UpdateError("回滚快照必须位于 STATE_DIR/backups 下")
    if not (resolved / "backup.json").is_file():
        raise UpdateError("回滚快照缺少 backup.json")
    return resolved


def rollback_job(app, job_id: str = "", backup_dir: str = "") -> dict[str, Any]:
    if not supported(app):
        raise UpdateError("Windows 版默认不支持网页自动回滚")
    with _lock(app):
        state_dir = app.config.data_dir.parent
        if backup_dir:
            backup = _valid_backup(Path(backup_dir), state_dir)
        elif job_id:
            source = status_of(app, job_id)
            if not source.get("backup_dir"):
                raise UpdateError("该升级任务没有可用的回滚快照")
            backup = _valid_backup(Path(source["backup_dir"]), state_dir)
        else:
            raise UpdateError("请指定升级任务或回滚快照")
        meta = _read_json(backup / "backup.json", {}) or {}
        assert_can_execute(app)
        new_id_ = new_id("U")
        job = _updates_root(app) / new_id_
        job.mkdir(parents=True, exist_ok=False)
        status = {
            "id": new_id_, "phase": "queued", "message": "已提交回滚，等待执行",
            "progress": 10, "current_version": current_version(),
            "target_version": str(meta.get("old_version") or ""),
            "package_sha256": "", "size": 0, "backup_dir": str(backup), "error": "",
            "created_at": iso_now(), "updated_at": iso_now(), "release_notes": "",
            "action": "rollback", "source_dir": "",
        }
        request = _request_base(app, new_id_, "rollback")
        request.update({"backup_dir": str(backup),
                        "target_version": status["target_version"]})
        return _launch(app, new_id_, request, status)


def status_public(app, job_id: str) -> dict[str, Any]:
    return status_of(app, job_id)


def history_public(app) -> list[dict[str, Any]]:
    return list_jobs(app)


def cleanup_job(app, job_id: str) -> None:
    """仅删除失败/已回滚任务的包与解压目录，保留状态供审计。"""
    job = _job_dir(app, job_id)
    status = status_of(app, job_id)
    if status.get("phase") not in TERMINAL_PHASES:
        raise UpdateError("只能清理已结束的升级任务")
    shutil.rmtree(job / "release", ignore_errors=True)
    (job / "package.tar.gz").unlink(missing_ok=True)
