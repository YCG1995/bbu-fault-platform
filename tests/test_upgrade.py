import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from bbu.db import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent.parent
CURRENT_VERSION = re.search(
    r'__version__\s*=\s*"([^"]+)"',
    (ROOT / "bbu" / "__init__.py").read_text(encoding="utf-8"),
).group(1)


class OfflineUpgradeTests(unittest.TestCase):
    def test_upgrade_preserves_state_and_full_rollback_restores_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="bbu-upgrade-test-") as tmp_name:
            tmp = Path(tmp_name)
            prefix = tmp / "opt" / "bbu-fault-platform"
            state = tmp / "state"
            (prefix / "bbu").mkdir(parents=True)
            (state / "data").mkdir(parents=True)
            (state / "skills").mkdir()
            (state / "cases").mkdir()
            (prefix / "bbu" / "__init__.py").write_text(
                "from pathlib import Path\n"
                "BASE_DIR = Path(__file__).resolve().parent.parent\n"
                "__version__ = \"1.2.0\"\n", encoding="utf-8")
            (prefix / "old-marker.txt").write_text("old", encoding="utf-8")
            (state / "data" / "keep.txt").write_text("keep me", encoding="utf-8")
            config = {
                "brand": "升级测试-自定义品牌",
                "host": "127.0.0.1", "port": 18089,
                "data_dir": str(state / "data"),
                "skills_dir": str(state / "skills"),
                "cases_dir": str(state / "cases"),
                "examples_dir": str(prefix / "examples"),
                "meta_skill_dir": str(prefix / "meta" / "bbu-fault-skill-builder"),
            }
            (prefix / "config.json").write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8")

            # 旧安装脚本不能作为升级入口，避免误覆盖配置。
            reinstall = subprocess.run(
                ["bash", str(ROOT / "deploy" / "install_offline.sh"),
                 "--prefix", str(prefix), "--state-dir", str(state), "--no-service"],
                cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(reinstall.returncode, 0)
            self.assertIn("upgrade_offline.sh", reinstall.stderr)

            upgrade = subprocess.run(
                ["bash", str(ROOT / "deploy" / "upgrade_offline.sh"),
                 "--prefix", str(prefix), "--state-dir", str(state), "--no-service"],
                cwd=ROOT, capture_output=True, text=True)
            if upgrade.returncode != 0:
                self.fail(f"upgrade failed\nSTDOUT:\n{upgrade.stdout}\nSTDERR:\n{upgrade.stderr}")

            self.assertIn(f'__version__ = "{CURRENT_VERSION}"',
                          (prefix / "bbu" / "__init__.py").read_text(encoding="utf-8"))
            self.assertFalse((prefix / "old-marker.txt").exists())
            self.assertTrue((prefix / "deploy" / "rollback_offline.sh").exists())
            self.assertEqual(json.loads((prefix / "config.json").read_text(encoding="utf-8")), config)
            self.assertEqual((state / "data" / "keep.txt").read_text(encoding="utf-8"), "keep me")
            self.assertTrue((state / "data" / "bbu.db").exists())
            self.assertTrue((state / "data" / "master.key").exists())
            with sqlite3.connect(state / "data" / "bbu.db") as conn:
                schema = conn.execute(
                    "SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(schema, str(SCHEMA_VERSION))

            backups = list((state / "backups").iterdir())
            self.assertEqual(len(backups), 1)
            backup = backups[0]
            self.assertTrue((backup / "code.tar.gz").exists())
            self.assertTrue((backup / "state.tar.gz").exists())
            meta = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["old_version"], "1.2.0")
            self.assertEqual(meta["new_version"], CURRENT_VERSION)

            (state / "data" / "post-upgrade.txt").write_text("after", encoding="utf-8")
            rollback = subprocess.run(
                ["bash", str(ROOT / "deploy" / "rollback_offline.sh"),
                 "--backup", str(backup), "--with-state", "--no-service"],
                cwd=ROOT, capture_output=True, text=True)
            if rollback.returncode != 0:
                self.fail(f"rollback failed\nSTDOUT:\n{rollback.stdout}\nSTDERR:\n{rollback.stderr}")
            self.assertIn('__version__ = "1.2.0"',
                          (prefix / "bbu" / "__init__.py").read_text(encoding="utf-8"))
            self.assertTrue((prefix / "old-marker.txt").exists())
            self.assertEqual((state / "data" / "keep.txt").read_text(encoding="utf-8"), "keep me")
            self.assertFalse((state / "data" / "post-upgrade.txt").exists())
            self.assertTrue((state / "rollback-safety").exists())


if __name__ == "__main__":
    unittest.main()
