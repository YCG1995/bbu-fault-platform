"""账号状态、邀请激活与用户字段规范化。"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from .db import Database
from .security import hash_password, validate_password
from .util import iso_now, new_id, utc_now

ROLES = ("engineer", "reviewer", "admin")
USER_STATUSES = ("invited", "active", "disabled", "expired")
INVITE_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def normalize_status(user: dict[str, Any]) -> str:
    status = str(user.get("status") or "").strip()
    if status in USER_STATUSES:
        if status == "active" and user.get("expires_at") and user["expires_at"] < iso_now():
            return "expired"
        return status
    return "active" if user.get("active") else "disabled"


def is_user_active(user: dict[str, Any]) -> bool:
    return bool(user.get("active")) and normalize_status(user) == "active"


def notification_prefs(user: dict[str, Any]) -> dict[str, Any]:
    raw = user.get("notification_prefs") or "{}"
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(4)) + "-" + "".join(
        secrets.choice(INVITE_ALPHABET) for _ in range(4))


def invite_hash(code: str) -> str:
    import hashlib
    return hashlib.sha256(str(code).strip().upper().encode("utf-8")).hexdigest()


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    """用户列表/详情共用字段，绝不返回口令哈希。"""
    user = {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "domain": row.get("domain") or "",
        "active": bool(row.get("active")),
        "status": normalize_status(row),
        "must_change": bool(row.get("must_change")),
        "created_at": row.get("created_at"),
        "last_login_at": row.get("last_login_at"),
        "expires_at": row.get("expires_at"),
        "updated_at": row.get("updated_at"),
        "notification_prefs": notification_prefs(row),
    }
    return user


def create_invite(db: Database, actor_id: int, username: str, display_name: str,
                  role: str, hours: int = 24,
                  expires_at: str | None = None) -> tuple[dict[str, Any], str]:
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名只能包含字母/数字/._-，3–32 位")
    if role not in ROLES:
        raise ValueError("角色只能是 admin / reviewer / engineer")
    existing = db.one("SELECT * FROM users WHERE username=?", (username,))
    if existing and normalize_status(existing) != "invited":
        raise ValueError("用户名已存在")
    active_invite = db.one(
        "SELECT id FROM user_invites WHERE username=? AND used_at IS NULL "
        "AND revoked_at IS NULL AND expires_at>=?", (username, iso_now()))
    if active_invite:
        raise ValueError("该用户已有未使用的激活码")
    code = invite_code()
    invite_id = new_id("IV")
    now = iso_now()
    expiry = expires_at or (utc_now() + timedelta(hours=max(1, min(168, hours)))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    if existing:
        db.execute(
            "UPDATE users SET display_name=?, role=?, invited_by=?, "
            "invited_at=?, updated_at=?, status='invited', active=0, must_change=1 WHERE id=?",
            (display_name.strip() or username, role, actor_id, now, now, existing["id"]))
    else:
        db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,must_change,active,"
            "created_at,status,invited_by,invited_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (username, display_name.strip() or username, role, "", 1, 0, now,
             "invited", actor_id, now, now))
    db.execute(
        "INSERT INTO user_invites(id,username,display_name,role,code_hash,"
        "expires_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
        (invite_id, username, display_name.strip() or username, role,
         invite_hash(code), expiry, now, actor_id),
    )
    invite = {
        "id": invite_id, "username": username,
        "display_name": display_name.strip() or username,
        "role": role,
        "expires_at": expiry, "created_at": now, "status": "pending",
    }
    return invite, code


def list_invites(db: Database, status: str = "all") -> list[dict[str, Any]]:
    rows = db.all("SELECT * FROM user_invites ORDER BY created_at DESC LIMIT 500")
    now = iso_now()
    result = []
    for row in rows:
        if row["used_at"]:
            state = "used"
        elif row["revoked_at"]:
            state = "revoked"
        elif row["expires_at"] < now:
            state = "expired"
        else:
            state = "pending"
        if status not in ("all", "", None) and state != status:
            continue
        result.append({
            "id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"],
            "expires_at": row["expires_at"], "created_at": row["created_at"],
            "used_at": row["used_at"], "revoked_at": row["revoked_at"],
            "status": state,
        })
    return result


def revoke_invite(db: Database, invite_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM user_invites WHERE id=?", (invite_id,))
    if not row:
        raise ValueError("激活码不存在")
    if row["used_at"]:
        raise ValueError("激活码已使用，不能撤销")
    if row["revoked_at"]:
        return row
    db.execute("UPDATE user_invites SET revoked_at=? WHERE id=?", (iso_now(), invite_id))
    return db.one("SELECT * FROM user_invites WHERE id=?", (invite_id,)) or row


def register_user(db: Database, username: str, password: str, display_name: str = "",
                  domain: str = "", min_length: int = 10) -> dict[str, Any]:
    """自助注册：固定创建普通工程师账号，不开放角色和班组/站点字段。"""
    username = str(username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名只能包含字母/数字/._-，3–32 位")
    if db.one("SELECT id FROM users WHERE username=?", (username,)):
        raise ValueError("用户名已存在")
    validate_password(password, username, min_length)
    now = iso_now()
    try:
        uid = db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,must_change,active,"
            "created_at,status,domain,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (username, (str(display_name or "").strip() or username)[:64], "engineer",
             hash_password(password), 0, 1, now, "active", str(domain or "").strip()[:64], now))
    except sqlite3.IntegrityError as exc:
        raise ValueError("用户名已存在") from exc
    return db.one("SELECT * FROM users WHERE id=?", (uid,)) or {}


def activate_invite(db: Database, code: str, password: str, min_length: int = 10,
                    ip: str = "") -> dict[str, Any]:
    code = code.strip()
    if not code:
        raise ValueError("请输入激活码")
    row = db.one("SELECT * FROM user_invites WHERE code_hash=?", (invite_hash(code),))
    if not row or row["used_at"] or row["revoked_at"] or row["expires_at"] < iso_now():
        raise ValueError("激活码无效、已使用或已过期")
    existing = db.one("SELECT * FROM users WHERE username=?", (row["username"],))
    if existing and normalize_status(existing) != "invited":
        raise ValueError("该用户名已存在，请联系管理员")
    validate_password(password, row["username"], min_length)
    now = iso_now()
    if existing:
        uid = existing["id"]
        db.execute(
            "UPDATE users SET password_hash=?, must_change=0, active=1, status='active', "
            "display_name=?, role=?, expires_at=NULL, invited_by=?, "
            "invited_at=?, updated_at=? WHERE id=?",
            (hash_password(password), row["display_name"], row["role"],
             row["created_by"], row["created_at"], now, uid))
    else:
        uid = db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,must_change,active,created_at,"
            "status,expires_at,invited_by,invited_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["username"], row["display_name"], row["role"], hash_password(password), 0, 1,
             now, "active", None, row["created_by"], row["created_at"], now),
        )
    db.execute("UPDATE user_invites SET used_at=?, used_by=? WHERE id=?", (now, uid, row["id"]))
    return db.one("SELECT * FROM users WHERE id=?", (uid,)) or {}
