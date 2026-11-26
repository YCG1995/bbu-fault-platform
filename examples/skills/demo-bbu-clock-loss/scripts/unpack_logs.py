#!/usr/bin/env python3
"""
unpack_logs.py — BBU 日志包递归解压工具（故障导向选择性提取 + 多站点识别 + 高容错）

特性：
  - 支持 .zip / .tar / .tar.gz / .tgz / .tar.bz2 / .tbz2 / .tar.xz / .tar.lzma / .gz / .bz2
  - 7z / .rar：系统存在 7z/7za/unar/bsdtar 时自动调用，否则登记 unsupported_archive
  - 按文件头 magic bytes 识别归档（后缀被改/无后缀也能识别，如 .bak.N 实为 gzip）
  - .xlsx/.docx 等 OOXML 容器文档：提取为整文件，不递归展开内部 XML
  - 多级嵌套自动递归（用户二次打包、目录层级变化均可）；路径穿越防护
  - 高容错：zip 单个成员损坏（CRC 等）跳过并告警，不中断整包；zip 中文文件名乱码修复
  - glob 层级兜底：路径模式不匹配时按 basename 递归匹配（层级变化/多站点前缀）
  - 多站点识别：按特征目录（当前告警/BSP/OamLog/...）聚类站点根，写入 manifest
  - 故障导向：--include-glob / --include-rules 只提取匹配文件，未提取项登记 manifest
  - --list-only：只列清单（含 sniff 类型与嵌套归档）；--progress：进度输出
"""

import argparse
import bz2
import gzip
import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_format import classify_file, OPAQUE_KINDS, long_path  # noqa: E402  通用格式分类

TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar.lzma")
ZIP_SUFFIXES = (".zip",)
SEVENZ_SUFFIXES = (".7z",)
RAR_SUFFIXES = (".rar",)
GZ_SUFFIX = ".gz"
BZ2_SUFFIX = ".bz2"
ALL_ARCHIVE_SUFFIXES = TAR_SUFFIXES + ZIP_SUFFIXES + SEVENZ_SUFFIXES + RAR_SUFFIXES

# OOXML/容器文档：文件头是 zip 魔数，但按"文档"处理——只提取、不递归展开内部 XML
CONTAINER_DOC_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".docx", ".docm", ".pptx",
                          ".pptm", ".vsdx", ".odt", ".ods", ".odp", ".epub",
                          ".jar", ".apk", ".ipa", ".whl")

# 站点/网元特征目录与文件名（出现即代表一个 BBU 站点数据根）
# "bsplog" 必须含：DCM 包 <子网>_<网元>/BspLog/ 根下嵌套 tar 解开的 BSP/ 目录
# 不能成为独立站点根（首个特征段命中决定站点根）
SITE_FEATURE_DIRS = ("当前告警", "历史告警", "活动告警", "bsp", "bsplog", "oamlog",
                     "cloudmat", "pk_root", "pkiroot", "pki_root", "collect",
                     "perf", "diag")
SITE_FEATURE_FILES = ("fm-active", "fm-history", "reboot_history", "clockstatuslog",
                      "collectinfo", "collect_info")
SITE_FEATURE_MIN = 2  # 同一候选根下命中 ≥2 个不同特征才判定为站点

# 网元号目录：如 6323_2595757 / 99851_13195742（子网_网元），是强站点锚点
NE_DIR_RE = re.compile(r"^\d{2,5}_\d{5,9}$")
# 归档/解压容器段（fm-active-xxx.zip / .zip_unpacked / .tar_unpacked 等），
# 这类“目录段”本身不是日志文件，禁止被文件特征前缀锚定为站点根
_ARCH_DIR_SEG_RE = re.compile(
    r"\.(zip|tar|tgz|tbz2|7z|rar|gz|bz2|xz|tar\.gz|tar\.bz2)"
    r"(_unpacked|_gunzipped|_bunzipped|_xunzipped)?$", re.IGNORECASE)
# 打包外壳里的告警导出文件名（C10）：仅靠它们不能把多网元容器锚成站点
CONTAINER_WEAK_FILES = ("fm-active", "fm-history")

# 轮转/压缩尾缀（clockstatuslog.txt.01、.gz、.gunzipped 视为同一特征文件；
# 与 search_evidence.py 同款归一，防止轮转副本被计为独立站点特征）
_ROT_SUFFIX_RE = re.compile(r"(?:\.\d+|\.gz|\.bz2|\.xz|\.gunzipped|\.bunzipped|\.xunzipped)+$",
                            re.IGNORECASE)


def _feat_key(seg):
    """路径段归一：剥掉轮转/压缩尾缀，避免轮转文件被计为不同站点特征。"""
    return _ROT_SUFFIX_RE.sub("", seg.lower())


def _feat_segments(rel):
    """提取路径中的站点特征命中 [(段索引, 特征key, 类型)]。

    - 目录特征（BspLog/bsp/告警目录等）在任意段命中；
    - 文件特征（clockstatuslog/fm-active/collectinfo 等）只在真实文件段
      （最后一段）命中，归档/解压容器目录段（.zip_unpacked 等）不锚定，
      避免打包外壳把外层容器误判为站点。
    """
    parts = rel.replace("\\", "/").split("/")
    low = [p.lower() for p in parts]
    hits = []
    last = len(low) - 1
    for i, s in enumerate(low):
        if s in SITE_FEATURE_DIRS:
            hits.append((i, _feat_key(s), "dir"))
            continue
        if _ARCH_DIR_SEG_RE.search(s):
            continue
        if i != last:
            continue
        for f in SITE_FEATURE_FILES:
            if s.startswith(f):
                hits.append((i, _feat_key(s), "file"))
                break
            if f in s:
                hits.append((i, _feat_key(s), "weak"))
    return hits

