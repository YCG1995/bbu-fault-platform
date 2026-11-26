"""上传落盘与安全解压（zip/tar/gz，防目录穿越、压缩炸弹、符号链接）。"""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from .util import safe_filename, sha256_file, unique_path, within_directory


class ExtractLimit(Exception):
    pass


def save_upload(src_stream, dest_dir: Path, filename: str, max_bytes: int) -> dict[str, Any]:
    """流式保存上传文件；返回元数据。src_stream 需支持 read(n)。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    target = unique_path(dest_dir, name)
    size = 0
    h = __import__("hashlib").sha256()
    with open(target, "wb") as fh:
        while True:
            chunk = src_stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                fh.close()
                target.unlink(missing_ok=True)
                raise ExtractLimit(f"文件超过上限 {max_bytes // 1024 // 1024} MB")
            h.update(chunk)
            fh.write(chunk)
    return {"name": filename, "stored_name": target.name, "stored_path": str(target),
            "size": size, "sha256": h.hexdigest()}


def extract_archive(path: Path, dest: Path, limits: dict[str, int],
                    depth: int = 0) -> dict[str, Any]:
    """解压单个归档到 dest；返回统计。支持 zip/tar/tar.gz、gzip/bz2 单文件。"""
    stats = {"files": 0, "bytes": 0, "skipped": [], "archives": []}
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(path):
        _extract_zip(path, dest, limits, stats, depth)
    elif tarfile.is_tarfile(path):
        _extract_tar(path, dest, limits, stats, depth)
    elif path.suffix.lower() in (".gz", ".bz2") and not tarfile.is_tarfile(path):
        _extract_single_compressed(path, dest, limits, stats)
    elif path.suffix.lower() == ".xz" and not tarfile.is_tarfile(path):
        _extract_lzma(path, dest, limits, stats, depth)
    elif path.suffix.lower() == ".zst":
        _extract_zstd(path, dest, limits, stats, depth)
    elif path.suffix.lower() in (".7z", ".rar"):
        _extract_7z(path, dest, limits, stats, depth)
    elif path.suffix.lower() in (".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst"):
        # 已声明为归档但无法识别：视为损坏/伪装文件，不能当普通日志放行。
        stats["skipped"].append(f"{path.name}: 归档损坏或格式不受支持")
    else:
        shutil.copy2(path, dest / safe_filename(path.name))
        stats["files"] += 1
        stats["bytes"] += path.stat().st_size
    return stats


def _budget_ok(limits: dict[str, int], stats: dict[str, Any], add_files: int, add_bytes: int) -> None:
    if stats["files"] + add_files > int(limits.get("max_files", 200000)):
        raise ExtractLimit("解压文件数超过上限")
    if stats["bytes"] + add_bytes > int(limits.get("max_bytes", 8 * 1024 ** 3)):
        raise ExtractLimit("解压总大小超过上限")


def _extract_zip(path: Path, dest: Path, limits: dict[str, int],
                 stats: dict[str, Any], depth: int) -> None:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            _budget_ok(limits, stats, 1, info.file_size)
            target = _safe_member_path(dest, info.filename)
            if target is None:
                stats["skipped"].append(info.filename)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
            except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
                stats["skipped"].append(f"{info.filename}: {exc}")
                target.unlink(missing_ok=True)
                continue
            stats["files"] += 1
            stats["bytes"] += info.file_size
            _maybe_nested(target, limits, stats, depth)


def _extract_tar(path: Path, dest: Path, limits: dict[str, int],
                 stats: dict[str, Any], depth: int) -> None:
    with tarfile.open(path) as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            _budget_ok(limits, stats, 1, member.size)
            target = _safe_member_path(dest, member.name)
            if target is None:
                stats["skipped"].append(member.name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fh = tf.extractfile(member)
            if not fh:
                continue
            with fh, open(target, "wb") as out:
                shutil.copyfileobj(fh, out, 1024 * 1024)
            stats["files"] += 1
            stats["bytes"] += member.size
            _maybe_nested(target, limits, stats, depth)


def _extract_single_compressed(path: Path, dest: Path, limits: dict[str, int],
                               stats: dict[str, Any]) -> None:
    opener = gzip.open if path.suffix.lower() == ".gz" else bz2.open
    target = dest / safe_filename(path.name[:-3] if path.suffix else path.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with opener(path, "rb") as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                _budget_ok(limits, stats, 0, len(chunk))
                out.write(chunk)
    except (OSError, EOFError) as exc:
        target.unlink(missing_ok=True)
        stats["skipped"].append(f"{path.name}: {exc}")
        return
    stats["files"] += 1
    stats["bytes"] += size


def _extract_lzma(path: Path, dest: Path, limits: dict[str, int],
                  stats: dict[str, Any], depth: int = 0) -> None:
    target = dest / safe_filename(path.name[:-3] if path.suffix else path.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with lzma.open(path, "rb") as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                _budget_ok(limits, stats, 0, len(chunk))
                out.write(chunk)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        target.unlink(missing_ok=True)
        stats["skipped"].append(f"{path.name}: {exc}")
        return
    stats["files"] += 1
    stats["bytes"] += size
    _maybe_nested(target, limits, stats, depth + 1)


def _extract_zstd(path: Path, dest: Path, limits: dict[str, int],
                  stats: dict[str, Any], depth: int = 0) -> None:
    target = dest / safe_filename(path.name[:-4] if path.suffix else path.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            import zstandard  # type: ignore
        except ImportError:
            zstandard = None
        if zstandard is not None:
            with open(path, "rb") as src, open(target, "wb") as out:
                zstandard.ZstdDecompressor().copy_stream(src, out)
        else:
            tool = shutil.which("zstd") or shutil.which("unzstd")
            if not tool:
                stats["skipped"].append(f"{path.name}: 未安装 zstd 解码器")
                return
            with open(target, "wb") as out:
                proc = subprocess.run([tool, "-d", "-c", str(path)], stdout=out,
                                      capture_output=True, timeout=600)
            if proc.returncode != 0:
                target.unlink(missing_ok=True)
                stats["skipped"].append(
                    f"{path.name}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
                return
        size = target.stat().st_size
        _budget_ok(limits, stats, 1, size)
        stats["files"] += 1
        stats["bytes"] += size
        _maybe_nested(target, limits, stats, depth + 1)
    except Exception as exc:  # 解码库异常不应中断其他输入
        target.unlink(missing_ok=True)
        stats["skipped"].append(f"{path.name}: {exc}")


def _extract_7z(path: Path, dest: Path, limits: dict[str, int],
                stats: dict[str, Any], depth: int = 0) -> None:
    try:
        import py7zr  # type: ignore
    except ImportError:
        py7zr = None
    if py7zr is not None:
        before = {p for p in dest.rglob("*") if p.is_file()}
        try:
            with py7zr.SevenZipFile(path, mode="r") as archive:
                archive.extractall(path=dest)
        except Exception as exc:
            stats["skipped"].append(f"{path.name}: {exc}")
            return
        added = [p for p in dest.rglob("*") if p.is_file() and p not in before]
        _budget_ok(limits, stats, len(added), sum(p.stat().st_size for p in added))
        for item in added:
            stats["files"] += 1
            stats["bytes"] += item.stat().st_size
            _maybe_nested(item, limits, stats, depth + 1)
        return
    tool = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    if not tool:
        stats["skipped"].append(f"{path.name}: 未安装 7z")
        return
    before = {p for p in dest.rglob("*") if p.is_file()}
    try:
        proc = subprocess.run([tool, "x", "-y", f"-o{dest}", str(path)],
                              capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        stats["skipped"].append(f"{path.name}: {exc}")
        return
    if proc.returncode != 0:
        stats["skipped"].append(f"{path.name}: {proc.stderr.decode('utf-8', 'replace')[:200]}")
        return
    added = [p for p in dest.rglob("*") if p.is_file() and p not in before]
    _budget_ok(limits, stats, len(added), sum(p.stat().st_size for p in added))
    for item in added:
        stats["files"] += 1
        stats["bytes"] += item.stat().st_size
        _maybe_nested(item, limits, stats, depth + 1)


def _maybe_nested(target: Path, limits: dict[str, int], stats: dict[str, Any], depth: int) -> None:
    if depth >= int(limits.get("max_depth", 3)):
        return
    try:
        suffix = target.suffix.lower()
        if (zipfile.is_zipfile(target) or tarfile.is_tarfile(target)
                or suffix in (".gz", ".bz2", ".xz", ".zst", ".7z", ".rar")):
            nested_dest = target.parent / (target.name + ".extracted")
            stats["archives"].append(str(target))
            sub = extract_archive(target, nested_dest, limits, depth + 1)
            stats["files"] += sub["files"]
            stats["bytes"] += sub["bytes"]
            stats["skipped"].extend(sub["skipped"])
            stats["archives"].extend(sub["archives"])
    except (ExtractLimit, OSError, zipfile.BadZipFile, tarfile.TarError):
        return


def _safe_member_path(dest: Path, member: str) -> Path | None:
    raw = member.replace("\\", "/")
    # 绝对路径、盘符路径、UNC 路径一律拒绝；不能先 lstrip 再判断。
    if raw.startswith("/") or raw.startswith("//") or (len(raw) >= 2 and raw[1] == ":"):
        return None
    member = raw.lstrip("/")
    if not member or member.startswith("../") or "/../" in member:
        return None
    target = (dest / member).resolve()
    if not within_directory(dest, target):
        return None
    return target


def extract_inputs(inputs: list[Path], dest: Path, limits: dict[str, int]) -> dict[str, Any]:
    total = {"files": 0, "bytes": 0, "skipped": [], "archives": [], "sources": []}
    for path in inputs:
        sub = extract_archive(path, dest, limits)
        total["sources"].append({"name": path.name, "files": sub["files"], "bytes": sub["bytes"]})
        total["files"] += sub["files"]
        total["bytes"] += sub["bytes"]
        total["skipped"].extend(sub["skipped"])
        total["archives"].extend(sub["archives"])
    return total


def sample_text_files(root: Path, max_files: int = 6, max_lines: int = 40,
                      max_chars: int = 6000) -> list[dict[str, Any]]:
    """给工坊 AI 的样例行：挑小文本文件，截断，避免把几十 MB 日志塞进 prompt。"""
    samples: list[dict[str, Any]] = []
    budget = max_chars
    candidates = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".log", ".txt", ".csv", ".ini", ".cfg", ".out"):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 8 * 1024 * 1024:
            continue
        candidates.append((size, path))
    candidates.sort()
    for size, path in candidates:
        if len(samples) >= max_files or budget <= 200:
            break
        try:
            lines = []
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for idx, line in enumerate(fh):
                    if idx >= max_lines:
                        break
                    lines.append(line.rstrip("\n")[:200])
        except OSError:
            continue
        text = "\n".join(lines)
        if len(text) > budget:
            text = text[:budget]
        samples.append({"file": str(path.relative_to(root)).replace("\\", "/"),
                        "size": size, "lines": text})
        budget -= len(text)
    return samples
