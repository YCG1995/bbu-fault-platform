#!/usr/bin/env python3
"""root/systemd 一次性维护助手（仅由 bbu-upgrade-helper@.service 调用）。

只接受 STATE_DIR/updates/<job>/request.json；支持离线升级/回滚，以及
工作目录切换（workspace）和迁移包覆盖恢复（restore）。不接受任意命令或任意路径。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bbu.filelock import exclusive_lock  # noqa: E402

JOB_RE = re.compile(r"^U[A-Za-z0-9_-]{6,40}$")
CONF_PATH = Path(os.environ.get("BBU_UPGRADE_HELPER_CONF", "/etc/bbu-upgrade.json"))
JOB = sys.argv[1] if len(sys.argv) > 1 else ""
DEFAULT_WORK_ROOTS = ("/var/lib", "/srv", "/data", "/mnt")


def fail(message: str) -> None:
    print(f"[helper] {message}", file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_version(prefix: Path) -> str:
    try:
        text = (prefix / "bbu/__init__.py").read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        return match.group(1) if match else ""
    except OSError:
        return ""


def within(base: Path, candidate: Path) -> bool:
    try:
        base = base.resolve()
        candidate = candidate.resolve()
        return candidate == base or base in candidate.parents
    except OSError:
        return False


def _allowed_roots(conf: dict) -> list[Path]:
    raw = conf.get("allowed_work_roots") or list(DEFAULT_WORK_ROOTS)
    if isinstance(raw, str):
        raw = [raw]
    roots = []
    for item in raw:
        try:
            roots.append(Path(str(item)).resolve())
        except OSError:
            continue
    return roots


def _validated_target(target: Path, conf: dict, prefix: Path, state: Path) -> Path:
    target = target.resolve()
    if target == target.parent or target == prefix or target == state:
        raise RuntimeError("目标工作目录非法")
    if target.exists() and not target.is_dir():
        raise RuntimeError("目标路径必须是目录")
    if prefix in target.parents or target in prefix.parents or state in target.parents or target in state.parents:
        raise RuntimeError("目标工作目录不能与程序/数据目录相互嵌套")
    roots = _allowed_roots(conf)
    if not any(target == root or root in target.parents for root in roots):
        raise RuntimeError(f"目标工作目录不在允许根目录内：{', '.join(str(x) for x in roots)}")
    return target


def _run_service(service: str, action: str) -> None:
    proc = subprocess.run(["systemctl", action, service], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"systemctl {action} {service} 失败：{(proc.stderr or proc.stdout)[-500:]}")


def _wait_service(service: str, active: bool, timeout: float = 45) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = subprocess.run(["systemctl", "is-active", "--quiet", service],
                              capture_output=True, text=True)
        if (proc.returncode == 0) == active:
            return
        time.sleep(0.5)
    raise RuntimeError(f"服务 {service} 未进入 {'运行' if active else '停止'} 状态")


def _chown_tree(path: Path, user: str) -> None:
    import grp
    import pwd
    account = pwd.getpwnam(user)
    group = grp.getgrnam(user)
    for root, dirs, files in os.walk(path):
        os.chown(root, account.pw_uid, group.gr_gid)
        for name in dirs:
            os.chown(Path(root) / name, account.pw_uid, group.gr_gid)
        for name in files:
            os.chown(Path(root) / name, account.pw_uid, group.gr_gid)


def _health_ok(conf: dict, target_version: str, timeout: float = 45) -> tuple[bool, str]:
    url = str(conf.get("health_url") or "http://127.0.0.1:8080/api/health")
    version = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            version = str(data.get("version") or "")
            if data.get("ok") and (not target_version or version == target_version):
                return True, version
        except Exception:
            pass
        time.sleep(1)
    return False, version


def _patch_unit(unit_path: Path, target: Path, state: Path) -> tuple[str, str]:
    original = unit_path.read_text(encoding="utf-8")
    text = original
    env_lines = (
        f"Environment=BBU_DATA_DIR={target / 'data'}\n"
        f"Environment=BBU_SKILLS_DIR={target / 'skills'}\n"
        f"Environment=BBU_CASES_DIR={target / 'cases'}\n"
    )
    text = re.sub(r"(?m)^Environment=(?:BBU_DATA_DIR|BBU_SKILLS_DIR|BBU_CASES_DIR)=.*\n?", "", text)
    if "[Service]\n" not in text:
        raise RuntimeError("systemd 单元缺少 [Service]")
    text = text.replace("[Service]\n", "[Service]\n" + env_lines, 1)
    read_write = f"ReadWritePaths={target}"
    if re.search(r"(?m)^ReadWritePaths=", text):
        text = re.sub(r"(?m)^ReadWritePaths=.*$", read_write, text)
    else:
        text = text.replace("[Service]\n", "[Service]\n" + read_write + "\n", 1)
    if state != target and f"ReadWritePaths={state}" in text:
        text = text.replace(f"ReadWritePaths={state}", read_write)
    return original, text


def _write_config(path: Path, config: dict, target: Path, user: str) -> None:
    import pwd
    config["work_dir"] = str(target)
    config["data_dir"] = str(target / "data")
    config["skills_dir"] = str(target / "skills")
    config["cases_dir"] = str(target / "cases")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    try:
        account = pwd.getpwnam(user)
        import grp
        group = grp.getgrnam(user)
        os.chown(tmp, account.pw_uid, group.gr_gid)
    except Exception:
        pass
    os.replace(tmp, path)


def _copy_status_to_target(job_dir: Path, status: dict, target: Path, job_id: str,
                           user: str = "") -> None:
    target_job = target / "updates" / job_id
    target_job.mkdir(parents=True, exist_ok=True)
    (target_job / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    if user:
        _chown_tree(target_job, user)


def run_workspace(job_dir: Path, request: dict, status: dict, conf: dict,
                  prefix: Path, state: Path, save, log) -> int:
    from bbu.migration import verify_workspace

    service = str(conf.get("service_name") or "bbu-fault-platform")
    user = str(conf.get("service_user") or "bbu")
    job_id = str(request.get("job_id") or JOB)
    config_path = prefix / "config.json"
    unit_path = Path(str(conf.get("unit_path") or f"/etc/systemd/system/{service}.service"))
    config_original = ""
    unit_original = ""
    conf_original = ""
    try:
        target = _validated_target(Path(str(request.get("target_root") or "")), conf, prefix, state)
        stage = Path(str(request.get("stage_dir") or "")).resolve()
        manifest = Path(str(request.get("manifest_path") or "")).resolve()
        if not within(job_dir, stage) or not within(job_dir, manifest):
            raise RuntimeError("迁移工作区越界")
        verify_workspace(stage, manifest)
        if target.exists() and any(target.iterdir()):
            raise RuntimeError("目标工作目录不为空")
        if not config_path.is_file() or not unit_path.is_file():
            raise RuntimeError("缺少 config.json 或 systemd 单元")
        config_original = config_path.read_text(encoding="utf-8")
        config = json.loads(config_original)
        unit_original, unit_new = _patch_unit(unit_path, target, state)
        conf_original = json.dumps(conf, ensure_ascii=False, indent=2) + "\n"
        stage_target = target.parent / f".{target.name}.bbu-{job_id}"
        shutil.rmtree(stage_target, ignore_errors=True)
        save("backup", "正在停止服务并准备目标工作目录", 20)
        _run_service(service, "stop")
        _wait_service(service, active=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, stage_target, symlinks=False)
        (stage_target / "updates").mkdir(exist_ok=True)
        (stage_target / "backups").mkdir(exist_ok=True)
        _chown_tree(stage_target, user)
        if target.exists():
            target.rmdir()
        os.replace(stage_target, target)
        _write_config(config_path, config, target, user)
        unit_path.write_text(unit_new, encoding="utf-8")
        os.chmod(unit_path, 0o644)
        conf_new = dict(conf)
        conf_new["state_dir"] = str(target)
        conf_new.setdefault("allowed_work_roots", [str(x) for x in _allowed_roots(conf)])
        CONF_PATH.write_text(
            json.dumps(conf_new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(CONF_PATH, 0o644)
        subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True, text=True)
        save("restarting", "工作目录已切换，正在重启服务", 80)
        _run_service(service, "start")
        _wait_service(service, active=True)
        ok, version = _health_ok(conf, str(request.get("target_version") or ""))
        if not ok:
            raise RuntimeError(f"切换后健康检查失败：版本 {version or '无响应'}")
        save("succeeded", "工作目录切换成功", 100, target_root=str(target),
             current_workdir=str(target), error="")
        _copy_status_to_target(job_dir, status, target, job_id, user)
        log(f"工作目录切换成功: {state} -> {target}")
        return 0
    except Exception as exc:
        # 尽力恢复旧配置并重启旧服务；旧工作目录始终未被修改。
        try:
            if config_original:
                config_path.write_text(config_original, encoding="utf-8")
            if unit_original:
                unit_path.write_text(unit_original, encoding="utf-8")
                subprocess.run(["systemctl", "daemon-reload"], check=False,
                               capture_output=True, text=True)
            if conf_original:
                CONF_PATH.write_text(conf_original, encoding="utf-8")
            _run_service(service, "start")
        except Exception:
            pass
        save("failed", "工作目录切换失败，已尝试恢复旧目录", 100, error=str(exc))
        log(f"工作目录切换失败: {exc}")
        return 1


def run_restore(job_dir: Path, request: dict, status: dict, conf: dict,
                prefix: Path, state: Path, save, log) -> int:
    from bbu.migration import verify_workspace

    service = str(conf.get("service_name") or "bbu-fault-platform")
    job_id = str(request.get("job_id") or JOB)
    stage = Path(str(request.get("stage_dir") or "")).resolve()
    manifest = Path(str(request.get("manifest_path") or "")).resolve()
    backup_root = state / "backups" / f"import-{job_id}"
    try:
        if not within(job_dir, stage) or not within(job_dir, manifest):
            raise RuntimeError("导入工作区越界")
        verify_workspace(stage, manifest)
        config = json.loads((prefix / "config.json").read_text(encoding="utf-8"))
        data_raw = str(config.get("data_dir") or "").strip()
        if not data_raw and str(config.get("work_dir") or "").strip():
            data_raw = str(Path(str(config["work_dir"])) / "data")
        if not data_raw or Path(data_raw).resolve().parent != state:
            raise RuntimeError("当前配置数据目录与维护助手状态目录不一致")
        if backup_root.exists():
            raise RuntimeError("导入备份目录已存在")
        save("restoring", "正在停止服务并备份当前业务数据", 35)
        _run_service(service, "stop")
        _wait_service(service, active=False)
        backup_root.mkdir(parents=True)
        moved_old: list[str] = []
        moved_new: list[str] = []
        try:
            for name in ("data", "skills", "cases"):
                current = state / name
                if current.exists():
                    os.replace(current, backup_root / name)
                    moved_old.append(name)
            for name in ("data", "skills", "cases"):
                source = stage / name
                if not source.is_dir():
                    raise RuntimeError(f"迁移工作区缺少 {name}")
                os.replace(source, state / name)
                moved_new.append(name)
            _chown_tree(state / "data", str(conf.get("service_user") or "bbu"))
            _chown_tree(state / "skills", str(conf.get("service_user") or "bbu"))
            _chown_tree(state / "cases", str(conf.get("service_user") or "bbu"))
            save("restarting", "数据已替换，正在启动并检查服务", 80)
            _run_service(service, "start")
            _wait_service(service, active=True)
            ok, version = _health_ok(conf, str(request.get("target_version") or ""))
            if not ok:
                raise RuntimeError(f"恢复后健康检查失败：版本 {version or '无响应'}")
            save("succeeded", "迁移数据恢复成功", 100, backup_dir=str(backup_root), error="")
            log(f"覆盖恢复成功，旧数据备份: {backup_root}")
            return 0
        except Exception:
            # 把已放入的新数据移出，并恢复旧目录。
            for name in moved_new:
                current = state / name
                if current.exists():
                    failed = backup_root / f"failed-new-{name}"
                    shutil.rmtree(failed, ignore_errors=True)
                    os.replace(current, failed)
            for name in moved_old:
                old = backup_root / name
                if old.exists():
                    os.replace(old, state / name)
            raise
    except Exception as exc:
        try:
            _run_service(service, "start")
        except Exception:
            pass
        save("failed", "迁移数据恢复失败，已尝试恢复旧数据", 100, error=str(exc))
        log(f"覆盖恢复失败: {exc}")
        return 1


def main() -> int:
    if not JOB_RE.match(JOB):
        fail("任务 ID 非法")
    conf = load_json(CONF_PATH, None)
    if not isinstance(conf, dict):
        fail(f"缺少 root 助手配置 {CONF_PATH}")
    state = Path(str(conf.get("state_dir") or "")).resolve()
    prefix = Path(str(conf.get("prefix") or "")).resolve()
    if not state.is_dir() or not prefix.is_dir():
        fail("安装目录或数据目录不存在")
    job_dir = (state / "updates" / JOB).resolve()
    if not within(state / "updates", job_dir) or not job_dir.is_dir():
        fail("任务目录越界或不存在")
    request = load_json(job_dir / "request.json", None)
    if not isinstance(request, dict) or request.get("job_id") != JOB:
        fail("request.json 缺失或任务 ID 不匹配")
    action = request.get("action")
    if action not in ("upgrade", "rollback", "workspace", "restore"):
        fail("不支持的动作")
    status_path = job_dir / "status.json"
    status = load_json(status_path, {}) or {}
    if not isinstance(status, dict):
        status = {}
    status.update({"id": JOB, "action": action, "current_version": read_version(prefix),
                   "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    log_path = job_dir / "helper.log"

    # 单实例锁：并发触发只允许一个助手真正执行。
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive_lock(state / "updates" / ".helper.lock", blocking=False))
        except BlockingIOError:
            fail("已有 root 升级助手在执行")

        def save(phase: str, message: str, progress: int | None = None, **extra) -> None:
            status.update({"phase": phase, "message": message,
                           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            if progress is not None:
                status["progress"] = max(0, min(100, int(progress)))
            status.update(extra)
            write_json(status_path, status)

        def log(line: str) -> None:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line.rstrip() + "\n")

        if action in ("workspace", "restore"):
            save("switching" if action == "workspace" else "restoring",
                 "root 助手已启动，准备切换工作目录" if action == "workspace"
                 else "root 助手已启动，准备覆盖恢复", 20)
            if action == "workspace":
                return run_workspace(job_dir, request, status, conf, prefix, state, save, log)
            return run_restore(job_dir, request, status, conf, prefix, state, save, log)

        save("backup" if action == "upgrade" else "rolling_back",
             "root 助手已启动，准备执行" if action == "upgrade" else "root 助手已启动，准备回滚",
             20)
        if action == "upgrade":
            package = job_dir / "package.tar.gz"
            expected_sha = str(request.get("package_sha256") or "").lower()
            if not package.is_file() or not expected_sha:
                save("failed", "升级包缺失或未登记 SHA256", 100, error="升级包缺失")
                fail("升级包缺失")
            digest = hashlib.sha256()
            with open(package, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected_sha:
                save("failed", "升级包 SHA256 校验失败", 100, error="升级包在 root 侧哈希不匹配")
                fail("SHA256 校验失败")
            # root 侧重新解压已校验的包，避免使用 Web 进程可写的解压目录。
            helper_release = job_dir / "helper-release"
            shutil.rmtree(helper_release, ignore_errors=True)
            required = {"run.py", "bbu/__init__.py", "web/index.html",
                        "vendor/yaml/__init__.py", "deploy/upgrade_offline.sh",
                        "deploy/rollback_offline.sh"}
            tops, found = set(), set()
            with tarfile.open(package, "r:gz") as tar:
                for member in tar.getmembers():
                    name = member.name
                    if name.startswith("/") or "\\" in name or ".." in Path(name).parts:
                        save("failed", "升级包路径非法", 100, error="路径穿越/绝对路径")
                        fail("升级包路径非法")
                    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                        save("failed", "升级包包含链接/特殊文件", 100, error="链接/特殊文件")
                        fail("升级包包含链接或特殊文件")
                    parts = Path(name).parts
                    tops.add(parts[0])
                    rel = "/".join(parts[1:])
                    if rel in required:
                        found.add(rel)
                if len(tops) != 1 or required - found:
                    save("failed", "升级包结构不完整", 100, error="缺少必需文件或顶层目录不唯一")
                    fail("升级包结构不完整")
                helper_release.mkdir(parents=True, exist_ok=True)
                tar.extractall(helper_release)
            source = helper_release / next(iter(tops))
            if not within(job_dir, source) or not (source / "deploy/upgrade_offline.sh").is_file():
                save("failed", "升级源目录非法", 100, error="升级源目录非法")
                fail("升级源目录非法")
            script = source / "deploy/upgrade_offline.sh"
            cmd = ["bash", str(script), "--prefix", str(prefix), "--state-dir", str(state),
                   "--service", str(conf.get("service_name") or "bbu-fault-platform"),
                   "--user", str(conf.get("service_user") or "bbu"),
                   "--health-url", str(conf.get("health_url") or "http://127.0.0.1:8080/api/health")]
            cwd = source
            target = str(request.get("target_version") or "")
            expected_phase = "succeeded"
        else:
            backup = Path(str(request.get("backup_dir") or "")).resolve()
            if not within(state / "backups", backup) or not (backup / "backup.json").is_file():
                save("failed", "回滚快照非法", 100, error="回滚快照不在受控目录")
                fail("回滚快照非法")
            script = prefix / "deploy/rollback_offline.sh"
            if not script.is_file():
                save("failed", "回滚脚本不存在", 100, error="回滚脚本不存在")
                fail("回滚脚本不存在")
            cmd = ["bash", str(script), "--backup", str(backup), "--with-state",
                   "--prefix", str(prefix), "--state-dir", str(state),
                   "--service", str(conf.get("service_name") or "bbu-fault-platform"),
                   "--user", str(conf.get("service_user") or "bbu"),
                   "--health-url", str(conf.get("health_url") or "http://127.0.0.1:8080/api/health")]
            cwd = prefix
            target = str(request.get("target_version") or "")
            expected_phase = "rolled_back"

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        helper_python = str(conf.get("python_bin") or "").strip()
        if helper_python:
            env["PYTHON_BIN"] = helper_python
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env)
        tail: list[str] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            tail.append(line)
            tail[:] = tail[-40:]
            log(line)
            if "备份" in line:
                m = re.search(r"(/[^\s]+/backups/[^\s]+)", line)
                if m:
                    path = Path(m.group(1))
                    status["backup_dir"] = str(path.parent if path.name == "state.tar.gz" else path)
                save("backup", "正在备份数据与旧代码", 25)
            elif "替换程序代码" in line:
                save("migrating", "正在替换代码并迁移数据库", 55)
            elif "初始化" in line or "迁移" in line:
                save("migrating", "正在执行幂等初始化与数据库迁移", 70)
            elif "升级快照" in line or "回滚" in line:
                save("restarting", "正在重启服务并检查健康状态", 85)
        code = proc.wait()
        status["log_tail"] = "\n".join(tail)
        if code != 0:
            save("failed", "升级/回滚脚本执行失败", 100,
                 error=("\n".join(tail[-8:]) or f"退出码 {code}"))
            return 1
        # 健康检查：服务重启后最多等 45 秒。
        health_url = str(conf.get("health_url") or "http://127.0.0.1:8080/api/health")
        version = ""
        for _ in range(30):
            try:
                with urllib.request.urlopen(health_url, timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                version = str(data.get("version") or "")
                if data.get("ok") and (not target or version == target):
                    break
            except Exception:
                pass
            time.sleep(1.5)
        else:
            save("failed", "健康检查超时或版本不匹配", 100,
                 error=f"健康检查未通过：期望 {target or '-'}，实际 {version or '无响应'}；可使用回滚入口恢复")
            return 1
        save("health_check", "健康检查通过，版本 " + (version or target), 95)
        status["package_sha256"] = request.get("package_sha256", "")
        save(expected_phase, "升级成功" if action == "upgrade" else "回滚成功", 100,
             target_version=version or target, error="")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
