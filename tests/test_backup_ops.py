"""O10 备份、校验、恢复演练与保留策略。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.db import Database  # noqa: E402
from tools.backup_ops import (create_backup, restore_drill, retention_plan,  # noqa: E402
                              verify_backup)


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    Database(state / "data" / "bbu.db")
    (state / "skills").mkdir()
    (state / "skills" / "demo.yaml").write_text("skill: demo\n", encoding="utf-8")
    (state / "skills" / "master.key").write_text("secret", encoding="utf-8")
    return state


def test_backup_verify_and_restore_drill(tmp_path: Path):
    state = _state(tmp_path)
    out = tmp_path / "backups" / "daily-1.tar.gz"
    result = create_backup(state, out)
    assert result["ok"] and len(result["sha256"]) == 64
    verified = verify_backup(out)
    assert verified["ok"]
    drill = restore_drill(out)
    assert drill["ok"]
    assert set(drill["counts"]) == {"tasks", "users", "skills"}


def test_corrupted_backup_is_rejected(tmp_path: Path):
    state = _state(tmp_path)
    out = tmp_path / "backups" / "daily-1.tar.gz"
    create_backup(state, out)
    out.write_bytes(out.read_bytes() + b"tamper")
    try:
        verify_backup(out)
    except ValueError as exc:
        assert "SHA256" in str(exc)
    else:
        raise AssertionError("损坏备份未拒绝")


def test_retention_keeps_seven_daily_and_four_weekly(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    root = tmp_path / "backups"
    root.mkdir()
    # 直接构造带元数据的归档，避免测试依赖真实时间。
    import json, tarfile
    for i in range(9):
        path = root / f"daily-{i}.tar.gz"
        with tarfile.open(path, "w:gz") as tf:
            meta = tmp_path / "backup.json"
            meta.write_text(json.dumps({"kind": "daily",
                                        "created_at": (datetime.now(timezone.utc) - timedelta(days=i)).isoformat()}))
            tf.add(meta, arcname="backup.json")
    plan = retention_plan(root)
    assert len(plan["delete"]) == 2
