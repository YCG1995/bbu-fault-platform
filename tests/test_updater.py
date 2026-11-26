"""R4 升级预检单元测试：版本、哈希、路径穿越、链接与结构。"""
from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bbu import updater
from bbu.config import Config

ROOT = Path(__file__).resolve().parent.parent
CURRENT_VERSION = re.search(
    r'__version__\s*=\s*"([^"]+)"',
    (ROOT / "bbu" / "__init__.py").read_text(encoding="utf-8")).group(1)


def build_package(path: Path, version: str, *, extra=None, symlink=False) -> Path:
    root = path.parent / f"src-{version}-{path.name}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    for rel in ("run.py", "vendor/yaml/__init__.py", "web/index.html",
                "deploy/upgrade_offline.sh", "deploy/rollback_offline.sh"):
        src = ROOT / rel
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (root / "bbu").mkdir(parents=True)
    text = (ROOT / "bbu/__init__.py").read_text(encoding="utf-8")
    text = re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"', text)
    (root / "bbu/__init__.py").write_text(text, encoding="utf-8")
    if extra:
        for name, content in extra.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    with tarfile.open(path, "w:gz") as tar:
        tar.add(root, arcname="bbu-fault-platform")
        if symlink:
            info = tarfile.TarInfo("bbu-fault-platform/evil-link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return path


class UpdaterPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-updater-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_higher_version_and_structure_ready(self):
        package = build_package(self.tmp / "u1.tar.gz", "9.9.9")
        result = updater.inspect_archive(package, self.tmp / "release", CURRENT_VERSION)
        self.assertEqual(result["target_version"], "9.9.9")
        self.assertTrue((Path(result["source_dir"]) / "run.py").is_file())
        self.assertEqual(len(result["sha256"]), 64)

    def test_same_version_requires_explicit_confirmation(self):
        package = build_package(self.tmp / "u2.tar.gz", CURRENT_VERSION)
        with self.assertRaises(updater.UpdateError):
            updater.inspect_archive(package, self.tmp / "release2", CURRENT_VERSION)
        result = updater.inspect_archive(package, self.tmp / "release3", CURRENT_VERSION,
                                         allow_same=True)
        self.assertEqual(result["target_version"], CURRENT_VERSION)

    def test_hash_mismatch_and_path_traversal_and_symlink_are_rejected(self):
        package = build_package(self.tmp / "u3.tar.gz", "9.9.9")
        with self.assertRaises(updater.UpdateError):
            updater.inspect_archive(package, self.tmp / "release4", CURRENT_VERSION,
                                    expected_sha256="0" * 64)
        bad = self.tmp / "bad.tar.gz"
        with tarfile.open(bad, "w:gz") as tar:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 4
            import io
            tar.addfile(info, io.BytesIO(b"evil"))
        with self.assertRaises(updater.UpdateError):
            updater.inspect_archive(bad, self.tmp / "release5", CURRENT_VERSION)
        linked = build_package(self.tmp / "link.tar.gz", "9.9.9", symlink=True)
        with self.assertRaises(updater.UpdateError):
            updater.inspect_archive(linked, self.tmp / "release6", CURRENT_VERSION)

    def test_missing_required_file_is_rejected(self):
        path = self.tmp / "missing.tar.gz"
        root = self.tmp / "missing-src" / "bbu-fault-platform"
        (root / "bbu").mkdir(parents=True)
        (root / "bbu/__init__.py").write_text('__version__ = "9.9.9"')
        (root / "run.py").write_text("")
        with tarfile.open(path, "w:gz") as tar:
            tar.add(root, arcname="bbu-fault-platform")
        with self.assertRaises(updater.UpdateError):
            updater.inspect_archive(path, self.tmp / "release7", CURRENT_VERSION)

    def test_create_job_writes_ready_status_and_rejects_big_package(self):
        cfg_root = self.tmp / "cfg"
        cfg = Config(cfg_root)
        cfg.values["max_update_mb"] = 8
        app = SimpleNamespace(config=cfg)
        package = build_package(self.tmp / "u4.tar.gz", "9.9.9")
        status = updater.create_job(app, package, "u4.tar.gz")
        self.assertEqual(status["phase"], "ready")
        self.assertEqual(updater.status_of(app, status["id"])["target_version"], "9.9.9")
        big = self.tmp / "big.tar.gz"
        big.write_bytes(b"x" * (9 * 1024 * 1024))
        with self.assertRaises(updater.UpdateError):
            updater.create_job(app, big, "big.tar.gz")


if __name__ == "__main__":
    unittest.main()
