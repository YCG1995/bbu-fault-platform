"""跨平台进程文件锁。

Linux 使用 ``fcntl.flock``，Windows 使用 ``msvcrt.locking``。Windows 没有
``fcntl``，因此任何模块都不能在顶层直接 import 它。
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - 极端精简的 Windows Python
        msvcrt = None  # type: ignore[assignment]
else:
    msvcrt = None  # type: ignore[assignment]

try:
    import fcntl
except ImportError:  # Windows 正常没有 fcntl
    fcntl = None  # type: ignore[assignment]


def _windows_lock_byte(fh: BinaryIO) -> None:
    """Windows 的锁针对字节区间，空文件先补一个字节。"""
    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"\0")
        fh.flush()
    fh.seek(0)


def _try_lock(fh: BinaryIO) -> None:
    if IS_WINDOWS:
        if msvcrt is None:
            raise RuntimeError("当前 Windows Python 缺少 msvcrt，无法创建进程锁")
        _windows_lock_byte(fh)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError("文件已被其他进程锁定") from exc
        return
    if fcntl is None:
        raise RuntimeError("当前 Python 缺少 fcntl，无法创建进程锁")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise
    except OSError as exc:
        raise BlockingIOError("文件已被其他进程锁定") from exc


def _unlock(fh: BinaryIO) -> None:
    if IS_WINDOWS:
        if msvcrt is None:
            return
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    if fcntl is not None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def exclusive_lock(path: str | Path, blocking: bool = True,
                   timeout: float | None = None, create: bool = True) -> Iterator[Path]:
    """获取路径对应的独占文件锁，退出上下文时释放。

    blocking=False 时若锁被占用立即抛出 ``BlockingIOError``；timeout 仅对
    阻塞模式生效。锁文件不存在时默认创建。
    """
    lock_path = Path(path)
    if create:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    acquired = False
    try:
        while True:
            try:
                _try_lock(fh)
                acquired = True
                break
            except BlockingIOError:
                if not blocking:
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"等待文件锁超时：{lock_path}")
                time.sleep(0.1)
        yield lock_path
    finally:
        if acquired:
            _unlock(fh)
        fh.close()
