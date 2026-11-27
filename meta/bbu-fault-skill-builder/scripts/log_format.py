#!/usr/bin/env python3
"""
log_format.py — 日志文件通用格式识别与处理路由（零三方依赖，域策略可注入）

统一分类学（kind）与处理路由：
  empty              空文件                 → 登记忽略
  archive            zip/tar/7z/rar         → unpack_logs 递归解包
  compressed_archive gzip/bz2 内层是归档    → 解压后继续递归
  compressed_text    gzip/bz2 内层是文本    → 解压后按文本检索
  compressed_binary  gzip/bz2 内层是二进制  → 登记 opaque（不解析）
  container_doc      xlsx 等 OOXML          → search_evidence 按行检索（仅 xlsx 系）
  text               明文                   → search_evidence 逐行检索
  ciphered           密文/私有格式（按名）   → 登记 opaque，需专用工具人工解析
  binary             二进制                 → 登记 opaque（pcap→Wireshark 等）

用法：
    python log_format.py <目录|zip> [--json out.json] [--sample 5] [--quiet]
输出：按 kind 汇总计数 + 每类样本路径；--json 写全量明细。
"""
import argparse
import bz2
import fnmatch
import json
import lzma
import os
import re
import sys
import zlib
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 名称层常量（与 log-package-map.md §6 保持一致）
# ---------------------------------------------------------------------------
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz",
                ".txz", ".tar.lzma")
ARCHIVE_SUFFIXES = TAR_SUFFIXES + (".zip", ".7z", ".rar")
CONTAINER_DOC_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".docx", ".docm", ".pptx",
                          ".pptm", ".vsdx", ".odt", ".ods", ".odp", ".epub",
                          ".jar", ".apk", ".ipa", ".whl")
XLSX_SUFFIXES = {".xlsx", ".xlsm", ".xltx"}
# 域策略由 assets/format-policy.json 注入；core 不再内置任何域文件名。
DEFAULT_FORMAT_POLICY = {
    "detect_by": "magic",
    "opaque_name_patterns": [],
    "force_text": [],
    "force_opaque": [],
}
_POLICY_STATE = {"path": None, "mtime": None, "policy": None}


def load_format_policy(path=None):
    """读取技能自带的 format-policy.json（或域包 YAML）；无策略时返回纯 magic 默认。"""
    policy_path = path or os.environ.get("SKILL_FORMAT_POLICY")
    if not policy_path:
        candidate = Path(__file__).resolve().parent.parent / "assets" / "format-policy.json"
        policy_path = str(candidate) if candidate.exists() else None
    if not policy_path:
        return dict(DEFAULT_FORMAT_POLICY)
    try:
        text = Path(policy_path).read_text(encoding="utf-8")
        if str(policy_path).lower().endswith((".yaml", ".yml")):
            import yaml  # 可选依赖：域包 YAML；无 PyYAML 时退回纯 magic
            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
    except Exception:  # 配置损坏/缺 PyYAML 时退回纯 magic，不阻断日志分析
        return dict(DEFAULT_FORMAT_POLICY)
    if isinstance(raw, dict) and isinstance(raw.get("format_policy"), dict):
        raw = raw["format_policy"]
    if not isinstance(raw, dict):
        return dict(DEFAULT_FORMAT_POLICY)
    out = dict(DEFAULT_FORMAT_POLICY)
    out.update({k: raw.get(k) for k in out if k in raw})
    return out


def active_format_policy():
    """按 env 路径缓存；mtime 变化自动重载（回归/构建同一进程可换策略）。"""
    path = os.environ.get("SKILL_FORMAT_POLICY")
    if not path:
        candidate = Path(__file__).resolve().parent.parent / "assets" / "format-policy.json"
        path = str(candidate) if candidate.exists() else ""
    mtime = None
    if path:
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = None
    if (_POLICY_STATE["path"] != path or _POLICY_STATE["mtime"] != mtime
            or _POLICY_STATE["policy"] is None):
        _POLICY_STATE.update(path=path, mtime=mtime, policy=load_format_policy(path or None))
    return _POLICY_STATE["policy"]


