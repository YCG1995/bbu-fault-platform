import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.accounts import register_user
from bbu.db import Database, SCHEMA_VERSION


class SelfRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-register-")
        self.db = Database(Path(self.tmp.name) / "bbu.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_engineer_with_domain(self):
        row = register_user(self.db, "self_user", "SelfReg!2026x",
                            "自助用户", "时钟同步")
        self.assertEqual(row["role"], "engineer")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["domain"], "时钟同步")
        self.assertEqual(row["must_change"], 0)

    def test_duplicate_and_invalid_inputs_are_rejected(self):
        register_user(self.db, "self_user", "SelfReg!2026x")
        with self.assertRaisesRegex(ValueError, "已存在"):
            register_user(self.db, "self_user", "SelfReg!2026x")
        with self.assertRaises(ValueError):
            register_user(self.db, "bad name", "SelfReg!2026x")
        with self.assertRaises(ValueError):
            register_user(self.db, "short_pass", "short")

class Schema11MigrationTests(unittest.TestCase):
    def test_schema_10_users_and_invites_drop_team_site(self):
        with tempfile.TemporaryDirectory(prefix="bbu-schema11-") as tmp:
            path = Path(tmp) / "bbu.db"
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO settings VALUES('schema_version', '10');
                    CREATE TABLE users (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      username TEXT UNIQUE NOT NULL,
                      display_name TEXT NOT NULL,
                      role TEXT NOT NULL DEFAULT 'engineer',
                      password_hash TEXT NOT NULL,
                      must_change INTEGER NOT NULL DEFAULT 0,
                      active INTEGER NOT NULL DEFAULT 1,
                      created_at TEXT NOT NULL,
                      last_login_at TEXT,
                      status TEXT NOT NULL DEFAULT 'active',
                      expires_at TEXT,
                      invited_by INTEGER,
                      invited_at TEXT,
                      team TEXT NOT NULL DEFAULT '',
                      site TEXT NOT NULL DEFAULT '',
                      domain TEXT NOT NULL DEFAULT '',
                      updated_at TEXT,
                      notification_prefs TEXT NOT NULL DEFAULT '{}'
                    );
                    INSERT INTO users(username, display_name, role, password_hash, active,
                                      created_at, team, site, domain)
                    VALUES('legacy', '旧用户', 'engineer', 'x', 1, '2026-01-01T00:00:00Z',
                           '无线班组', '深圳站点', '时钟同步');
                    CREATE TABLE user_invites (
                      id TEXT PRIMARY KEY, username TEXT NOT NULL,
                      display_name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'engineer',
                      code_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL,
                      created_at TEXT NOT NULL, created_by INTEGER,
                      used_at TEXT, used_by INTEGER, revoked_at TEXT,
                      team TEXT NOT NULL DEFAULT '', site TEXT NOT NULL DEFAULT ''
                    );
                """)
            db = Database(path)
            cols = {r["name"] for r in db.all("PRAGMA table_info(users)")}
            invite_cols = {r["name"] for r in db.all("PRAGMA table_info(user_invites)")}
            self.assertNotIn("team", cols)
            self.assertNotIn("site", cols)
            self.assertNotIn("team", invite_cols)
            self.assertNotIn("site", invite_cols)
            self.assertEqual(db.one("SELECT domain FROM users WHERE username='legacy'")["domain"],
                             "时钟同步")
            self.assertEqual(db.one("SELECT value FROM settings WHERE key='schema_version'")["value"],
                             str(SCHEMA_VERSION))


if __name__ == "__main__":
    unittest.main()