EXTERNAL_TOOLS = {
    "7z": ["7z", "7za"],
    "rar": ["unar", "bsdtar", "7z", "7za"],
}


def archive_kind(name: str):
    n = name.lower()
    if n.endswith(ZIP_SUFFIXES):
        return "zip"
    if n.endswith(SEVENZ_SUFFIXES):
        return "7z"
    if n.endswith(RAR_SUFFIXES):
        return "rar"
    if n.endswith(TAR_SUFFIXES):
        return "tar"
    if n.endswith(GZ_SUFFIX):
        return "gzip"
    if n.endswith(BZ2_SUFFIX):
        return "bzip2"
    if n.endswith(".xz"):
        return "xz"
    if n.endswith(".zst"):
        return "zstd"
    return None


def sniff_kind(path: Path):
    """按文件头 magic bytes 识别归档类型，后缀缺失/被改也能识别。"""
    try:
        with open(path, "rb") as f:
            head = f.read(263)
    except OSError:
        return None
    return sniff_bytes(head)


def sniff_bytes(head: bytes):
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
    return None


def detect_kind(path: Path):
    k = archive_kind(path.name)
    if k:
        return k
    if path.name.lower().endswith(CONTAINER_DOC_SUFFIXES):
        return None  # xlsx 等容器文档不按归档递归
    return sniff_kind(path)


def safe_member_name(name: str) -> bool:
    if not name or name.startswith(("/", "\\")):
        return False
    head = name.replace("\\", "/").split("/")[0]
    if ":" in head:
        return False
    return ".." not in name.replace("\\", "/").split("/")


def safe_join(root: Path, name: str) -> Path:
    dest = (root / name).resolve()
    if not str(dest).startswith(str(root.resolve())):
        raise ValueError(f"unsafe path: {name}")
    return dest


_GLOB_CACHE = {}


def glob_to_re(pattern: str):
    if pattern in _GLOB_CACHE:
        return _GLOB_CACHE[pattern]
    i, out, n = 0, "^", len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    out += "(?:.*/)?"
                    i += 1
                else:
                    out += ".*"
            else:
                out += "[^/]*"
                i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        elif c == "/":
            out += "/"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    out += "$"
    rx = re.compile(out)
    _GLOB_CACHE[pattern] = rx
    return rx


def pattern_matches(member_name: str, pattern: str) -> bool:
    """glob 匹配，带层级兜底：
    1) 原样匹配；2) 任意前缀 **/pattern；3) basename 任意深度（层级变化兜底）。
    """
    name = member_name.replace("\\", "/")
    pats = [pattern]
    if not pattern.startswith("**/"):
        pats.append("**/" + pattern.lstrip("/"))
    if "/" in pattern:
        pats.append("**/" + pattern.rsplit("/", 1)[-1])
    if "/" not in pattern:
        pats.append("**/" + pattern)
    return any(glob_to_re(p).match(name) for p in dict.fromkeys(pats))


