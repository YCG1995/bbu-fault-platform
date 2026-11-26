"""工作目录与精简迁移包回归：路径派生、排除原始输入、加密往返与导入校验。"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database, SCHEMA_VERSION  # noqa: E402
from bbu.migration import (  # noqa: E402
    WORKDIR_TOKEN,
    MaintenanceGate,
    MigrationError,
    MigrationManager,
    build_workspace,
    import_package,
    validate_target_root,
    verify_workspace,
    zip_workspace,
)
from bbu.migration_crypto import MigrationCryptoError, decrypt_file, encrypt_file  # noqa: E402
from bbu.security import bootstrap_admin  # noqa: E402
from bbu.startup import apply_pending  # noqa: E402


def _make_config(root: Path, overrides: dict | None = None, work: Path | None = None) -> Config:
    base = {
        "brand": "迁移测试",
        "host": "127.0.0.1",
        "port": 18081,
        "examples_dir": str(root / "examples"),
        "meta_skill_dir": str(root / "meta"),
    }
    if work is not None:
        base["work_dir"] = str(work)
    else:
        base.update({
            "data_dir": str(root / "state" / "data"),
            "skills_dir": str(root / "state" / "skills"),
            "cases_dir": str(root / "state" / "cases"),
        })
    base.update(overrides or {})
    (root / "config.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return Config(root)


class WorkDirConfigTests(unittest.TestCase):
    def test_work_dir_derives_three_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "runtime"
            cfg = _make_config(root, work=work)
            self.assertEqual(cfg.work_dir, work.resolve())
            self.assertEqual(cfg.data_dir, work.resolve() / "data")
            self.assertEqual(cfg.skills_dir, work.resolve() / "skills")
            self.assertEqual(cfg.cases_dir, work.resolve() / "cases")
            for path in (cfg.data_dir, cfg.skills_dir, cfg.cases_dir,
                         cfg.work_dir / "updates", cfg.work_dir / "backups"):
                self.assertTrue(path.is_dir())

    def test_legacy_single_path_env_wins_over_stale_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "actual"
            _make_config(root, work=root / "stale")
            with patch.dict(os.environ, {
                "BBU_DATA_DIR": str(work / "data"),
                "BBU_SKILLS_DIR": str(work / "skills"),
                "BBU_CASES_DIR": str(work / "cases"),
            }, clear=False):
                loaded = Config(root)
            self.assertEqual(loaded.data_dir, work.resolve() / "data")
            self.assertEqual(loaded.work_dir, work.resolve())

    def test_disjoint_data_and_skills_fall_back_to_data_parent(self):
        # 选一个与默认临时目录不同的顶层目录，模拟三个路径分处不同挂载点。
        external_base = Path("/var/tmp")
        if not (external_base.is_dir() and os.access(external_base, os.W_OK)):
            external_base = ROOT
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory(dir=external_base) as external:
            root = Path(tmp)
            data = root / "runtime" / "data"
            skills = Path(external) / "skills"
            cases = Path(external) / "cases"
            with patch.dict(os.environ, {
                "BBU_DATA_DIR": str(data),
                "BBU_SKILLS_DIR": str(skills),
                "BBU_CASES_DIR": str(cases),
            }, clear=True):
                loaded = Config(root)
            common = Path(os.path.commonpath(
                [str(data), str(skills), str(cases)]))
            if common.name in ("data", "skills", "cases"):
                common = common.parent
            expected = data.parent if common == Path(common.anchor) else common
            self.assertEqual(loaded.work_dir, expected.resolve())
            self.assertTrue((loaded.work_dir / "updates").is_dir())


class MigrationRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bbu-mig-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.cfg = _make_config(self.root, work=self.work)
        self.db = Database(self.cfg.db_path)
        bootstrap_admin(self.db, self.cfg.data_dir)
        now = "2026-11-25T00:00:00Z"
        self.uid = self.db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,active,created_at,status) "
            "VALUES(?,?,?,?,?,?,?)",
            ("member", "成员", "engineer", "x", 1, now, "active"))
        self.db.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
            "input_json,report_md,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("T1", self.uid, "时钟失锁", "描述", "succeeded", "done", 100, "fast",
             json.dumps([{"path": str(self.work / "data/tasks/T1/logs/a.log"),
                          "name": "a.log", "size": 10}],
                        ensure_ascii=False),
             f"日志位于 {self.work}/data/tasks/T1/logs/a.log", now))
        (self.work / "data" / "tasks" / "T1" / "logs").mkdir(parents=True)
        (self.work / "data" / "tasks" / "T1" / "logs" / "a.log").write_text("raw", encoding="utf-8")
        (self.work / "data" / "tasks" / "T1" / "report.json").write_text(
            json.dumps({"path": str(self.work / "data/tasks/T1/logs/a.log")}), encoding="utf-8")
        (self.work / "data" / "uploads").mkdir(parents=True, exist_ok=True)
        (self.work / "data" / "uploads" / "raw.zip").write_bytes(b"raw-upload")
        (self.work / "skills" / "demo").mkdir()
        (self.work / "skills" / "demo" / "SKILL.md").write_text("# demo", encoding="utf-8")
        (self.work / "cases" / "demo").mkdir()
        (self.work / "cases" / "demo" / "case.yaml").write_text("case: demo\n", encoding="utf-8")
        (self.work / "cases" / "demo" / "input.log").write_text("raw-case", encoding="utf-8")

    def test_build_excludes_raw_and_rewrites_paths(self):
        job = self.root / "updates" / "Uexport000001"
        job.mkdir(parents=True)
        build = build_workspace(self.cfg, job / "workspace", WORKDIR_TOKEN,
                                include_sessions=False, limit_bytes=16 * 1024 * 1024)
        verify_workspace(build["workspace"], build["manifest_path"])
        manifest = build["manifest"]
        paths = [item["path"] for item in manifest["files"]]
        self.assertFalse(any("uploads" in p or "/logs/" in p or "input.log" in p for p in paths))
        self.assertTrue(any(p.endswith("skills/demo/SKILL.md") for p in paths))
        report_file = (build["workspace"] / "data" / "tasks" / "T1" / "report.json").read_text(
            encoding="utf-8")
        self.assertIn(WORKDIR_TOKEN, report_file)
        self.assertEqual(manifest["db_counts"]["sessions"], 0)
        with sqlite3.connect(build["workspace"] / "data" / "bbu.db") as conn:
            input_json = conn.execute("SELECT input_json FROM tasks WHERE id='T1'").fetchone()[0]
            state = conn.execute("SELECT inputs_state FROM tasks WHERE id='T1'").fetchone()[0]
            report = conn.execute("SELECT report_md FROM tasks WHERE id='T1'").fetchone()[0]
            settings = dict(conn.execute(
                "SELECT key,value FROM settings WHERE key IN "
                "('schema_version','migration_inputs_excluded')").fetchall())
        self.assertEqual(state, "excluded_by_migration")
        self.assertNotIn("stored_path", input_json)
        self.assertNotIn("logs/a.log", input_json)
        self.assertIn(WORKDIR_TOKEN, report)
        self.assertEqual(settings["schema_version"], str(SCHEMA_VERSION))
        self.assertEqual(settings["migration_inputs_excluded"], "1")

    def test_encrypted_import_roundtrip_and_tamper_rejected(self):
        job = self.root / "updates" / "Uexport000002"
        job.mkdir(parents=True)
        build = build_workspace(self.cfg, job / "workspace", WORKDIR_TOKEN,
                                include_sessions=False, limit_bytes=16 * 1024 * 1024)
        zip_path = zip_workspace(job, build["workspace"], build["manifest_path"])
        package = job / "export.bbumig"
        phrase = "MigrationPass!2026"
        encrypt_file(zip_path, package, phrase)
        with self.assertRaises(MigrationCryptoError):
            decrypt_file(package, job / "wrong.zip", "WrongPass!2026")

        target = self.root / "target"
        source = job / "package.bbumig"
        package.replace(source)
        result = import_package(job, source, phrase, target, 32 * 1024 * 1024)
        workspace = result["workspace"]
        self.assertEqual(result["integrity"]["integrity"], "ok")
        self.assertEqual(result["integrity"]["schema_version"], SCHEMA_VERSION)
        self.assertFalse(any((workspace / "data").rglob("raw.zip")))
        with sqlite3.connect(workspace / "data" / "bbu.db") as conn:
            report = conn.execute("SELECT report_md FROM tasks WHERE id='T1'").fetchone()[0]
            state = conn.execute("SELECT inputs_state FROM tasks WHERE id='T1'").fetchone()[0]
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertIn(str(target.resolve()), report)
        self.assertNotIn(WORKDIR_TOKEN, report)
        report_file = (workspace / "data" / "tasks" / "T1" / "report.json").read_text(
            encoding="utf-8")
        self.assertIn(str(target.resolve()), report_file)
        self.assertNotIn(WORKDIR_TOKEN, report_file)
        self.assertEqual(state, "excluded_by_migration")
        self.assertEqual(sessions, 0)

    def test_tampered_package_rejected(self):
        job = self.root / "updates" / "Uexport000003"
        job.mkdir(parents=True)
        build = build_workspace(self.cfg, job / "workspace", WORKDIR_TOKEN,
                                include_sessions=False, limit_bytes=16 * 1024 * 1024)
        zip_path = zip_workspace(job, build["workspace"], build["manifest_path"])
        package = job / "export.bbumig"
        encrypt_file(zip_path, package, "MigrationPass!2026")
        data = bytearray(package.read_bytes())
        data[-20] ^= 0x01
        package.write_bytes(bytes(data))
        with self.assertRaises(MigrationCryptoError):
            decrypt_file(package, job / "out.zip", "MigrationPass!2026")


class TargetRootValidationTests(unittest.TestCase):
    def test_rejects_relative_program_and_current_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            root = Path(tmp)
            allowed_root = Path(allowed_tmp)
            cfg = _make_config(root, work=root / "work",
                               overrides={"upgrade_helper_cmd": "/bin/true",
                                              "allowed_work_roots": [str(allowed_root)]})
            from types import SimpleNamespace
            app = SimpleNamespace(config=cfg, upgrade_helper_cmd="/bin/true")
            with self.assertRaises(MigrationError):
                validate_target_root(app, "relative/path")
            with self.assertRaises(MigrationError):
                validate_target_root(app, str(root))
            with self.assertRaises(MigrationError):
                validate_target_root(app, str(cfg.work_dir))
            target = allowed_root / "new-work"
            self.assertEqual(validate_target_root(app, str(target)), target.resolve())

    def test_non_empty_target_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as allowed_tmp:
            root = Path(tmp)
            allowed_root = Path(allowed_tmp)
            cfg = _make_config(root, work=root / "work",
                               overrides={"upgrade_helper_cmd": "/bin/true",
                                              "allowed_work_roots": [str(allowed_root)]})
            from types import SimpleNamespace
            app = SimpleNamespace(config=cfg, upgrade_helper_cmd="/bin/true")
            target = allowed_root / "occupied"
            target.mkdir()
            (target / "x").write_text("x", encoding="utf-8")
            with self.assertRaises(MigrationError):
                validate_target_root(app, str(target))
            file_target = allowed_root / "not-a-dir"
            file_target.write_text("x", encoding="utf-8")
            with self.assertRaises(MigrationError):
                validate_target_root(app, str(file_target))


class WindowsManagerActivationTests(unittest.TestCase):
    def test_manager_writes_startup_contract_and_activates(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as target_tmp:
            root = Path(tmp)
            current = root / "current"
            target = Path(target_tmp) / "next"
            cfg = _make_config(root, work=current)
            Database(cfg.db_path)
            app = SimpleNamespace(config=cfg, audit=lambda *a, **k: None)
            gate = MaintenanceGate()
            manager = MigrationManager(app, gate)
            from unittest.mock import patch as _patch
            with _patch("bbu.migration.assert_idle", return_value=None), \
                    _patch("bbu.migration._is_windows", return_value=True):
                manager.start_workspace(target, "admin")
                pending = root / "pending_maintenance.json"
                for _ in range(100):
                    if pending.is_file():
                        break
                    __import__("time").sleep(0.1)
                self.assertTrue(pending.is_file(), "Windows 待激活文件未生成")
                data = json.loads(pending.read_text(encoding="utf-8"))
                self.assertEqual(data["action"], "workspace")
                self.assertTrue(Path(data["manifest_path"]).is_file())
                self.assertTrue(gate.active)
                self.assertEqual(gate.job_id, data["job_id"])
            result = apply_pending(root)
            self.assertTrue(result["ok"], result)
            self.assertFalse(pending.exists())
            loaded = Config(root)
            self.assertEqual(loaded.work_dir, target.resolve())
            status = json.loads((target / "updates" / data["job_id"] / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["phase"], "succeeded")


class WindowsActivationTests(unittest.TestCase):
    def test_workspace_pending_activation_updates_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current"
            target = root / "next"
            cfg = _make_config(root, work=current)
            Database(cfg.db_path)
            job_id = "Uworkspace0001"
            job = current / "updates" / job_id
            job.mkdir(parents=True)
            build = build_workspace(cfg, job / "workspace", target.resolve().as_posix(),
                                    include_sessions=True, limit_bytes=8 * 1024 * 1024)
            (job / "status.json").write_text(json.dumps({
                "id": job_id, "action": "workspace", "kind": "workspace",
                "phase": "waiting_restart", "progress": 75, "created_at": "2026-11-25T00:00:00Z",
                "updated_at": "2026-11-25T00:00:00Z",
            }), encoding="utf-8")
            (root / "pending_maintenance.json").write_text(json.dumps({
                "action": "workspace", "job_id": job_id, "target_root": str(target),
                "stage_dir": str(build["workspace"]), "manifest_path": str(build["manifest_path"]),
            }), encoding="utf-8")
            result = apply_pending(root)
            self.assertTrue(result["ok"], result)
            self.assertFalse((root / "pending_maintenance.json").exists())
            loaded = Config(root)
            self.assertEqual(loaded.work_dir, target.resolve())
            self.assertEqual(loaded.data_dir, target.resolve() / "data")
            status = json.loads((target / "updates" / job_id / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["phase"], "succeeded")

    def test_import_pending_activation_backs_up_old_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            cfg = _make_config(root, work=work)
            db = Database(cfg.db_path)
            bootstrap_admin(db, cfg.data_dir)
            (work / "data" / "old-sentinel.txt").write_text("old", encoding="utf-8")
            job_id = "Uimport000001"
            job = work / "updates" / job_id
            job.mkdir(parents=True)
            build = build_workspace(cfg, job / "import-stage" / "workspace",
                                    work.resolve().as_posix(), include_sessions=False,
                                    limit_bytes=8 * 1024 * 1024)
            (job / "status.json").write_text(json.dumps({
                "id": job_id, "action": "import", "kind": "import", "phase": "waiting_restart",
                "progress": 90, "created_at": "2026-11-25T00:00:00Z",
                "updated_at": "2026-11-25T00:00:00Z",
            }), encoding="utf-8")
            (root / "pending_maintenance.json").write_text(json.dumps({
                "action": "import", "job_id": job_id,
                "stage_dir": str(build["workspace"]),
                "manifest_path": str(build["manifest_path"]),
            }), encoding="utf-8")
            result = apply_pending(root)
            self.assertTrue(result["ok"], result)
            self.assertFalse((root / "pending_maintenance.json").exists())
            self.assertFalse((work / "data" / "old-sentinel.txt").exists())
            backup = work / "backups" / f"import-{job_id}" / "data" / "old-sentinel.txt"
            self.assertTrue(backup.exists())
            self.assertTrue((work / "data" / "bbu.db").exists())
            status = json.loads((work / "updates" / job_id / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["phase"], "succeeded")


if __name__ == "__main__":
    unittest.main()
