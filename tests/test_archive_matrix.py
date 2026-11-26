"""复杂归档矩阵：xz / zst / 7z / 嵌套 tar.xz 的只读解包。"""
import importlib.util
import lzma
import shutil
import subprocess
import tarfile
import tempfile
import unittest
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
