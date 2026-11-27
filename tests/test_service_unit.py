"""Deployment unit regression: web upgrades rely on a constrained setuid sudo."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ACTIVE_NNP = re.compile(
    r"(?m)^[ \t]*NoNewPrivileges[ \t]*=[ \t]*(?:true|yes|1)[ \t]*$"
)


class ServiceUnitTests(unittest.TestCase):
    def test_systemd_template_allows_constrained_sudo_helper(self):
        text = (ROOT / "deploy/bbu-fault-platform.service.in").read_text(encoding="utf-8")
        self.assertNotRegex(text, ACTIVE_NNP)
        self.assertIn("不要设置 NoNewPrivileges=true", text)

    def test_oneclick_unit_allows_constrained_sudo_helper(self):
        text = (ROOT / "deploy/oneclick/install.sh").read_text(encoding="utf-8")
        self.assertNotRegex(text, ACTIVE_NNP)
        self.assertIn("不启用 NoNewPrivileges", text)

    def test_helper_trigger_resolves_systemctl_path(self):
        text = (ROOT / "deploy/bbu-upgrade-helper-trigger").read_text(encoding="utf-8")
        self.assertNotIn("/usr/bin/systemctl start", text)
        self.assertIn("command -v systemctl", text)
        self.assertIn('/bin/systemctl', text)
        self.assertIn("--no-block start", text)

    def test_upgrade_refreshes_helper_trigger(self):
        text = (ROOT / "deploy/upgrade_offline.sh").read_text(encoding="utf-8")
        self.assertIn("patch_helper_trigger", text)
        self.assertIn("bbu-upgrade-helper-trigger", text)

    def test_launch_error_hint_covers_chinese_and_english_sudo(self):
        from bbu.updater import _launch_failure_detail
        for raw in (
            "sudo: 有效用户 ID 不是 0，/usr/bin/sudo 位于一个设置了 nosuid 的文件系统",
            "sudo: effective uid is not 0, is /usr/bin/sudo on a file system",
            "The no new privileges flag is set, which prevents sudo from running as root",
        ):
            self.assertIn("NoNewPrivileges", _launch_failure_detail(raw))

    def test_upgrade_and_repair_have_nnp_migration(self):
        upgrade = (ROOT / "deploy/upgrade_offline.sh").read_text(encoding="utf-8")
        repair = (ROOT / "deploy/fix_upgrade_helper.sh").read_text(encoding="utf-8")
        self.assertIn("patch_service_unit_nnp", upgrade)
        self.assertIn("--fix-nnp", repair)
        self.assertIn("nnp_state", repair)


if __name__ == "__main__":
    unittest.main()
