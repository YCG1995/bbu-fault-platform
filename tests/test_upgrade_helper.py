"""Linux root/systemd 助手的 workspace/restore 纯逻辑回归（不触碰真实 systemd）。"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.migration import build_workspace, WORKDIR_TOKEN  # noqa: E402
from bbu.security import bootstrap_admin  # noqa: E402


def load_helper():
    path = ROOT / "deploy" / "bbu_upgrade_helper.py"
    spec = importlib.util.spec_from_file_location("bbu_upgrade_helper_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_config(root: Path, work: Path) -> Config:
    (root / "config.json").write_text(json.dumps({
        "brand": "helper-test", "work_dir": str(work),
        "examples_dir": str(root / "examples"), "meta_skill_dir": str(root / "meta"),
        "upgrade_helper_cmd": "/bin/true", "upgrade_helper_trigger": "/bin/true",
        "allowed_work_roots": [str(root)],
    }), encoding="utf-8")
    return Config(root)


class HelperWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-helper-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = self.root / "opt"
        self.prefix.mkdir()
        self.state = self.root / "state"
        self.cfg = make_config(self.prefix, self.state)
        Database(self.cfg.db_path)
        self.helper = load_helper()
        self.helper.CONF_PATH = self.root / "helper.json"

    def _build(self, job_id: str, target: str) -> tuple[Path, Path, Path]:
        job = self.state / "updates" / job_id
        job.mkdir(parents=True)
        build = build_workspace(self.cfg, job / "workspace", target,
                                include_sessions=True, limit_bytes=8 * 1024 * 1024)
        return job, build["workspace"], build["manifest_path"]

    def test_workspace_switch_updates_config_and_unit(self):
        service = "bbu-test"
        unit = self.prefix / "systemd" / f"{service}.service"
        unit.parent.mkdir()
        unit.write_text("[Service]\nUser=bbu\nEnvironment=BBU_DATA_DIR=/old/data\n"
                        "ReadWritePaths=/old\n", encoding="utf-8")
        conf = {
            "prefix": str(self.prefix), "state_dir": str(self.state), "service_name": service,
            "service_user": "bbu", "unit_path": str(unit),
            "allowed_work_roots": [str(self.root)], "health_url": "http://127.0.0.1:1/health",
            "python_bin": sys.executable,
        }
        self.helper.CONF_PATH.write_text(json.dumps(conf), encoding="utf-8")
        self.helper.JOB = "Uworkspace01"
        job, stage, manifest = self._build("Uworkspace01", str(self.root / "target"))
        target = self.root / "target"
        request = {"job_id": job.name, "action": "workspace", "target_root": str(target),
                   "stage_dir": str(stage), "manifest_path": str(manifest),
                   "target_version": "1.9.0"}
        status = {"id": job.name, "phase": "queued"}
        (job / "status.json").write_text(json.dumps(status), encoding="utf-8")
        calls = []
        def save(phase, message, progress=0, **extra):
            status.update({"phase": phase, "message": message, "progress": progress, **extra})
            (job / "status.json").write_text(json.dumps(status), encoding="utf-8")
        class Proc:
            returncode = 0; stderr = ""; stdout = ""
        with patch.object(self.helper, "_run_service", side_effect=lambda s, a: calls.append(a)), \
             patch.object(self.helper, "_wait_service", return_value=None), \
             patch.object(self.helper, "_health_ok", return_value=(True, "1.9.0")), \
             patch.object(self.helper, "_chown_tree", return_value=None), \
             patch.object(self.helper.subprocess, "run", return_value=Proc()):
            rc = self.helper.run_workspace(job, request, status, conf, self.prefix, self.state, save, lambda x: None)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["stop", "start"])
        self.assertTrue((target / "data" / "bbu.db").is_file())
        config = json.loads((self.prefix / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(config["work_dir"]), target.resolve())
        self.assertIn(f"BBU_DATA_DIR={target.resolve()}/data", unit.read_text(encoding="utf-8"))
        self.assertIn("ReadWritePaths=" + str(target.resolve()), unit.read_text(encoding="utf-8"))
        self.assertEqual(json.loads((target / "updates" / job.name / "status.json").read_text())["phase"], "succeeded")

    def test_restore_moves_old_data_to_backup(self):
        service = "bbu-test"
        conf = {"prefix": str(self.prefix), "state_dir": str(self.state), "service_name": service,
                "service_user": "bbu", "allowed_work_roots": [str(self.root)],
                "health_url": "http://127.0.0.1:1/health"}
        self.helper.JOB = "Uimport00001"
        job, stage, manifest = self._build("Uimport00001", str(self.state))
        (self.state / "data" / "old-sentinel").write_text("old", encoding="utf-8")
        request = {"job_id": job.name, "action": "restore", "stage_dir": str(stage),
                   "manifest_path": str(manifest), "target_version": "1.9.0"}
        status = {"id": job.name, "phase": "queued"}
        (job / "status.json").write_text(json.dumps(status), encoding="utf-8")
        def save(phase, message, progress=0, **extra):
            status.update({"phase": phase, "message": message, "progress": progress, **extra})
            (job / "status.json").write_text(json.dumps(status), encoding="utf-8")
        with patch.object(self.helper, "_run_service", return_value=None), \
             patch.object(self.helper, "_wait_service", return_value=None), \
             patch.object(self.helper, "_health_ok", return_value=(True, "1.9.0")), \
             patch.object(self.helper, "_chown_tree", return_value=None):
            rc = self.helper.run_restore(job, request, status, conf, self.prefix, self.state, save, lambda x: None)
        self.assertEqual(rc, 0)
        backup = self.state / "backups" / f"import-{job.name}"
        self.assertTrue((backup / "data" / "old-sentinel").is_file())
        self.assertFalse((self.state / "data" / "old-sentinel").exists())
        self.assertTrue((self.state / "data" / "bbu.db").is_file())
        self.assertEqual(json.loads((job / "status.json").read_text())["phase"], "succeeded")

    def test_validated_target_rejects_overlap_and_disallowed(self):
        conf = {"allowed_work_roots": ["/var/lib"]}
        with self.assertRaises(RuntimeError):
            self.helper._validated_target(self.state / "nested", conf, self.prefix, self.state)
        with self.assertRaises(RuntimeError):
            self.helper._validated_target(self.root / "outside", conf, self.prefix, self.state)
        conf = {"allowed_work_roots": [str(self.root)]}
        target_file = self.root / "not-a-dir"
        target_file.write_text("x", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.helper._validated_target(target_file, conf, self.prefix, self.state)


if __name__ == "__main__":
    unittest.main()
