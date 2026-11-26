"""O9 任务生命周期：归档、引用保护、SHA256 与恢复。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.db import Database  # noqa: E402
from bbu.lifecycle import LifecycleService  # noqa: E402


class Cfg:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.values = {"task_archive_after_days": 90, "task_raw_retention_days": 180}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _task(db: Database, tmp: Path, task_id: str, *, age: int = 100,
          name: str = "a.zip") -> Path:
    upload = tmp / "uploads"
    upload.mkdir(parents=True, exist_ok=True)
    path = upload / f"{task_id}-{name}"
    path.write_bytes(b"log data " + task_id.encode())
    created = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
    db.execute(
        "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
        "input_json,options_json,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, 1, task_id, "", "done", "done", 100, "fast",
         json.dumps([{"name": name, "stored_path": str(path)}]), "{}",
         created, created))
    return path


def test_archive_due_moves_raw_inputs_and_sha_matches(tmp_path: Path):
    db = Database(tmp_path / "bbu.db")
    cfg = Cfg(tmp_path)
    path = _task(db, tmp_path, "T1")
    svc = LifecycleService(db, cfg)
    out = svc.archive_due()
    assert out["count"] == 1
    assert not path.exists()
    row = db.one("SELECT * FROM tasks WHERE id='T1'")
    assert row["inputs_state"] == "archived"
    assert Path(row["archive_path"]).is_file()
    assert len(row["archive_sha256"]) == 64
    assert svc.overview()["summary"]["archived"] == 1


def test_referenced_input_is_not_deleted(tmp_path: Path):
    db = Database(tmp_path / "bbu.db")
    cfg = Cfg(tmp_path)
    path = _task(db, tmp_path, "T1")
    db.execute(
        "INSERT INTO real_cases(id,name,title,input_paths_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        ("C1", "case", "case", json.dumps([str(path)]),
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    svc = LifecycleService(db, cfg)
    out = svc.archive_due()
    assert out["count"] == 0
    assert path.exists()
    row = db.one("SELECT * FROM tasks WHERE id='T1'")
    assert row["retention_state"] == "referenced"
    assert "C1" in row["retained_reason"]


def test_restore_and_corruption_check(tmp_path: Path):
    db = Database(tmp_path / "bbu.db")
    cfg = Cfg(tmp_path)
    _task(db, tmp_path, "T1")
    svc = LifecycleService(db, cfg)
    svc.archive_task("T1")
    row = db.one("SELECT * FROM tasks WHERE id='T1'")
    archive = Path(row["archive_path"])
    restored = svc.restore("T1", tmp_path / "restore")
    assert any(x.startswith("uploads/") for x in restored["files"])
    assert "manifest.json" in restored["files"]
    # 损坏归档必须被拒绝，而不是静默解出半截文件。
    archive.write_bytes(archive.read_bytes()[:-10] + b"broken")
    try:
        svc.restore("T1", tmp_path / "restore2")
    except ValueError as exc:
        assert "SHA256" in str(exc) or "损坏" in str(exc)
    else:
        raise AssertionError("损坏归档未报错")
