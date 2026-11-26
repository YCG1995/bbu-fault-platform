#!/usr/bin/env python3
"""
search_evidence.py — BBU 日志证据检索（流式/时区/站点关联/数值趋势/多站点）

特性：
  - 流式逐行扫描（大文件不爆内存），自动识别 UTF-8/GB18030/Big5
  - 透明支持 .gz；伪装后缀的 gzip（.bak.N 等）按 magic bytes 识别；坏 gzip 跳过不中断
  - CSV 按列匹配；.xlsx（OOXML，如 fm-active/fm-history 导出）按"行=记录"检索，
    表头行列名匹配（columns），域策略声明的密文/私有格式跳过
  - 时间戳统一转 UTC（naive 按 --local-tz-offset，默认 +8）
  - 规则类型：text（关键字/正则）、numeric_trend（阈值/持续度/单调性）、
    record_delta（块记录/实体分组/多快照比较）
  - associate_by：关联键防串证（时序比较只匹配同键证据）；
    associate_by: {site: true} 自动按站点/网元根目录分组（多站点包防串证）
  - 层级容错：glob 路径不匹配时自动按 basename 递归匹配（二次打包/多站点前缀）
  - temporal：规则间先后顺序/时间窗约束
  - verdict：evidence-rules 含 root_causes 段时，自动按 and/or/not 条件 + 关联时序约束
    计算各根因是否成立（ast 白名单安全求值；≥2 站点时按站点分组各算一份）
  - evidence-rules 统一 .yaml（读 yaml 需 PyYAML）

用法：
    python3 search_evidence.py <root> --rules evidence-rules.yaml --json evidence.json
（Windows 下 python3 替换为 python）
"""

import argparse
import ast
import bz2
import csv
import gzip
import hashlib
import lzma
from collections import deque
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_format import classify_file, long_path  # noqa: E402  通用格式分类路由

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

def _utf8_stdio():
    """Windows GBK 控制台/管道下避免中文或符号 UnicodeEncodeError。"""
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


XLSX_SUFFIXES = {"xlsx", "xlsm", "xltx"}  # container_doc 子类型名（log_format）

# 站点/网元特征目录（用于 associate_by: {site: true} 自动分组）
# "bsplog" 必须含：DCM 包 <子网>_<网元>/BspLog/ 根下嵌套 tar 解开的 BSP/ 目录
# 不能成为独立站点根，首个特征段命中决定站点根（见 unpack_logs.detect_sites）
SITE_FEATURE_DIRS = ("当前告警", "历史告警", "活动告警", "bsp", "bsplog", "oamlog",
                     "cloudmat", "pk_root", "pkiroot", "pki_root", "collect",
                     "perf", "diag")
SITE_FEATURE_FILES = ("fm-active", "fm-history", "reboot_history", "clockstatuslog",
                      "collectinfo", "collect_info")

# 轮转/压缩尾缀（clockstatuslog.txt.01、.gz、.gunzipped 视为同一特征文件）
_ROT_SUFFIX_RE = re.compile(r"(?:\.\d+|\.gz|\.bz2|\.xz|\.gunzipped|\.bunzipped|\.xunzipped)+$",
                            re.IGNORECASE)


def _feat_key(seg):
    """路径段归一：剥掉轮转/压缩尾缀，避免轮转文件被当成不同站点特征。"""
    return _ROT_SUFFIX_RE.sub("", seg.lower())

TIME_FORMATS = [
    # 时钟类日志逗号分隔时间（Clk[000]YYYY-MM-DD,HH:MM:SS）优先匹配，避免与 %Y%m%d%H%M%S 误配
    "%Y-%m-%d,%H:%M:%S.%f", "%Y-%m-%d,%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S",
    "%Y.%m.%d %H:%M:%S.%f", "%Y.%m.%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
    "%Y%m%d%H%M%S",
]
TIME_LOOKS = [
    r"\d{4}[-/.,]\d{1,2}[-/.,]\d{1,2}[ T,]\d{1,2}:\d{1,2}:\d{1,2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
    r"\d{14}",
]


def parse_timestamp(raw, state=None):
    """state: 可选 dict 记忆上次命中格式下标（同文件时间格式一致，先试它）。"""
    raw = raw.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    last = state.get("fmt") if state is not None else None
    order = range(len(TIME_FORMATS)) if last is None else \
        ([last] + [i for i in range(len(TIME_FORMATS)) if i != last])
    for i in order:
        try:
            dt = datetime.strptime(raw, TIME_FORMATS[i])
            if state is not None:
                state["fmt"] = i
            return dt
        except ValueError:
            continue
    try:
        v = float(raw)
        if 1e9 < v < 4e12:
            if v > 1e12:
                v /= 1000.0
            return _utc_naive(v)
    except (ValueError, OSError):
        pass
    return None


def _utc_naive(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)


def to_utc(dt, offset_hours):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt - timedelta(hours=offset_hours)


def extract_time(line, te, offset, te_re=None, fmt_state=None):
    if te:
        m = (te_re or re.compile(te["pattern"])).search(line)
        if not m:
            return None, None
        raw = m.group(te.get("group", 1))
        if te.get("epoch"):
            try:
                v = float(raw)
                if v > 1e12:
                    v /= 1000.0
                return _utc_naive(v), raw
            except ValueError:
                return None, None
        # 规则显式声明 formats 时优先按其顺序解析（与 pattern 协同，规避默认嗅探盲区）
        dt = None
        fmts = te.get("formats")
        if fmts:
            for fmt in fmts:
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            dt = parse_timestamp(raw, fmt_state)
        return to_utc(dt, offset), raw
    for look in TIME_LOOKS:
        m = re.search(look, line)
        if m:
            dt = parse_timestamp(m.group(0), fmt_state)
            if dt:
                return to_utc(dt, offset), m.group(0)
    return None, None


def open_lines(path, encoding, cls=None):
    """按 log_format 分类路由打开文本流；非文本类返回 (None, None)。

    text/compressed_text 才打开；压缩按 cls['compression']（gzip/bz2/xz）透明解压；
    编码取分类器探测结果，规则显式 encoding 优先。损坏文件打开即失败返回 None。
    """
    cls = cls or classify_file(path)
    if cls.get("kind") not in ("text", "compressed_text"):
        return None, None
    actual = (encoding if encoding not in ("auto", None)
              else cls.get("encoding") or "utf-8-sig")
    comp = cls.get("compression")
    lp = long_path(path)
    try:
        if comp == "gzip":
            return gzip.open(lp, "rt", encoding=actual, errors="replace"), actual
        if comp == "bzip2":
            return bz2.open(lp, "rt", encoding=actual, errors="replace"), actual
        if comp == "xz":
            return lzma.open(lp, "rt", encoding=actual, errors="replace"), actual
        return open(lp, "r", encoding=actual, errors="replace"), actual
    except OSError:
        return None, None


