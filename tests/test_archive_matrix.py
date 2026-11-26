"""复杂归档矩阵：xz / zst / 7z / 嵌套 tar.xz 的只读解包。"""
import importlib.util
import lzma
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from bbu.storage import extract_archive

LIMITS = {"max_files": 1000, "max_bytes": 32 * 1024 * 1024, "max_depth": 3}


class ArchiveMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_xz_extracts_text(self):
        src = self.tmp / "clock.log.xz"
        src.write_bytes(lzma.compress(b"CLK032 PLL unlocked\nline2\n"))
        dest = self.tmp / "out"
        stats = extract_archive(src, dest, LIMITS)
        self.assertEqual(stats["files"], 1)
        self.assertEqual((dest / "clock.log").read_text(), "CLK032 PLL unlocked\nline2\n")

    def test_path_traversal_and_absolute_members_are_skipped(self):
        src = self.tmp / "evil.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("../escape.txt", "must not escape")
            zf.writestr("/absolute.txt", "must not be absolute")
            zf.writestr("safe.log", "CLK032 normal")
        dest = self.tmp / "out"
        stats = extract_archive(src, dest, LIMITS)
        self.assertEqual(stats["files"], 1)
        self.assertEqual(len(stats["skipped"]), 2)
        self.assertTrue((dest / "safe.log").is_file())
        self.assertFalse((self.tmp / "escape.txt").exists())
        self.assertFalse(Path("/absolute.txt").exists())

    def test_zip_bomb_hits_declared_budget_before_writing(self):
        src = self.tmp / "bomb.zip"
        with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.log", b"0" * (2 * 1024 * 1024))
        dest = self.tmp / "out"
        with self.assertRaises(Exception) as caught:
            extract_archive(src, dest, {"max_files": 1000, "max_bytes": 1024 * 1024})
        self.assertIn("上限", str(caught.exception))

    def test_corrupt_zip_is_skipped_without_becoming_a_log(self):
        src = self.tmp / "broken.zip"
        src.write_bytes(b"PK\x03\x04this is not a zip")
        dest = self.tmp / "out"
        stats = extract_archive(src, dest, LIMITS)
        self.assertEqual(stats["files"], 0)
        self.assertEqual(len(stats["skipped"]), 1)
        self.assertFalse((dest / "broken.zip").exists())

    @unittest.skipUnless(shutil.which("zip"), "zip CLI not installed")
    def test_encrypted_zip_is_skipped(self):
        work = self.tmp / "work"; work.mkdir()
        (work / "secret.log").write_text("CLK032 secret\n")
        src = self.tmp / "encrypted.zip"
        subprocess.run(["zip", "-q", "-P", "secret", str(src), "secret.log"],
                       cwd=work, check=True, capture_output=True)
        dest = self.tmp / "out"
        stats = extract_archive(src, dest, LIMITS)
        self.assertEqual(stats["files"], 0)
        self.assertTrue(stats["skipped"])
        self.assertFalse(any(dest.rglob("secret.log")))

    def test_nested_zip_extracts_inner_file_and_depth_limit(self):
        inner = self.tmp / "inner.zip"
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("clock.log", "CLK032 nested\n")
        outer = self.tmp / "outer.zip"
        with zipfile.ZipFile(outer, "w") as zf:
            zf.write(inner, "nested/inner.zip")
        dest = self.tmp / "out"
        stats = extract_archive(outer, dest, LIMITS)
        self.assertTrue(any(p.name == "clock.log" for p in dest.rglob("clock.log")))
        self.assertGreaterEqual(stats["files"], 2)

    @unittest.skipUnless(shutil.which("zstd") or importlib.util.find_spec("zstandard"),
                         "zstd decoder not installed")
    def test_zstd_wrapped_tar_is_nested(self):
        inner = self.tmp / "nested.tar"
        with tarfile.open(inner, "w") as tf:
            data = self.tmp / "a.log"
            data.write_text("ALM-001 clock\n")
            tf.add(data, arcname="a.log")
        src = self.tmp / "nested.tar.zst"
        if shutil.which("zstd"):
            subprocess.run(["zstd", "-q", "-f", str(inner), "-o", str(src)], check=True)
        else:
            import zstandard
            src.write_bytes(zstandard.ZstdCompressor().compress(inner.read_bytes()))
        dest = self.tmp / "out"
        stats = extract_archive(src, dest, LIMITS)
        self.assertGreaterEqual(stats["files"], 1)
        self.assertTrue(any(p.name == "a.log" for p in dest.rglob("a.log")))

    @unittest.skipUnless(shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
                         or importlib.util.find_spec("py7zr"), "7z decoder not installed")
    def test_7z_extracts(self):
        work = self.tmp / "work"
        work.mkdir()
        (work / "alarm.log").write_text("ALM-002\n")
        src = self.tmp / "logs.7z"
        tool = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
        if tool:
            subprocess.run([tool, "a", str(src), str(work / "alarm.log")], check=True,
                           capture_output=True)
        else:
            import py7zr
            with py7zr.SevenZipFile(src, "w") as archive:
                archive.write(work / "alarm.log", "alarm.log")
        dest = self.tmp / "out"
        stats = extract_archive(src, dest, LIMITS)
        self.assertGreaterEqual(stats["files"], 1)
        self.assertTrue(any(p.name == "alarm.log" for p in dest.rglob("alarm.log")))


if __name__ == "__main__":
    unittest.main()
