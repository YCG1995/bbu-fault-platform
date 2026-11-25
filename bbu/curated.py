"""平台自带的专家推理剧本：随部署包分发，启动时写入经验库。

这些记录是合成案例回归过的“排查路径”，只提供竞争假设、鉴别点和停止条件，
不预判任何真实任务的根因；真实判定仍以本次证据为准。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database
from .util import iso_now


def seed_curated_experiences(db: Database, path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payloads = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payloads, list):
        return 0
    inserted = 0
    for item in payloads:
        if not isinstance(item, dict) or not item.get("task_id"):
            continue
        if db.one("SELECT 1 FROM plan_experiences WHERE task_id=?", (item["task_id"],)):
            continue
        db.execute(
            "INSERT INTO plan_experiences(task_id,skill_name,root_cause_name,status,confidence,"
            "description,hints_json,plan_json,outcome_json,created_at,trust_level,reviewed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET description=excluded.description,"
            "plan_json=excluded.plan_json,outcome_json=excluded.outcome_json,"
            "trust_level=excluded.trust_level",
            (item["task_id"], item.get("skill_name", ""), "", "done", "reference",
             str(item.get("description") or "")[:6000],
             json.dumps(item.get("hints") or [], ensure_ascii=False),
             json.dumps(item.get("plan") or {}, ensure_ascii=False),
             json.dumps(item.get("outcome") or {}, ensure_ascii=False),
             iso_now(), "verified", iso_now()))
        inserted += 1
    return inserted
