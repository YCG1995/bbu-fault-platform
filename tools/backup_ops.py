"""O10 每日/每周备份、校验与恢复演练（仅标准库）。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REQUIRED_TABLES = ("tasks", "users", "skills")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_snapshot(source: Path, target: Path) -> None:
    if not source.is_file():
        raise ValueError(f"数据库不存在：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        dst.execute("PRAGMA integrity_check")
    finally:
        dst.close()
        src.close()


def verify_database(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(str(path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise ValueError(f"数据库完整性检查失败：{result}")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    finally:
        conn.close()
    return {"ok": True, "integrity": "ok", "tables": tables}


def create_backup(state_dir: Path, out: Path, kind: str = "daily") -> dict[str, Any]:
    state_dir = Path(state_dir).resolve()
    out = Path(out).resolve()
    db_path = state_dir / "data" / "bbu.db"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="bbu-backup-"))
    try:
        snapshot = tmp / "bbu.db"
        sqlite_snapshot(db_path, snapshot)
        verify_database(snapshot)
        meta = {"kind": kind, "created_at": datetime.now(timezone.utc).isoformat(),
                "state_dir": str(state_dir), "database_bytes": snapshot.stat().st_size}
        (tmp / "backup.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        with tarfile.open(out, "w:gz") as tf:
            tf.add(snapshot, arcname="data/bbu.db")
            for name in ("skills", "cases"):
                root = state_dir / name
                if not root.exists():
                    continue
                for path in root.rglob("*"):
                    if not path.is_file() or path.is_symlink() or path.name == "master.key":
                        continue
                    if any(x in path.parts for x in ("input", "real_cases", "uploads")):
                        continue
                    tf.add(path, arcname=str(path.relative_to(state_dir)))
            tf.add(tmp / "backup.json", arcname="backup.json")
        digest = sha256_file(out)
        (out.parent / (out.name + ".sha256")).write_text(
            f"{digest}  {out.name}\n", encoding="utf-8")
        return {"ok": True, "path": str(out), "sha256": digest, "kind": kind,
                "bytes": out.stat().st_size}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _meta_from_tar(path: Path) -> dict[str, Any]:
    with tarfile.open(path, "r:gz") as tf:
        try:
            member = tf.getmember("backup.json")
        except KeyError as exc:
            raise ValueError("备份缺少 backup.json") from exc
        fh = tf.extractfile(member)
        if fh is None:
            raise ValueError("无法读取 backup.json")
        return json.loads(fh.read().decode("utf-8"))


def verify_backup(path: Path, sidecar: Path | None = None) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ValueError("备份文件不存在")
    sidecar = sidecar or path.parent / (path.name + ".sha256")
    if not sidecar.is_file():
        raise ValueError("缺少 SHA256 校验文件")
    expected = sidecar.read_text(encoding="utf-8").split()[0]
    actual = sha256_file(path)
    if expected != actual:
        raise ValueError("备份 SHA256 校验失败")
    meta = _meta_from_tar(path)
    return {"ok": True, "sha256": actual, "meta": meta}


def restore_drill(path: Path) -> dict[str, Any]:
    verify_backup(path)
    tmp = Path(tempfile.mkdtemp(prefix="bbu-restore-drill-"))
    try:
        with tarfile.open(path, "r:gz") as tf:
            for member in tf.getmembers():
                dest = (tmp / member.name).resolve()
                if not str(dest).startswith(str(tmp.resolve()) + os.sep):
                    raise ValueError("备份包含越界路径")
            tf.extractall(tmp, filter="data")
        db = tmp / "data" / "bbu.db"
        verified = verify_database(db)
        counts = {}
        conn = sqlite3.connect(str(db))
        try:
            for table in REQUIRED_TABLES:
                if table in verified["tables"]:
                    counts[table] = conn.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()
        report = {"ok": True, "counts": counts, "tables": len(verified["tables"]),
                  "report_path": ""}
        report_path = tmp / "restore-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(report_path)
        return report
    finally:
        # 演练目录保留报告路径供检查；生产库从不触碰。
        pass


def retention_plan(backup_dir: Path, now: datetime | None = None,
                   daily_keep: int = 7, weekly_keep: int = 4) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    daily, weekly = [], []
    for path in sorted(Path(backup_dir).glob("*.tar.gz")):
        meta = _meta_from_tar(path)
        kind = meta.get("kind") or ("weekly" if "weekly" in path.name else "daily")
        created = datetime.fromisoformat(str(meta.get("created_at")).replace("Z", "+00:00"))
        (weekly if kind == "weekly" else daily).append((created, path))
    daily.sort(reverse=True)
    weekly.sort(reverse=True)
    keep = set()
    for created, path in daily[:daily_keep] + weekly[:weekly_keep]:
        if created >= now - timedelta(days=8 if path in [p for _, p in daily] else 35):
            keep.add(path)
    delete = []
    for created, path in daily[daily_keep:] + weekly[weekly_keep:]:
        delete.append({"path": str(path), "created_at": created.isoformat()})
    return {"keep": sorted(str(x) for x in keep), "delete": delete}


def main() -> int:
    parser = argparse.ArgumentParser(description="BBU 备份/校验/恢复演练")
    parser.add_argument("--state-dir", default="/var/lib/bbu-fault-platform")
    parser.add_argument("--out")
    parser.add_argument("--daily", action="store_true")
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument("--verify")
    parser.add_argument("--restore-drill")
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify_backup(Path(args.verify)), ensure_ascii=False, indent=2))
        return 0
    if args.restore_drill:
        print(json.dumps(restore_drill(Path(args.restore_drill)), ensure_ascii=False, indent=2))
        return 0
    state = Path(args.state_dir)
    kind = "weekly" if args.weekly else "daily"
    out = Path(args.out) if args.out else state.parent / "backups" / (
        f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.tar.gz")
    result = create_backup(state, out, kind)
    plan = retention_plan(out.parent) if out.parent.exists() else {"delete": []}
    for item in plan["delete"]:
        try:
            Path(item["path"]).unlink()
            side = Path(item["path"] + ".sha256")
            if side.exists():
                side.unlink()
        except OSError:
            pass
    print(json.dumps({**result, "retention": plan}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
