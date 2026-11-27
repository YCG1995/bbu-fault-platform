"""系统资源快照：CPU、内存、当前进程内存与数据盘。

只使用标准库。Linux 读取 ``/proc``；Windows 使用 ``ctypes`` 的
``GetSystemTimes`` / ``GlobalMemoryStatusEx`` / ``GetProcessMemoryInfo``；
其它平台返回可用的子集，不因为某个指标不可读而让 API 失败。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROCESS_START = time.time()
_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None
_CACHE_AT = 0.0
_CACHE_PATH = ""
_CACHE_TTL_SEC = 1.2
_LAST_CPU: tuple[int, int, float] | None = None
_LAST_WIN_CPU: tuple[int, int, float] | None = None


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return ""


def _parse_cpu_line(text: str) -> tuple[int, int] | None:
    """返回 (total_jiffies, idle_jiffies)；解析失败返回 None。"""
    parts = text.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        # user nice system idle iowait irq softirq steal；guest 已计入 user。
        values = [int(x) for x in parts[1:9]]
    except (TypeError, ValueError):
        return None
    if len(values) < 4:
        return None
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def _linux_cpu_percent() -> float | None:
    global _LAST_CPU
    line = _read_text("/proc/stat").splitlines()
    sample = _parse_cpu_line(line[0]) if line else None
    if not sample:
        return None
    now = time.monotonic()
    previous = _LAST_CPU
    if previous is not None and now - previous[2] < 0.05:
        time.sleep(0.05)
        line = _read_text("/proc/stat").splitlines()
        current = _parse_cpu_line(line[0]) if line else None
        if current:
            sample = current
        now = time.monotonic()
    if previous is None:
        _LAST_CPU = (sample[0], sample[1], now)
        # 首次请求做一次很短的采样窗口；之后使用 1.2 秒快照缓存。
        time.sleep(0.06)
        line = _read_text("/proc/stat").splitlines()
        current = _parse_cpu_line(line[0]) if line else None
        if not current:
            return None
        previous = _LAST_CPU
        now = time.monotonic()
        sample = current
    total_delta = sample[0] - previous[0]
    idle_delta = sample[1] - previous[1]
    _LAST_CPU = (sample[0], sample[1], now)
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))


def _windows_cpu_percent() -> float | None:
    global _LAST_WIN_CPU
    if sys.platform != "win32":
        return None
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                        ("dwHighDateTime", ctypes.c_ulong)]

        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                       ctypes.byref(user)):
            return None
        idle_100 = idle.dwLowDateTime + (idle.dwHighDateTime << 32)
        kernel_100 = kernel.dwLowDateTime + (kernel.dwHighDateTime << 32)
        user_100 = user.dwLowDateTime + (user.dwHighDateTime << 32)
        # kernel 包含 idle；总时间 = kernel + user。
        total = kernel_100 + user_100
        now = time.monotonic()
        previous = _LAST_WIN_CPU
        if previous is None:
            _LAST_WIN_CPU = (total, idle_100, now)
            time.sleep(0.06)
            if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                           ctypes.byref(user)):
                return None
            idle_100 = idle.dwLowDateTime + (idle.dwHighDateTime << 32)
            kernel_100 = kernel.dwLowDateTime + (kernel.dwHighDateTime << 32)
            user_100 = user.dwLowDateTime + (user.dwHighDateTime << 32)
            total = kernel_100 + user_100
            previous = _LAST_WIN_CPU
            now = time.monotonic()
        total_delta = total - previous[0]
        idle_delta = idle_100 - previous[1]
        _LAST_WIN_CPU = (total, idle_100, now)
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))
    except Exception:  # noqa: BLE001 - 资源采样失败不能影响业务接口
        return None


def _cpu_snapshot() -> dict[str, Any]:
    cores = int(os.cpu_count() or 1)
    load_1m: float | None = None
    try:
        if hasattr(os, "getloadavg"):
            load_1m = round(float(os.getloadavg()[0]), 2)
    except (OSError, AttributeError, ValueError):
        load_1m = None
    percent = _linux_cpu_percent() if sys.platform.startswith("linux") else _windows_cpu_percent()
    source = "proc" if sys.platform.startswith("linux") else "os" if percent is not None else "unavailable"
    return {
        "percent": round(percent, 1) if percent is not None else None,
        "cores": cores,
        "load_1m": load_1m,
        "source": source,
    }


def _linux_memory() -> dict[str, int] | None:
    values: dict[str, int] = {}
    for line in _read_text("/proc/meminfo").splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2:
            try:
                values[parts[0]] = int(parts[1]) * 1024
            except ValueError:
                continue
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable")
    if available is None:
        available = (values.get("MemFree", 0) + values.get("Buffers", 0)
                     + values.get("Cached", 0))
    if not total:
        return None
    available = max(0, min(total, available))
    return {"total_bytes": total, "available_bytes": available,
            "used_bytes": max(0, total - available)}


def _windows_memory() -> dict[str, int] | None:
    if sys.platform != "win32":
        return None
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        total = int(status.ullTotalPhys)
        available = int(status.ullAvailPhys)
        return {"total_bytes": total, "available_bytes": available,
                "used_bytes": max(0, total - available)}
    except Exception:  # noqa: BLE001
        return None


def _fallback_memory() -> dict[str, int] | None:
    try:
        page = int(os.sysconf("SC_PAGE_SIZE"))
        total = page * int(os.sysconf("SC_PHYS_PAGES"))
        available = page * int(os.sysconf("SC_AVPHYS_PAGES"))
        if total:
            available = max(0, min(total, available))
            return {"total_bytes": total, "available_bytes": available,
                    "used_bytes": total - available}
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _memory_snapshot() -> dict[str, Any] | None:
    if sys.platform.startswith("linux"):
        memory = _linux_memory()
    elif sys.platform == "win32":
        memory = _windows_memory()
    else:
        memory = _fallback_memory()
    if not memory:
        return None
    total = memory["total_bytes"]
    used = memory["used_bytes"]
    return {**memory, "percent": round(100.0 * used / total, 1) if total else None}


def _linux_process_rss() -> int | None:
    for line in _read_text("/proc/self/status").splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1]) * 1024
                except ValueError:
                    return None
    return None


def _windows_process_rss() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong),
                        ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters),
                ctypes.sizeof(counters)):
            return None
        return int(counters.WorkingSetSize)
    except Exception:  # noqa: BLE001
        return None


def _process_snapshot(memory: dict[str, Any] | None) -> dict[str, Any]:
    if sys.platform.startswith("linux"):
        rss = _linux_process_rss()
    elif sys.platform == "win32":
        rss = _windows_process_rss()
    else:
        try:
            import resource
            rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
        except (ImportError, ValueError):
            rss = None
    threads = None
    for line in _read_text("/proc/self/status").splitlines():
        if line.startswith("Threads:"):
            try:
                threads = int(line.split()[1])
            except (IndexError, ValueError):
                threads = None
            break
    total = int((memory or {}).get("total_bytes") or 0)
    return {
        "pid": os.getpid(),
        "rss_bytes": int(rss or 0),
        "percent_of_total": round(100.0 * int(rss or 0) / total, 1) if total and rss else None,
        "threads": threads,
        "uptime_sec": round(time.time() - _PROCESS_START, 1),
    }


def _disk_snapshot(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        usage = shutil.disk_usage(str(path))
    except (OSError, ValueError):
        return None
    total = int(usage.total)
    return {
        "path": str(path),
        "total_bytes": total,
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
        "percent": round(100.0 * int(usage.used) / total, 1) if total else None,
    }


def snapshot(data_dir: str | Path | None = None) -> dict[str, Any]:
    """返回带 1.2 秒进程内缓存的系统资源快照。"""
    global _CACHE, _CACHE_AT, _CACHE_PATH
    path = str(data_dir or "")
    now = time.monotonic()
    with _LOCK:
        if (_CACHE is not None and _CACHE_PATH == path
                and now - _CACHE_AT < _CACHE_TTL_SEC):
            return dict(_CACHE)
        try:
            cpu = _cpu_snapshot()
            memory = _memory_snapshot()
            process = _process_snapshot(memory)
            disk = _disk_snapshot(data_dir)
            result = {
                "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "cpu": cpu,
                "memory": memory,
                "process": process,
                "disk": disk,
            }
        except Exception:  # noqa: BLE001 - 资源面板是增强信息，不能拖垮主接口
            result = {
                "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "cpu": {"percent": None, "cores": int(os.cpu_count() or 1),
                        "load_1m": None, "source": "unavailable"},
                "memory": None,
                "process": {"pid": os.getpid(), "rss_bytes": 0,
                           "percent_of_total": None, "threads": None,
                           "uptime_sec": round(time.time() - _PROCESS_START, 1)},
                "disk": None,
            }
        _CACHE = result
        _CACHE_AT = time.monotonic()
        _CACHE_PATH = path
        return dict(result)



if sys.platform.startswith("linux"):
    try:
        _lines = _read_text("/proc/stat").splitlines()
        _sample = _parse_cpu_line(_lines[0]) if _lines else None
        if _sample:
            _LAST_CPU = (_sample[0], _sample[1], time.monotonic())
    except Exception:  # noqa: BLE001
        pass
