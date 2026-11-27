"""系统资源快照的纯标准库回归。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.system_metrics import _parse_cpu_line, snapshot  # noqa: E402


class SystemMetricsTests(unittest.TestCase):
    def test_parse_proc_cpu_line(self) -> None:
        total, idle = _parse_cpu_line("cpu  1 2 3 4 5 6 7 8 9 10")
        self.assertEqual(total, 36)
        self.assertEqual(idle, 9)
        self.assertIsNone(_parse_cpu_line("not-a-cpu-line"))

    def test_snapshot_has_safe_shape_and_no_crash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bbu-sysmetrics-") as tmp:
            data = snapshot(tmp)
        self.assertIn("cpu", data)
        self.assertIn("memory", data)
        self.assertIn("process", data)
        self.assertGreaterEqual(int(data["cpu"].get("cores") or 0), 1)
        self.assertEqual(data["process"]["pid"], os.getpid())
        self.assertGreaterEqual(int(data["process"].get("rss_bytes") or 0), 0)
        if data["disk"]:
            self.assertGreaterEqual(int(data["disk"]["total_bytes"]), 0)
            self.assertGreaterEqual(int(data["disk"]["percent"] or 0), 0)
