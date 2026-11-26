#!/usr/bin/env python3
"""BBU 故障诊断平台入口。

用法：
    python3 run.py                 # 默认 0.0.0.0:8080
    python3 run.py --port 9000
    python3 run.py --host 127.0.0.1
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
from pathlib import Path

from bbu import BASE_DIR, __version__

sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "vendor"))

from bbu.api import App  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.httpd import AppServer  # noqa: E402
from bbu.startup import apply_pending  # noqa: E402


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def main() -> int:
    parser = argparse.ArgumentParser(description="BBU 故障诊断平台")
    parser.add_argument("--host", default=None, help="监听地址（默认取 config.json）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8080）")
    parser.add_argument("--init-only", action="store_true", help="只初始化数据库与目录，不启动服务")
    args = parser.parse_args()

    applied = apply_pending(BASE_DIR)
    if applied:
        if applied.get("ok"):
            print(f"[维护] 已应用待激活任务：{applied.get('action')} {applied.get('job_id')}")
        else:
            print(f"[维护] 待激活任务失败，继续使用原数据：{applied.get('error', '')}", file=sys.stderr)
    if applied and applied.get("ok") and applied.get("action") == "update":
        # 代码刚被替换，当前进程仍持有旧模块；重新执行入口以加载新版本。
        print("[更新] 程序文件已替换，正在重新启动以加载新版本…")
        sys.stdout.flush()
        try:
            os.execv(sys.executable, [sys.executable, str(BASE_DIR / "run.py"), *sys.argv[1:]])
        except OSError as exc:
            print(f"[错误] 无法重新启动新版本：{exc}", file=sys.stderr)
            return 3

    stopping = threading.Event()
    server_ref = [None]

    def _stop(signum, frame):  # noqa: ARG001
        if stopping.is_set():
            return
        stopping.set()
        print("\n[停止] 正在关闭服务…")
        if server_ref[0] is not None:
            threading.Thread(target=server_ref[0].shutdown, daemon=True).start()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _stop)
        except (OSError, ValueError):
            # Windows 个别 Python 发行版/受限环境不支持注册该信号。
            pass

    while True:
        config = Config()
        host = args.host or str(config.get("host"))
        port = args.port or int(config.get("port"))
        app = App(config)

        print("=" * 66)
        print(f"  {config.get('brand')}  v{__version__}")
        print("=" * 66)
        print(f"  数据目录 : {config.data_dir}")
        print(f"  技能目录 : {config.skills_dir}")
        print(f"  案例目录 : {config.cases_dir}")
        print(f"  元 Skill : {config.meta_skill_dir}")
        print(f"  工作线程 : {app.worker.max_workers}  | 单任务技能并行 : {config.get('max_skill_parallel')}")
        if app.admin_password:
            print("-" * 66)
            print("  [首次启动] 已创建管理员账号")
            print("    用户名: admin")
            print(f"    密码  : {app.admin_password}")
            print(f"    密码同时写入: {config.data_dir / '初始管理员密码.txt'}")
            print("    首次登录后请立即修改密码。")
        print("=" * 66)

        if args.init_only:
            print("初始化完成（--init-only，不启动服务）")
            return 0

        if not _port_free(host, port):
            print(f"[错误] {host}:{port} 已被占用，请用 --port 指定其它端口", file=sys.stderr)
            return 2

        server = AppServer((host, port), app)
        app.http_server = server
        server_ref[0] = server
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        print(f"  访问地址 : http://{display_host}:{port}/")
        print("=" * 66)

        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            app.shutdown_services()
            server.server_close()
            server_ref[0] = None
        if app.restart_event.is_set() and not stopping.is_set():
            print("[重启] 正在重新加载配置并装配服务…")
            stopping.wait(0.4)
            continue
        break
    print("[停止] 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
