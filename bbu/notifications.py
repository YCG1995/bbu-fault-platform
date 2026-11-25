"""站内通知：写入时尊重个人通知偏好，读取只返回当前用户记录。"""
from __future__ import annotations

import json
from typing import Any

from .util import iso_now, new_id


def _prefs(db, user_id: int) -> dict[str, Any]:
    row = db.one("SELECT notification_prefs FROM users WHERE id=?", (user_id,))
    try:
        value = json.loads((row or {}).get("notification_prefs") or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def create_notification(db, user_id: int, kind: str, title: str, body: str = "",
                        link: str = "") -> str | None:
    """写入一条站内通知；用户关闭该类型时静默跳过。"""
    if not user_id:
        return None
    prefs = _prefs(db, user_id)
    if prefs.get(kind) is False:
        return None
    note_id = new_id("N")
    db.execute(
        "INSERT INTO notifications(id,user_id,kind,title,body,link,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (note_id, int(user_id), kind[:32], title[:200], body[:2000], link[:300], iso_now()))
    return note_id


def notification_public(row: dict) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "title": row["title"],
        "body": row.get("body") or "", "link": row.get("link") or "",
        "read": bool(row.get("read_at")), "read_at": row.get("read_at"),
        "created_at": row["created_at"],
    }
