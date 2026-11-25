"""精简迁移：工作区构建、路径重写、清单校验与加密包收发。

迁移契约：只导出业务元数据和派生产物，不包含原始上传、案例输入、解压目录和临时缓存。
数据库中的绝对路径在包内使用 ``__BBU_WORKDIR__`` 占位，导入时统一重写到目标工作目录。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from . import __version__
from .migration_crypto import MigrationCryptoError, decrypt_file, encrypt_file, file_sha256
from .util import iso_now, new_id, within_directory

WORKDIR_TOKEN = "__BBU_WORKDIR__"
FORMAT_VERSION = 1
LAYOUT_VERSION = 1
MIN_SCHEMA = 8
JOB_RE = re.compile(r"^U[A-Za-z0-9_-]{6,40}$")
MAX_MEMBERS = 100000
EXCLUDED_PATTERNS = (
    "uploads/", "draft_uploads/", "tmp/", "logs/", "eval_runs/",
    "real_logs/", "real_cases_seed/", "unpacked/", "extracted/", ".cache/",
)
EXCLUDED_FILE_NAMES = {"server.log", "server.pid", "初始管理员密码.txt", "package.tar.gz"}
GENERATED_NAMES = {"__pycache__", ".pytest_cache", ".git", ".cache", ".tmp",
                   "unpacked", "extracted", "logs", "node_modules"}
EXPORT_CATEGORIES = [
    "用户、角色与密码哈希",
    "模型配置与加密密钥",
    "任务元数据、结论、报告与证据",
    "技能源码、配置和版本记录",
    "案例与回归案例元数据",
    "历史评测结果",
    "经验、复核与审计记录",
]
EXCLUDED_CATEGORIES = [
    "原始上传日志包与草稿上传",
    "真实案例与回归案例原始输入",
    "任务/工坊解压目录与缓存",
    "工坊原始日志与抽取结果",
    "临时评测运行目录",
    "登录会话、认证挑战与用户邀请码",
    "初始管理员密码文件",
    "旧升级包、更新临时目录和历史备份",
]
TERMINAL_PHASES = {"succeeded", "failed", "rolled_back"}
RUNNING_PHASES = {"queued", "preparing", "packaging", "ready", "switching", "restoring"}


class MigrationError(Exception):
    """可安全返回给管理员的迁移错误。"""


class MigrationConflict(MigrationError):
    """当前状态不允许执行迁移。"""


def _json_read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def job_root(app) -> Path:
    root = Path(app.config.data_dir).parent / "updates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_dir(app, job_id: str) -> Path:
    if not JOB_RE.match(job_id or ""):
        raise MigrationError("迁移任务 ID 非法")
    root = job_root(app).resolve()
    path = (root / job_id).resolve()
    if not path.is_dir() or not within_directory(root, path):
        raise MigrationError("迁移任务不存在")
    return path


def new_job_id() -> str:
    return new_id("U")


def _status(app, job_id: str) -> dict[str, Any]:
    path = _job_dir(app, job_id) / "status.json"
    data = _json_read(path, None)
    if not isinstance(data, dict):
        raise MigrationError("迁移任务状态不存在")
    data.setdefault("id", job_id)
    return data


def _save_status(app, job_id: str, **changes: Any) -> dict[str, Any]:
    path = _job_dir(app, job_id) / "status.json"
    data = _json_read(path, {}) or {}
    data.update(changes)
    data["updated_at"] = iso_now()
    data.setdefault("created_at", iso_now())
    data.setdefault("id", job_id)
    _json_write(path, data)
    return data


def public_status(app, job_id: str) -> dict[str, Any]:
    data = _status(app, job_id)
    # 口令、临时路径等内部字段绝不返回。
    for key in ("passphrase", "password", "stage_dir", "helper_request"):
        data.pop(key, None)
    data["download_url"] = (
        f"/api/system/migrations/{job_id}/download"
        if data.get("phase") == "ready" and data.get("kind") == "export" else ""
    )
    return data


def list_jobs(app, limit: int | None = 30) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    root = job_root(app)
    for job in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not job.is_dir() or not JOB_RE.match(job.name):
            continue
        data = _json_read(job / "status.json", None)
        if not isinstance(data, dict) or data.get("action") not in ("export", "workspace", "import"):
            continue
        data.setdefault("id", job.name)
        data["download_url"] = (
            f"/api/system/migrations/{job.name}/download"
            if data.get("phase") == "ready" and data.get("kind") == "export" else ""
        )
        items.append(data)
    return items if limit is None else items[:limit]


def find_job_file(app, job_id: str, name: str = "export.bbumig") -> Path:
    job = _job_dir(app, job_id)
    path = (job / name).resolve()
    if not path.is_file() or not within_directory(job, path):
        raise MigrationError("迁移文件不存在")
    return path


def prune_jobs(app, keep: int = 30) -> None:
    """只清理已终止且非导出就绪状态的旧任务，保留下载包等待用户主动删除。"""
    candidates = []
    for item in list_jobs(app, limit=None):
        if item.get("phase") in TERMINAL_PHASES and item.get("kind") != "export":
            candidates.append(item)
    for item in candidates[keep:]:
        try:
            shutil.rmtree(_job_dir(app, item["id"]), ignore_errors=True)
        except Exception:
            pass


def delete_job(app, job_id: str) -> None:
    job = _job_dir(app, job_id)
    status = _status(app, job_id)
    # 导出 ready 只是等待下载，允许删除；其它 ready 仍处于交接窗口，不允许删除。
    if status.get("phase") in ({"queued", "preparing", "packaging", "switching",
                                "restoring", "waiting_restart"}):
        raise MigrationConflict("迁移任务仍在执行，不能删除")
    shutil.rmtree(job)


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_copy_file(src: Path, dst: Path, total: dict[str, int], limit: int) -> None:
    if src.is_symlink():
        raise MigrationError(f"迁移源包含符号链接，拒绝打包：{src}")
    size = src.stat().st_size
    total["bytes"] += size
    if total["bytes"] > limit:
        raise MigrationError("工作区超过迁移大小上限，请缩小数据后重试")
    total["files"] += 1
    if total["files"] > MAX_MEMBERS:
        raise MigrationError("迁移文件数量超过上限")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if src.name == "master.key":
        os.chmod(dst, 0o600)


def _copy_tree(src: Path, dst: Path, total: dict[str, int], limit: int,
               exclude_names: set[str] | None = None,
               only_exts: set[str] | None = None,
               direct: bool = False) -> None:
    if not src.is_dir():
        return
    exclude = GENERATED_NAMES | (exclude_names or set())
    if direct:
        for child in src.iterdir():
            if child.is_file() and not child.is_symlink():
                if child.name in exclude:
                    continue
                if only_exts is not None and child.suffix.lower() not in only_exts:
                    continue
                _safe_copy_file(child, dst / child.name, total, limit)
        return
    for entry in src.iterdir():
        name = entry.name
        if name in exclude or name.endswith(".pyc"):
            continue
        target = dst / name
        if entry.is_symlink():
            continue
        if entry.is_dir():
            _copy_tree(entry, target, total, limit, exclude_names, only_exts, False)
        elif entry.is_file():
            _safe_copy_file(entry, target, total, limit)


def _copy_tasks(data_dir: Path, dst: Path, total: dict[str, int], limit: int) -> None:
    source = data_dir / "tasks"
    if not source.is_dir():
        return
    allowed_top = {".json", ".md", ".html"}
    for task in source.iterdir():
        if not task.is_dir() or task.is_symlink():
            continue
        out_task = dst / task.name
        for file in task.iterdir():
            if file.is_file() and not file.is_symlink() and file.suffix.lower() in allowed_top \
                    and file.name not in {"server.log", "runner.out", "runner.err"}:
                _safe_copy_file(file, out_task / file.name, total, limit)
        skills = task / "skills"
        if skills.is_dir():
            for skill in skills.iterdir():
                if not skill.is_dir() or skill.is_symlink():
                    continue
                out_skill = out_task / "skills" / skill.name
                for file in skill.iterdir():
                    if file.is_file() and not file.is_symlink() and file.suffix.lower() in allowed_top \
                            and not file.name.startswith(("runner.", "server.")):
                        _safe_copy_file(file, out_skill / file.name, total, limit)


def _copy_workshop(data_dir: Path, dst: Path, total: dict[str, int], limit: int) -> None:
    source = data_dir / "workshop"
    if not source.is_dir():
        return
    for draft in source.iterdir():
        if not draft.is_dir() or draft.is_symlink() or draft.name.startswith("."):
            continue
        out = dst / draft.name
        for file in draft.iterdir():
            if file.is_file() and not file.is_symlink() and \
                    file.name in {"profile.yaml", "profile.yml", "build-result.json",
                                  "rule_candidates.json", "analysis.json", "summary.json"}:
                _safe_copy_file(file, out / file.name, total, limit)
        build = draft / "build"
        if build.is_dir():
            _copy_tree(build, out / "build", total, limit)


def _copy_real_cases(data_dir: Path, dst: Path, total: dict[str, int], limit: int) -> None:
    source = data_dir / "real_cases"
    if not source.is_dir():
        return
    for case in source.iterdir():
        if not case.is_dir() or case.is_symlink():
            continue
        meta = case / "case.json"
        if not meta.is_file() or meta.is_symlink():
            continue
        payload = _json_read(meta, None)
        if not isinstance(payload, dict):
            continue
        payload["input_paths"] = []
        payload["inputs_state"] = "excluded_by_migration"
        payload["raw_inputs_available"] = False
        destination = dst / case.name / "case.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        total["bytes"] += len(text.encode("utf-8"))
        total["files"] += 1
        if total["bytes"] > limit or total["files"] > MAX_MEMBERS:
            raise MigrationError("工作区超过迁移大小上限，请缩小数据后重试")
        destination.write_text(text, encoding="utf-8")


def _copy_evaluations(data_dir: Path, dst: Path, total: dict[str, int], limit: int) -> None:
    source = data_dir / "evaluations"
    if not source.is_dir():
        return
    allowed = {"summary.json", "report.md", "report.html", "result.json"}
    for evaluation in source.iterdir():
        if not evaluation.is_dir() or evaluation.is_symlink():
            continue
        for file in evaluation.iterdir():
            if file.is_file() and not file.is_symlink() and file.name in allowed:
                _safe_copy_file(file, dst / evaluation.name / file.name, total, limit)


def _copy_cases(cases_dir: Path, dst: Path, total: dict[str, int], limit: int) -> None:
    if not cases_dir.is_dir():
        return
    for file in cases_dir.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        if file.name in {"case.yaml", "case.json"}:
            _safe_copy_file(file, dst / file.relative_to(cases_dir), total, limit)


def snapshot_db(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise MigrationError("数据库文件不存在")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    dest_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(dest_conn)
        dest_conn.commit()
    finally:
        dest_conn.close()
        source_conn.close()
    os.chmod(destination, 0o600)


def _replace_paths(text: str, old_root: str, new_root: str) -> str:
    if not text or not old_root:
        return text
    text = text.replace(old_root, new_root)
    win_old = old_root.replace("/", "\\")
    if win_old != old_root:
        text = text.replace(win_old, new_root)
        text = text.replace(win_old.replace("\\", "\\\\"), new_root)
    return text


def _rewrite_text_tree(root: Path, old_root: str, new_root: str) -> None:
    """重写报告、任务 JSON、案例元数据和派生文本中的工作目录路径。"""
    if not old_root or old_root == new_root:
        return
    suffixes = {".json", ".md", ".html", ".htm", ".yaml", ".yml",
                ".txt", ".csv", ".js", ".css", ".py", ".xml", ".ini"}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in suffixes:
            continue
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        changed = _replace_paths(text, old_root, new_root)
        if changed != text:
            try:
                path.write_text(changed, encoding="utf-8")
            except OSError:
                continue


def _strip_descriptor(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_descriptor(v) for v in value]
    if isinstance(value, dict):
        result = {}
        for key, val in value.items():
            if key in {"stored_path", "path", "abs_path", "local_path"}:
                continue
            if key in {"sha256", "size", "name", "filename", "kind"}:
                result[key] = val
            else:
                result[key] = _strip_descriptor(val)
        result["migration_input_available"] = False
        result["migration_input_state"] = "excluded_by_migration"
        return result
    return value


def rewrite_database(db_path: Path, old_workdir: Path, target: str,
                     *, include_sessions: bool = False) -> dict[str, int]:
    """重写数据库内的绝对路径并标记缺失原始输入；target 为真实目录或占位 token。"""
    old = WORKDIR_TOKEN if str(old_workdir) == WORKDIR_TOKEN else old_workdir.resolve().as_posix()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        # 兼容旧快照：先补齐迁移依赖的加法字段，再做业务改写。
        for table in ("tasks", "real_cases", "cases"):
            if table in tables:
                cols = {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                if "inputs_state" not in cols:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN inputs_state TEXT NOT NULL DEFAULT \'available\'')
        # 先处理与原始输入有关的特殊字段，再从所有文本列清理旧工作目录前缀。
        if "real_cases" in tables:
            conn.execute("UPDATE real_cases SET input_paths_json='[]', inputs_state='excluded_by_migration'")
        if "cases" in tables:
            conn.execute("UPDATE cases SET inputs_state='excluded_by_migration'")
        if "tasks" in tables:
            rows = conn.execute("SELECT id, input_json FROM tasks").fetchall()
            for row in rows:
                try:
                    descriptors = json.loads(row["input_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    descriptors = []
                descriptors = _strip_descriptor(descriptors)
                state = "excluded_by_migration" if descriptors else "available"
                conn.execute("UPDATE tasks SET input_json=?, inputs_state=? WHERE id=?",
                             (json.dumps(descriptors, ensure_ascii=False), state, row["id"]))
        if "workshop_drafts" in tables:
            conn.execute("UPDATE workshop_drafts SET log_path=''")
        if "sessions" in tables and not include_sessions:
            conn.execute("DELETE FROM sessions")
        if "user_invites" in tables and not include_sessions:
            conn.execute("DELETE FROM user_invites")
        for table in tables:
            cols = [r for r in conn.execute(f'PRAGMA table_info("{table}")') if r["type"].upper() in
                    ("TEXT", "VARCHAR", "CHAR", "CLOB", "JSON")]
            if not cols:
                continue
            rows = conn.execute(f'SELECT rowid, {", ".join(c["name"] for c in cols)} FROM "{table}"').fetchall()
            for row in rows:
                changes = {}
                for col in cols:
                    value = row[col["name"]]
                    if isinstance(value, str) and value:
                        new = _replace_paths(value, old, target)
                        if new != value:
                            changes[col["name"]] = new
                if changes:
                    sql = f'UPDATE "{table}" SET ' + ", ".join(f'"{k}"=?' for k in changes) + " WHERE rowid=?"
                    conn.execute(sql, (*changes.values(), row[0]))
        if "settings" in tables:
            conn.execute("INSERT INTO settings(key,value) VALUES('schema_version','10') "
                         "ON CONFLICT(key) DO UPDATE SET value='10'")
            conn.execute("INSERT INTO settings(key,value) VALUES('migration_inputs_excluded','1') "
                         "ON CONFLICT(key) DO UPDATE SET value='1'")
            conn.execute("INSERT INTO settings(key,value) VALUES('migration_notice',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         ("原始上传、案例输入和解压数据未随迁移包导出；依赖原始输入的重跑、案例回流和评测已停用。",))
        conn.commit()
        counts = {}
        for table in ("users", "tasks", "task_skills", "skills", "skill_versions",
                      "workshop_drafts", "cases", "real_cases", "evaluations",
                      "plan_experiences", "audit_logs", "sessions"):
            if table in tables:
                counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        return counts
    finally:
        conn.close()


def build_workspace(source_config: Any, destination: Path, target: str,
                    *, include_sessions: bool = False, limit_bytes: int = 4096 * 1024 * 1024,
                    progress: Callable[[str, int], None] | None = None) -> dict[str, Any]:
    """构建可迁移工作区。target 可为真实根目录或 WORKDIR_TOKEN。"""
    source_config = source_config
    work = destination
    if work.exists():
        shutil.rmtree(work)
    (work / "data").mkdir(parents=True)
    (work / "skills").mkdir(parents=True)
    (work / "cases").mkdir(parents=True)
    total = {"bytes": 0, "files": 0}
    if progress:
        progress("正在生成数据库一致性快照", 10)
    snapshot_db(Path(source_config.data_dir) / "bbu.db", work / "data" / "bbu.db")
    if progress:
        progress("正在重写数据库路径", 20)
    counts = rewrite_database(work / "data" / "bbu.db", Path(source_config.work_dir),
                              target, include_sessions=include_sessions)
    master = Path(source_config.data_dir) / "master.key"
    if master.is_file():
        _safe_copy_file(master, work / "data" / "master.key", total, limit_bytes)
    if progress:
        progress("正在复制技能与派生结果", 25)
    _copy_tree(Path(source_config.skills_dir), work / "skills", total, limit_bytes)
    _copy_tree(Path(source_config.data_dir) / "skill_versions", work / "data" / "skill_versions",
               total, limit_bytes)
    _copy_tasks(Path(source_config.data_dir), work / "data" / "tasks", total, limit_bytes)
    _copy_workshop(Path(source_config.data_dir), work / "data" / "workshop", total, limit_bytes)
    _copy_real_cases(Path(source_config.data_dir), work / "data" / "real_cases", total, limit_bytes)
    _copy_evaluations(Path(source_config.data_dir), work / "data" / "evaluations", total, limit_bytes)
    _copy_cases(Path(source_config.cases_dir), work / "cases", total, limit_bytes)
    # 文件中的报告/案例元数据也要去掉旧绝对路径；数据库单独处理。
    _rewrite_text_tree(work, Path(source_config.work_dir).resolve().as_posix(),
                       str(target))
    if progress:
        progress("正在计算校验清单", 45)
    files = []
    for path in sorted(work.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files.append({"path": _rel(path, work), "sha256": file_sha256(path),
                          "size": path.stat().st_size})
    manifest = {
        "format_version": FORMAT_VERSION,
        "layout_version": LAYOUT_VERSION,
        "app_version": __version__,
        "schema_version": 10,
        "created_at": iso_now(),
        "workdir_token": WORKDIR_TOKEN,
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "db_counts": counts,
        "exported_categories": EXPORT_CATEGORIES,
        "excluded_categories": EXCLUDED_CATEGORIES,
        "inputs_state": "excluded_by_migration",
        "sessions_included": bool(include_sessions),
    }
    manifest_path = work.parent / "manifest.json"
    _json_write(manifest_path, manifest)
    if progress:
        progress("工作区构建完成", 55)
    return {"workspace": work, "manifest": manifest, "manifest_path": manifest_path}


def verify_workspace(workspace: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _json_read(manifest_path, None)
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise MigrationError("迁移清单格式非法")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise MigrationError("迁移清单缺少文件列表")
    listed = set()
    for item in files:
        rel = str(item.get("path") or "")
        if not rel or rel.startswith("/") or rel.startswith("\\") or ".." in PurePosixPath(rel).parts:
            raise MigrationError("迁移清单包含越界路径")
        path = workspace / PurePosixPath(rel)
        if not path.is_file() or path.is_symlink():
            raise MigrationError(f"迁移文件缺失：{rel}")
        size = path.stat().st_size
        if size != int(item.get("size") or -1):
            raise MigrationError(f"迁移文件大小不符：{rel}")
        if file_sha256(path) != str(item.get("sha256") or ""):
            raise MigrationError(f"迁移文件校验失败：{rel}")
        listed.add(rel)
    actual = {_rel(p, workspace) for p in workspace.rglob("*") if p.is_file()}
    if actual != listed:
        raise MigrationError("迁移工作区存在清单外文件")
    for rel in listed:
        parts = set(PurePosixPath(rel).parts)
        if "uploads" in parts or "draft_uploads" in parts or "unpacked" in parts \
                or "extracted" in parts or "eval_runs" in parts or "real_logs" in parts:
            raise MigrationError(f"迁移工作区包含应排除的原始/解压数据：{rel}")
    return manifest


def zip_workspace(job: Path, workspace: Path, manifest_path: Path) -> Path:
    zip_path = job / "payload.zip"
    if zip_path.exists():
        zip_path.unlink()
    manifest = _json_read(manifest_path, {}) or {}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.write(manifest_path, "manifest.json")
        for item in manifest.get("files", []):
            rel = str(item["path"])
            zf.write(workspace / PurePosixPath(rel), f"workspace/{rel}")
    os.chmod(zip_path, 0o600)
    return zip_path


def _safe_extract_inner(zip_path: Path, destination: Path, max_bytes: int) -> tuple[Path, dict[str, Any]]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    names: set[str] = set()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_MEMBERS:
                raise MigrationError("迁移包文件数量超出上限")
            for info in infos:
                name = info.filename
                if not name or name.startswith("/") or name.startswith("\\") or "\\" in name:
                    raise MigrationError("迁移包包含非法路径")
                parts = PurePosixPath(name).parts
                if ".." in parts or not parts:
                    raise MigrationError("迁移包包含路径穿越")
                if name in names:
                    raise MigrationError("迁移包包含重复文件")
                names.add(name)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise MigrationError("迁移包包含符号链接")
                if name.endswith("/"):
                    continue
                if not (name == "manifest.json" or name.startswith("workspace/")):
                    raise MigrationError("迁移包包含未知顶层文件")
                total += info.file_size
                if total > max_bytes:
                    raise MigrationError("迁移包解压后超过大小上限")
                target = destination / PurePosixPath(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                os.chmod(target, 0o600)
    except zipfile.BadZipFile as exc:
        raise MigrationError("迁移包不是有效的 ZIP 文件") from exc
    manifest_path = destination / "manifest.json"
    manifest = _json_read(manifest_path, None)
    if not isinstance(manifest, dict):
        raise MigrationError("迁移包缺少 manifest.json")
    workspace = destination / "workspace"
    verify_workspace(workspace, manifest_path)
    return workspace, manifest


def safe_extract(zip_path: Path, destination: Path, max_bytes: int) -> tuple[Path, dict[str, Any]]:
    """解压并校验；任何失败都清理半成品目录，避免残留空间。"""
    try:
        return _safe_extract_inner(zip_path, destination, max_bytes)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def import_package(job: Path, package: Path, passphrase: str, target_root: Path,
                   max_bytes: int, progress: Callable[[str, int], None] | None = None) -> dict[str, Any]:
    if progress:
        progress("正在解密迁移包", 10)
    zip_path = job / "payload.zip"
    try:
        decrypt_file(package, zip_path, passphrase,
                     progress=lambda done, total: progress(
                         f"正在解密迁移包（{int(done*100/max(1,total))}%）", 10 + int(done * 25 / max(1, total)))
                     if progress else None)
    except MigrationCryptoError as exc:
        raise MigrationError(str(exc)) from exc
    if progress:
        progress("正在校验并解压迁移包", 40)
    workspace, manifest = safe_extract(zip_path, job / "import-stage", max_bytes)
    zip_path.unlink(missing_ok=True)
    if progress:
        progress("正在重写目标工作目录路径", 70)
    rewrite_database(workspace / "data" / "bbu.db", Path(WORKDIR_TOKEN),
                     target_root.resolve().as_posix(), include_sessions=False)
    _rewrite_text_tree(workspace, WORKDIR_TOKEN, target_root.resolve().as_posix())
    # 导入包已经去除会话；再次标记避免旧快照残留。
    conn = sqlite3.connect(workspace / "data" / "bbu.db")
    try:
        conn.execute("UPDATE tasks SET inputs_state=CASE WHEN input_json IN ('[]','',NULL) THEN 'available' ELSE 'excluded_by_migration' END")
        conn.execute("DELETE FROM sessions")
        conn.commit()
    finally:
        conn.close()
    integrity = validate_database(workspace / "data" / "bbu.db")
    if progress:
        progress("迁移包校验完成", 85)
    return {"workspace": workspace, "manifest": manifest, "integrity": integrity}


def validate_database(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MigrationError("迁移包缺少数据库")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise MigrationError(f"迁移数据库完整性检查失败：{check}")
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        schema = int(row[0]) if row and str(row[0]).isdigit() else 0
        if schema < MIN_SCHEMA:
            raise MigrationError(f"源数据库 schema {schema} 过旧，无法导入")
        counts = {}
        for table in ("users", "tasks", "skills", "skill_versions"):
            counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        admins = int(conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0])
        if counts["users"] < 1 or admins < 1:
            raise MigrationError("迁移包缺少可用的管理员账号，拒绝导入")
        return {"integrity": "ok", "schema_version": schema, "counts": counts,
                "active_admins": admins}
    finally:
        conn.close()


def _is_windows() -> bool:
    return os.name == "nt"


def workdir_supported(app) -> bool:
    """Windows 由重启激活，Linux 必须有 root 助手触发器或自定义命令。"""
    if _is_windows():
        return True
    if str(app.config.get("upgrade_helper_cmd") or "").strip():
        return True
    trigger = str(app.config.get("upgrade_helper_trigger")
                  or "/usr/local/bin/bbu-upgrade-helper-trigger")
    return Path(trigger).is_file()


def read_helper_conf() -> dict[str, Any]:
    path = Path(os.environ.get("BBU_UPGRADE_HELPER_CONF", "/etc/bbu-upgrade.json"))
    data = _json_read(path, {})
    return data if isinstance(data, dict) else {}


def allowed_work_roots(app) -> list[str]:
    custom = app.config.get("allowed_work_roots") or []
    if isinstance(custom, str):
        custom = [custom]
    custom = [str(x) for x in custom if str(x).strip()]
    conf = read_helper_conf()
    roots = conf.get("allowed_work_roots") or custom
    if isinstance(roots, str):
        roots = [roots]
    roots = [str(x) for x in roots if str(x).strip()]
    if roots:
        return roots
    if _is_windows():
        return []
    return ["/var/lib", "/srv", "/data", "/mnt"]


def validate_target_root(app, raw: str) -> Path:
    raw = str(raw or "").strip()
    if not raw:
        raise MigrationError("请输入工作目录绝对路径")
    if len(raw) > 260 or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise MigrationError("工作目录路径非法")
    target = Path(raw)
    if not target.is_absolute():
        raise MigrationError("工作目录必须是绝对路径")
    target = target.resolve()
    if target == target.parent:
        raise MigrationError("不能使用文件系统根目录作为工作目录")
    prefix = Path(str(app.config.get("_root") or "")).resolve()
    work = Path(app.config.work_dir).resolve()
    if target == prefix or prefix in target.parents or target in prefix.parents:
        raise MigrationError("工作目录不能位于程序目录内或包含程序目录")
    if target == work or work in target.parents or target in work.parents:
        raise MigrationError("工作目录不能是当前工作目录或与其相互嵌套")
    if work.name and target.name == work.name:
        raise MigrationError("目标目录与当前工作目录重名，请使用独立目录")
    if target.exists():
        if not target.is_dir():
            raise MigrationError("目标路径必须是目录")
        if any(target.iterdir()):
            raise MigrationError("目标目录必须为空或尚不存在")
    if not workdir_supported(app):
        raise MigrationError("当前部署未安装 root 维护助手，不能网页切换工作目录")
    if not _is_windows():
        roots = []
        for raw_root in allowed_work_roots(app):
            try:
                root = Path(raw_root).resolve()
            except OSError:
                continue
            if root.is_absolute():
                roots.append(root)
        if roots and not any(target == root or root in target.parents for root in roots):
            raise MigrationError("工作目录不在部署允许的根目录内，请使用 root 助手安装脚本配置 allowed_work_roots")
    return target


def helper_apply(app, job_id: str, action: str, request_extra: dict[str, Any]) -> dict[str, Any]:
    job = _job_dir(app, job_id)
    status = _status(app, job_id)
    request = {
        "job_id": job_id,
        "action": action,
        "update_dir": str(job),
        "prefix": str(app.config.get("_root") or ""),
        "state_dir": str(app.config.data_dir.parent),
        "service_name": str(app.config.get("upgrade_service_name", "bbu-fault-platform")),
        "service_user": str(app.config.get("upgrade_service_user", "bbu")),
        "health_url": f"http://127.0.0.1:{int(app.config.get('port', 8080))}/api/health",
        "target_version": str(__version__),
        "created_at": iso_now(),
    }
    request.update(request_extra)
    _json_write(job / "request.json", request)
    _save_status(app, job_id, phase="queued", message="已提交给 root 维护助手，等待执行",
                 progress=15, error="")
    custom = str(app.config.get("upgrade_helper_cmd") or "").strip()
    if custom:
        command = re.split(r"\s+", custom) + [job_id]
    else:
        trigger = str(app.config.get("upgrade_helper_trigger")
                      or "/usr/local/bin/bbu-upgrade-helper-trigger")
        command = ["sudo", "-n", trigger, job_id]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        _save_status(app, job_id, phase="failed", message="维护助手启动超时", progress=100,
                     error="维护助手启动超时")
        raise MigrationError("维护助手启动超时") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()[-500:]
        _save_status(app, job_id, phase="failed", message="无法启动 root 维护助手", progress=100,
                     error=f"助手启动失败：{detail}")
        raise MigrationError(f"无法启动 root 维护助手：{detail}")
    return _status(app, job_id)


def assert_idle(app) -> None:
    running = int(app.db.one(
        "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','running')")["n"])
    if running:
        raise MigrationConflict(f"当前有 {running} 个排队/运行中的诊断任务")
    try:
        evaluations = int(app.db.one(
            "SELECT COUNT(*) AS n FROM evaluations WHERE status='running'")["n"])
    except Exception:
        evaluations = 0
    if evaluations:
        raise MigrationConflict(f"当前有 {evaluations} 个运行中的评测任务")
    try:
        if app.learning.busy():
            raise MigrationConflict("自动沉淀队列仍有任务，请稍后再试")
    except AttributeError:
        pass
    try:
        from . import updater
        active = updater.active_execution(app)
        if active:
            raise MigrationConflict(f"已有升级任务正在执行：{active.get('id')}")
    except ImportError:
        pass


def disk_free(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


class MaintenanceGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self.job_id = ""
        self.action = ""

    @property
    def active(self) -> bool:
        return self._active

    def begin(self, job_id: str, action: str) -> None:
        with self._lock:
            if self._active:
                raise MigrationConflict(f"已有维护任务正在执行：{self.job_id}")
            self._active = True
            self.job_id = job_id
            self.action = action

    def end(self) -> None:
        with self._lock:
            self._active = False
            self.job_id = ""
            self.action = ""


class MigrationManager:
    def __init__(self, app, gate: MaintenanceGate) -> None:
        self.app = app
        self.gate = gate

    def _progress(self, job_id: str, phase: str, message: str,
                  start: int, end: int) -> Callable[[str, int], None] | None:
        def report(message_: str, percent: int) -> None:
            mapped = start + int((end - start) * max(0, min(100, percent)) / 100)
            _save_status(self.app, job_id, phase=phase, message=message_,
                         progress=mapped, error="")
        return report

    def start_export(self, passphrase: str, actor: str) -> dict[str, Any]:
        app = self.app
        assert_idle(app)
        prune_jobs(app)
        job_id = new_job_id()
        job = job_root(app) / job_id
        job.mkdir(parents=True, exist_ok=False)
        _save_status(app, job_id, action="export", kind="export", phase="queued",
                     message="已创建导出任务", progress=0, actor=actor, error="",
                     excluded_categories=EXCLUDED_CATEGORIES)
        self.gate.begin(job_id, "export")

        def run() -> None:
            try:
                _save_status(app, job_id, phase="preparing", message="正在构建精简工作区", progress=5)
                limit = int(app.config.get("max_migration_mb", 4096)) * 1024 * 1024
                build = build_workspace(app.config, job / "workspace", WORKDIR_TOKEN,
                                        include_sessions=False, limit_bytes=limit,
                                        progress=self._progress(job_id, "preparing", "", 5, 45))
                _save_status(app, job_id, phase="packaging", message="正在打包迁移数据", progress=55,
                             file_count=build["manifest"].get("file_count"),
                             total_bytes=build["manifest"].get("total_bytes"))
                zip_path = zip_workspace(job, build["workspace"], build["manifest_path"])
                _save_status(app, job_id, phase="packaging", message="正在加密迁移包", progress=70)
                package = job / "export.bbumig"
                info = encrypt_file(zip_path, package, passphrase,
                                    progress=lambda done, total: self._progress(
                                        job_id, "packaging", f"正在加密迁移包（{int(done*100/max(1,total))}%）",
                                        70, 95)(f"正在加密迁移包（{int(done*100/max(1,total))}%）", int(done*100/max(1,total))))
                zip_path.unlink(missing_ok=True)
                shutil.rmtree(job / "workspace", ignore_errors=True)
                (job / "manifest.json").unlink(missing_ok=True)
                _save_status(app, job_id, phase="ready", message="迁移包已生成，可下载",
                             progress=100, package_sha256=info["sha256"], size=info["size"],
                             cipher=info.get("cipher", ""), file_name=f"bbu-migration-{job_id}.bbumig")
                self.scope_audit("migration.export", actor, job_id)
            except Exception as exc:  # noqa: BLE001
                _save_status(app, job_id, phase="failed", message="导出失败", progress=100,
                             error=f"{type(exc).__name__}: {exc}")
            finally:
                self.gate.end()

        threading.Thread(target=run, name=f"migration-export-{job_id}", daemon=True).start()
        return _status(app, job_id)

    def start_workspace(self, target: Path, actor: str) -> dict[str, Any]:
        app = self.app
        assert_idle(app)
        prune_jobs(app)
        job_id = new_job_id()
        job = job_root(app) / job_id
        job.mkdir(parents=True, exist_ok=False)
        _save_status(app, job_id, action="workspace", kind="workspace", phase="queued",
                     message="已创建工作目录迁移任务", progress=0, actor=actor, error="",
                     target_root=target.as_posix())
        self.gate.begin(job_id, "workspace")

        def run() -> None:
            try:
                _save_status(app, job_id, phase="preparing", message="正在构建精简工作区", progress=5)
                limit = int(app.config.get("max_migration_mb", 4096)) * 1024 * 1024
                build = build_workspace(app.config, job / "workspace", target.as_posix(),
                                        include_sessions=True, limit_bytes=limit,
                                        progress=self._progress(job_id, "preparing", "", 5, 65))
                verify_workspace(build["workspace"], build["manifest_path"])
                _save_status(app, job_id, phase="ready", message="工作区已准备，等待切换", progress=70,
                             file_count=build["manifest"].get("file_count"))
                if _is_windows():
                    _write_pending(app, {
                        "action": "workspace", "job_id": job_id, "target_root": target.as_posix(),
                        "stage_dir": str(job / "workspace"), "manifest_path": str(job / "manifest.json"),
                        "created_at": iso_now(),
                    })
                    _save_status(app, job_id, phase="waiting_restart",
                                 message="工作目录已准备，请重启平台完成切换", progress=75,
                                 requires_restart=True)
                    # 保持维护锁直到进程重启，避免待激活文件被第二个迁移覆盖。
                    return
                helper_apply(app, job_id, "workspace", {
                    "target_root": target.as_posix(),
                    "stage_dir": str(job / "workspace"),
                    "manifest_path": str(job / "manifest.json"),
                    "current_workdir": str(app.config.work_dir),
                    "config_path": str(Path(str(app.config.get("_root"))) / "config.json"),
                })
                self.scope_audit("migration.workspace", actor, job_id)
                # 成功切换后当前进程会被 systemd 替换；若助手失败则回收维护锁。
                self._watch_helper(job_id)
            except Exception as exc:  # noqa: BLE001
                _save_status(app, job_id, phase="failed", message="工作目录迁移失败", progress=100,
                             error=f"{type(exc).__name__}: {exc}")
                self.gate.end()

        threading.Thread(target=run, name=f"migration-workspace-{job_id}", daemon=True).start()
        return _status(app, job_id)

    def start_import(self, package: Path, passphrase: str, actor: str) -> dict[str, Any]:
        app = self.app
        assert_idle(app)
        prune_jobs(app)
        job_id = new_job_id()
        job = job_root(app) / job_id
        job.mkdir(parents=True, exist_ok=False)
        source = job / "package.bbumig"
        shutil.move(str(package), source)
        _save_status(app, job_id, action="import", kind="import", phase="queued",
                     message="已创建导入任务", progress=0, actor=actor, error="",
                     package_size=source.stat().st_size)
        self.gate.begin(job_id, "import")

        def run() -> None:
            try:
                limit = int(app.config.get("max_migration_mb", 4096)) * 1024 * 1024
                result = import_package(job, source, passphrase, app.config.work_dir,
                                        limit, progress=self._progress(job_id, "preparing", "", 5, 80))
                source.unlink(missing_ok=True)
                _save_status(app, job_id, phase="ready", message="迁移包已校验，准备覆盖恢复", progress=85,
                             schema_version=result["integrity"].get("schema_version"),
                             db_counts=result["integrity"].get("counts"))
                manifest_path = job / "import-stage" / "manifest.json"
                if _is_windows():
                    _write_pending(app, {
                        "action": "import", "job_id": job_id,
                        "stage_dir": str(result["workspace"]),
                        "manifest_path": str(manifest_path),
                        "created_at": iso_now(),
                    })
                    _save_status(app, job_id, phase="waiting_restart",
                                 message="迁移包已校验，请重启平台完成恢复", progress=90,
                                 requires_restart=True)
                    # 保持维护锁直到进程重启，避免待激活文件被第二个迁移覆盖。
                    return
                helper_apply(app, job_id, "restore", {
                    "stage_dir": str(result["workspace"]),
                    "manifest_path": str(manifest_path),
                    "current_workdir": str(app.config.work_dir),
                    "config_path": str(Path(str(app.config.get("_root"))) / "config.json"),
                })
                self.scope_audit("migration.import", actor, job_id)
                self._watch_helper(job_id)
            except Exception as exc:  # noqa: BLE001
                _save_status(app, job_id, phase="failed", message="导入失败", progress=100,
                             error=f"{type(exc).__name__}: {exc}")
                self.gate.end()

        threading.Thread(target=run, name=f"migration-import-{job_id}", daemon=True).start()
        return _status(app, job_id)

    def _watch_helper(self, job_id: str) -> None:
        """Linux 助手未重启服务时回收维护锁；成功切换由 process 终止自然结束。"""
        for _ in range(900):
            time.sleep(2)
            try:
                status = _status(self.app, job_id)
            except Exception:
                time.sleep(4)
                continue
            if status.get("phase") == "failed":
                self.gate.end()
                return
            if status.get("phase") == "succeeded":
                return
        # 助手长时间未回写状态：解除页面维护锁，避免永久 503；
        # 真正的执行互斥仍由 root 助手的单实例锁保证。
        self.gate.end()

    def scope_audit(self, action: str, actor: str, job_id: str) -> None:
        try:
            self.app.audit(actor, action, job_id, {"job_id": job_id})
        except Exception:
            pass


def _write_pending(app, data: dict[str, Any]) -> None:
    root = Path(str(app.config.get("_root") or ""))
    path = root / "pending_maintenance.json"
    _json_write(path, data)


def mark_interrupted(app) -> None:
    """启动时把上一个进程留下的非终态任务标为失败，避免永久维护锁。"""
    for item in list_jobs(app):
        export_download = item.get("kind") == "export" and item.get("phase") == "ready"
        if item.get("phase") in RUNNING_PHASES and not export_download:
            try:
                _save_status(app, item["id"], phase="failed",
                             message="平台重启导致迁移任务中断", progress=100,
                             error="interrupted: 任务在重启前未完成")
            except Exception:
                pass
    prune_jobs(app)
