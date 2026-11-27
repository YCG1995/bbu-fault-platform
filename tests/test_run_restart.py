"""Windows 自更新重启的 argv/启动方式回归测试。"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import run


class RunRestartTests(unittest.TestCase):
    def test_restart_argv_keeps_existing_cli_arguments(self):
        with mock.patch.object(run.sys, "argv", ["run.py", "--port", "9000"]):
            argv = run._restart_argv()
        self.assertEqual(argv[0], run.sys.executable)
        self.assertEqual(Path(argv[1]), run.BASE_DIR / "run.py")
        self.assertEqual(argv[2:], ["--port", "9000"])

    def test_windows_uses_popen_argv_list_not_shell_string(self):
        # Windows Python 3.14 的 os.execv 会拆开含空格的解释器路径；
        # 这里锁定修复：必须把完整路径作为列表首项交给 Popen。
        with mock.patch.object(run.os, "name", "nt"), \
                mock.patch.object(run.subprocess, "Popen") as popen:
            run._restart_self()
        argv, kwargs = popen.call_args
        self.assertIsInstance(argv[0], list)
        self.assertEqual(argv[0][0], run.sys.executable)
        self.assertEqual(Path(argv[0][1]), run.BASE_DIR / "run.py")
        self.assertEqual(Path(kwargs["cwd"]), run.BASE_DIR)
        self.assertFalse(kwargs["close_fds"])

    def test_posix_keeps_execv(self):
        with mock.patch.object(run.os, "name", "posix"), \
                mock.patch.object(run.os, "execv") as execv:
            run._restart_self()
        execv.assert_called_once()
        self.assertEqual(execv.call_args.args[0], run.sys.executable)


if __name__ == "__main__":
    unittest.main()
