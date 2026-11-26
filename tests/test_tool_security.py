"""R3 工具合同、沙箱、模型能力与导入供应链回归。

测试刻意覆盖失败路径：路径越权、审批改参/过期、危险子进程、外网、
敏感环境变量、符号链接/重复路径/压缩炸弹。沙箱是 Python audit+rlimit，
不是容器级隔离；这里验证其实际能阻断的边界，避免把声明当能力。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from bbu.config import Config
from bbu.db import Database
from bbu.schema_validate import validate_schema
from bbu.model_capabilities import ModelCapabilityStore, endpoint_key
from bbu.supply import (ImportLimits, ImportRejected, build_manifest, quarantine_zip,
                        sign_skill, verify_skill)
from bbu.tool_contract import ToolPolicy, manifest_for_skill

ROOT = Path(__file__).resolve().parent.parent


def write_skill(root: Path, profile: dict | None = None,
                script: str = "print('ok')\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("---\nname: test-tool\ndescription: test\n---\n# t\n",
                                  encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "run_analysis.py").write_text(script, encoding="utf-8")
    if profile is not None:
        (root / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return root


def make_skill(root: Path, profile: dict | None = None) -> SimpleNamespace:
    profile = profile or {}
    return SimpleNamespace(name="test-tool", version="1.0.0", title="测试工具",
                           path=root, source="imported", status="ready", profile=profile)


class SchemaValidateTests(unittest.TestCase):
    def test_types_required_additional_properties_and_ref(self):
        schema = {
            "$defs": {"positive": {"type": "integer", "minimum": 1}},
            "type": "object",
            "required": ["name", "count", "flag"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 2, "maxLength": 10},
                "count": {"$ref": "#/$defs/positive"},
                "flag": {"type": "boolean"},
                "ratio": {"type": "number", "multipleOf": 0.5},
            },
        }
        self.assertEqual(validate_schema({"name": "ab", "count": 2, "flag": True}, schema), [])
        errors = validate_schema({"name": "a", "count": 0, "flag": 1, "x": 1}, schema)
        self.assertTrue(any("name" in e for e in errors))
        self.assertTrue(any("count" in e for e in errors))
        self.assertTrue(any("flag" in e for e in errors))
        self.assertTrue(any("x" in e for e in errors))

    def test_oneof_enum_pattern_unique_and_not(self):
        schema = {
            "type": "object",
            "properties": {
                "mode": {"enum": ["a", "b"]},
                "code": {"type": "string", "pattern": "^[A-Z]{2}\\d{2}$"},
                "rows": {"type": "array", "minItems": 2, "maxItems": 3,
                         "uniqueItems": True, "items": {"type": "integer"}},
                "value": {"oneOf": [{"type": "string"}, {"type": "integer", "minimum": 0}]},
                "forbidden": {"not": {"const": "bad"}},
            },
        }
        self.assertEqual(validate_schema({"mode": "a", "code": "AB12", "rows": [1, 2],
                                          "value": 3, "forbidden": "ok"}, schema), [])
        errors = validate_schema({"mode": "c", "code": "bad", "rows": [1, 1],
                                  "value": -1, "forbidden": "bad"}, schema)
        for key in ("mode", "code", "rows", "value", "forbidden"):
            self.assertTrue(any(key in e for e in errors), (key, errors))

    def test_tuple_items_additional_items_false(self):
        schema = {"type": "array", "items": [{"type": "string"}],
                  "additionalItems": False}
        self.assertEqual(validate_schema(["x"], schema), [])
        self.assertTrue(validate_schema(["x", "y"], schema))


class ToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-tool-test-"))
        self.cfg = Config(self.tmp)
        self.cfg.values.update({
            "skills_dir": str(self.tmp / "skills"),
            "cases_dir": str(self.tmp / "cases"),
            "tool_policy_mode": "enforce",
            "tool_approval_ttl_min": 10,
        })
        self.db = Database(self.tmp / "data" / "bbu.db")
        self.input_path = self.tmp / "data" / "uploads" / "a.log"
        self.input_path.parent.mkdir(parents=True, exist_ok=True)
        self.input_path.write_text("x", encoding="utf-8")
        self.db.execute("INSERT INTO users(username,display_name,role,password_hash,created_at) "
                        "VALUES(?,?,?,?,?)", ("admin", "Admin", "admin", "x", "now"))
        self.db.execute("INSERT INTO tasks(id,user_id,title,description,status,stage,progress,"
                        "mode,input_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("T1", 1, "t", "d", "queued", "queued", 0, "fast",
                         json.dumps([{"stored_path": str(self.input_path)}]), "now"))
        self.skill_root = write_skill(self.tmp / "skills" / "test-tool")
        self.policy = ToolPolicy(self.db, self.cfg)
        self.skill = make_skill(self.skill_root)
        self.task = self.db.one("SELECT * FROM tasks WHERE id='T1'")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_stable_and_public_has_no_secret(self):
        manifest = manifest_for_skill(self.skill, self.cfg)
        again = manifest_for_skill(self.skill, self.cfg)
        self.assertEqual(manifest.contract_sha256, again.contract_sha256)
        self.assertEqual(manifest.contract_sha256, again.contract_sha256)
        self.assertIn("input_schema", manifest.public())
        self.assertNotIn("api_key", json.dumps(manifest.public()))

    def test_path_outside_is_denied(self):
        outside = self.tmp / "outside.log"
        outside.write_text("x", encoding="utf-8")
        decision = self.policy.check(task=self.task, skill=self.skill, inputs=[outside],
                                     description="d", actor_role="admin")
        self.assertFalse(decision["allow"])
        self.assertIn("path_denied", decision["reasons"])

    def test_task_input_outside_default_roots_is_allowed(self):
        # 任务 input_json 是权威输入清单，迁移/评测案例可能不在 data/cases 下。
        decision = self.policy.check(task=self.task, skill=self.skill,
                                     inputs=[self.input_path], description="d",
                                     actor_role="admin")
        self.assertTrue(decision["allow"], decision)

    def test_role_site_and_device_scope(self):
        profile = {"tool": {"min_role": "reviewer", "allowed_sites": ["NE-A"],
                            "allowed_device_types": ["BBU"]}}
        skill = make_skill(self.skill_root, profile)
        self.task["options_json"] = json.dumps({"site": "NE-B", "device_type": "SDR"})
        den = self.policy.check(task=self.task, skill=skill, inputs=[self.input_path],
                                description="d", actor_role="engineer")
        self.assertFalse(den["allow"])
        self.assertIn("role_denied", den["reasons"])
        self.assertIn("site_denied", den["reasons"])
        self.assertIn("device_denied", den["reasons"])
        self.task["options_json"] = json.dumps({"site": "NE-A", "device_type": "BBU"})
        self.assertTrue(self.policy.check(task=self.task, skill=skill,
                                          inputs=[self.input_path], description="d",
                                          actor_role="reviewer")["allow"])

    def test_high_risk_requires_approval_and_rejects_changed_args(self):
        profile = {"tool": {"risk": "high", "approval": "required"}}
        skill = make_skill(self.skill_root, profile)
        decision = self.policy.check(task=self.task, skill=skill,
                                     inputs=[self.input_path], description="d",
                                     actor_role="admin")
        self.assertFalse(decision["allow"])
        self.assertIn("approval_required", decision["reasons"])
        manifest = self.policy.manifest(skill)
        args = self.policy.arguments("T1", [self.input_path], "d", 0)
        approval = self.policy.create_approval(
            task_id="T1", manifest=manifest,
            arguments_hash=self.policy.arguments_hash(args), requested_by="admin")
        self.policy.decide_approval(approval["id"], approved=True, actor="admin")
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(self.policy.approval_status(approval["id"])["status"], "approved")
        self.assertTrue(self.policy.check(task=self.task, skill=skill,
                                          inputs=[self.input_path], description="d",
                                          actor_role="admin",
                                          approval_id=approval["id"])["allow"])
        changed = self.policy.check(task=self.task, skill=skill,
                                    inputs=[self.input_path], description="changed",
                                    actor_role="admin", approval_id=approval["id"])
        self.assertFalse(changed["allow"])
        self.assertIn("approval_mismatch", changed["reasons"])

    def test_shadow_denies_high_risk_without_approval_and_maps_aggregate_risk(self):
        self.cfg.values["tool_policy_mode"] = "shadow"
        shadow = ToolPolicy(self.db, self.cfg)
        skill = make_skill(self.skill_root, {"tool": {"risk": "change", "approval": "none"}})
        manifest = shadow.manifest(skill)
        self.assertEqual(manifest.risk, "high")
        self.assertEqual(manifest.approval, "required")
        decision = shadow.check(task=self.task, skill=skill,
                                inputs=[self.input_path], description="d",
                                actor_role="admin")
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["decision"], "deny")
        self.assertIn("approval_required", decision["shadow_critical"])


class SandboxTests(unittest.TestCase):
    def _policy(self, tmp: Path, script: Path, *, network: str = "none",
                allow_subprocess: bool = False) -> tuple[list[str], dict, object]:
        cfg = Config(tmp)
        cfg.values["tool_sandbox_mode"] = "audit"
        manifest = SimpleNamespace(timeout_sec=5, network=network,
                                   process={"allow_subprocess": allow_subprocess,
                                            "executables": []})
        from bbu.sandbox import build_command, sandbox_policy
        policy = sandbox_policy(manifest, tmp / "out", [script], cfg)
        policy["read_roots"].append(str(script.parent))
        path = tmp / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        cmd, env, preexec = build_command(script, [], manifest, tmp / "out", [script], cfg)
        return cmd, env, preexec

    def test_environment_whitelist_and_network_block(self):
        with tempfile.TemporaryDirectory(prefix="bbu-sbx-test-") as tmp_name:
            tmp = Path(tmp_name)
            script = tmp / "evil.py"
            script.write_text("import socket\nsocket.create_connection(('127.0.0.1', 9), 1)\n",
                              encoding="utf-8")
            cmd, env, preexec = self._policy(tmp, script)
            self.assertNotIn("BBU_SECRET_TOKEN", env)
            os.environ["BBU_SECRET_TOKEN"] = "do-not-pass"
            try:
                env2 = dict(env)
                self.assertNotIn("BBU_SECRET_TOKEN", env2)
            finally:
                os.environ.pop("BBU_SECRET_TOKEN", None)
            proc = subprocess.run(cmd, env=env, cwd=ROOT, preexec_fn=preexec,
                                  text=True, capture_output=True, timeout=20)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("沙箱拒绝", proc.stderr + proc.stdout)

    def test_write_outside_output_and_executable_whitelist(self):
        with tempfile.TemporaryDirectory(prefix="bbu-sbx-test-") as tmp_name:
            tmp = Path(tmp_name)
            script = tmp / "evil2.py"
            script.write_text(
                "import subprocess, sys\nopen('/tmp/bbu-sandbox-escape.txt','w').write('x')\n"
                "subprocess.run(['sh','-c','echo bad'])\n", encoding="utf-8")
            cmd, env, preexec = self._policy(tmp, script)
            try:
                Path("/tmp/bbu-sandbox-escape.txt").unlink()
            except OSError:
                pass
            proc = subprocess.run(cmd, env=env, cwd=ROOT, preexec_fn=preexec,
                                  text=True, capture_output=True, timeout=20)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("沙箱拒绝", proc.stderr + proc.stdout)
            self.assertFalse(Path("/tmp/bbu-sandbox-escape.txt").exists())

    def test_allowed_python_child_runs(self):
        with tempfile.TemporaryDirectory(prefix="bbu-sbx-test-") as tmp_name:
            tmp = Path(tmp_name)
            script = tmp / "child.py"
            script.write_text(
                "import subprocess, sys\n"
                "p=subprocess.run([sys.executable,'-c','print(123)'],capture_output=True,text=True)\n"
                "print(p.stdout.strip())\n", encoding="utf-8")
            cmd, env, preexec = self._policy(tmp, script, allow_subprocess=True)
            proc = subprocess.run(cmd, env=env, cwd=ROOT, preexec_fn=preexec,
                                  text=True, capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("123", proc.stdout)


class SupplyTests(unittest.TestCase):
    def test_zip_traversal_duplicate_symlink_and_ratio(self):
        with tempfile.TemporaryDirectory(prefix="bbu-supply-test-") as tmp_name:
            tmp = Path(tmp_name)
            limits = ImportLimits(max_files=10, max_total_bytes=200_000, max_ratio=10)
            bad = tmp / "bad.zip"
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("../evil.txt", "x")
            with self.assertRaisesRegex(ImportRejected, "路径穿越"):
                quarantine_zip(bad, tmp / "q1", limits)
            dup = tmp / "dup.zip"
            with zipfile.ZipFile(dup, "w") as zf:
                zf.writestr("demo/SKILL.md", "x")
                zf.writestr("demo/SKILL.md", "y")
            with self.assertRaisesRegex(ImportRejected, "重复路径"):
                quarantine_zip(dup, tmp / "q2", limits)
            sym = tmp / "sym.zip"
            with zipfile.ZipFile(sym, "w") as zf:
                info = zipfile.ZipInfo("demo/link")
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, "/etc/passwd")
                zf.writestr("demo/SKILL.md", "x")
            with self.assertRaisesRegex(ImportRejected, "特殊文件"):
                quarantine_zip(sym, tmp / "q3", limits)
            ratio = tmp / "ratio.zip"
            with zipfile.ZipFile(ratio, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("demo/SKILL.md", "x" * 100_000)
            with self.assertRaisesRegex(ImportRejected, "压缩比"):
                quarantine_zip(ratio, tmp / "q4", ImportLimits(max_total_bytes=10_000_000,
                                                               max_ratio=10))

    def test_sign_verify_tamper_and_install(self):
        with tempfile.TemporaryDirectory(prefix="bbu-supply-test-") as tmp_name:
            tmp = Path(tmp_name)
            src = write_skill(tmp / "src", {"name": "test-tool"})
            signed = sign_skill(src, data_dir=tmp / "data", name="test-tool", version="1")
            self.assertEqual(signed["manifest"]["manifest_version"], "bbu-skill-manifest/1")
            self.assertEqual(verify_skill(src, data_dir=tmp / "data")["status"], "verified")
            (src / "scripts" / "run_analysis.py").write_text("print('tamper')\n",
                                                             encoding="utf-8")
            result = verify_skill(src, data_dir=tmp / "data")
            self.assertEqual(result["status"], "tampered")
            self.assertIn("内容", result["reason"])

    def test_real_import_zip_passes_quarantine(self):
        with tempfile.TemporaryDirectory(prefix="bbu-supply-test-") as tmp_name:
            tmp = Path(tmp_name)
            src = write_skill(tmp / "demo", {"name": "test-tool"})
            zip_path = tmp / "ok.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for item in src.rglob("*"):
                    if item.is_file():
                        zf.write(item, f"demo/{item.relative_to(src).as_posix()}")
            result = quarantine_zip(zip_path, tmp / "q", ImportLimits())
            self.assertEqual(result.root.name, "demo")
            self.assertGreater(result.total_bytes, 0)


class ModelCapabilityTests(unittest.TestCase):
    def test_mock_and_default_degrade(self):
        with tempfile.TemporaryDirectory(prefix="bbu-cap-test-") as tmp_name:
            tmp = Path(tmp_name)
            db = Database(tmp / "bbu.db")
            cfg = Config(tmp)
            store = ModelCapabilityStore(db, cfg)
            caps = store.get({"provider": "mock", "base_url": "", "model": "m"})
            self.assertTrue(caps["json_mode"])
            self.assertFalse(caps["degraded"])
            caps2 = store.get({"provider": "openai", "base_url": "http://x", "model": "m"})
            self.assertTrue(caps2["json_mode"])  # 未探测时保守假设可用
            self.assertTrue(caps2["degraded"])
            self.assertEqual(endpoint_key({"provider": "openai", "base_url": "http://x",
                                           "model": "m"}),
                             endpoint_key({"provider": "openai", "base_url": "http://x",
                                           "model": "m"}))

    def test_probe_result_is_cached_and_declared_capabilities_win(self):
        with tempfile.TemporaryDirectory(prefix="bbu-cap-test-") as tmp_name:
            tmp = Path(tmp_name)
            db = Database(tmp / "bbu.db")
            cfg = Config(tmp)
            calls = []

            def probe(model):
                calls.append(1)
                return {"json_mode": False, "strict_schema": False, "usage": False,
                        "context_window": 8192, "max_output": 1024,
                        "degraded": True, "probe_error": "unsupported"}
            store = ModelCapabilityStore(db, cfg, call_probe=probe)
            model = {"provider": "openai", "base_url": "http://x", "model": "m"}
            first = store.get(model, probe=True)
            second = store.get(model, probe=True)
            self.assertFalse(first["json_mode"])
            self.assertEqual(len(calls), 1)
            self.assertTrue(second["cached"])
            declared = dict(model, extra={"capabilities": {"json_mode": True,
                                                           "strict_schema": True}})
            self.assertTrue(store.get(declared)["json_mode"])


if __name__ == "__main__":
    unittest.main()
