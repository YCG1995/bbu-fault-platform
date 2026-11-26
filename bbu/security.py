"""口令、会话、CSRF 与 RBAC。仅使用标准库 hashlib.scrypt。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from typing import Any

from .db import Database
from .util import iso_now, utc_now

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SESSION_COOKIE = "bbu_session"
ROLE_RANK = {"engineer": 1, "reviewer": 2, "admin": 3}
COMMON_PASSWORDS = {
    "12345678", "123456789", "1234567890", "password", "password1", "passw0rd",
    "qwerty123", "admin123", "letmein", "welcome1", "iloveyou", "abc12345",
    "password123", "passw0rd123",
}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                           r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b16encode(salt).decode().lower(),
        base64.b16encode(digest).decode().lower(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b16decode(salt_hex.upper())
        expected = base64.b16decode(hash_hex.upper())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n),
                                r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def validate_password(password: str, username: str = "", min_length: int = 10) -> None:
    """校验新口令；不满足时抛 ValueError，消息可直接返回给用户。"""
    value = str(password or "")
    if len(value) < min_length:
        raise ValueError(f"密码至少需要 {min_length} 位")
    if len(value) > 128:
        raise ValueError("密码不能超过 128 位")
    if value != value.strip():
        raise ValueError("密码首尾不能包含空白字符")
    lower = value.lower()
    if username and len(username) >= 4 and username.lower() in lower:
        raise ValueError("密码不能包含用户名")
    if lower in COMMON_PASSWORDS:
        raise ValueError("密码过于常见，请更换更复杂的密码")
    categories = sum((
        any(ch.islower() for ch in value),
        any(ch.isupper() for ch in value),
        any(ch.isdigit() for ch in value),
        any(not ch.isalnum() for ch in value),
    ))
    if categories < 2:
        raise ValueError("密码需至少包含字母、数字、大写、符号中的两类")


def validate_initial_password(password: str, username: str = "") -> None:
    """一次性初始密码：允许 9 位，但账号必须强制首次登录改密。"""
    validate_password(password, username, 9)



def password_policy_hint(min_length: int = 10) -> str:
    return f"至少 {min_length} 位，且包含字母/数字/大写/符号中的两类，不能包含用户名"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_csrf() -> str:
    return secrets.token_urlsafe(18)


def create_session(db: Database, user_id: int, hours: int,
                   ip: str = "", ua: str = "") -> dict[str, str]:
    token = new_token()
    csrf = new_csrf()
    now = utc_now()
    expires = now + timedelta(hours=hours)
    db.execute(
        "INSERT INTO sessions(token, user_id, csrf, created_at, expires_at, ip, ua) "
        "VALUES(?,?,?,?,?,?,?)",
        (token, user_id, csrf, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         expires.strftime("%Y-%m-%dT%H:%M:%SZ"), ip[:64], ua[:255]),
    )
    return {"token": token, "csrf": csrf, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}


def get_session(db: Database, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = db.one(
        "SELECT s.token, s.csrf, s.expires_at, u.id AS user_id, u.username, "
        "u.display_name, u.role, u.must_change, u.active, u.status, u.expires_at AS account_expires_at "
        "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
        (token,),
    )
    if not row:
        return None
    if (row["expires_at"] < iso_now() or not row["active"]
            or (row.get("status") and row["status"] != "active")
            or (row.get("account_expires_at") and row["account_expires_at"] < iso_now())):
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    return row


def delete_session(db: Database, token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token=?", (token,))


def session_public_id(token: str) -> str:
    """不暴露会话令牌，仅返回稳定短标识供前端管理会话。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def list_user_sessions(db: Database, user_id: int) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT token, created_at, expires_at, ip, ua FROM sessions "
        "WHERE user_id=? AND expires_at>=? ORDER BY created_at DESC",
        (user_id, iso_now()),
    )
    result = []
    for row in rows:
        item = dict(row)
        item["id"] = session_public_id(item.pop("token"))
        result.append(item)
    return result


def delete_other_sessions(db: Database, user_id: int, keep_token: str) -> int:
    return int(db.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                          (user_id, keep_token)) or 0)


