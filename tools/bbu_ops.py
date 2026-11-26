#!/usr/bin/env python3
"""BBU 平台统一运维入口：状态、启停、重启、备份、空间统计和运行参数。

默认从 config.json / BBU_* 环境变量读取配置；不依赖第三方库。
网页运行参数仍建议在“系统信息 → 运行参数”修改，本脚本用于无网页或批量维护场景。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402


def human_bytes(value: int) -> str:
    value = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def health(config: Config, timeout: float = 2.0) -> tuple[bool, dict]:
    host = str(config.get("host") or "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    url = f"http://{host}:{int(config.get('port'))}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200, json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False, {}


def service_name(config: Config) -> str:
    return str(config.get("upgrade_service_name") or "bbu-fault-platform")


def systemctl(config: Config, action: str) -> int:
    unit = service_name(config)
    base = ["systemctl", action, unit]
    if os.geteuid() != 0:
        base = ["sudo", "-n"] + base
    print("$", " ".join(base))
    return subprocess.run(base, check=False).returncode


def manual_pid(config: Config) -> int:
    path = Path(config.data_dir) / "bbu.pid"
    try:
        pid = int(path.read_text().strip())
        if pid > 0:
            os.kill(pid, 0)
            return pid
    except (OSError, ValueError):
        return 0
    return 0


def wait_health(config: Config, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        ok, _ = health(config)
        if ok:
            return True
        time.sleep(0.5)
    return False


def manual_stop(config: Config) -> int:
    pid = manual_pid(config)
    if not pid:
        print("未发现手动模式的 PID 文件，服务可能未运行")
        return 0
    print(f"停止进程 {pid}")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            (Path(config.data_dir) / "bbu.pid").unlink(missing_ok=True)
            return 0
        time.sleep(0.5)
    print("30 秒内未退出，请检查日志", file=sys.stderr)
    return 1


def manual_start(config: Config) -> int:
    if manual_pid(config):
        print("服务已在运行")
        return 0
    host = str(config.get("host"))
    port = int(config.get("port"))
    log = Path(config.data_dir) / "logs" / "ops-run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "ab")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "--host", host, "--port", str(port)],
        cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    (Path(config.data_dir) / "bbu.pid").write_text(str(proc.pid))
    print(f"已启动 PID {proc.pid}，日志 {log}")
    return 0 if wait_health(config, 30) else 2


def cmd_status(config: Config, _args) -> int:
    ok, data = health(config)
    print(f"服务   : {'运行中' if ok else '未响应'} ({config.get('host')}:{config.get('port')})")
    if ok:
        print(f"版本   : v{data.get('version', '?')}  品牌: {data.get('brand', '?')}")
    print(f"配置文件: {config.config_file}")
    print(f"工作目录: {config.work_dir}")
    print(f"数据目录: {config.data_dir}")
    print(f"技能/案例: {config.skills_dir} / {config.cases_dir}")
    try:
        disk = shutil.disk_usage(config.data_dir)
        print(f"磁盘   : 可用 {human_bytes(disk.free)} / 总 {human_bytes(disk.total)}")
    except OSError:
        pass
    if config.db_path.exists():
        conn = sqlite3.connect(str(config.db_path))
        for table in ("tasks", "workshop_drafts", "workshop_jobs", "storage_jobs", "real_cases"):
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                print(f"{table:<16}: {n}")
            except sqlite3.Error:
                pass
        conn.close()
    print(f"systemd: {service_name(config)}")
    return 0 if ok else 1


def cmd_start(config: Config, args) -> int:
    if args.systemd:
        code = systemctl(config, "start")
        return code if code else (0 if wait_health(config, 30) else 2)
    return manual_start(config)


def cmd_stop(config: Config, args) -> int:
    if args.systemd:
        return systemctl(config, "stop")
    return manual_stop(config)


def cmd_restart(config: Config, args) -> int:
    if args.systemd:
        code = systemctl(config, "restart")
        if code:
            return code
        if not wait_health(config, 30):
            print("systemd 已返回，但健康检查未通过", file=sys.stderr)
            return 2
        print("重启完成，健康检查通过")
        return 0
    code = manual_stop(config)
    if code:
        return code
    return manual_start(config)


def cmd_logs(config: Config, args) -> int:
    if args.systemd:
        return subprocess.run(["journalctl", "-u", service_name(config), "-n", str(args.lines),
                               "-f" if args.follow else "--no-pager"], check=False).returncode
    log = Path(config.data_dir) / "logs" / "ops-run.log"
    if not log.exists():
        print(f"日志不存在：{log}", file=sys.stderr)
        return 1
    with open(log, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()[-args.lines:]
    print("".join(lines), end="")
    if args.follow:
        with open(log, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                line = fh.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.5)
    return 0


def cmd_backup(config: Config, args) -> int:
    root = Path(config.data_dir).parent
    out_dir = Path(args.output) if args.output else root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"bbu-backup-{stamp}.tar.gz"
    skip_names = {"tmp", "updates"} if not args.include_updates else {"tmp"}
    skip_uploads = not args.include_uploads

    def filt(info: tarfile.TarInfo):
        rel = Path(info.name)
        parts = rel.parts
        if parts and parts[0] in skip_names:
            return None
        if skip_uploads and "uploads" in parts:
            return None
        if info.name.endswith("bbu.pid"):
            return None
        return info

    with tarfile.open(out, "w:gz") as tar:
        if config.config_file.exists():
            tar.add(config.config_file, arcname="config.json")
        tar.add(config.data_dir, arcname="data", filter=filt)
        tar.add(config.skills_dir, arcname="skills")
        tar.add(config.cases_dir, arcname="cases")
    size = out.stat().st_size
    print(f"备份完成：{out} ({human_bytes(size)})")
    print("默认不含原始上传和更新临时目录；需要时加 --include-uploads / --include-updates。")
    return 0


def storage_report(config: Config) -> dict:
    conn = sqlite3.connect(str(config.db_path))
    conn.row_factory = sqlite3.Row
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    state_col = "inputs_state" if "inputs_state" in cols else "'available' AS inputs_state"
    tasks = conn.execute(
        f"SELECT id,title,input_json,{state_col} FROM tasks").fetchall()
    refs: dict[str, set[str]] = {}
    for task in tasks:
        if task["inputs_state"] in ("released", "excluded_by_migration"):
            continue
        try:
            data = json.loads(task["input_json"] or "[]")
        except json.JSONDecodeError:
            data = []
        for item in data if isinstance(data, list) else []:
            path = str(item.get("stored_path") or "")
            if path:
                refs.setdefault(path, set()).add(task["id"])
    details = []
    upload_root = (Path(config.data_dir) / "uploads").resolve()
    for task in tasks:
        raw = 0
        shared = 0
        try:
            data = json.loads(task["input_json"] or "[]")
        except json.JSONDecodeError:
            data = []
        for item in data if isinstance(data, list) else []:
            path = Path(str(item.get("stored_path") or ""))
            try:
                path.resolve().relative_to(upload_root)
                if path.is_file():
                    raw += path.stat().st_size
                    if refs.get(str(path), set()) - {task["id"]}:
                        shared += 1
            except (OSError, ValueError):
                continue
        artifact_dir = Path(config.data_dir) / "tasks" / task["id"]
        artifacts = sum(p.stat().st_size for p in artifact_dir.rglob("*")
                        if p.is_file() and not p.is_symlink()) if artifact_dir.exists() else 0
        details.append({"id": task["id"], "title": task["title"],
                        "inputs_state": task["inputs_state"], "raw": raw,
                        "artifacts": artifacts, "shared": shared})
    conn.close()
    return {
        "raw": sum(x["raw"] for x in details),
        "artifacts": sum(x["artifacts"] for x in details),
        "tasks": details,
    }


def cmd_storage(config: Config, args) -> int:
    report = storage_report(config)
    print(f"原始输入 : {human_bytes(report['raw'])}")
    print(f"中间产物 : {human_bytes(report['artifacts'])}")
    if args.tasks:
        for item in report["tasks"]:
            print(f"{item['id']:<22} raw={human_bytes(item['raw']):>10} "
                  f"art={human_bytes(item['artifacts']):>10} "
                  f"shared={item['shared']} state={item['inputs_state']} {item['title'] or ''}")
    return 0


def cmd_release(config: Config, args) -> int:
    ok, _ = health(config)
    if ok:
        print("服务正在运行。请登录管理页“任务空间”选择释放，或先停止服务后再离线释放。", file=sys.stderr)
        return 2
    if not args.yes:
        print("离线释放会删除原始上传日志；确认后加 --yes 重试。", file=sys.stderr)
        return 2
    from bbu.db import Database
    from bbu.task_storage import TaskStorageService
    db = Database(config.db_path)
    service = TaskStorageService(db, config)
    try:
        job = service.create(args.task, int(args.user or 0))
        deadline = time.time() + 120
        while time.time() < deadline:
            job = service.get(job["id"])
            if job["status"] in ("done", "failed", "interrupted"):
                break
            time.sleep(0.5)
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 0 if job["status"] == "done" else 1
    finally:
        service.shutdown()


def cmd_config(config: Config, args) -> int:
    if args.action == "show":
        print(json.dumps(config.runtime_items(), ensure_ascii=False, indent=2))
        return 0
    values = {}
    for item in args.set_values or []:
        if "=" not in item:
            print(f"参数格式应为 key=value：{item}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        values[key] = value
    try:
        saved = config.save_runtime(values)
    except ValueError as exc:
        print(f"保存失败：{exc}", file=sys.stderr)
        return 2
    print(f"已保存 {len(saved)} 项到 {config.config_file}；执行 restart 后生效。")
    return 0


def cmd_doctor(config: Config, _args) -> int:
    problems = []
    ok, _ = health(config)
    print(f"[{'OK' if ok else '!!'}] HTTP 健康检查")
    for path in (config.data_dir, config.skills_dir, config.cases_dir, config.work_dir):
        writable = path.exists() and os.access(path, os.W_OK)
        print(f"[{'OK' if writable else '!!'}] {path}")
        if not writable:
            problems.append(f"目录不可写或不存在: {path}")
    if config.db_path.exists():
        try:
            conn = sqlite3.connect(str(config.db_path))
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            print(f"[{'OK' if result == 'ok' else '!!'}] SQLite integrity_check: {result}")
            if result != "ok":
                problems.append("数据库完整性检查失败")
        except sqlite3.Error as exc:
            problems.append(f"数据库不可读: {exc}")
    disk = shutil.disk_usage(config.data_dir)
    if disk.free < 2 * 1024 ** 3:
        problems.append(f"磁盘剩余不足 2GB: {human_bytes(disk.free)}")
    print(f"[{'OK' if not problems else '!!'}] 磁盘剩余 {human_bytes(disk.free)}")
    if problems:
        print("发现问题：")
        for item in problems:
            print(" -", item)
        return 1
    print("检查通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BBU 故障诊断平台统一运维脚本")
    parser.add_argument("--systemd", action="store_true", help="使用 systemd 管理服务")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="服务、目录、磁盘和数据库概览")
    sub.add_parser("doctor", help="配置、目录、数据库和磁盘检查")

    for name, help_text in (("start", "启动服务"), ("stop", "停止服务"), ("restart", "重启服务")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--systemd", action="store_true", default=argparse.SUPPRESS)
    logs = sub.add_parser("logs", help="查看运行日志")
    logs.add_argument("--lines", type=int, default=80)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--systemd", action="store_true", default=argparse.SUPPRESS)

    backup = sub.add_parser("backup", help="备份配置、数据库、技能和案例")
    backup.add_argument("--output", help="输出目录，默认 work_dir/backups")
    backup.add_argument("--include-uploads", action="store_true", help="包含原始上传日志")
    backup.add_argument("--include-updates", action="store_true", help="包含更新包缓存")

    storage = sub.add_parser("storage", help="统计任务原始输入和中间产物空间")
    storage.add_argument("--tasks", action="store_true", help="逐任务列出")
    release = sub.add_parser("release", help="服务停止后离线释放指定任务的原始输入")
    release.add_argument("--task", required=True)
    release.add_argument("--user", type=int, default=0, help="审计用户 ID，可填 0")
    release.add_argument("--yes", action="store_true")

    config_parser = sub.add_parser("config", help="查看或保存运行参数")
    config_parser.add_argument("action", choices=("show", "set"))
    config_parser.add_argument("set_values", nargs="*", metavar="key=value")

    args = parser.parse_args()
    config = Config()
    handlers = {
        "status": cmd_status, "doctor": cmd_doctor, "start": cmd_start,
        "stop": cmd_stop, "restart": cmd_restart, "logs": cmd_logs,
        "backup": cmd_backup, "storage": cmd_storage, "release": cmd_release,
        "config": cmd_config,
    }
    return handlers[args.command](config, args)


if __name__ == "__main__":
    raise SystemExit(main())
