"""通用工具：时间、ID、原子 JSON、文件安全、文本截断、哈希。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

CST = timezone(timedelta(hours=8))
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._\-]+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def local_display(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def new_id(prefix: str, with_time: bool = True, entropy: int = 3) -> str:
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S") if with_time else ""
    # 默认 3 字节沿用旧格式；知识库批量导入显式用 8 字节，避免短 ID 生日碰撞。
    rand = secrets.token_hex(max(1, min(16, int(entropy))))
    return f"{prefix}{stamp + '-' if stamp else ''}{rand}"


def safe_filename(name: str, fallback: str = "file") -> str:
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = name.replace("\x00", "")
    cleaned = _SAFE_NAME_RE.sub("_", name).lstrip(".")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > 180:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 12:
            cleaned = stem[:160] + "." + ext
        else:
            cleaned = cleaned[:180]
    return cleaned


def unique_path(directory: Path, filename: str) -> Path:
    """同名文件自动加序号，避免覆盖。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        return target
    stem, dot, ext = filename.rpartition(".")
    base = stem if dot else filename
    suffix = f".{ext}" if dot else ""
    for i in range(1, 10000):
        candidate = directory / f"{base}({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("同名文件过多")


def read_json(path: Path | str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path | str, data: Any, indent: int = 2) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def write_text(path: Path | str, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def sha256_file(path: Path | str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def human_size(num: int | float) -> str:
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def truncate(text: str, limit: int = 300) -> str:
    text = (text or "").replace("\r", "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def unique(seq: Iterable[Any]) -> list[Any]:
    seen: dict[Any, None] = {}
    for item in seq:
        if item not in seen:
            seen[item] = None
    return list(seen)


def try_rmtree(path: Path | str) -> None:
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def env_with_vendor(extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：把 vendor 目录放到 PYTHONPATH，保证 yaml 可用。"""
    from . import BASE_DIR, VENDOR_DIR

    env = os.environ.copy()
    parts = [str(VENDOR_DIR), str(BASE_DIR)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def within_directory(base: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve()
        base_resolved = base.resolve()
        return resolved == base_resolved or base_resolved in resolved.parents
    except OSError:
        return False


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