def delete_user_sessions(db: Database, user_id: int) -> int:
    return int(db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)) or 0)


def revoke_user_session(db: Database, user_id: int, session_id: str) -> bool:
    for row in db.all("SELECT token FROM sessions WHERE user_id=?", (user_id,)):
        if hmac.compare_digest(session_public_id(row["token"]), session_id):
            delete_session(db, row["token"])
            return True
    return False


def extend_session(db: Database, token: str, hours: int) -> str:
    expires = utc_now() + timedelta(hours=hours)
    value = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE sessions SET expires_at=? WHERE token=?", (value, token))
    return value


def rotate_session_csrf(db: Database, token: str) -> str:
    csrf = new_csrf()
    db.execute("UPDATE sessions SET csrf=? WHERE token=?", (csrf, token))
    return csrf


def cleanup_sessions(db: Database) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso_now(),))


def bootstrap_admin(db: Database, data_dir, password_file_name: str = "初始管理员密码.txt") -> str | None:
    """首次启动创建 admin；返回随机密码（已存在用户时返回 None）。"""
    row = db.one("SELECT COUNT(*) AS n FROM users")
    if row and row["n"] > 0:
        return None
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(14))
        try:
            validate_password(password, "admin", 10)
            break
        except ValueError:
            continue
    now = iso_now()
    db.execute(
        "INSERT INTO users(username, display_name, role, password_hash, must_change, active, "
        "created_at, status, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("admin", "系统管理员", "admin", hash_password(password), 1, 1, now, "active", now),
    )
    path = data_dir / password_file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "BBU 故障诊断平台 初始管理员账号\n"
        "用户名: admin\n"
        f"密码: {password}\n"
        "首次登录后请立即修改密码；修改成功后本文件会自动删除。\n",
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return password


def check_role(user: dict[str, Any] | None, minimum: str = "engineer") -> bool:
    if not user:
        return False
    return ROLE_RANK.get(user.get("role", ""), 0) >= ROLE_RANK.get(minimum, 1)


class SecretBox:
    """API Key 本地加密：SHA256-CTR 流加密 + HMAC-SHA256（encrypt-then-MAC）。

    仅用标准库；主密钥存 data/master.key（0600）。不是通用加密库，
    但足以避免 API Key 以明文落库/出现在备份里。
    """

    PREFIX = "enc:v1:"

    def __init__(self, data_dir) -> None:
        from pathlib import Path

        self.path = Path(data_dir) / "master.key"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.key = self.path.read_bytes()
        else:
            self.key = secrets.token_bytes(32)
            self.path.write_bytes(self.key)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _stream(self, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            block = hashlib.sha256(self.key + nonce + counter.to_bytes(8, "big")).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        nonce = secrets.token_bytes(16)
        data = plaintext.encode("utf-8")
        cipher = bytes(a ^ b for a, b in zip(data, self._stream(nonce, len(data))))
        tag = hmac.new(self.key, nonce + cipher, hashlib.sha256).hexdigest()
        return (self.PREFIX + base64.b16encode(nonce).decode().lower() + ":"
                + base64.b16encode(cipher).decode().lower() + ":" + tag)

    def decrypt(self, stored: str) -> str:
        if not stored:
            return ""
        if not stored.startswith(self.PREFIX):
            return stored  # 兼容手工写入的明文
        try:
            _, _, nonce_hex, cipher_hex, tag = stored.split(":")
            nonce = base64.b16decode(nonce_hex.upper())
            cipher = base64.b16decode(cipher_hex.upper())
            expected = hmac.new(self.key, nonce + cipher, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, tag):
                raise ValueError("API Key 完整性校验失败（master.key 可能已更换）")
            data = bytes(a ^ b for a, b in zip(cipher, self._stream(nonce, len(cipher))))
            return data.decode("utf-8")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"API Key 解密失败: {exc}") from exc

    @staticmethod
    def mask(stored_plain: str) -> str:
        if not stored_plain:
            return ""
        if len(stored_plain) <= 8:
            return "****"
        return stored_plain[:4] + "****" + stored_plain[-4:]