def fix_zip_filename(info: zipfile.ZipInfo) -> str:
    """修复 zip 中文文件名：未设 UTF-8 标志位时还原原始字节后按 UTF-8→GBK 探测。

    UTF-8 严格校验先行（GBK 字节流几乎不可能恰好是合法 UTF-8，反向却常见，
    若先按 GBK 会把"UTF-8 字节无标志位"的成员名解成乱码，如 fm-history 导出包）。
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename  # 含 cp437 无法表示的字符，保留 zipfile 原解码
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def collect_globs_from_rules(rules_path: Path):
    text = Path(rules_path).read_text(encoding="utf-8")
    if rules_path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
            data = yaml.safe_load(text)
        except ImportError:
            data = json.loads(text)
    else:
        data = json.loads(text)
    rules = data.get("rules", data) if isinstance(data, dict) else data
    globs = []
    for r in rules or []:
        globs.extend(r.get("globs", []) or [])
        # extract_globs：该规则明确要求"即使未命中也需提取"的辅助文件
        # （如 collectinfo.log / running_version.csv / summary.json 等预检、版本文件）
        globs.extend(r.get("extract_globs", []) or [])
    return sorted(set(globs))


def find_tool(kind):
    for t in EXTERNAL_TOOLS.get(kind, []):
        if shutil.which(t):
            return t
    return None


def detect_sites(rel_paths):
    """根据特征目录/文件把已提取文件聚类为站点根。

    返回 {site_id: {"root": str, "features": [...], "files": n}} 以及
    file->site_id 映射（按最长匹配的站点根）。

    选根优先级（C2/C10）：
    1. 网元号目录（<digits>_<digits>，其下含 BspLog/时钟/告警文件）为强站点根，
       始终保留，且不被外层打包容器吞并；
    2. 归档/解压容器段（.zip/_unpacked）不作为文件特征锚点，告警导出
       fm-active/fm-history 仅在真实文件段命中，不能把多网元容器锚成站点；
    3. 无网元号目录时回退到原有特征聚类（≥2 特征 + 容器包含去重）。
    """
    norm = [r.replace("\\", "/") for r in rel_paths]
    # 候选特征根（按首个特征段聚类）
    candidates = {}
    for rel in norm:
        hits = _feat_segments(rel)
        if not hits:
            continue
        root = "/".join(rel.split("/")[:hits[0][0]]) or "."
        for _, feat, _ in hits:
            candidates.setdefault(root, set()).add(feat)

    # 强网元号根（NE 目录 + 其下含结构/文件特征）
    strong = {}
    for rel in norm:
        parts = rel.split("/")
        low = [p.lower() for p in parts]
        for i in range(len(low) - 1):
            if not NE_DIR_RE.match(low[i]):
                continue
            tail = low[i + 1:]
            has_dir = any(s in SITE_FEATURE_DIRS for s in tail)
            has_file = any(
                j == len(tail) - 1
                and any(seg.startswith(f) for f in SITE_FEATURE_FILES)
                for j, seg in enumerate(tail))
            if has_dir or has_file:
                root = "/".join(parts[:i + 1])
                bucket = strong.setdefault(root, set())
                for rel2 in norm:
                    if rel2.startswith(root + "/"):
                        for _, feat, _ in _feat_segments(rel2):
                            bucket.add(feat)
            break
    strong_set = set(strong)

    old_sites = {r: fs for r, fs in candidates.items()
                 if len(fs) >= SITE_FEATURE_MIN}
    final_roots = {r: (fs or {"ne_dir"}) for r, fs in strong.items()}

    if strong_set:
        # 有强网元根：普通候选根只在不属于、不包含强网元根时才独立成站
        for r, fs in old_sites.items():
            if r in strong_set:
                continue
            # 位于某个强网元根内部（如 BspLog 下的子目录）：跳过
            if any(r == n or r.startswith(n + "/") for n in strong_set):
                continue
            # 是强网元根的祖先容器：仅当容器自身（NE 子树外）有非告警
            # 导出类的独立特征时才保留，避免打包外壳吞掉叶子网元
            desc = [n for n in strong_set if n.startswith(r + "/")]
            if desc:
                own = set()
                for rel2 in norm:
                    if rel2.startswith(r + "/") and not any(
                            rel2 == n or rel2.startswith(n + "/") for n in desc):
                        for _, feat, _ in _feat_segments(rel2):
                            if not any(feat.startswith(w)
                                       for w in CONTAINER_WEAK_FILES):
                                own.add(feat)
                if len(own) >= SITE_FEATURE_MIN:
                    final_roots[r] = fs
                continue
            final_roots[r] = fs
        chosen = sorted(final_roots)
    else:
        # 无网元号目录：回退原包含去重（保留更深站点根，多站点容器除外）
        roots = sorted(old_sites, key=lambda r: r.count("/"))
        kept = []
        for r in roots:
            if r == ".":
                continue
            if any(o != r and r.startswith(o + "/") for o in roots):
                children = [o for o in roots if o != r and o.startswith(r + "/")]
                if len(children) >= 2:
                    kept.append(r)
                continue
            kept.append(r)
        if not kept and "." in old_sites:
            kept = ["."]
        chosen = sorted(set(kept))
        final_roots = {r: candidates.get(r, set()) for r in chosen}

    result = {}
    for idx, root in enumerate(chosen, 1):
        sid = f"site{idx}" if root == "." else f"site{idx}_{root.split('/')[-1]}"
        result[sid] = {"root": root,
                       "features": sorted(final_roots.get(root, set())),
                       "files": 0}
    for rel in norm:
        best = None
        for sid, info in result.items():
            r = info["root"]
            if r == "." or rel == r or rel.startswith(r + "/"):
                if best is None or len(r) > len(result[best]["root"]):
                    best = sid
        if best:
            result[best]["files"] += 1
    file_site = {}
    for rel in norm:
        best = None
        for sid, info in result.items():
            r = info["root"]
            if r == "." or rel == r or rel.startswith(r + "/"):
                if best is None or len(r) > len(result[best]["root"]):
                    best = sid
        if best:
            file_site[rel] = best
    return result, file_site


class Unpacker:
    def __init__(self, workdir, max_depth, max_bytes, max_files, includes,
                 progress=False, progress_interval=200, progress_fh=None):
        self.workdir = workdir
        self.max_depth = max_depth
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.includes = includes
        self.progress = progress
        self.progress_interval = max(1, int(progress_interval or 200))
        # 进度流：默认 stderr（stdout 仅输出最终 JSON，二者不混流）；
        # --progress-log 时传入文件句柄，把进度写文件，彻底与 stdout 的 JSON 分离。
        self.progress_fh = progress_fh if progress_fh is not None else sys.stderr
        self.total_bytes = 0
        self.total_files = 0
        self.archives = []
        self.binary_files = []
        self.opaque_files = []
        self.kind_counts = {}
        self.extracted = []
        self.not_matched = []
        self.skipped = []
        self.unsupported = []
        self.warnings = []
        self.handled = set()
        self._member_count = 0

    def _tick(self):
        self._member_count += 1
        if self.progress and self._member_count % self.progress_interval == 0:
            print(f"  ... processed {self._member_count} members", file=self.progress_fh)

    def _budget_ok(self, size):
        if self.total_bytes + size > self.max_bytes:
            raise RuntimeError(f"超出体积上限 {self.max_bytes} 字节")
        if self.total_files + 1 > self.max_files:
            raise RuntimeError(f"超出文件数上限 {self.max_files}")

    def wanted(self, member_name: str) -> bool:
        if not self.includes:
            return True
        return any(pattern_matches(member_name, pat) for pat in self.includes)

    def _classify(self, path: Path):
        """按 log_format 统一分类登记：opaque 类记入明细，全部文件计入 kind 统计。"""
        rel = str(path.relative_to(self.workdir))
        info = classify_file(path)
        kind = info.get("kind", "binary")
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
        if kind in OPAQUE_KINDS:
            if rel not in self.binary_files:
                self.binary_files.append(rel)
            self.opaque_files.append({"path": rel, "kind": kind,
                                      "subtype": info.get("subtype"),
                                      "note": info.get("note")})

    def _extract_member(self, member_name, src_iter, size, dest_root, head=b""):
        self._budget_ok(size)  # 写前预检（成员大小已知，超大成员不落盘）
        target = safe_join(dest_root, member_name)
        Path(long_path(target.parent)).mkdir(parents=True, exist_ok=True)
        with open(long_path(target), "wb") as out:
            if head:
                out.write(head)
            shutil.copyfileobj(src_iter, out)
        self.total_files += 1
        self.total_bytes += size
        self.extracted.append(str(target.relative_to(self.workdir)))
        self._classify(target)
        return target

    def handle_zip(self, archive, dest, depth):
        with zipfile.ZipFile(long_path(archive)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = fix_zip_filename(info)
                if not safe_member_name(name):
                    self.skipped.append({"archive": str(archive), "member": name,
                                         "reason": "unsafe path"})
                    continue
                self._tick()
                kind = detect_kind_by_name(name)
                if not kind and not name.lower().endswith(CONTAINER_DOC_SUFFIXES):
                    # 后缀识别失败时 sniff 文件头（全量/选择性模式都探测）：
                    # 既防改后缀的嵌套归档漏提取，也识别 .bak.N 等伪装后缀的压缩文件
                    try:
                        with zf.open(info) as probe:
                            kind = sniff_bytes(probe.read(263))
                    except (zipfile.BadZipFile, RuntimeError, OSError):
                        kind = None
                if kind or self.wanted(name):
                    try:
                        target = self._extract_member(name, zf.open(info),
                                                      info.file_size, dest)
                    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as e:
                        # 单个成员损坏不中断整包
                        self.warnings.append(f"成员损坏已跳过 {name} in {archive.name}: {e}")
                        self.skipped.append({"archive": archive.name, "member": name,
                                             "reason": f"corrupt: {e}"})
                        continue
                    if kind:
                        self.process(target, depth + 1)
                else:
                    self.not_matched.append({"archive": archive.name, "member": name,
                                             "size": info.file_size})

    def handle_tar(self, archive, dest, depth):
        # r|* 单遍流式：r:* 的 getmembers 对 .tar.gz 先整流解压建表、
        # 每成员 extractfile 再 seek 回退重解压（O(N²)）；r|* 顺序迭代总成本=一遍解压。
        # 每成员单次 extractfile：探针头与写盘一趟完成；未命中成员不读数据（tarfile 顺序跳过）。
        with tarfile.open(long_path(archive), "r|*") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                if not safe_member_name(member.name):
                    self.skipped.append({"archive": str(archive), "member": member.name,
                                         "reason": "unsafe path"})
                    continue
                self._tick()
                kind = detect_kind_by_name(member.name)
                need_probe = (not kind
                              and not member.name.lower().endswith(CONTAINER_DOC_SUFFIXES))
                if not kind and not self.wanted(member.name) and not need_probe:
                    self.not_matched.append({"archive": archive.name, "member": member.name,
                                             "size": member.size})
                    continue
                try:
                    src = tf.extractfile(member)
                except (RuntimeError, OSError, tarfile.TarError, EOFError) as e:
                    self.warnings.append(f"成员损坏已跳过 {member.name} in {archive.name}: {e}")
                    self.skipped.append({"archive": archive.name, "member": member.name,
                                         "reason": f"corrupt: {e}"})
                    continue
                if src is None:
                    continue
                head = b""
                if need_probe:
                    try:
                        head = src.read(263)
                        kind = sniff_bytes(head)
                    except (tarfile.TarError, OSError, EOFError):
                        kind = None
                if kind or self.wanted(member.name):
                    try:
                        target = self._extract_member(member.name, src, member.size, dest,
                                                      head=head)
                    except (RuntimeError, OSError, tarfile.TarError, EOFError) as e:
                        self.warnings.append(f"成员损坏已跳过 {member.name} in {archive.name}: {e}")
                        self.skipped.append({"archive": archive.name, "member": member.name,
                                             "reason": f"corrupt: {e}"})
                        continue
                    if kind:
                        self.process(target, depth + 1)
                else:
                    self.not_matched.append({"archive": archive.name, "member": member.name,
                                             "size": member.size})

    def handle_gzip(self, archive, depth, out_parent=None):
        # .gz 去后缀；伪装后缀（.bak.N 等 magic 识别）保留原名加 .gunzipped
        parent = out_parent or archive.parent
        base = archive.name[:-3] if archive.name.lower().endswith(".gz") else archive.name
        target = parent / base
        if target.exists():
            target = parent / (base + ".gunzipped")
            n = 2
            while target.exists():
                target = parent / f"{base}.gunzipped{n}"
                n += 1
        if not self.wanted(target.name) and not self.wanted(str(target.relative_to(self.workdir))):
            self.not_matched.append({"archive": archive.name, "member": target.name,
                                     "size": archive.stat().st_size})
            return
        with gzip.open(long_path(archive), "rb") as src, \
                open(long_path(target), "wb") as out:
            shutil.copyfileobj(src, out)
        size = target.stat().st_size
        try:
            self._budget_ok(size)
        except Exception:
            # 越限熔断：删除已写盘的越限文件，避免残留
            try:
                target.unlink()
            except OSError:
                pass
            raise
        self.total_files += 1
        self.total_bytes += size
        self.extracted.append(str(target.relative_to(self.workdir)))
        self._classify(target)
        kind = detect_kind(target)  # 内容嗅探：.gz 里可能是 tar
        if kind:
            self.process(target, depth + 1)

    def handle_bzip2(self, archive, depth, out_parent=None):
        parent = out_parent or archive.parent
        out_name = archive.name[:-4]
        target = parent / out_name
        if target.exists():
            target = parent / (out_name + ".bunzipped")
        if not self.wanted(target.name) and not self.wanted(str(target.relative_to(self.workdir))):
            self.not_matched.append({"archive": archive.name, "member": target.name,
                                     "size": archive.stat().st_size})
            return
        with bz2.open(long_path(archive), "rb") as src, \
                open(long_path(target), "wb") as out:
            shutil.copyfileobj(src, out)
        size = target.stat().st_size
        try:
            self._budget_ok(size)
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        self.total_files += 1
        self.total_bytes += size
        self.extracted.append(str(target.relative_to(self.workdir)))
        self._classify(target)
        kind = detect_kind(target)  # .bz2 里可能是 tar
        if kind:
            self.process(target, depth + 1)

    def handle_xz(self, archive, depth, out_parent=None):
        parent = out_parent or archive.parent
        out_name = archive.name[:-3]
        target = parent / out_name
        if target.exists():
            target = parent / (out_name + ".xunzipped")
        if not self.wanted(target.name) and not self.wanted(str(target.relative_to(self.workdir))):
            self.not_matched.append({"archive": archive.name, "member": target.name,
                                     "size": archive.stat().st_size})
            return
        with lzma.open(long_path(archive), "rb") as src, \
                open(long_path(target), "wb") as out:
            shutil.copyfileobj(src, out)
        size = target.stat().st_size
        try:
            self._budget_ok(size)
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        self.total_files += 1
        self.total_bytes += size
        self.extracted.append(str(target.relative_to(self.workdir)))
        self._classify(target)
        kind = detect_kind(target)  # .xz 里可能是 tar
        if kind:
            self.process(target, depth + 1)

    def handle_external(self, archive, dest, kind, depth):
        tool = find_tool(kind)
        if not tool:
            self.unsupported.append({"archive": str(archive), "kind": kind,
                                     "hint": f"请安装 {'/'.join(EXTERNAL_TOOLS[kind])} 后重试"})
            self.warnings.append(f"不支持的归档格式 {kind}（缺少解压工具）: {archive}")
            return
        dest.mkdir(parents=True, exist_ok=True)
        if tool in ("7z", "7za"):
            cmd = [tool, "x", "-y", f"-o{dest}", str(archive)]
        elif tool == "unar":
            cmd = [tool, "-force-overwrite", "-o", str(dest), str(archive)]
        else:  # bsdtar
            cmd = [tool, "-xf", str(archive), "-C", str(dest)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        except (subprocess.SubprocessError, OSError) as e:
            self.warnings.append(f"外部工具 {tool} 解压失败 {archive}: {e}")
            return
        if r.returncode != 0:
            self.warnings.append(f"外部工具 {tool} 返回 {r.returncode}: {r.stderr[:200]}")
            return
        # 统计外部解压产物（可能不经 includes 过滤——外部工具全量解压，登记并按需继续递归）
        for p in dest.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(self.workdir))
            if self.includes and not self.wanted(rel) and not detect_kind(p):
                continue
            if rel not in self.extracted:
                self._budget_ok(p.stat().st_size)
                self.total_files += 1
                self.total_bytes += p.stat().st_size
                self.extracted.append(rel)
                self._classify(p)
        # 递归处理产物中的嵌套归档
        for p in sorted(dest.rglob("*")):
            if p.is_file() and detect_kind(p):
                self.process(p, depth + 1)

    def process(self, archive, depth, out_parent=None):
        real = archive.resolve()
        if real in self.handled:
            return
        self.handled.add(real)
        if depth > self.max_depth:
            self.warnings.append(f"达到最大递归层数 {self.max_depth}，跳过 {archive}")
            return
        kind = detect_kind(archive)
        if kind is None:
            self.unsupported.append({"archive": str(archive), "kind": "unknown"})
            return
        # out_parent：顶层源包在 workdir 外（默认不复制模式）时，产物仍落 workdir
        parent = out_parent or archive.parent
        try:
            rel = str(archive.relative_to(self.workdir))
        except ValueError:
            rel = str(archive)
        info = {"archive": rel, "depth": depth, "type": kind, "detected_by":
                "suffix" if archive_kind(archive.name) == kind else "magic", "dest": ""}
        try:
            if kind == "zip":
                dest = parent / (archive.name + "_unpacked")
                dest.mkdir(parents=True, exist_ok=True)
                self.handle_zip(archive, dest, depth)
                info["dest"] = str(dest)
            elif kind == "tar":
                dest = parent / (archive.name + "_unpacked")
                dest.mkdir(parents=True, exist_ok=True)
                self.handle_tar(archive, dest, depth)
                info["dest"] = str(dest)
            elif kind == "gzip":
                self.handle_gzip(archive, depth, parent)
                info["dest"] = rel
            elif kind == "bzip2":
                self.handle_bzip2(archive, depth, parent)
                info["dest"] = rel
            elif kind == "xz":
                self.handle_xz(archive, depth, parent)
                info["dest"] = rel
            elif kind == "zstd":
                self.unsupported.append({"archive": str(archive), "kind": kind,
                                         "hint": "zstd 需外部工具（zstd 命令或 python-zstandard）"})
                self.warnings.append(f"不支持的归档格式 zstd（缺少解压工具）: {archive}")
            elif kind in ("7z", "rar"):
                dest = parent / (archive.name + "_unpacked")
                self.handle_external(archive, dest, kind, depth)
                info["dest"] = str(dest)
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, RuntimeError) as e:
            self.warnings.append(f"解压失败 {archive}: {e}")
            info["error"] = str(e)
        self.archives.append(info)

    def run(self, source: Path, copy_source=False):
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = self.workdir.resolve()  # 相对路径 -o 时与 resolve 后的成员路径保持一致
        outside = source.resolve().parent != self.workdir
        top_parent = None
        if outside:
            if copy_source:
                # --copy-source：workdir 自包含（旧行为），源包复制进来再解压
                dest_archive = self.workdir / source.name
                if not dest_archive.exists():
                    shutil.copy2(long_path(source), long_path(dest_archive))
                source = dest_archive
                outside = False
            elif detect_kind(source) is None:
                # 非归档输入只是单文件，复制成本极低，照旧收入 workdir 保证路径相对性
                dest_archive = self.workdir / source.name
                if not dest_archive.exists():
                    shutil.copy2(long_path(source), long_path(dest_archive))
                source = dest_archive
                outside = False
            else:
                # 默认不复制源包（大包动辄 GB 级）：解压产物仍全部落在 workdir 内
                top_parent = self.workdir
        if detect_kind(source) is None:
            # 非归档输入（纯文本日志/.xlsx 等）：直接登记为已提取文件
            self.total_files += 1
            self.total_bytes += source.stat().st_size
            self.extracted.append(source.name)
            self._classify(source)
        else:
            self.process(source, 1, out_parent=top_parent)
        sites, file_site = detect_sites(self.extracted)
        manifest = {
            "source": str(source),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "selective" if self.includes else "full",
            "include_globs": self.includes or [],
            "total_extracted_files": self.total_files,
            "total_extracted_bytes": self.total_bytes,
            "archives_extracted": self.archives,
            "sites": list(sites.values()),
            "file_site_map": file_site,
            "binary_or_opaque_files": sorted(set(self.binary_files)),
            "opaque_files": self.opaque_files,
            "file_kind_counts": dict(sorted(self.kind_counts.items(),
                                            key=lambda x: -x[1])),
            "extracted_files": sorted(self.extracted),
            "not_matched_not_extracted": self.not_matched,
            "unsupported_archives": self.unsupported,
            "skipped": self.skipped,
            "warnings": self.warnings,
        }
        return manifest


def merge_manifests(existing, new):
    """把本次解压的 manifest 合并进同一 workdir 已有的 manifest（多包合并到同一根）。

    返回合并后的 manifest：sources[] 按序追加本次；站点/文件清单取并集（按站点根、
    文件路径去重）；计数与格式分布按并集重算；未匹配/跳过/告警去重拼接。
    file_site_map 由合并后的提取文件清单用 detect_sites 重新聚类，保证与 sites 一致。
    """
    old = existing.get("sources") or ([existing] if existing.get("source") else [])
    sources = list(old) + [new]
    # 提取文件并集
    extracted, seen = [], set()
    for s in sources:
        for f in s.get("extracted_files", []) or []:
            if f not in seen:
                seen.add(f)
                extracted.append(f)
    extracted.sort()
    # 站点/文件→站点映射：基于并集重新聚类（权威口径，避免与 evidence 不一致）
    sites, file_site = detect_sites(extracted)
    sites_list = list(sites.values())
    # 二进制/密文并集
    binary = sorted(set().union(*[set(s.get("binary_or_opaque_files", []) or []) for s in sources]))
    opaque, seen_op = [], set()
    for s in sources:
        for o in s.get("opaque_files", []) or []:
            if o.get("path") not in seen_op:
                seen_op.add(o.get("path"))
                opaque.append(o)
    # 格式分布求和
    kc = {}
    for s in sources:
        for k, v in (s.get("file_kind_counts", {}) or {}).items():
            kc[k] = kc.get(k, 0) + v
    kc = dict(sorted(kc.items(), key=lambda x: -x[1]))

    def _dedup(seq, keyer):
        out, kset = [], set()
        for x in seq:
            k = keyer(x)
            if k not in kset:
                kset.add(k)
                out.append(x)
        return out

    return {
        "source": existing.get("source") or (sources[0].get("source") if sources else ""),
        "sources": sources,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "merged",
        "include_globs": sorted(set().union(*[set(s.get("include_globs", []) or []) for s in sources])),
        "total_extracted_files": len(extracted),
        "total_extracted_bytes": sum(s.get("total_extracted_bytes", 0) or 0 for s in sources),
        "archives_extracted": [a for s in sources for a in s.get("archives_extracted", []) or []],
        "sites": sites_list,
        "file_site_map": file_site,
        "binary_or_opaque_files": binary,
        "opaque_files": opaque,
        "file_kind_counts": kc,
        "extracted_files": extracted,
        "not_matched_not_extracted": _dedup(
            [x for s in sources for x in s.get("not_matched_not_extracted", []) or []],
            lambda x: json.dumps(x, ensure_ascii=False)),
        "unsupported_archives": _dedup(
            [x for s in sources for x in s.get("unsupported_archives", []) or []],
            lambda x: json.dumps(x, ensure_ascii=False)),
        "skipped": _dedup([x for s in sources for x in s.get("skipped", []) or []],
                          lambda x: json.dumps(x, ensure_ascii=False)),
        "warnings": _dedup([w for s in sources for w in s.get("warnings", []) or []],
                           lambda x: x),
    }


def write_manifest_summary(manifest, workdir):
    """manifest.summary.json：只留 LLM 可直接读取的小字段，完整清单在 manifest.json。"""
    summary = {
        "source": manifest.get("source"),
        "generated_at": manifest.get("generated_at"),
        "mode": manifest.get("mode"),
        "include_globs": manifest.get("include_globs", []),
        "total_extracted_files": manifest.get("total_extracted_files", 0),
        "total_extracted_bytes": manifest.get("total_extracted_bytes", 0),
        "sites": manifest.get("sites", []),
        "file_kind_counts": manifest.get("file_kind_counts", {}),
        "binary_or_opaque_count": len(manifest.get("binary_or_opaque_files", [])),
        "opaque_files": manifest.get("opaque_files", [])[:50],
        "not_matched_count": len(manifest.get("not_matched_not_extracted", [])),
        "unsupported_archives": manifest.get("unsupported_archives", []),
        "skipped_count": len(manifest.get("skipped", [])),
        "warnings": manifest.get("warnings", []),
        "manifest": str(Path(workdir) / "manifest.json"),
        "note": "完整文件清单（extracted_files/file_site_map/not_matched_not_extracted）见 manifest.json，体积较大勿整体读取",
    }
    (Path(workdir) / "manifest.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_kind_by_name(name: str):
    """归档成员名判断（用于递归），含 sniff 兜底由调用方补充。"""
    return archive_kind(name)


def list_only(archive: Path):
    kind = detect_kind(archive)
    items = []
    if kind == "zip":
        with zipfile.ZipFile(long_path(archive)) as zf:
            for i in zf.infolist():
                if not i.is_dir():
                    name = fix_zip_filename(i)
                    items.append({"name": name, "size": i.file_size,
                                  "kind": archive_kind(name) or "file"})
    elif kind == "tar":
        with tarfile.open(long_path(archive), "r|*") as tf:
            for m in tf:
                if m.isfile():
                    items.append({"name": m.name, "size": m.size,
                                  "kind": archive_kind(m.name) or "file"})
    elif kind == "gzip":
        items.append({"name": archive.name[:-3], "size": archive.stat().st_size,
                      "kind": sniff_kind(archive.with_name(archive.name[:-3])) or "file"})
    elif kind == "bzip2":
        inner = archive.name[:-4]
        items.append({"name": inner, "size": archive.stat().st_size,
                      "kind": archive_kind(inner) or "file"})
    elif kind in ("7z", "rar"):
        tool = find_tool(kind)
        if not tool:
            print(json.dumps({"archive": str(archive), "kind": kind,
                              "error": f"缺少解压工具 {'/'.join(EXTERNAL_TOOLS[kind])}"},
                             ensure_ascii=False, indent=2))
            return
        if tool in ("7z", "7za"):
            r = subprocess.run([tool, "l", str(archive)], capture_output=True, text=True)
            print(r.stdout)
            return
        else:
            raise SystemExit(f"{tool} 不支持清单模式，请直接解压")
    else:
        raise SystemExit(f"不支持的归档类型: {archive}")
    kind_counts, top_dirs = {}, {}
    for it in items:
        kind_counts[it["kind"]] = kind_counts.get(it["kind"], 0) + 1
        top = it["name"].split("/", 1)[0]
        top_dirs[top] = top_dirs.get(top, 0) + 1
    # 大包成员清单可达数千行：summary 段给计数/类型分布/顶层目录，明细需落盘后按键提取
    print(json.dumps({"archive": str(archive), "kind": kind,
                      "summary": {"members": len(items), "kind_counts": kind_counts,
                                  "top_entries": sorted(top_dirs.items(),
                                                        key=lambda x: -x[1])[:30]},
                      "members": items}, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="BBU 日志包递归解压（故障导向/多站点/高容错）")
    ap.add_argument("source")
    ap.add_argument("-o", "--workdir")
    ap.add_argument("--include-glob", action="append", default=[])
    ap.add_argument("--include-rules")
    ap.add_argument("--merge-manifest", action="store_true",
                    help="目标 workdir 已有 manifest.json 时，把本次 source 合并进 sources[]（多包解压到同一根）"
                         "；站点/文件清单取并集，未匹配/跳过/告警去重拼接，而非覆盖")
    ap.add_argument("--copy-source", action="store_true",
                    help="把源包复制进 workdir（旧行为，workdir 自包含）；默认不复制，解压产物仍落在 workdir")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--max-depth", type=int, default=12)
    ap.add_argument("--max-bytes", type=int, default=20 * 1024 ** 3)
    ap.add_argument("--max-files", type=int, default=100000)
    ap.add_argument("--progress", action="store_true",
                    help="打印解压进度（每 N 个成员一行，N 由 --progress-interval 控制，默认 200）")
    ap.add_argument("--progress-interval", type=int, default=200,
                    help="进度打印间隔（成员数）；PowerShell 下若进度导致中断可加大或去掉 --progress 重跑，脚本幂等")
    ap.add_argument("--progress-log",
                    help="把解压进度写入该文件而非 stderr；stdout 始终只输出最终 JSON，"
                         "便于脚本解析（也可用 2>/dev/null 丢弃 stderr 进度）")
    ap.add_argument("--force-overwrite", action="store_true",
                    help="目标 workdir 已有多包合并历史（manifest.sources[]）时，默认拒绝覆盖并退出码 3；"
                         "加此参数确认丢弃合并历史并覆盖。追加解压请改用 --merge-manifest")
    args = ap.parse_args()
    for _s in (sys.stdout, sys.stderr):  # Windows GBK 控制台下避免 UnicodeEncodeError
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

    source = Path(args.source)
    if not source.exists():
        raise SystemExit(f"输入不存在: {source}")
    if args.list_only:
        if source.is_dir():
            raise SystemExit("--list-only 仅支持压缩包")
        list_only(source)
        return

    includes = list(args.include_glob)
    if args.include_rules:
        includes.extend(collect_globs_from_rules(Path(args.include_rules)))
    includes = sorted(set(includes)) or None
    if not includes and not source.is_dir():
        print("[i] 未提供 --include-rules/--include-glob，本次将全量解压。故障分析通常只用到少数文件，"
              "建议按需提取（--include-rules <rules.yaml>）以节省时间与空间。", file=sys.stderr)

    workdir = Path(args.workdir)
    if not workdir:
        raise SystemExit("非 list-only 模式必须提供 -o/--workdir")
    workdir.mkdir(parents=True, exist_ok=True)

    # 防覆盖守卫（在解压前判定，避免误覆盖多包合并历史）：
    # 已有 manifest 且含非空 sources[]（多包合并产物）时，必须显式选择追加（--merge-manifest）
    # 或覆盖（--force-overwrite）；二者都不带则拒绝并退出码 3，不产生任何解压副作用。
    # 单包重跑（manifest 只有 source、无 sources）不受拦截，保持幂等。
    mani_path = workdir / "manifest.json"
    existing = None
    if mani_path.exists():
        try:
            existing = json.loads(mani_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = None
            print("[!] 读取已有 manifest.json 失败，本次将覆盖（可能丢失合并历史）",
                  file=sys.stderr)
    if existing is not None and (existing.get("sources") or []) and not args.merge_manifest:
        n_src = len(existing.get("sources") or [])
        if not args.force_overwrite:
            print(f"[x] 拒绝覆盖：workdir 已有多包合并历史（sources={n_src}）。\n"
                  f"    - 要把本次包追加进同一根 → 加 --merge-manifest；\n"
                  f"    - 确认丢弃合并历史、重新覆盖 → 加 --force-overwrite。",
                  file=sys.stderr)
            raise SystemExit(3)
        print(f"[!] --force-overwrite：将覆盖已有的多包合并历史（sources={n_src}）。",
              file=sys.stderr)

    if source.is_dir():
        binary, opaque, kind_counts, total = [], [], {}, 0
        files = []
        for p in source.rglob("*"):
            if p.is_file():
                total += 1
                rel = str(p.relative_to(source))
                files.append(rel)
                info = classify_file(p)
                kind = info.get("kind", "binary")
                kind_counts[kind] = kind_counts.get(kind, 0) + 1
                if kind in OPAQUE_KINDS:
                    binary.append(rel)
                    opaque.append({"path": rel, "kind": kind,
                                   "subtype": info.get("subtype"),
                                   "note": info.get("note")})
        sites, _ = detect_sites(files)
        manifest = {"source": str(source), "mode": "pre-unpacked", "include_globs": [],
                    "total_extracted_files": total, "sites": list(sites.values()),
                    "binary_or_opaque_files": sorted(binary),
                    "opaque_files": opaque,
                    "file_kind_counts": dict(sorted(kind_counts.items(),
                                                  key=lambda x: -x[1])),
                    "unsupported_archives": [], "warnings": []}
    else:
        progress_fh = open(args.progress_log, "w", encoding="utf-8") if args.progress_log else None
        try:
            up = Unpacker(workdir, args.max_depth, args.max_bytes, args.max_files, includes,
                          progress=args.progress, progress_interval=args.progress_interval,
                          progress_fh=progress_fh)
            manifest = up.run(source, copy_source=args.copy_source)
        finally:
            if progress_fh is not None:
                progress_fh.close()

    # 多包合并到同一根：守卫已确保 sources[] 历史只在 --merge-manifest（追加）或
    # --force-overwrite（确认覆盖）下走到这里；单包重跑无 sources，正常覆盖、保持幂等。
    prev_sources = (len(existing.get("sources") or [1]) if existing else 0)
    if args.merge_manifest and existing is not None:
        manifest = merge_manifests(existing, manifest)

    (workdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    write_manifest_summary(manifest, workdir)
    print(json.dumps({
        "mode": manifest.get("mode"),
        "merged_from": len(manifest.get("sources", [1])) if manifest.get("sources") else None,
        "merged_delta": (len(manifest["sources"]) - prev_sources)
                        if manifest.get("sources") and prev_sources else None,
        "extracted": manifest.get("total_extracted_files", 0),
        "sites": len(manifest.get("sites", [])),
        "binary": len(manifest.get("binary_or_opaque_files", [])),
        "not_matched": len(manifest.get("not_matched_not_extracted", [])),
        "unsupported": len(manifest.get("unsupported_archives", [])),
        "warnings": len(manifest.get("warnings", [])),
        "manifest": str(workdir / "manifest.json"),
        "manifest_summary": str(workdir / "manifest.summary.json"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
