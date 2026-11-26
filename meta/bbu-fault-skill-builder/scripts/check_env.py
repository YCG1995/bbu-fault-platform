#!/usr/bin/env python3
"""
check_env.py — 运行环境自检（builder 与子 Skill 共用的最低要求）

检查：Python 版本（>=3.11 为必需，其余为可选能力）、PyYAML、7z/unar/bsdtar（7z/rar 包）、
zstd 命令、控制台编码、Windows 长路径、模板目录是否干净（无 __pycache__）。
退出码：0 满足必需项；1 Python 版本不满足。`--json` 输出机读结果。

用法：python3 check_env.py [--json env.json]
"""
import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE.parent / "assets" / "templates" / "child-skill"


def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


def main():
    ap = argparse.ArgumentParser(description="bbu-fault-skill-builder 运行环境自检")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()
    _utf8_stdio()

    py_ok = sys.version_info >= MIN_PYTHON
    try:
        import yaml  # noqa: F401
        has_yaml = True
    except ImportError:
        has_yaml = False
    tools = {t: shutil.which(t) for t in ("7z", "7za", "unar", "bsdtar", "zstd")}
    stdout_enc = getattr(sys.stdout, "encoding", None) or "?"
    long_path = None
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
                long_path = bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
        except OSError:
            long_path = None
    pycache = sorted(str(p.relative_to(TEMPLATE_DIR)) for p in TEMPLATE_DIR.rglob("__pycache__")) if TEMPLATE_DIR.exists() else []

    items = [
        ("Python 版本", py_ok, f"{platform.python_version()}（要求 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）", True),
        ("平台", True, f"{platform.system()} {platform.release()} / {platform.machine()}", False),
        ("PyYAML", has_yaml, "已安装" if has_yaml else "未安装：读 .yaml profile 需 pip install pyyaml", False),
        ("7z/rar 解包工具", any(tools[t] for t in ("7z", "7za", "unar", "bsdtar")),
         ", ".join(f"{t}={'有' if p else '无'}" for t, p in tools.items() if t != "zstd") + "（缺失时 .7z/.rar 登记 unsupported_archives）", False),
        ("zstd", bool(tools["zstd"]), "有" if tools["zstd"] else "无（.zst 登记 opaque）", False),
        ("控制台编码", True, f"{stdout_enc}（脚本自动切 UTF-8 输出；Windows 建议 PYTHONUTF8=1）", False),
    ]
    if os.name == "nt":
        items.append(("Windows 长路径", long_path is not False,
                      {True: "已启用", False: "未启用（脚本以 \\\\?\\ 前缀兜底，建议启用 LongPathsEnabled）", None: "无法读取注册表"}[long_path], False))
    items.append(("模板目录干净（无 __pycache__）", not pycache, "是" if not pycache else f"存在: {pycache}", False))

    for name, ok, note, required in items:
        tag = "OK " if ok else ("FAIL" if required else "WARN")
        print(f"  [{tag}] {name}：{note}")
    if os.name == "nt":
        print("  [TIP] PowerShell：命令串联用 `;` 不用 `&&`；每条命令自带 "
              "`Set-Location <dir>; python ...`；乱码以输出文件为准；"
              "避免多行 `python -c`（写临时 .py）")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"python": platform.python_version(), "python_ok": py_ok, "pyyaml": has_yaml,
             "tools": tools, "stdout_encoding": stdout_enc, "long_path": long_path,
             "template_pycache": pycache, "platform": platform.platform()},
            ensure_ascii=False, indent=2), encoding="utf-8")
    print("[OK] 环境满足必需项" if py_ok else f"[x] Python 版本不满足 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")
    sys.exit(0 if py_ok else 1)


if __name__ == "__main__":
    main()
