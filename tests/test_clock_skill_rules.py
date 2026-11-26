import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH = ROOT / "skills/demo-bbu-clock-loss/scripts/search_evidence.py"
RULES = ROOT / "skills/demo-bbu-clock-loss/assets/evidence-rules.yaml"


class ClockSkillRuleTests(unittest.TestCase):
    def _verdict(self, files):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "6321_1111111" / "BspLog"
            root.mkdir(parents=True)
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            out = Path(td) / "evidence.json"
            subprocess.run(
                [sys.executable, str(SEARCH), str(root.parent), "--rules", str(RULES),
                 "--json", str(out)],
                check=True, capture_output=True, text=True, timeout=60,
            )
            return json.loads(out.read_text(encoding="utf-8"))["verdict"]

    def test_normal_ok_lines_do_not_trigger_clock_loss(self):
        data = (ROOT / "tests/fixtures/clock-normal/6321_1111111_BspLog_NtfDiag.log").read_text(encoding="utf-8")
        self.assertIn("鉴相时钟失锁: OK", data)
        self.assertIn("PLL unlocked", data)
        verdict = self._verdict({
            "NtfDiag.log": data,
            "clockstatuslog.txt": (ROOT / "tests/fixtures/clock-normal/6321_1111111_BspLog_clockstatuslog.txt").read_text(encoding="utf-8"),
        })
        self.assertEqual(verdict["status"], "unmatched")
        self.assertEqual(verdict["primary"], "NONE")

    def test_gnss_lock_failure_is_gnss_root_cause(self):
        verdict = self._verdict({
            "clockstatuslog.txt": "Clk[032]2026-01-01,00:00:00 LockSatFail: 5\n",
        })
        self.assertEqual(verdict["primary"], "R2")

    def test_ocxo_sustained_drift_is_ocxo_root_cause(self):
        lines = "\n".join(
            f"Clk[100]2026-01-01,00:00:{i:02d} {100.0 + i:.1f}" for i in range(20)
        ) + "\n"
        verdict = self._verdict({"clockocxolog.txt": lines})
        self.assertEqual(verdict["primary"], "R3")


if __name__ == "__main__":
    unittest.main()
