import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.db import Database


class UserSchemaMigrationTests(unittest.TestCase):
    def test_schema_4_database_migrates_to_current_without_losing_users(self):
        with tempfile.TemporaryDirectory(prefix="bbu-user-migrate-") as tmp:
            path = Path(tmp) / "bbu.db"
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO settings(key, value) VALUES('schema_version', '4');
                    CREATE TABLE users (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      username TEXT UNIQUE NOT NULL,
                      display_name TEXT NOT NULL,
                      role TEXT NOT NULL DEFAULT 'engineer',
                      password_hash TEXT NOT NULL,
                      must_change INTEGER NOT NULL DEFAULT 0,
                      active INTEGER NOT NULL DEFAULT 1,
                      created_at TEXT NOT NULL,
                      last_login_at TEXT
                    );
                    INSERT INTO users(username, display_name, role, password_hash, active, created_at)
                    VALUES('old_admin', '旧管理员', 'admin', 'x', 1, '2026-01-01T00:00:00Z'),
                           ('old_disabled', '旧停用', 'engineer', 'x', 0, '2026-01-02T00:00:00Z');
                """)

            db = Database(path)
            cols = {r["name"] for r in db.all("PRAGMA table_info(users)")}
            for name in ("status", "team", "site", "expires_at", "invited_by",
                         "invited_at", "updated_at", "notification_prefs"):
                self.assertIn(name, cols)
            rows = {r["username"]: r for r in db.all("SELECT * FROM users")}
            self.assertEqual(rows["old_admin"]["status"], "active")
            self.assertEqual(rows["old_disabled"]["status"], "disabled")
            self.assertEqual(rows["old_admin"]["updated_at"], "2026-01-01T00:00:00Z")
            self.assertEqual(
                db.one("SELECT value FROM settings WHERE key='schema_version'")["value"], "10")
            self.assertTrue(db.one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='user_invites'"))
            self.assertTrue(db.one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='user_model_configs'"))
            task_cols = {r["name"] for r in db.all("PRAGMA table_info(tasks)")}
            self.assertIn("reviewer_id", task_cols)
            self.assertIn("reviewer_assigned_at", task_cols)


if __name__ == "__main__":
    unittest.main()