def _xlsx_col_idx(ref):
    """单元格引用 'BC12' → 列号 54。"""
    m = re.match(r"([A-Za-z]+)", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def iter_xlsx_rows(path):
    """OOXML(.xlsx/.xlsm) → 逐行产出 (sheet名, 行号, [单元格字符串...])。

    纯 stdlib 实现：sharedStrings + workbook rels + sheet XML（iterparse 流式）；
    稀疏单元格按 r 引用补空位；shared/inlineStr/数值直出。
    """
    def local(tag):
        return tag.rsplit("}", 1)[-1]

    with zipfile.ZipFile(long_path(path)) as zf:
        names = set(zf.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for _ev, el in ET.iterparse(zf.open("xl/sharedStrings.xml"),
                                       events=("end",)):
                if local(el.tag) == "si":
                    shared.append("".join(t.text or "" for t in el.iter()
                                          if local(t.tag) == "t"))
                    el.clear()
        rels, sheets = {}, []
        if "xl/_rels/workbook.xml.rels" in names:
            for _ev, el in ET.iterparse(zf.open("xl/_rels/workbook.xml.rels"),
                                       events=("end",)):
                if local(el.tag) == "Relationship":
                    rels[el.get("Id")] = el.get("Target", "")
        if "xl/workbook.xml" in names:
            for _ev, el in ET.iterparse(zf.open("xl/workbook.xml"), events=("end",)):
                if local(el.tag) == "sheet":
                    rid = el.get(
                        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                    tgt = rels.get(rid, "")
                    if tgt:
                        tgt = tgt if tgt.startswith("xl/") else "xl/" + tgt.lstrip("/")
                        sheets.append((el.get("name", "sheet"), tgt))
        if not sheets:  # rels 缺失兜底：按 sheetN.xml 顺序
            sheets = [(Path(n).stem, n) for n in sorted(names)
                      if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
        for sname, member in sheets:
            if member not in names:
                continue
            rno = 0
            for _ev, el in ET.iterparse(zf.open(member), events=("end",)):
                if local(el.tag) != "row":
                    continue
                rno += 1
                cells = []
                for c in el:
                    if local(c.tag) != "c":
                        continue
                    ci = _xlsx_col_idx(c.get("r"))
                    while ci is not None and len(cells) <= ci:
                        cells.append("")
                    ctype, val = c.get("t"), None
                    for ch in c:
                        if local(ch.tag) == "v":
                            val = ch.text
                        elif local(ch.tag) == "is" and ctype == "inlineStr":
                            val = "".join(t.text or "" for t in ch.iter()
                                          if local(t.tag) == "t")
                    if ctype == "s" and val is not None:
                        try:
                            val = shared[int(val)]
                        except (ValueError, IndexError):
                            val = ""
                    val = "" if val is None else str(val)
                    if ci is None:
                        cells.append(val)
                    else:
                        cells[ci] = val
                yield sname, rno, cells
                el.clear()


_GLOB_CACHE = {}


def glob_to_re(pattern):
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


def build_index(root):
    """遍历一次文件树建立索引 [(Path, 归一化相对路径)]；多规则共享避免重复遍历。"""
    root = Path(root)
    return [(p, str(p.relative_to(root)).replace("\\", "/"))
            for p in sorted(root.rglob("*")) if p.is_file()]


def iter_files(root, globs, index=None):
    """按 glob 在文件索引上匹配（内存匹配，语义与 Path.glob 等价），层级兜底：
    1) 原样 glob；2) **/ 前缀；3) 该模式完整路径未命中时按 basename 递归。

    `.gunzipped` 同源副本去重：原件与本规则的命中副本同时命中时保留原件
    （内容相同，证据路径指向原始文件名）；仅副本命中时正常产出。
    """
    root = Path(root)
    if index is None:
        index = build_index(root)
    matched, seen = [], set()

    def emit(p):
        rp = str(p.resolve())
        if rp not in seen:
            seen.add(rp)
            matched.append(p)

    for pat in globs:
        pats = [pat]
        if "/" in pat and not pat.startswith("**/"):
            pats.append("**/" + pat.lstrip("/"))
        elif "/" not in pat:
            pats.append("**/" + pat)
        res = [glob_to_re(c) for c in pats]
        pat_before = len(matched)  # 本 glob 命中前的计数（per-pattern，避免顺序耦合）
        for p, rel in index:
            if any(r.match(rel) for r in res):
                emit(p)
        # basename 兜底：仅当该模式的完整路径候选未命中任何文件时启用
        if len(matched) == pat_before and "/" in pat:
            base = pat.rsplit("/", 1)[-1]
            for p, rel in index:
                if _name_match(p.name, base):
                    emit(p)

    # .gunzipped 副本与原件同时命中时去重（原件为 gzip 也可透明读，内容一致）
    matched_rp = {str(p.resolve()) for p in matched}
    return [p for p in matched
            if not (str(p.resolve()).lower().endswith(".gunzipped")
                    and str(p.resolve())[:-10] in matched_rp)]


def _name_match(name, pattern):
    rx = glob_to_re(pattern.replace("/", ""))
    return bool(re.fullmatch(rx.pattern.replace("^", "").replace("$", ""), name))


def detect_site_root(rel_path):
    """从文件相对路径推断站点/网元根：特征目录的父目录。

    特征段先剥轮转尾缀再匹配（clockstatuslog.txt.01 → clockstatuslog.txt）。
    特征位于扫描根（i==0）时返回 None：扫描根本身即站点，不得以文件名
    （尤其轮转副本）充当站点键，否则单个 BSP 目录会被拆成伪多站点分组。
    """
    parts = rel_path.replace("\\", "/").split("/")
    low = [p.lower() for p in parts]
    for i, seg in enumerate(low):
        if seg in SITE_FEATURE_DIRS:
            return "/".join(parts[:i]) if i > 0 else None
        if any(f in _feat_key(seg) for f in SITE_FEATURE_FILES):
            return "/".join(parts[:i]) if i > 0 else None
    return None


class LineWin:
    """流式行窗口：逐行迭代 + 回看 ctx 上文（deque）/ 前看 ctx 下文（pushback）。

    不把整文件读入内存（GB 级日志安全）；前看行通过回推队列仍参与后续迭代，
    行号与匹配语义与一次性全读完全一致。压缩流中途损坏时优雅终止（保留已读）。"""

    def __init__(self, stream, ctx):
        self._it = iter(stream)
        self._ctx = ctx
        self.prev = deque(maxlen=ctx)
        self._pushback = deque()

    def __iter__(self):
        try:
            while True:
                cur = (self._pushback.popleft() if self._pushback
                       else next(self._it)).rstrip("\n")
                yield cur
                self.prev.append(cur)
        except (StopIteration, OSError):
            return

    def peek(self, n):
        """前看 n 行（不消费，仍参与后续迭代）。"""
        while len(self._pushback) < n:
            try:
                self._pushback.append(next(self._it))
            except (StopIteration, OSError):
                break
        return [l.rstrip("\n") for l in list(self._pushback)[:n]]


def compile_pats(patterns, cs):
    """无 regex 元字符的纯字面量走 ("lit", (文本, ci)) 用 in 判断（远快于 regex）。"""
    out = []
    for p in patterns:
        if re.escape(p) == p:  # 纯字面量（不含元字符）
            out.append(("lit", (p if cs else p.lower(), not cs)))
        else:
            out.append(("re", re.compile(p, 0 if cs else re.IGNORECASE)))
    return out


def line_ok(text, all_of, any_of, none_of):
    low = None

    def hit(entry):
        nonlocal low
        tag, v = entry
        if tag == "re":
            return bool(v.search(text))
        lit, ci = v
        if ci:
            if low is None:
                low = text.lower()
            return lit in low
        return lit in text

    if all_of and not all(hit(p) for p in all_of):
        return False
    if any_of and not any(hit(p) for p in any_of):
        return False
    if none_of and any(hit(p) for p in none_of):
        return False
    return True


def extract_assoc(line, ab, rel_path, rx=None):
    if not ab:
        return None
    if ab.get("site"):
        return detect_site_root(rel_path)
    m = (rx or re.compile(ab["pattern"])).search(line)
    return m.group(ab.get("group", 1)) if m else None


def _eval_one_group(vals, rule):
    """vals: [(ts, value), ...] 已排序。返回 (hit, detail, seq)。"""
    trend = rule.get("trend", "exceed_threshold")
    threshold = rule.get("threshold")
    abs_threshold = rule.get("abs_threshold")
    sustained = int(rule.get("sustained_count", 1))
    window = rule.get("window_minutes")
    if not vals:
        return False, "no values", []
    if window and vals[-1][0]:
        cutoff = vals[-1][0] - timedelta(minutes=window)
        vals = [v for v in vals if v[0] and v[0] >= cutoff]
    if trend in ("exceed_threshold", "sustained_above"):
        if abs_threshold is not None:
            # 绝对值阈值：|v| > abs_threshold，支持负值失效（如 cFCV=-1098.9）
            label = f"|v| > {abs_threshold}"
            pred = lambda v: abs(v) > abs_threshold
        elif threshold is not None:
            label = f"> {threshold}"
            pred = lambda v: v > threshold
        else:
            return False, "缺少 threshold/abs_threshold", []
        if trend == "exceed_threshold":
            above = [v for v in vals if pred(v[1])]
            if len(above) >= sustained:
                return True, f"{len(above)}/{len(vals)} {label}", above[-sustained:]
            return False, f"{len(above)}/{len(vals)} {label}", []
        run, best_seq = 0, []
        for v in vals:
            if pred(v[1]):
                run += 1
                best_seq.append(v)
                if run >= sustained:
                    return True, f"连续 {sustained} 点 {label}", best_seq[-sustained:]
            else:
                run, best_seq = 0, []
        return False, "未形成持续超阈值", []
    if trend in ("monotonic_increase", "monotonic_decrease"):
        if len(vals) < 2:
            return False, "采样不足", []
        increasing = trend == "monotonic_increase"
        ok = all(vals[i][1] <= vals[i + 1][1] for i in range(len(vals) - 1)) if increasing \
            else all(vals[i][1] >= vals[i + 1][1] for i in range(len(vals) - 1))
        n = len(vals)
        return ok, (f"{n}点单调{'递增' if increasing else '递减'}" if ok else "非单调"), \
            (vals[-2:] if ok else [])
    return False, f"unknown {trend}", []


def eval_trend(samples, rule):
    """samples: [(ts, value, key), ...]；有关联键时按键（站点/网元）分组独立评估，
    任一组满足即命中，避免多站点数据互相打断连续性。返回 (hit, detail, seq, key)。"""
    if not samples:
        return False, "no values", [], None
    groups = {}
    for ts, val, key in samples:
        groups.setdefault(key, []).append((ts, val))
    best = (False, "未形成持续超阈值", [], None)
    for key, grp in groups.items():
        grp.sort(key=lambda x: x[0] or datetime.min)
        hit, detail, seq = _eval_one_group(grp, rule)
        if hit:
            return True, detail + (f"（{key}）" if key else ""), seq, key
        if len(grp) > len(best[2]):
            best = (False, detail, seq, key)
    return best


_CLS_CACHE = {}


def classify_cached(path):
    """classify_file 进程级缓存：同一文件多规则命中只读一次头部。"""
    key = str(Path(path).resolve())
    cls = _CLS_CACHE.get(key)
    if cls is None:
        cls = classify_file(path)
        _CLS_CACHE[key] = cls
    return cls


def _prep_rule(rule):
    """编译规则运行态（pattern/te/ab 预编译一次；非法正则落地为 error 态）。"""
    st = {"rule": rule, "matches": [], "matches_by_site": {}, "values": [],
          "files_scanned": 0, "encodings": set(), "glob_fallback": set(),
          "skipped_files": [], "hits_total": 0, "hits_by_site": {}, "done": False,
          "hits_by_file": {}, "eligible_files": set(), "hits_by_associate": {},
          "near_misses": []}
    try:
        rtype = rule.get("type", "text")
        te = rule.get("time_extract")
        ab = rule.get("associate_by")
        cs = rule.get("case_sensitive", False)
        st.update({
            "rtype": rtype, "globs": rule.get("globs", []),
            "encoding": rule.get("encoding", "auto"),
            "te": te, "te_re": re.compile(te["pattern"]) if te else None,
            "ab": ab,
            "ab_re": re.compile(ab["pattern"]) if ab and ab.get("pattern") else None,
            "ctx": int(rule.get("context_lines", 0)),
            "min_m": int(rule.get("min_matches", 1)),
            "value_pat": (re.compile(rule["value_pattern"])
                          if rtype == "numeric_trend" else None),
            "all_of": compile_pats(rule.get("all_of", []), cs),
            "any_of": compile_pats(rule.get("any_of", []), cs),
            "none_of": compile_pats(rule.get("none_of", []), cs),
        })
        if rtype == "record_delta":
            rec = rule.get("record") or {}
            fields = rec.get("fields") or {}
            if not rec.get("start"):
                raise re.error("record_delta 缺少 record.start")
            if not isinstance(fields, dict) or not fields:
                raise re.error("record_delta 缺少 record.fields")
            st["record_start_re"] = re.compile(rec["start"])
            st["record_field_res"] = {str(k): re.compile(str(v)) for k, v in fields.items()}
            st["record_cfg"] = rec
    except re.error as e:
        st["error"] = str(e)
        st["done"] = True
    return st


def _add_near_miss(st, line):
    """O6：总体未命中但 any_of 已部分出现的行，最多保留 3 条。"""
    if len(st.get("near_misses") or []) >= 3:
        return
    low = None
    for entry in st.get("any_of") or []:
        tag, v = entry
        if tag == "re":
            if v.search(line):
                st["near_misses"].append(str(line).strip()[:300])
                return
        else:
            lit, ci = v
            if ci:
                if low is None:
                    low = line.lower()
                if lit in low:
                    st["near_misses"].append(str(line).strip()[:300])
                    return
            elif lit in line:
                st["near_misses"].append(str(line).strip()[:300])
                return


def _rule_header_idx(st, row, is_xlsx):
    """从表头行算 columns 命中列索引（xlsx 表头措辞不符时全列兜底）。"""
    hc = [str(h).strip().lstrip("\ufeff") for h in row] if row else []
    cols = st["rule"].get("columns", [])
    idx = ([i for i, h in enumerate(hc) if any(c in h for c in cols)]
           if cols else list(range(len(hc))))
    if is_xlsx and cols and not idx:
        idx = list(range(len(hc)))
    st["_idx"] = idx


def _row_match(st, row, rel, site, offset, limit, fmt_state):
    """评估一条表格记录（joined=命中列拼串，raw=全行），命中则追加 matches/values。"""
    idx = st.get("_idx") or list(range(len(row)))
    joined = " | ".join(str(row[i]) for i in idx if i < len(row))
    raw = ",".join(str(x) for x in row)
    if st["rtype"] == "numeric_trend":
        vm = st["value_pat"].search(raw)
        if vm:
            try:
                ts, _ = extract_time(raw, st["te"], offset, st["te_re"], fmt_state)
                key = extract_assoc(raw, st["ab"], rel, st["ab_re"])
                st["values"].append((ts, float(vm.group(1)), key))
            except (ValueError, IndexError):
                pass
        return
    if line_ok(joined, st["all_of"], st["any_of"], st["none_of"]):
        st["hits_total"] += 1
        st["hits_by_file"][rel] = st["hits_by_file"].get(rel, 0) + 1
        sk = site or "ALL"
        st["hits_by_site"][sk] = st["hits_by_site"].get(sk, 0) + 1
        # 证据收集上限按站点独立配额（B2）：每个网元至少保留 limit 条代表证据，
        # 避免先扫描到的站点占满全局上限导致其他站点证据全部丢失
        if st["matches_by_site"].get(sk, 0) >= limit:
            return  # 超出该站点证据上限仍继续计数，但不再取上下文
        ts, _ = extract_time(raw, st["te"], offset, st["te_re"], fmt_state)
        assoc = extract_assoc(raw, st["ab"], rel, st["ab_re"])
        if assoc is not None:
            st["hits_by_associate"][str(assoc)] = st["hits_by_associate"].get(str(assoc), 0) + 1
        st["matches"].append({"file": rel, "line_no": st["_lineno"],
                              "line": joined[:2000],
                              "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S UTC") if ts else None,
                              "site": site, "associate": assoc, "context": []})
        st["matches_by_site"][sk] = st["matches_by_site"].get(sk, 0) + 1
    else:
        _add_near_miss(st, raw)


def search_all(root, rules, limit, offset, progress, index=None):
    """文件级合并扫描：每文件单遍评估所有命中规则，输出与逐规则 search_rule 等价。

    先在共享索引上算每规则命中集（iter_files 语义：**/ 前缀兜底 + basename 兜底
    + .gunzipped 同源去重），再反转为 文件→规则；xlsx/csv/行三种读取模式每文件
    各最多一遍。规则证据达 max-evidence 上限后停止取上下文，但继续累计
    hits_total（match_count=真实命中总数，evidence_truncated 标记截断）。
    """
    root = Path(root)
    if index is None:
        index = build_index(root)
    states = [_prep_rule(r) for r in rules]

    # 每规则命中文件集 → 反转为 文件→规则索引
    file_rules = {}
    for i, st in enumerate(states):
        if st.get("error"):
            continue
        try:
            fs = iter_files(root, st["globs"], index)
        except re.error as e:  # 非法 glob：该规则落 error，不影响其他规则
            st["error"] = str(e)
            st["done"] = True
            continue
        for f in fs:
            rel = str(f.relative_to(root))
            file_rules.setdefault(str(f.resolve()), []).append(i)
            if not any(_glob_rel_match(rel, g) for g in st["globs"]):
                st["glob_fallback"].add(f.name)

    for f, _rel_norm in index:
        active = [i for i in file_rules.get(str(f.resolve()), ())
                  if not states[i]["done"]]
        if not active:
            continue
        rel = str(f.relative_to(root))          # 证据路径保持 os.sep（与原版一致）
        site = detect_site_root(rel)            # 同文件恒定，按文件提升
        fmt_state = {}                          # 时间格式记忆（同文件共享）
        record_ids = [i for i in active if states[i]["rtype"] == "record_delta"]
        if record_ids:
            for i in record_ids:
                _scan_record_file(f, rel, site, states[i], limit)
            active = [i for i in active if states[i]["rtype"] != "record_delta"]
            if not active:
                continue
        cls = classify_cached(f)
        is_xlsx = (cls.get("kind") == "container_doc"
                   and cls.get("subtype") in XLSX_SUFFIXES)

        if is_xlsx:
            # xlsx：行=记录，所有命中规则共享一遍行迭代；每 sheet 首行为表头
            for i in active:
                states[i]["files_scanned"] += 1
                states[i]["encodings"].add("xlsx")
                states[i]["eligible_files"].add(rel)
                states[i]["_idx"] = None
            try:
                for _sheet, lineno, row in iter_xlsx_rows(f):
                    for i in active:
                        st = states[i]
                        if st["done"]:
                            continue
                        st["_lineno"] = lineno
                        if lineno == 1:
                            _rule_header_idx(st, row, True)
                            continue
                        if not row:
                            continue
                        _row_match(st, row, rel, site, offset, limit, fmt_state)
            except (csv.Error, StopIteration, zipfile.BadZipFile,
                    KeyError, ET.ParseError, OSError, EOFError):
                pass
            continue

        # 非 xlsx：按规则 format 分 csv 组与行组
        csv_ids = [i for i in active
                   if states[i]["rule"].get("format") in ("csv", "xlsx")]
        line_ids = [i for i in active if i not in set(csv_ids)]
        if not csv_ids and not line_ids:
            continue

        # 行组：LineWin 单遍（ctx 取各规则最大值，按规则切片）
        if line_ids:
            encs = {}
            stream_by_enc = {}
            for i in line_ids:
                st = states[i]
                enc = st["encoding"]
                stream_by_enc.setdefault(enc, None)
                encs[i] = enc
            # 不同 encoding 的规则各开各的流（罕见）；同 encoding 共享一遍
            for enc, _ in stream_by_enc.items():
                ids = [i for i in line_ids if encs[i] == enc]
                stream, real_enc = open_lines(f, enc, cls)
                if stream is None:
                    for i in ids:
                        states[i]["skipped_files"].append(
                            {"file": rel, "kind": cls.get("kind"),
                             "subtype": cls.get("subtype"), "note": cls.get("note")})
                    continue
                for i in ids:
                    states[i]["eligible_files"].add(rel)
                max_ctx = max(states[i]["ctx"] for i in ids)
                lineno = 0
                win = LineWin(stream, max_ctx)
                try:
                    for raw_line in win:
                        lineno += 1
                        for i in ids:
                            st = states[i]
                            if st["done"]:
                                continue
                            if st["rtype"] == "numeric_trend":
                                vm = st["value_pat"].search(raw_line)
                                if vm:
                                    try:
                                        ts, _ = extract_time(
                                            raw_line, st["te"], offset,
                                            st["te_re"], fmt_state)
                                        key = extract_assoc(raw_line, st["ab"],
                                                            rel, st["ab_re"])
                                        st["values"].append((ts, float(vm.group(1)), key))
                                    except (ValueError, IndexError):
                                        pass
                                continue
                            if not line_ok(raw_line, st["all_of"], st["any_of"],
                                           st["none_of"]):
                                _add_near_miss(st, raw_line)
                                continue
                            if line_ok(raw_line, st["all_of"], st["any_of"],
                                       st["none_of"]):
                                st["hits_total"] += 1
                                st["hits_by_file"][rel] = st["hits_by_file"].get(rel, 0) + 1
                                sk = site or "ALL"
                                st["hits_by_site"][sk] = st["hits_by_site"].get(sk, 0) + 1
                                # 按站点独立配额收集证据（B2），保证每个网元都有代表证据
                                if st["matches_by_site"].get(sk, 0) >= limit:
                                    continue  # 超该站点上限仍继续计数，不再取 ts/上下文
                                ts, _ = extract_time(raw_line, st["te"], offset,
                                                     st["te_re"], fmt_state)
                                assoc = extract_assoc(raw_line, st["ab"], rel,
                                                      st["ab_re"])
                                if assoc is not None:
                                    st["hits_by_associate"][str(assoc)] =                                         st["hits_by_associate"].get(str(assoc), 0) + 1
                                c = st["ctx"]
                                context = (
                                    [l.strip()[:2000] for l in list(win.prev)[-c:]]
                                    + [l.strip()[:2000] for l in win.peek(c)]
                                    if c else [])
                                st["matches"].append({
                                    "file": rel, "line_no": lineno,
                                    "line": raw_line.strip()[:2000],
                                    "timestamp": ts.strftime(
                                        "%Y-%m-%d %H:%M:%S UTC") if ts else None,
                                    "site": site, "associate": assoc,
                                    "context": context})
                                st["matches_by_site"][sk] = st["matches_by_site"].get(sk, 0) + 1
                finally:
                    stream.close()
                for i in ids:
                    states[i]["files_scanned"] += 1
                    if real_enc:
                        states[i]["encodings"].add(real_enc)
                if progress:
                    hits = sum(states[i]["hits_total"] for i in ids)
                    print(f"  ... {f.name}: {lineno} lines, {hits} hits",
                          file=sys.stderr)

        # csv 组：csv.reader 单遍，首行为表头
        if csv_ids:
            for i in csv_ids:
                st = states[i]
                stream, real_enc = open_lines(f, st["encoding"], cls)
                if stream is None:
                    st["skipped_files"].append(
                        {"file": rel, "kind": cls.get("kind"),
                         "subtype": cls.get("subtype"), "note": cls.get("note")})
                    continue
                st["eligible_files"].add(rel)
                st["files_scanned"] += 1
                if real_enc:
                    st["encodings"].add(real_enc)
                try:
                    rows = ((None, n, r) for n, r in enumerate(csv.reader(stream), 1))
                    _first = next(rows, None)
                    _rule_header_idx(st, _first[2] if _first else [], False)
                    for _sheet, lineno, row in rows:
                        if st["done"]:
                            break
                        st["_lineno"] = lineno
                        if not row:
                            continue
                        _row_match(st, row, rel, site, offset, limit, fmt_state)
                except (csv.Error, StopIteration, OSError, EOFError):
                    pass
                finally:
                    stream.close()

    out = []
    for st in states:
        if st.get("error"):
            out.append({"rule_id": st["rule"].get("id", "?"), "hit": False,
                        "match_count": 0, "evidence": [], "files_scanned": 0,
                        "error": st["error"]})
            continue
        out.append(_final(st["rule"], st["matches"], st["values"],
                          st["files_scanned"], st["encodings"], st["ab"],
                          st["min_m"], st["glob_fallback"], st["skipped_files"],
                          st["hits_total"], st["hits_by_site"],
                          st["hits_by_file"], st["eligible_files"],
                          st["hits_by_associate"], st["near_misses"]))
    return out


def search_rule(root, rule, limit, offset, progress, index=None):
    """兼容包装：单规则 → search_all。"""
    return search_all(root, [rule], limit, offset, progress, index)[0]


def _glob_rel_match(rel, pattern):
    pats = [pattern]
    if not pattern.startswith("**/"):
        pats.append("**/" + pattern.lstrip("/"))
    return any(glob_to_re(p).match(rel.replace("\\", "/")) for p in pats)


def _record_spec_ok(values, spec):
    """record_delta 的单字段比较；values 为该实体的字段序列。"""
    if not values:
        return False
    first, last = values[0], values[-1]
    if isinstance(spec, (int, float)):
        try:
            return float(last) == float(spec)
        except (TypeError, ValueError):
            return False
    text = str(spec).strip()
    low = text.lower()
    if low in ("increasing", "increases", "not_increasing", "non_increasing",
               "flat_or_decreasing"):
        try:
            nums = [float(v) for v in values]
        except (TypeError, ValueError):
            nums = None
        if nums is not None:
            if low in ("increasing", "increases"):
                return nums[-1] > nums[0] and all(b >= a for a, b in zip(nums, nums[1:]))
            return all(b <= a for a, b in zip(nums, nums[1:]))
        if low in ("increasing", "increases"):
            return str(last) > str(first)
        return str(last) <= str(first)
    if low in ("zero", "zero_or_not_increasing"):
        try:
            if low == "zero":
                return float(last) == 0
            return float(last) == 0 or float(last) <= float(first)
        except (TypeError, ValueError):
            return str(last) == "0"
    if low in ("positive", "nonzero"):
        try:
            return float(last) != 0
        except (TypeError, ValueError):
            return bool(str(last))
    if low in ("unchanged", "stable"):
        return all(str(v) == str(first) for v in values)
    if low in ("open", "closed"):
        return str(last).strip().lower() == low
    m = re.fullmatch(r"(last|first|max|min)_(gt|ge|lt|le|eq)\s*:\s*([0-9.]+)", text)
    if m:
        which, op, num = m.groups()
        try:
            val = (float(first) if which == "first" else
                   float(last) if which == "last" else
                   float(max(float(x) for x in values)) if which == "max" else
                   float(min(float(x) for x in values)))
        except (TypeError, ValueError):
            return False
        target = float(num)
        return {">": val > target, ">=": val >= target, "lt": val < target,
                "le": val <= target, "eq": val == target}[
                    {"gt": ">", "ge": ">=", "lt": "lt", "le": "le", "eq": "eq"}[op]]
    return str(last) == text


def _record_entity(rec, start_groups, fields, site):
    names = rec.get("entity") or ["site"]
    vals = []
    for name in names:
        if name == "site":
            vals.append(site or "ALL")
        elif name in start_groups:
            vals.append(start_groups.get(name))
        elif name in fields:
            values = fields.get(name) or []
            vals.append(values[-1] if values else None)
        else:
            vals.append(None)
    return tuple(vals)


def _scan_record_file(path, rel, site, st, limit):
    """扫描一个文件的块记录，返回按实体聚合后的命中证据。"""
    rec = st.get("record_cfg") or {}
    cls = classify_cached(path)
    stream, enc = open_lines(path, st["encoding"], cls)
    if stream is None:
        st["skipped_files"].append({"file": rel, "kind": cls.get("kind"),
                                    "subtype": cls.get("subtype"), "note": cls.get("note")})
        return
    records = []
    current = None
    lineno = 0
    try:
        for raw in stream:
            lineno += 1
            start = st["record_start_re"].search(raw)
            if start:
                if current:
                    records.append(current)
                current = {"start_groups": start.groupdict(), "start_line": lineno,
                           "line": raw.strip()[:2000], "fields": {}}
            if current is None:
                continue
            for name, rx in st["record_field_res"].items():
                m = rx.search(raw)
                if not m:
                    continue
                try:
                    value = m.group("value")
                except IndexError:
                    value = m.group(1) if m.groups() else m.group(0)
                current["fields"].setdefault(name, []).append(value)
        if current:
            records.append(current)
    finally:
        stream.close()

    groups = {}
    for idx, record in enumerate(records):
        key = _record_entity(rec, record["start_groups"], record["fields"], site)
        groups.setdefault(key, []).append(record)

    hits = []
    for key, rows in groups.items():
        require = rec.get("require") or {}
        any_spec = rec.get("any") or {}
        def field_values(field):
            return [r["fields"][field][-1] for r in rows if r["fields"].get(field)]
        def ok(spec_map):
            if not spec_map:
                return True
            return all(_record_spec_ok(field_values(f), spec) for f, spec in spec_map.items())
        if require and not ok(require):
            continue
        if any_spec and not any(_record_spec_ok(field_values(f), spec)
                                for f, spec in any_spec.items()):
            continue
        last = rows[-1]
        detail = ", ".join(f"{k}={v[-1] if v else ''}" for k, v in last["fields"].items())
        hits.append({
            "file": rel, "line_no": last["start_line"],
            # 记录起始行常用星号分隔；报告表格会把 ** 误当强调，展示行去掉星号。
            "line": (last["line"].replace("*", "") + " | " + detail)[:2000],
            "timestamp": None, "site": site, "associate": key[-1] if len(key) > 1 else None,
            "context": [], "entity": list(key),
        })
    st["files_scanned"] += 1
    st["eligible_files"].add(rel)
    if hits:
        st["hits_total"] += len(hits)
        st["hits_by_file"][rel] = st["hits_by_file"].get(rel, 0) + len(hits)
        for item in hits[:limit]:
            st["matches"].append(item)
            sk = site or "ALL"
            st["hits_by_site"][sk] = st["hits_by_site"].get(sk, 0) + 1
        for item in hits:
            assoc = item.get("associate")
            if assoc is not None:
                st["hits_by_associate"][str(assoc)] =                     st["hits_by_associate"].get(str(assoc), 0) + 1


def _final(rule, matches, values, files_scanned, encodings, ab, min_m,
             glob_fallback, skipped_files=None, hits_total=None, hits_by_site=None,
             hits_by_file=None, eligible_files=None, hits_by_associate=None,
             near_misses=None):
    total = hits_total if hits_total is not None else len(matches)
    hits_by_file = dict(hits_by_file or {})
    hits_by_associate = dict(hits_by_associate or {})
    if rule.get("type") == "numeric_trend" and values:
        for _ts, _val, key in values:
            if key is not None:
                hits_by_associate[str(key)] = hits_by_associate.get(str(key), 0) + 1
    eligible = set(eligible_files or ())
    for rel in eligible:
        hits_by_file.setdefault(rel, 0)
    qualifying = sorted(rel for rel in eligible if hits_by_file.get(rel, 0) >= min_m)
    missed = sorted(rel for rel in eligible if rel not in set(qualifying))
    scope = str(rule.get("file_scope") or "any").strip().lower()
    if scope not in ("any", "at_least", "each"):
        scope = "any"
    min_files = int(rule.get("min_files") or 0)
    result = {"rule_id": rule.get("id", "?"), "step": rule.get("step", ""),
              "description": rule.get("description", ""), "type": rule.get("type", "text"),
              "match_count": total, "evidence_count": len(matches),
              "evidence_truncated": total > len(matches),
              "hits_by_site": hits_by_site or {},
              "hits_by_associate": dict(sorted(hits_by_associate.items())),
              "files_scanned": files_scanned,
              "files_eligible": len(eligible),
              "files_hit": qualifying,
              "files_missed": missed,
              "hits_by_file": dict(sorted(hits_by_file.items())),
              "file_coverage": round(len(qualifying) / len(eligible), 3) if eligible else 0.0,
              "file_scope": scope, "min_files": min_files,
              "min_matches": min_m, "encodings": sorted(encodings),
              "glob_fallback_files": sorted(glob_fallback) if glob_fallback else [],
              "skipped_files": (skipped_files or [])[:20],
              "skipped_count": len(skipped_files or []),
              "files_skipped": len(skipped_files or []),
              "near_misses": list(near_misses or []),
              "evidence": matches}
    if rule.get("verified") is False:
        result["verified"] = False
    if rule.get("type") == "numeric_trend":
        hit, detail, seq, hit_key = eval_trend(values, rule)
        result["hit"] = hit
        result["trend_detail"] = detail
        result["samples"] = [{"timestamp": t.strftime("%Y-%m-%d %H:%M:%S UTC") if t else None,
                              "value": v, "site": k} for t, v, k in values[:20]]
        if hit and seq:
            last = seq[-1]
            # 站点关联键时，命中证据的 site 取该组键值，保证时序在同站点内配对
            site_val = hit_key if (ab or {}).get("site") else None
            # M2：numeric_trend 证据行反斜杠统一编码为 HTML 实体 &#92;，
            # 保证写入 MD 的字节序列确定且单义（人类重写 MD 不再纠结反斜杠层数）；
            # render_report._inline 渲染 HTML 时还原为 \，verify 指纹对规范字节直接可用。
            ev_line = f"{rule.get('description','')}: value={last[1]} ({detail})"
            ev_line = ev_line.replace("\\", "&#92;")
            result["evidence"].append({
                "file": "numeric_trend", "line_no": 0,
                "line": ev_line,
                "timestamp": last[0].strftime("%Y-%m-%d %H:%M:%S UTC") if last[0] else None,
                "site": site_val, "associate": None if (ab or {}).get("site") else hit_key,
                "context": [],
            })
    else:
        if scope == "any":
            result["hit"] = total >= min_m
        elif scope == "at_least":
            result["hit"] = len(qualifying) >= max(1, min_files)
        else:  # each
            result["hit"] = bool(eligible) and not missed and min_files <= len(eligible)
        result["file_scope_detail"] = (
            f"{len(qualifying)}/{len(eligible)} 个文件达到 ≥{min_m} 次"
            + (f"；要求 {min_files} 个文件" if scope == "at_least" else "")
            if eligible else "无可用文本文件（跳过 binary/ciphered/空文件）")
    return result


def eval_temporal(temporal, results):
    by_id = {r["rule_id"]: r for r in results}
    out = []
    for con in temporal or []:
        cid = con.get("id", "T?")
        r1, r2 = by_id.get(con.get("first")), by_id.get(con.get("second"))
        entry = {"id": cid, "first": con.get("first"), "second": con.get("second"),
                 "satisfied": False, "min_gap_minutes": con.get("min_gap_minutes", 0),
                 "max_gap_minutes": con.get("max_gap_minutes"), "pairs": [], "reason": ""}
        if not r1 or not r2 or not r1["evidence"] or not r2["evidence"]:
            entry["reason"] = "规则未命中或无证据"
            out.append(entry)
            continue
        min_gap = float(con.get("min_gap_minutes", 0)) * 60
        max_gap = float(con["max_gap_minutes"]) * 60 if con.get("max_gap_minutes") is not None else None
        for e1 in r1["evidence"]:
            t1s = e1.get("timestamp")
            if not t1s:
                continue
            t1 = datetime.strptime(t1s, "%Y-%m-%d %H:%M:%S UTC")
            for e2 in r2["evidence"]:
                t2s = e2.get("timestamp")
                if not t2s:
                    continue
                # 关联键：双方都有键且不同则跳过（站点/网元隔离）
                k1 = e1.get("associate") or e1.get("site")
                k2 = e2.get("associate") or e2.get("site")
                if k1 and k2 and k1 != k2:
                    continue
                t2 = datetime.strptime(t2s, "%Y-%m-%d %H:%M:%S UTC")
                gap = (t2 - t1).total_seconds()
                if gap >= min_gap and (max_gap is None or gap <= max_gap):
                    entry["satisfied"] = True
                    entry["pairs"].append({
                        "first": {k: e1.get(k) for k in ("file", "line_no", "timestamp", "site", "associate")},
                        "second": {k: e2.get(k) for k in ("file", "line_no", "timestamp", "site", "associate")},
                        "gap_minutes": round(gap / 60, 2),
                    })
        if not entry["satisfied"] and not entry["reason"]:
            entry["reason"] = "未发现满足间隔/关联键要求的先后顺序"
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# 根因判定（verdict）：安全布尔求值 + 按站点分组 + 关联时序约束全满足
# ---------------------------------------------------------------------------
_RULE_ID_RE = re.compile(r"\b[A-Z]\d+\b")
_ALLOWED_AST = (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.And, ast.Or, ast.Not,
                ast.Name, ast.Constant, ast.Load)


def eval_condition(expr, hit_ids):
    """用 ast 白名单求值 and/or/not 布尔表达式；规则 ID 整词替换为 True/False。
    返回 (bool|None, substituted_expr, error)。"""
    ids = set(hit_ids)
    substituted = _RULE_ID_RE.sub(lambda m: str(m.group(0) in ids), expr or "")
    if not substituted.strip():
        return None, substituted, "空表达式"
    try:
        tree = ast.parse(substituted, mode="eval")
    except SyntaxError as e:
        return None, substituted, f"语法错误: {e.msg}"
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST):
            return None, substituted, f"不允许的表达式节点: {type(node).__name__}"
        if isinstance(node, ast.Name) and node.id not in ("True", "False"):
            return None, substituted, f"未知标识符: {node.id}"
        if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
            return None, substituted, f"不允许的常量: {node.value!r}"
    return bool(eval(compile(tree, "<condition>", "eval"), {"__builtins__": {}}, {})), substituted, None


def positive_refs(cond):
    """condition 中以肯定形式引用的规则 ID（排除 `not E4` 这类否定引用）。"""
    return set(_RULE_ID_RE.findall(re.sub(r"\bnot\s+[A-Z]\d+\b", " ", cond or "")))


def related_temporal(rc, temporal_defs):
    """根因关联的时序约束：显式 root_causes[].temporal 优先；
    否则取 first/second 都以肯定形式出现在 condition 中的约束（被 not 否定的规则不参与）。"""
    explicit = rc.get("temporal")
    if explicit:
        return [str(t) for t in explicit]
    refs = positive_refs(rc.get("condition", ""))
    return [t.get("id") for t in temporal_defs
            if t.get("first") in refs and t.get("second") in refs]


def _group_hit(result, key, include_all=True):
    if key is None:
        return bool(result.get("hit"))
    if result.get("type") == "numeric_trend":
        return bool(result.get("hit")) and any(
            (e.get("site") or key) == key for e in result.get("evidence", []))
    # 分站点命中数取 hits_by_site（证据截断时仍为真实计数；旧输出无该字段时回退证据数）。
    # O2：site 作用域下 include_all=False，ALL 桶（无站点归属）不再计入任何站点，
    # 避免“站点 A 的 E1 + 无站点的 E2”被拼成同一站点结论。
    hbs = result.get("hits_by_site")
    if hbs:
        n = hbs.get(key, 0)
        if include_all and key != "ALL":
            n += hbs.get("ALL", 0)
    else:
        n = sum(1 for e in result.get("evidence", [])
                if (e.get("site") or key) == key or (include_all and not e.get("site")))
    return n >= int(result.get("min_matches", 1))


def _group_hit_associate(result, key):
    """O3：只在单个关联键（SN/单板/小区）的证据上求值，缺键不借 ALL。"""
    if key is None:
        return bool(result.get("hit"))
    if result.get("type") == "numeric_trend":
        return bool(result.get("hit")) and any(
            str(e.get("associate") or e.get("site") or "") == str(key)
            for e in result.get("evidence", []))
    hba = result.get("hits_by_associate")
    if hba:
        return hba.get(str(key), 0) >= int(result.get("min_matches", 1))
    return any(str(e.get("associate") or e.get("site") or "") == str(key)
               for e in result.get("evidence", []))


def _group_temporal_ok(entry, key, associate=False):
    if key is None:
        return bool(entry.get("satisfied"))
    field = "associate" if associate else "site"
    return any((p["first"].get(field) or key) == key
               and (p["second"].get(field) or key) == key
               for p in entry.get("pairs", []))


def _legacy_eval_verdict(root_causes, results, teval, temporal_defs, sites, truncated):
    """1.25 及以前的站点求值：ALL 桶计入每个站点，primary 永远取 groups["ALL"]。"""
    by_t = {t["id"]: t for t in teval}
    keys = [None] + (list(sites) if len(sites) >= 2 else [])
    groups = {}
    for key in keys:
        groups["ALL" if key is None else key] = _eval_group(
            root_causes, results, by_t, temporal_defs, key, True)
    return {"primary": groups["ALL"]["primary"], "status": groups["ALL"]["status"],
            "grouped_by_site": len(keys) > 1, "truncated": truncated, "groups": groups}


def _eval_group(root_causes, results, by_t, temporal_defs, key, include_all,
                hit_fn=None, temporal_associate=False):
    """在单个分组（ALL 或某站点/关联键）内求值全部根因。"""
    hit_fn = hit_fn or _group_hit
    hits = {r["rule_id"] for r in results if hit_fn(r, key, include_all=include_all)}
    rcs, established = [], []
    for rc in root_causes:
        value, sub, err = eval_condition(rc.get("condition", ""), hits)
        req = related_temporal(rc, temporal_defs)
        t_ok = {tid: _group_temporal_ok(by_t.get(tid, {}), key,
                                        associate=temporal_associate) for tid in req}
        ok = bool(value) and all(t_ok.values())
        item = {"id": rc.get("id"), "name": rc.get("name", ""),
                "condition": rc.get("condition", ""), "substituted": sub,
                "value": value, "confidence": rc.get("confidence", "medium"),
                "temporal_required": req, "temporal_ok": t_ok, "established": ok}
        if err:
            item["error"] = err
        if value and not ok:
            item["note"] = "布尔条件成立但关联时序约束未全部满足，不得直接判定"
        rcs.append(item)
        if ok:
            established.append(rc.get("id"))
    conflict = [r["id"] for r in rcs if r["value"] and not r["established"]]
    status = "matched" if established else ("temporal_conflict" if conflict else "unmatched")
    return {"hit_rules": sorted(hits), "root_causes": rcs,
            "primary": established[0] if established else "NONE",
            "others": established[1:], "temporal_conflict": conflict, "status": status}


def _eval_verdict_v2(root_causes, results, teval, temporal_defs, sites, truncated,
                     rule_scope_v2="on"):
    """按根因 scope 求值：all 全局；site 逐站点且 ALL 不计入站点；associate 逐关联键。

    单站点/无站点包自动退化为 all，保持旧 profile 语义不变。
    """
    if not root_causes:
        return None
    by_t = {t["id"]: t for t in teval}
    multi = len(sites) >= 2
    site_ids = sorted(sites) if multi else []
    scopes = {rc.get("id"): str(rc.get("scope") or "site").lower() for rc in root_causes}
    assoc_roots = [rc for rc in root_causes if scopes.get(rc.get("id")) == "associate"]
    site_roots = [rc for rc in root_causes if scopes.get(rc.get("id")) not in ("all", "associate")]
    all_roots = [rc for rc in root_causes if scopes.get(rc.get("id")) == "all"]

    # ALL 始终计算：all 作用域结论 + site 冲突诊断（包含 site 根因，便于发现跨站拼证）
    groups = {"ALL": _eval_group(root_causes, results, by_t, temporal_defs, None, True)}
    for site in site_ids:
        groups[site] = _eval_group(site_roots, results, by_t, temporal_defs, site, False)

    all_primary = groups["ALL"]["primary"]
    established_sites = [s for s in site_ids if groups[s]["primary"] != "NONE"]
    all_established = [rc["id"] for rc in groups["ALL"]["root_causes"]
                       if rc.get("established") and rc.get("id") in {r.get("id") for r in all_roots}]

    # O3：关联键世界，缺失键不得借 ALL/别的键
    assoc_keys = sorted({str(k) for r in results
                         for k in (r.get("hits_by_associate") or {})
                         if str(k) not in ("ALL", "None", "")})
    associate_groups = {}
    keys_established, keys_missing = [], []
    assoc_hit_fn = lambda r, k, include_all=True: _group_hit_associate(r, k)
    for key in assoc_keys:
        g = _eval_group(assoc_roots or root_causes, results, by_t, temporal_defs,
                        key, False, hit_fn=assoc_hit_fn, temporal_associate=True)
        associate_groups[key] = g
        if g["primary"] != "NONE":
            keys_established.append(key)
        missing = sorted({rid for rc in (assoc_roots or root_causes)
                          for rid in positive_refs(rc.get("condition", ""))}
                         - set(g["hit_rules"]))
        if missing:
            keys_missing.append({"key": key, "rules": missing})
    if not assoc_keys and assoc_roots:
        keys_missing = [{"key": None, "rules": sorted({rid for rc in assoc_roots
                            for rid in positive_refs(rc.get("condition", ""))})}]

    primary = None
    primary_site = None
    primary_key = None
    if keys_established and assoc_roots:
        primary_key = keys_established[0]
        primary = associate_groups[primary_key]["primary"]
    elif established_sites:
        primary_site = established_sites[0]
        primary = groups[primary_site]["primary"]
    elif all_established:
        primary = all_established[0]
    elif all_primary != "NONE" and not multi and not assoc_roots:
        primary = all_primary
    elif all_primary != "NONE" and multi and not site_roots and not assoc_roots:
        primary = all_primary

    multi_site_conflict = bool(multi and not established_sites and not keys_established
                               and groups["ALL"]["primary"] != "NONE")
    status = "matched" if primary is not None else "unmatched"
    if multi_site_conflict:
        status = "unmatched"
    if assoc_roots and not keys_established:
        status = "unmatched"
    scope_name = "associate" if (assoc_roots and not site_roots) else ("site" if multi else "all")
    return {"primary": primary or "NONE", "status": status,
            "primary_site": primary_site, "primary_key": primary_key,
            "established_sites": established_sites,
            "keys_evaluated": assoc_keys,
            "keys_established": keys_established,
            "keys_missing": keys_missing,
            "associate_groups": associate_groups,
            "multi_site_conflict": multi_site_conflict,
            "scope": scope_name,
            "rule_scope_v2": rule_scope_v2,
            "grouped_by_site": multi, "truncated": truncated, "groups": groups}


def eval_verdict(root_causes, results, teval, temporal_defs, sites, truncated,
                 rule_scope_v2="shadow"):
    """规则作用域总入口：off=旧逻辑；shadow=旧结论 + v2 对比；on=新结论。"""
    if not root_causes:
        return None
    mode = str(rule_scope_v2 or "shadow").lower()
    if mode not in ("off", "shadow", "on"):
        mode = "shadow"
    legacy = _legacy_eval_verdict(root_causes, results, teval, temporal_defs, sites, truncated)
    if mode == "off":
        legacy["rule_scope_v2"] = "off"
        return legacy
    v2 = _eval_verdict_v2(root_causes, results, teval, temporal_defs, sites, truncated,
                          rule_scope_v2=mode)
    if mode == "on":
        return v2
    changed = (legacy.get("primary") != v2.get("primary")
               or legacy.get("status") != v2.get("status"))
    v2["shadow"] = {
        "legacy_primary": legacy.get("primary"),
        "legacy_status": legacy.get("status"),
        "changed": changed,
        "legacy_groups": {k: {"primary": g.get("primary"), "status": g.get("status")}
                          for k, g in (legacy.get("groups") or {}).items()},
    }
    v2["mode"] = "shadow"
    return v2


def _load_doc(path):
    try:
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() not in (".yaml", ".yml"):
            sys.exit(f"{path} 仅支持 .yaml 规则/配置")
        data = yaml.safe_load(text) if _HAS_YAML else None
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_mode(path):
    """rules 文档顶层 mode（如 triage）；读取失败返回 None。"""
    return _load_doc(path).get("mode")


def load_scope(path):
    """rules 文档顶层 rule_scope_v2（off/shadow/on）；缺省 shadow。"""
    value = _load_doc(path).get("rule_scope_v2")
    return str(value).lower() if value in ("off", "shadow", "on") else "shadow"


def load_rules(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() not in (".yaml", ".yml"):
        sys.exit(f"{path} 仅支持 .yaml 规则/配置")
    if not _HAS_YAML:
        sys.exit(f"{path} 为 YAML 但当前环境缺少 PyYAML：pip install pyyaml")
    data = yaml.safe_load(text)
    if isinstance(data, dict):
        return data.get("rules", []), data.get("temporal", []) or [], data.get("root_causes", []) or []
    return data, [], []


def brief_summary(summary: dict) -> str:
    """面向人/LLM 阅读的精简摘要（MD）：verdict、逐规则命中与首条证据、时序、兜底/截断提示。"""
    v = summary.get("verdict") or {}
    L = ["# 证据检索摘要", ""]
    if summary.get("mode") == "triage":
        L += ["> **分诊候选集**：规则均未经验证，命中仅为线索，不构成根因结论；需人工在日志中逐条核实。", ""]
    L += [
         f"- 根目录：`{summary.get('root')}`；站点 {len(summary.get('detected_sites', []))} 个"
         + (f"：{'、'.join(summary['detected_sites'])}" if summary.get("detected_sites") else ""),
         f"- 命中 {len(summary.get('hit_rules', []))}/{summary.get('total_rules', 0)} 条规则；"
         f"时序满足 {len(summary.get('temporal_satisfied', []))}/{len(summary.get('temporal', []))}；"
         f"规则错误 {len(summary.get('error_rules', []))}"]
    if v:
        L.append(f"- **verdict**：`{v.get('status')}` primary=`{v.get('primary')}`")
        for k, g in (v.get("groups") or {}).items():
            if k == "ALL" and not v.get("grouped_by_site"):
                continue
            L.append(f"  - [{k}] {g.get('status')} primary={g.get('primary')}"
                     + (f" 其他={g.get('others')}" if g.get("others") else "")
                     + (f" 时序冲突={g.get('temporal_conflict')}" if g.get("temporal_conflict") else ""))
        for r in (v.get("groups") or {}).get("ALL", {}).get("root_causes", []):
            L.append(f"  - {r.get('id')}: `{r.get('condition')}` → {r.get('substituted')} = {r.get('value')}"
                     + (f"；时序 {r.get('temporal_ok')}" if r.get("temporal_required") else "")
                     + f" → {'成立' if r.get('established') else '不成立'}")
        if v.get("truncated"):
            L.append(f"- 证据收集被截断的规则（match_count 为真实总数，仅前 --max-evidence-per-rule 条入 JSON）："
                     f"{', '.join(v['truncated'])}")
    if summary.get("glob_fallback"):
        L.append(f"- glob 兜底命中（需人工确认）：{', '.join(summary['glob_fallback'])}")
    L += ["", "| 规则 | 步骤 | 判定 | 匹配数 | 文件数 | 首条证据（文件:行号 时间戳） | 原文片段 |", "|---|---|---|---|---|---|---|"]
    for r in summary.get("results", []):
        flag = "HIT" if r.get("hit") else ("ERR" if "error" in r else "MISS")
        if flag == "HIT" and r.get("verified") is False:
            flag = "HIT(未验证)"
        e = (r.get("evidence") or [{}])[0]
        src = f"{str(e.get('file', '')).replace(chr(92), '/')}:{e.get('line_no', '')} {e.get('timestamp') or ''}".strip() if e else ""
        raw = str(e.get("line", ""))[:100].replace("|", "\\|") if e else ""
        extra = f" trend={r.get('trend_detail', '')}" if r.get("type") == "numeric_trend" else ""
        L.append(f"| {r['rule_id']} | {r.get('step', '')} | {flag}{extra} | {r.get('match_count', 0)}/≥{r.get('min_matches', 1)} "
                 f"| {r.get('files_scanned', 0)} | {src} | {raw} |")
    if summary.get("temporal"):
        L += ["", "| 时序 | 要求 | 结论 | 实际 |", "|---|---|---|---|"]
        for t in summary["temporal"]:
            p = (t.get("pairs") or [{}])[0]
            actual = (f"{p['first'].get('timestamp')} → {p['second'].get('timestamp')}，{p.get('gap_minutes')} 分钟"
                      if t.get("satisfied") and p else (t.get("reason") or "无满足的证据对"))
            L.append(f"| {t.get('id')} | {t.get('first')} 先于 {t.get('second')}，{t.get('min_gap_minutes', 0)}~{t.get('max_gap_minutes')} 分钟 "
                     f"| {'满足' if t.get('satisfied') else '不满足'} | {actual} |")
    return "\n".join(L) + "\n"


def rules_from_profile(profile: dict):
    """从 fault-profile 装配 rules 文档：汇集 analysis.steps[].rules +
    顶层 temporal/root_causes（等价生成的 evidence-rules.yaml）。"""
    rules = []
    for step in profile.get("analysis", {}).get("steps", []) or []:
        for r in step.get("rules", []) or []:
            r = dict(r)
            r.setdefault("step", step.get("id"))
            rules.append(r)
    return (rules,
            profile.get("temporal") or [],
            profile.get("root_causes") or [])


def _file_hashes(root, results, max_files=200):
    """为产生证据或被跳过的文件计算 sha256（原始字节），供证据溯源/报告展示。"""
    rels = set()
    for r in results:
        rels.update(e.get("file") for e in r.get("evidence", []))
        rels.update(s.get("file") for s in r.get("skipped_files", []))
    hashes = {}
    for rel in sorted(x for x in rels if x):
        p = root / rel
        try:
            h = hashlib.sha256()
            with open(long_path(p), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            hashes[rel] = h.hexdigest()
        except OSError:
            continue
        if len(hashes) >= max_files:
            break
    return hashes


def main():
    ap = argparse.ArgumentParser(description="BBU 日志证据检索（流式/时区/站点关联/数值趋势）")
    ap.add_argument("root")
    ap.add_argument("--rules", help="rules YAML（{rules,temporal,root_causes}）")
    ap.add_argument("--profile",
                    help="fault-profile.yaml：直取内嵌 rules（免生成 evidence-rules.yaml）")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--max-evidence-per-rule", type=int, default=50)
    ap.add_argument("--local-tz-offset", type=float, default=8.0)
    ap.add_argument("--progress", action="store_true")
    ap.add_argument("--brief", metavar="SUMMARY_MD",
                    help="另写一份面向阅读的精简摘要（verdict/逐规则命中/首条证据），免读大 JSON")
    args = ap.parse_args()
    _utf8_stdio()

    scope_mode = "shadow"
    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"目录不存在: {root}")
    if args.profile:
        prof_path = Path(args.profile)
        if prof_path.suffix.lower() not in (".yaml", ".yml"):
            sys.exit(f"{prof_path} 仅支持 .yaml 规则/配置")
        if not _HAS_YAML:
            sys.exit(f"{prof_path} 为 YAML 但当前环境缺少 PyYAML：pip install pyyaml")
        text = prof_path.read_text(encoding="utf-8")
        prof = yaml.safe_load(text)
        rules, temporal, root_causes = rules_from_profile(prof)
        mode = prof.get("mode")
        scope_mode = str(prof.get("rule_scope_v2") or "shadow").lower()
    elif args.rules:
        rules, temporal, root_causes = load_rules(args.rules)
        mode = load_mode(args.rules)
        scope_mode = load_scope(args.rules)
    else:
        sys.exit("必须提供 --rules 或 --profile")

    index = build_index(root)  # 全规则共享一次树遍历 + 文件级合并扫描
    results = search_all(root, rules, args.max_evidence_per_rule,
                         args.local_tz_offset, args.progress, index)

    teval = eval_temporal(temporal, results)
    # 站点键合并证据 site 与分站点计数桶：证据被截断时命中站点不丢
    all_sites = sorted({e.get("site") for r in results for e in r.get("evidence", [])
                        if e.get("site")}
                       | {k for r in results for k in (r.get("hits_by_site") or {})
                          if k != "ALL"})
    summary = {
        "root": str(root), "local_tz_offset": args.local_tz_offset,
        "detected_sites": all_sites,
        "total_rules": len(results),
        "hit_rules": [r["rule_id"] for r in results if r.get("hit")],
        "miss_rules": [r["rule_id"] for r in results if not r.get("hit") and "error" not in r],
        "error_rules": [r["rule_id"] for r in results if "error" in r],
        "glob_fallback": {r["rule_id"]: r.get("glob_fallback_files", [])
                          for r in results if r.get("glob_fallback_files")},
        "temporal_satisfied": [t["id"] for t in teval if t["satisfied"]],
        "temporal_violated": [t["id"] for t in teval if not t["satisfied"]],
        "results": results, "temporal": teval,
        "file_hashes": _file_hashes(root, results),
    }
    truncated = [r["rule_id"] for r in results if r.get("evidence_truncated")]
    verdict = eval_verdict(root_causes, results, teval, temporal, all_sites, truncated,
                           rule_scope_v2=scope_mode)
    if mode == "triage":
        summary["mode"] = "triage"
    unv = [r["rule_id"] for r in results if r.get("hit") and r.get("verified") is False]
    if unv:
        summary["unverified_hit_rules"] = unv
    if verdict is not None:
        summary["verdict"] = verdict
    out = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.brief:
        Path(args.brief).write_text(brief_summary(summary), encoding="utf-8")
        print(f"精简摘要 → {args.brief}")
    if args.json_out:
        Path(args.json_out).write_text(out, encoding="utf-8")
        print(f"命中 {len(summary['hit_rules'])}/{summary['total_rules']}；"
              f"时序满足 {len(summary['temporal_satisfied'])}/{len(teval)}；"
              f"站点 {len(all_sites)} 个 → {args.json_out}")
        if verdict is not None:
            print(f"  verdict: {verdict['status']} primary={verdict['primary']}"
                  + (f" 其他={verdict['groups']['ALL']['others']}" if verdict['groups']['ALL']['others'] else "")
                  + (f" 时序冲突={verdict['groups']['ALL']['temporal_conflict']}" if verdict['groups']['ALL']['temporal_conflict'] else ""))
            if verdict["grouped_by_site"]:
                for k, g in verdict["groups"].items():
                    if k != "ALL":
                        print(f"    [{k}] {g['status']} primary={g['primary']}")
        for r in results:
            flag = "HIT " if r.get("hit") else ("ERR " if "error" in r else "MISS")
            extra = f" trend={r.get('trend_detail','')}" if r.get("type") == "numeric_trend" else ""
            fb = f" glob兜底={len(r.get('glob_fallback_files', []))}" if r.get("glob_fallback_files") else ""
            print(f"  [{flag}] {r['rule_id']} ({r.get('step','')}) matches={r['match_count']} "
                  f"files={r['files_scanned']} enc={','.join(r.get('encodings', []))}{extra}{fb}")
    else:
        print(out)


if __name__ == "__main__":
    main()
