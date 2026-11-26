"""子进程沙箱引导：读取策略、安装审计钩子后运行技能入口。

这是 Python 层边界，不等同于容器或 seccomp。它阻断 Python socket、敏感文件
读取、越界写和未授权子进程；宿主机仍应使用独立账号/容器做最终隔离。
"""
from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any

ALLOWED_EXECUTABLES = {
    "python", "python3", "python3.11", "python3.12", "python3.13",
    "tar", "bsdtar", "gzip", "bzip2", "xz", "unzip", "zip", "7z", "7za",
    "zstd", "find", "head", "tail", "wc", "cat", "sed", "awk",
}
WRITE_EVENTS = {
    "os.remove", "os.rename", "os.replace", "os.rmdir", "os.mkdir",
    "os.chmod", "os.chown", "os.truncate", "os.utime",
}
NETWORK_EVENTS = {
    "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
    "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg",
}


def _within(path: str, root: str) -> bool:
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
        return p == r or r in p.parents
    except OSError:
        return False


def _install(policy: dict[str, Any]) -> None:
    out_dir = str(policy.get("out_dir") or "")
    write_roots = [str(x) for x in (policy.get("write_roots") or [out_dir])]
    read_roots = [str(x) for x in (policy.get("read_roots") or [])]
    blocked_names = {str(x).lower() for x in (policy.get("blocked_names") or [])}
    network = str(policy.get("network") or "none")
    allow_subprocess = bool(policy.get("allow_subprocess"))
    allowed_execs = set(ALLOWED_EXECUTABLES)
    for item in policy.get("executables") or []:
        allowed_execs.add(Path(str(item)).name.lower())
    allowed_execs.add(Path(sys.executable).name.lower())
    allow_ctypes = bool(policy.get("allow_ctypes"))

    def _deny(kind: str, detail: str) -> None:
        raise PermissionError(f"沙箱拒绝 {kind}: {detail}")

    def _check_path(path: Any, write: bool) -> None:
        if not path:
            return
        text = str(path)
        name = Path(text).name.lower()
        if name in blocked_names:
            _deny("sensitive_file", text)
        if write and write_roots and not any(_within(text, root) for root in write_roots):
            _deny("write_outside_output", text)
        if not write and read_roots and os.path.isabs(text):
            if not any(_within(text, root) for root in read_roots):
                _deny("read_outside_roots", text)

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event in NETWORK_EVENTS and network == "none":
            _deny("network", event)
        if event in ("ctypes.dlopen", "ctypes.dlsym") and not allow_ctypes:
            _deny("ctypes", str(args[0] if args else ""))
        if event in ("os.system", "os.exec", "os.posix_spawn", "os.spawn"):
            if not allow_subprocess:
                _deny("subprocess", event)
            executable = Path(str(args[0] if args else "")).name.lower()
            if executable and executable not in allowed_execs:
                _deny("executable", executable)
        if event == "subprocess.Popen":
            if not allow_subprocess:
                _deny("subprocess", "Popen")
            executable = ""
            if args:
                executable = Path(str(args[0] or "")).name.lower()
            if not executable and len(args) > 1 and isinstance(args[1], (list, tuple)):
                executable = Path(str(args[1][0] if args[1] else "")).name.lower()
            if executable and executable not in allowed_execs:
                _deny("executable", executable)
        if event == "open" and args:
            path = args[0]
            mode = str(args[1] or "")
            flags = int(args[2] or 0) if len(args) > 2 and isinstance(args[2], int) else 0
            write = any(ch in mode for ch in "wax+") or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC))
            if isinstance(path, (str, bytes, os.PathLike)):
                _check_path(path, write)
        if event in WRITE_EVENTS and args:
            paths = [x for x in args if isinstance(x, (str, bytes, os.PathLike))]
            for item in paths[:2]:
                _check_path(item, True)

    sys.addaudithook(hook)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("sandbox_bootstrap: 缺少技能入口")
    try:
        policy = json.loads(os.environ.get("BBU_SANDBOX_POLICY") or "{}")
    except json.JSONDecodeError:
        raise SystemExit("sandbox_bootstrap: 沙箱策略不是合法 JSON")
    if not isinstance(policy, dict) or not policy.get("out_dir"):
        raise SystemExit("sandbox_bootstrap: 沙箱策略缺少 out_dir")
    entry = Path(sys.argv[1]).resolve()
    if not entry.is_file():
        raise SystemExit(f"sandbox_bootstrap: 入口不存在 {entry}")
    sys.argv = [str(entry), *sys.argv[2:]]
    _install(policy)
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