def _matches_any(name: str, patterns) -> bool:
    low = str(name).lower()
    for pattern in patterns or []:
        if fnmatch.fnmatchcase(low, str(pattern).lower()):
            return True
    return False
BINARY_SUFFIXES = {".pcap", ".pcapng", ".cap", ".bin", ".raw", ".img",
                   ".exe", ".dll", ".so", ".o", ".a", ".lib", ".class", ".pyc",
                   ".db", ".sqlite", ".sqlite3"}
BINARY_SUBTYPE = {
    ".pcap": "pcap", ".pcapng": "pcap", ".cap": "pcap",
    ".exe": "executable", ".dll": "executable", ".so": "executable",
    ".o": "executable", ".a": "executable", ".lib": "executable",
    ".class": "executable", ".pyc": "executable",
    ".db": "database", ".sqlite": "database", ".sqlite3": "database",
}
TEXT_DECODES = ("utf-8-sig", "gb18030", "big5")
OPAQUE_KINDS = ("binary", "ciphered", "compressed_binary")
# OLE2 复合文档（旧版 Office，非 OOXML zip）：按后缀直接判 binary/ole2
OLE2_SUFFIXES = {".xls", ".doc", ".ppt", ".msg", ".vsd"}


def long_path(p):
    """Windows 长路径（默认 MAX_PATH=260）转 \\\\?\\ 前缀供 open() 等 OS API；
    非 Windows 或短路径原样返回字符串。"""
    s = str(p)
    if os.name != "nt" or len(s) < 240 or s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + os.path.abspath(s)

_TS_RE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T,]\d{1,2}:\d{1,2}:\d{1,2}")


def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


