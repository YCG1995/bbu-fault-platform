#!/usr/bin/env python3
"""
package_skill.py — 打 skill 分发 zip（排除 __pycache__/.cache/测试产物，附 sha256）

默认打包本生成器自身；也可打包任意子 Skill 目录（如需随 zip 分发到公司平台）。
zip 内以目录名为顶层前缀，文件顺序固定（可复现）。

用法：
    python3 package_skill.py [--skill <skill目录>] [--out dist/] [--name <自定义文件名>]
"""
import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scaffold_child_skill import TOOLKIT_VERSION  # noqa: E402

HERE = Path(__file__).resolve().parent
EXCLUDE_DIRS = {"__pycache__", ".cache", ".git", ".pytest_cache", "dist"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".tmp"}


def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts) or p.suffix in EXCLUDE_SUFFIXES:
            continue
        yield p, rel


def split_extra(spec):
    """解析 FILE[:ZIP_NAME]；兼容 Windows 盘符路径（C:\\x\\CL.md[:CHANGELOG.md]）。"""
    drive, body = "", spec
    if len(spec) > 2 and spec[1] == ":" and spec[2] in "\\/":
        drive, body = spec[:2], spec[2:]
    src, _, zip_name = body.partition(":")
    return drive + src, zip_name


def main():
    ap = argparse.ArgumentParser(description="打 skill 分发 zip")
    ap.add_argument("--skill", help="要打包的 skill 目录（默认本生成器）")
    ap.add_argument("--out", default="dist", help="输出目录（默认 skill 同级 dist/）")
    ap.add_argument("--name", help="zip 文件名（默认 <skill>-v<版本>.zip）")
    ap.add_argument("--extra", action="append", metavar="FILE[:ZIP_NAME]",
                    help="附加文件写入 zip（如 CHANGELOG.md:CHANGELOG.md）；"
                         "不影响 skill 目录本体，zip 内位于 <skill.name>/ 下")
    args = ap.parse_args()
    _utf8_stdio()

    skill = Path(args.skill).resolve() if args.skill else HERE.parent
    if not (skill / "SKILL.md").exists():
        sys.exit(f"不是 skill 目录（缺 SKILL.md）: {skill}")
    version = TOOLKIT_VERSION
    stamp = skill / "assets" / "toolkit-version.json"
    if args.skill and stamp.exists():
        import json
        try:
            version = json.loads(stamp.read_text(encoding="utf-8")).get("version", version)
        except (json.JSONDecodeError, OSError):
            pass
    out_dir = Path(args.out) if Path(args.out).is_absolute() else skill.parent / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / (args.name or f"{skill.name}-v{version}.zip")

    extras = []
    for spec in args.extra or []:
        src, zip_name = split_extra(spec)
        sp = Path(src)
        if not sp.is_file():
            sys.exit(f"--extra 文件不存在: {src}")
        extras.append((sp, zip_name or sp.name))

    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, rel in iter_files(skill):
            zi = zipfile.ZipInfo(f"{skill.name}/{rel.as_posix()}", date_time=(2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (0o755 if p.suffix == ".py" else 0o644) << 16
            zf.writestr(zi, p.read_bytes())
            n += 1
        for sp, zip_name in extras:
            zi = zipfile.ZipInfo(f"{skill.name}/{zip_name}", date_time=(2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, sp.read_bytes())
            n += 1
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    (zpath.with_suffix(".zip.sha256")).write_text(f"{digest}  {zpath.name}\n", encoding="utf-8")
    print(f"[OK] {zpath}（{n} 个文件，{zpath.stat().st_size // 1024} KB）")
    print(f"   sha256 {digest}")


if __name__ == "__main__":
    main()
