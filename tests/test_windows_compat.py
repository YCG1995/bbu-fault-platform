"""Windows 兼容性回归：不导入 fcntl，升级入口不因平台崩溃。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bbu import updater
from bbu.filelock import exclusive_lock

ROOT = Path(__file__).resolve().parent.parent


class FileLockTests(unittest.TestCase):
    def test_exclusive_lock_blocks_second_handle(self):
        with tempfile.TemporaryDirectory(prefix="bbu-lock-") as tmp:
            path = Path(tmp) / "x.lock"
            with exclusive_lock(path, blocking=False):
                with self.assertRaises(BlockingIOError):
                    with exclusive_lock(path, blocking=False):
                        pass
            with exclusive_lock(path, timeout=0.2):
                self.assertTrue(path.exists())


class WindowsCompatibilityTests(unittest.TestCase):
    def test_api_and_helper_import_without_fcntl(self):
        code = """
import builtins, runpy, sys
real = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == "fcntl":
        raise ImportError("fcntl blocked for Windows simulation")
    return real(name, *args, **kwargs)
builtins.__import__ = blocked
import bbu.api
import bbu.filelock
runpy.run_path("deploy/bbu_upgrade_helper.py", run_name="not_main")
assert bbu.filelock.fcntl is None
print("OK")
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_updater_uses_restart_mode_on_windows_without_custom_helper(self):
        app = SimpleNamespace(config={})
        with patch.object(updater.os, "name", "nt"):
            self.assertTrue(updater.supported(app))
            self.assertEqual(updater.apply_mode(app), "restart")
            self.assertFalse(updater.rollback_supported(app))
            app.config["upgrade_helper_cmd"] = "python helper.py"
            self.assertEqual(updater.apply_mode(app), "helper")
            self.assertTrue(updater.rollback_supported(app))

    def test_updater_is_enabled_on_posix(self):
        app = SimpleNamespace(config={})
        if os.name == "nt":
            self.skipTest("仅 POSIX 断言")
        self.assertTrue(updater.supported(app))


if __name__ == "__main__":
    unittest.main()
