"""子 Skill 执行沙箱：环境变量白名单、资源限制和 Python 审计钩子。

不是容器级隔离。Linux 上叠加 rlimit + Python audit hook；Windows 只提供
环境白名单/审计边界，明确要求外部导入技能必须走签名与审批。
"""
from __future__ import annotations

import json
import os
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Any

from . import BASE_DIR, VENDOR_DIR
from .util import env_with_vendor
from .tool_contract import ToolManifest

SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP",
}
SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "AUTH", "COOKIE", "CREDENTIAL")


def sanitized_env(manifest: ToolManifest, config: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in SAFE_ENV_KEYS:
        if key in os.environ:
            out[key] = os.environ[key]
    out["PYTHONPATH"] = os.pathsep.join([str(VENDOR_DIR), str(BASE_DIR)])
    out["PYTHONIOENCODING"] = "utf-8"
    out["PYTHONUNBUFFERED"] = "1"
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    # 不复制 os.environ 中的任何业务配置或个人凭证。
    return out


def _uid_thread_count() -> int:
    """RLIMIT_NPROC 统计同一 uid 的线程；按当前占用加预算，避免误伤宿主进程。"""
    try:
        uid = os.getuid()
    except AttributeError:
        return 0
    total = 0
    proc = Path("/proc")
    if not proc.exists():
        return 0
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            status = (item / "status").read_text(encoding="utf-8", errors="ignore")
            fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
            if int((fields.get("Uid") or str(uid)).split()[0]) != uid:
                continue
            total += int((fields.get("Threads") or "1").strip())
        except (OSError, ValueError):
            continue
    return total


def _rlimit_preexec(manifest: ToolManifest, config: Any):
    def _apply() -> None:  # pragma: no cover - 由子进程执行
        try:
            import resource
        except ImportError:
            return
        limits = [
            (resource.RLIMIT_CORE, 0, 0),
            (resource.RLIMIT_CPU, max(1, int(config.get("tool_sandbox_cpu_sec", 600))),
             max(1, int(config.get("tool_sandbox_cpu_sec", 600)))),
            (resource.RLIMIT_AS,
             max(64, int(config.get("tool_sandbox_memory_mb", 2048))) * 1024 * 1024,
             max(64, int(config.get("tool_sandbox_memory_mb", 2048))) * 1024 * 1024),
            (resource.RLIMIT_FSIZE,
             max(1, int(config.get("tool_sandbox_max_output_mb", 128))) * 1024 * 1024,
             max(1, int(config.get("tool_sandbox_max_output_mb", 128))) * 1024 * 1024),
            (resource.RLIMIT_NOFILE, 256, 256),
            (resource.RLIMIT_NPROC,
             max(256, _uid_thread_count() + int(config.get("tool_sandbox_max_children", 32))),
             max(256, _uid_thread_count() + int(config.get("tool_sandbox_max_children", 32)))),
        ]
        for which, soft, hard in limits:
            try:
                resource.setrlimit(which, (soft, hard))
            except (OSError, ValueError):
                pass
    return _apply


def _runtime_read_roots() -> list[str]:
    roots = [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
             str(Path(__file__).resolve().parent.parent)]
    paths = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        if paths.get(key):
            roots.append(paths[key])
    for extra in ("/usr/share/zoneinfo", "/etc/localtime", "/dev/null",
                  "/dev/zero", "/dev/urandom", "/dev/random", "/proc/self"):
        if Path(extra).exists():
            roots.append(extra)
    return roots


def sandbox_policy(manifest: ToolManifest, out_dir: Path, inputs: list[Path],
                   config: Any) -> dict[str, Any]:
    out_root = out_dir.resolve()
    tmp_dir = (out_root / ".sandbox-tmp").resolve()
    return {
        "out_dir": str(out_root),
        "tmp_dir": str(tmp_dir),
        "write_roots": [str(out_root), str(tmp_dir)],
        "read_roots": [str(Path(config.data_dir).resolve()),
                       str(Path(config.skills_dir).resolve()),
                       str(Path(config.cases_dir).resolve()),
                       str(BASE_DIR.resolve()), str(VENDOR_DIR.resolve())]
                      + _runtime_read_roots()
                      + [str(p.resolve()) for p in inputs],
        "input_paths": [str(p.resolve()) for p in inputs],
        "network": manifest.network,
        "allow_subprocess": bool(manifest.process.get("allow_subprocess")),
        "executables": manifest.process.get("executables") or [],
        "allow_ctypes": False,
        "blocked_names": ["master.key", "config.json", ".env", "id_rsa", "id_ed25519",
                          "credentials", "secrets.json"],
        "max_output_bytes": max(1, int(config.get("tool_sandbox_max_output_mb", 128))) * 1024 * 1024,
        "mode": str(config.get("tool_sandbox_mode", "audit")),
    }


def build_command(entry: Path, args: list[str], manifest: ToolManifest, out_dir: Path,
                  inputs: list[Path], config: Any) -> tuple[list[str], dict[str, str], Any | None]:
    mode = str(config.get("tool_sandbox_mode", "audit")).lower()
    if mode not in ("audit", "strict"):
        return ([sys.executable, str(entry), *args], env_with_vendor(), None)
    policy = sandbox_policy(manifest, out_dir, inputs, config)
    env = sanitized_env(manifest, config)
    tmp_dir = Path(policy["tmp_dir"])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmp_dir)
    env["TEMP"] = str(tmp_dir)
    env["TMP"] = str(tmp_dir)
    env["BBU_SANDBOX_POLICY"] = json.dumps(policy, ensure_ascii=False)
    cmd = [sys.executable, "-m", "bbu.sandbox_bootstrap", str(entry), *args]
    preexec = _rlimit_preexec(manifest, config)
    return cmd, env, preexec


def output_limit_bytes(config: Any) -> int:
    return max(1, int(config.get("tool_sandbox_max_output_mb", 128))) * 1024 * 1024