# ---------------------------------------------------------------------------
# 底层嗅探
# ---------------------------------------------------------------------------
def sniff_bytes(head: bytes):
    """按 magic bytes 识别归档/压缩/常见二进制容器。"""
    if len(head) >= 4 and head[:4] == b"PK\x03\x04":
        return "zip"
    if len(head) >= 6 and head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if len(head) >= 4 and head[:4] == b"Rar!":
        return "rar"
    if len(head) >= 2 and head[:2] == b"\x1f\x8b":
        return "gzip"
    if len(head) >= 3 and head[:3] == b"BZh":
        return "bzip2"
    if len(head) >= 6 and head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if len(head) >= 4 and head[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    if len(head) >= 4 and head[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4",
                                       b"\x0a\x0d\x0d\x0a", b"\x4d\x3c\xb2\xa1"):
        return "pcap"          # pcap LE/BE、pcapng、pcap-ns
    if len(head) >= 8 and head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole2"          # 旧版 Office/复合文档（.xls/.doc/.ppt/.msg…）
    return None


def archive_kind(name: str):
    n = name.lower()
    if n.endswith(".zip"):
        return "zip"
    if n.endswith(".7z"):
        return "7z"
    if n.endswith(".rar"):
        return "rar"
    if n.endswith(TAR_SUFFIXES):
        return "tar"
    if n.endswith(".gz"):
        return "gzip"
    if n.endswith(".bz2"):
        return "bzip2"
    return None


def _inflate_sample(kind: str, data: bytes, limit: int = 65536) -> bytes:
    """对截断的压缩流做尽力解压（取前 limit 字节，损坏返回 b''）。"""
    try:
        if kind == "gzip":
            return zlib.decompressobj(31).decompress(data, limit)
        if kind == "bzip2":
            return bz2.BZ2Decompressor().decompress(data)[:limit]
        if kind == "xz":
            return lzma.LZMADecompressor().decompress(data)[:limit]
    except Exception:
        return b""
    return b""


def decode_text(head: bytes):
    """编码链探测。返回 (编码名, 文本)；全部失败返回 (None, latin-1 兜底文本)。"""
    for enc in TEXT_DECODES:
        try:
            return enc, head.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None, head.decode("latin-1", errors="replace")


def _looks_text(head: bytes):
    """(是否文本, 探测到的编码)。判据：无 NUL 且编码链可解或字符可打印率高。"""
    if not head:
        return False, None
    if b"\x00" in head:
        return False, None
    enc, text = decode_text(head)
    if enc is not None:
        return True, enc
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\t\n\r")
    if printable / max(len(text), 1) > 0.85:
        return True, "latin-1"
    return False, None


def _text_subtype(text: str) -> str:
    """文本子类型：log（时间戳行）/ table（CSV 式多列）/ text。"""
    lines = [l for l in text.splitlines() if l.strip()][:5]
    if not lines:
        return "text"
    if any(_TS_RE.search(l) for l in lines[:3]):
        return "log"
    if all(l.count(",") >= 2 for l in lines[:3]):
        return "table"
    return "text"


# ---------------------------------------------------------------------------
# 分类主入口
# ---------------------------------------------------------------------------
def classify_bytes(name: str, head: bytes):
    """按 文件名 + 头部字节 分类。返回 dict：
    {kind, subtype, encoding, compression, note}（无值键省略）。"""
    low = name.lower()
    if not head:
        return {"kind": "empty"}

    # 1) 名称权威层：域包策略优先；core 默认不按文件名判密文
    policy = active_format_policy()
    force_text = _matches_any(name, policy.get("force_text"))
    force_opaque = _matches_any(name, policy.get("force_opaque"))
    opaque_name = _matches_any(name, policy.get("opaque_name_patterns"))
    if force_text:
        pass  # 域包显式声明为文本：继续走 magic/内容判断，不再按名称判密文
    elif force_opaque or opaque_name:
        return {"kind": "ciphered", "subtype": "vendor",
                "note": "域包声明为密文/私有格式，需专用工具人工解析"}
    if low.endswith(CONTAINER_DOC_SUFFIXES):
        suf = low.rsplit(".", 1)[-1]
        out = {"kind": "container_doc", "subtype": suf}
        if suf in ("xlsx", "xlsm", "xltx"):
            out["note"] = "OOXML 表格，按行检索（columns 匹配表头）"
        else:
            out["note"] = "OOXML/容器文档，登记不解析"
        return out
    k = archive_kind(low)
    if k in ("zip", "7z", "rar", "tar"):
        return {"kind": "archive", "subtype": k}
    if f".{low.rsplit('.', 1)[-1]}" in OLE2_SUFFIXES:
        return {"kind": "binary", "subtype": "ole2",
                "note": "旧版 Office/复合文档（.xls 系，需先转换格式）"}

    # 2) magic 层：归档 / 压缩 / 抓包 / OLE2
    magic = sniff_bytes(head)
    if magic in ("zip", "7z", "rar", "tar"):
        return {"kind": "archive", "subtype": magic,
                "note": "按 magic bytes 识别（后缀不可信）"}
    if magic in ("gzip", "bzip2", "xz"):
        comp = magic
        inner = _inflate_sample(comp, head)
        inner_magic = sniff_bytes(inner) if inner else None
        if inner_magic in ("zip", "7z", "rar", "tar"):
            return {"kind": "compressed_archive", "subtype": comp,
                    "compression": comp}
        ok, enc = _looks_text(inner)
        if ok:
            sub = _text_subtype(inner.decode(enc, errors="replace"))
            return {"kind": "compressed_text", "subtype": f"{comp}:{sub}",
                    "compression": comp, "encoding": enc}
        return {"kind": "compressed_binary", "subtype": comp,
                "compression": comp, "note": "压缩二进制，登记不解析"}
    if magic == "zstd":
        return {"kind": "compressed_binary", "subtype": "zstd",
                "compression": "zstd",
                "note": "zstd 压缩（非 stdlib，需 zstandard 或外部工具）"}
    if magic == "pcap":
        return {"kind": "binary", "subtype": "pcap", "note": "抓包，Wireshark 解析"}
    if magic == "ole2":
        return {"kind": "binary", "subtype": "ole2",
                "note": "旧版 Office/复合文档（如 .xls，需先转换格式）"}

    # 3) 已知二进制后缀
    suf = low.rsplit(".", 1)[-1] if "." in low else ""
    if f".{suf}" in BINARY_SUFFIXES:
        return {"kind": "binary",
                "subtype": BINARY_SUBTYPE.get(f".{suf}", "binary"),
                "note": "二进制，需专用工具或格式描述"}

    # 4) 内容层：文本 vs 未知二进制
    ok, enc = _looks_text(head)
    if ok:
        sub = _text_subtype(head.decode(enc, errors="replace"))
        return {"kind": "text", "subtype": sub, "encoding": enc}
    return {"kind": "binary", "subtype": "unknown",
            "note": "内容不可读，需用户提供格式描述"}


def classify_file(path):
    """classify_bytes 的文件版（读头 64KB + 空文件判断）。"""
    path = Path(path)
    try:
        if path.stat().st_size == 0:
            return {"kind": "empty"}
        with open(long_path(path), "rb") as f:
            head = f.read(65536)
    except OSError as e:
        return {"kind": "binary", "subtype": "unreadable", "note": str(e)}
    return classify_bytes(path.name, head)


def zip_member_name(info: zipfile.ZipInfo) -> str:
    """zip 成员名修复（UTF-8 标志位优先，否则 UTF-8→GBK 探测）。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


# ---------------------------------------------------------------------------
# 盘点
# ---------------------------------------------------------------------------
def inventory(target):
    """盘点目录或 zip。返回 {source,total_files,by_kind,files,unhandled}。"""
    target = Path(target)
    entries = []  # (rel_path, head_bytes 或 None)
    if target.is_dir():
        for p in sorted(target.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(target))
            try:
                if p.stat().st_size == 0:
                    entries.append((rel, b""))
                    continue
                with open(long_path(p), "rb") as f:
                    entries.append((rel, f.read(65536)))
            except OSError:
                entries.append((rel, None))
    elif target.is_file() and target.suffix.lower() == ".zip":
        with zipfile.ZipFile(long_path(target)) as zf:
            for i in zf.infolist():
                if i.is_dir():
                    continue
                rel = zip_member_name(i)
                try:
                    with zf.open(i) as fh:
                        entries.append((rel, fh.read(65536)))
                except (zipfile.BadZipFile, RuntimeError, OSError):
                    entries.append((rel, None))
    elif target.is_file():
        try:
            with open(long_path(target), "rb") as f:
                entries.append((target.name, f.read(65536)))
        except OSError:
            entries.append((target.name, None))
    else:
        raise SystemExit(f"输入不存在: {target}")

    files, unhandled, by_kind = [], [], {}
    for rel, head in entries:
        if head is None:
            cls = {"kind": "binary", "subtype": "unreadable",
                   "note": "读取失败"}
        else:
            cls = classify_bytes(Path(rel).name, head or b"\x00")
        cls = {"path": rel, **cls}
        files.append(cls)
        by_kind[cls["kind"]] = by_kind.get(cls["kind"], 0) + 1
        if cls["kind"] in OPAQUE_KINDS:
            unhandled.append(cls)
    return {"source": str(target), "total_files": len(files),
            "by_kind": dict(sorted(by_kind.items(), key=lambda x: -x[1])),
            "files": files, "unhandled": unhandled}


def main():
    ap = argparse.ArgumentParser(description="BBU 日志文件通用格式盘点")
    ap.add_argument("target", help="已解压目录 / zip 包 / 单文件")
    ap.add_argument("--json", dest="out")
    ap.add_argument("--sample", type=int, default=5, help="每类打印的样本数")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    _utf8_stdio()

    inv = inventory(Path(args.target))
    if args.out:
        Path(args.out).write_text(json.dumps(inv, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    if not args.quiet:
        print(f"共 {inv['total_files']} 个文件 → "
              + ", ".join(f"{k}:{v}" for k, v in inv["by_kind"].items()))
        groups = {}
        for f in inv["files"]:
            groups.setdefault(f["kind"], []).append(f)
        for kind in sorted(groups, key=lambda k: -len(groups[k])):
            fl = groups[kind]
            print(f"\n[{kind}] {len(fl)}")
            for f in fl[:args.sample]:
                extra = f.get("subtype") or ""
                note = f"  — {f['note']}" if f.get("note") else ""
                print(f"    {f['path']}  ({extra}){note}")
            if len(fl) > args.sample:
                print(f"    ... 其余 {len(fl) - args.sample} 个")
    if args.out:
        print(f"\n明细 → {args.out}")


if __name__ == "__main__":
    main()
