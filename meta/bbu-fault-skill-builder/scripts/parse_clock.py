#!/usr/bin/env python3
"""
parse_clock.py — BBU 时钟日志结构化解析（SDR/ITRAN clockstatuslog 等）

能力：
  1. 日志合并：目录模式按 mList 基名自动合并 gz/bz2 轮转——按 (父目录, 槽位,
     进程形态) 分组不混流（多分组键为 基名@组标签），组内按轮转序号 .0N
     倒序拼接（无后缀 .txt 为当前文件排最后），.gz/.bz2/.gunzipped 与解压副本
     去重，magic bytes 识别伪装后缀压缩，坏文件跳过不中断
  2. 行解析：Clk[XXX]时间 data... 按 CLK 索引分发；一行含多个 Clk 条目自动拆分；
     inputclock 无前缀（10ms=/RefClk=）归 CLK099
  3. 健壮键值提取：日期时间保护（避免冒号误判）、多词键名（Last Time）、
     带括号 Ant=1(1)、方括号 OcJmp[0]、0x 十六进制
  4. 位置格式：CLK021/027/028/095/100 无关键词格式、CLK100 GNS/GPS 卫星行
  5. 复位假时间修复（2021-01-01 段，基于 tick 差值，支持多次复位）
  6. 关键事件：CLK032 告警、CLK033 参考源切换、CLK036 Flg=-1、CLK094 复位原因、
     CLK099 ClkHealth bitmask（VBP 32bit / VBPd1 7bit 自动识别）、XO/OC 频偏、
     CLK100 OCXO 黑匣子
  7. UBX 0x0A1D SPEC 频谱报文（B5 62 0A 1D）解析
  8. gnsssvinfo 完整解析（PRN 映射、15 字段状态、卫星统计）+ GNSS 欺骗检测
  9. 阈值校验（status/absolute 两类），自动发现异常字段

用法：
  python3 parse_clock.py <clockstatuslog.txt> --json out.json
  python3 parse_clock.py <目录> [--glob 'clockstatuslog*'] [--merge] --json out.json
  python3 parse_clock.py <file> --no-fix-timestamp
"""
import argparse
import sys
import bz2
import gzip
import io
import json
import math
import re
import tarfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

# ----------------------------------------------------------------------------
# 正则与常量
# ----------------------------------------------------------------------------
LOG_HEADER_RE = re.compile(
    r'Clk\[(?P<clk>\d{3})\]\s*'
    r'(?P<time>\d{4}-\d{1,2}-\d{1,2}[, ]\d{1,2}:\d{2}:\d{2})'
    r'[,\s]*(?P<data>.*)')
CLK_MARKER_RE = re.compile(r'Clk\[\d{3}\]')
ALARM_RE = re.compile(r'(\w+)\s+(-[ar])')
ALARM_REF_RE = re.compile(r'ref[:=]\s*(\d+)')
PAREN_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+)\s*\(\s*(\d+)\s*\)')
BRACKET_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]')
SPACE_KEYWORD_RE = re.compile(r'(Ref sw end|Ref sw Tid)\s*[=:]\s*(\d+)')
TICK_RE = re.compile(r'[Tt]ick\s*=\s*(\d+)')
# 日期时间保护：提取 KV 前先把日期时间替换为占位符，避免其中冒号被误判为分隔符
DT_PROTECT_RE = re.compile(
    r'\d{4}-\d{1,2}-\d{1,2}[ ,|]\d{1,2}:\d{1,2}(?::\d{1,2})?'
    r'|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2}')
KV_SEP_RE = re.compile(r'[=:]')
TRAILING_KEY_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_ ]*$')
KEY_SPACE_RE = re.compile(r'\s+')
PLACEHOLDER_RE = re.compile(r'\x00(\d+)\x00')

GNSS_LINE_RE = re.compile(
    r'(?P<time>\d{4}-\d{2}-\d{2}[, ]\d{2}:\d{2}:\d{2})[ ,]+(?P<data>.+)')

RESET_BASE = datetime(2021, 1, 1)
RESET_YEAR = 2021

# 时钟文件基名（mList）
MLIST = ['gnsssvinfo', 'clockdatalog', 'clockstatuslog', 'gnssspecinfo',
         'gnssoriginalmsg', 'clockhwinfolog', 'clockocxolog',
         'gnssmovinfolog', 'gnsscrosscnomsg', 'inputclock']

# VBP ClkHealth 32bit（与 assets/shared/clk-field-dictionary.yaml 同步）
CLKHEALTH_VBP_BITS = {
    0: "61.44M输入丢失(EPLD)", 1: "61.44M输入丢失(PLL)",
    2: "61.44M输入频偏(PLL)", 3: "预留(FSYNC丢失)",
    4: "FSYNC异常(EPLD)", 5: "FSYNC异常(MCS主)", 6: "FSYNC异常(MCS从)",
    7: "FSYNC异常(CPU)", 8: "PLL XO本振异常", 9: "PLL参考源丢失",
    10: "PLL参考源频偏", 11: "PLL本地环失锁", 12: "PLL syncE环失锁",
    13: "PLL CPRI环失锁", 15: "MCS主内部PLL失锁", 16: "MCS主PSYNC失锁",
    18: "MCS从内部PLL失锁", 19: "MCS从PSYNC失锁", 21: "CPU内部PLL失锁",
    22: "EPLD帧同步抖动", 23: "MCS帧同步抖动", 24: "CPU帧同步抖动",
    25: "MCS帧号异常", 26: "CPU帧号异常",
    27: "MCS主1PPS异常", 28: "MCS从1PPS异常", 29: "CPU 1PPS异常",
    30: "MCS主1588异常", 31: "MCS从1588异常",
}
CLKHEALTH_VBPD1_BITS = {
    0: "PSYNC参考源无效", 1: "MCS通用PLL未锁定", 2: "PSYNC未锁定",
    3: "EPLD参考源异常", 4: "PLL参考源缺失", 5: "EPLD未锁定",
    6: "PLL输出未锁定",
}
# CLK099 Lock/Loss/Drift 4bit
LOCK_BITS = {0: "CPU环失锁", 1: "SYNCE环失锁", 2: "CPRI环失锁", 3: "DCO环失锁"}
LOSS_BITS = {0: "50M丢失", 1: "SYNCE丢失", 2: "DCO丢失",
             3: "OCXO丢失", 4: "54M丢失"}
DRIFT_BITS = {0: "50M频偏", 1: "SYNCE频偏", 2: "DCO频偏",
              3: "OCXO频偏", 4: "54M频偏"}

# NMEA PRN → 系统映射（GP/GL/GA/BD/QZ）
PRN_MAP = ((0, 32, 'GP'), (0, 64, 'GL'), (64, 96, 'GL'),
           (0, 158, 'GP'), (0, 202, 'QZ'),
           (300, 336, 'GA'), (400, 500, 'BD'))

# ----------------------------------------------------------------------------
# 基础解析
# ----------------------------------------------------------------------------
def parse_time(s):
    s = s.strip()
    if not s:
        return None
    has_dot = '.' in s
    has_comma = ',' in s
    has_sec = len(s) >= 17
    if has_dot:
        fmt = '%Y.%m.%d %H:%M:%S' if has_sec else '%Y.%m.%d %H:%M'
    elif has_comma:
        fmt = '%Y-%m-%d,%H:%M:%S' if has_sec else '%Y-%m-%d,%H:%M'
    else:
        fmt = '%Y-%m-%d %H:%M:%S' if has_sec else '%Y-%m-%d %H:%M'
    try:
        return datetime.strptime(s, fmt)
    except ValueError:
        return None


def extract_kv(data_str):
    """健壮键值提取：日期时间保护 + 多词键名 + 括号值 + 方括号字段。"""
    if not data_str or not data_str.strip():
        return {}
    placeholders = []

    def _protect(m):
        placeholders.append(m.group(0))
        return f'\x00{len(placeholders) - 1}\x00'

    s = DT_PROTECT_RE.sub(_protect, data_str)
    result, seen = {}, set()
    prev_end = 0
    for m in KV_SEP_RE.finditer(s):
        sep = m.start()
        if sep < prev_end:
            continue
        key_match = TRAILING_KEY_RE.search(s[prev_end:sep])
        if not key_match:
            continue
        key = KEY_SPACE_RE.sub('_', key_match.group(0).strip())
        if not key:
            continue
        i = m.end()
        n = len(s)
        while i < n and s[i] in ' \t':
            i += 1
        if i < n and s[i] == '\x00':
            j = s.index('\x00', i + 1) + 1
            val = s[i:j]
        else:
            j = i
            while j < n and s[j] not in ',;\t ':
                j += 1
            val = s[i:j].split('(', 1)[0]
        prev_end = j
        if key not in seen and val != '':
            if '\x00' in val:
                val = PLACEHOLDER_RE.sub(
                    lambda mm: placeholders[int(mm.group(1))], val)
            result[key] = val
            seen.add(key)
    # 带括号值 Ant=  1(  1)
    for m in PAREN_RE.finditer(data_str):
        result.setdefault(m.group(1), m.group(2))
        seen.add(m.group(1))
    # 方括号字段 OcJmp[0]
    for m in BRACKET_RE.finditer(data_str):
        result.setdefault(m.group(1), m.group(2))
    # 空格关键词 Ref sw Tid / Ref sw end
    for m in SPACE_KEYWORD_RE.finditer(data_str):
        result.setdefault(m.group(1).replace(' ', '_'), m.group(2))
    # 类型化
    for k, v in list(result.items()):
        result[k] = auto_value(v)
    return result


def auto_value(v):
    if isinstance(v, str):
        if v.lower().startswith('0x'):
            try:
                return int(v, 16)
            except ValueError:
                pass
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                pass
    return v


def decode_bitmask(value, bits_map):
    out = {}
    if not isinstance(value, int):
        return out
    for bit, name in bits_map.items():
        if value & (1 << bit):
            out[name] = True
    return out


# ----------------------------------------------------------------------------
# 特殊格式解析
# ----------------------------------------------------------------------------
def parse_ubx(data_str):
    """u-blox UBX 0x0A1D SPEC 报文：
    rang(X1) type(X1) specLen(U2) freOffect(I4,Hz) fre(U4,Hz) data(U1[specLen])。"""
    stripped = data_str.strip().lower()
    idx = stripped.find('b5 62')
    if idx == -1:
        return None
    try:
        raw = bytes(int(b, 16) for b in stripped[idx:].split())
    except ValueError:
        return None
    if len(raw) < 8 or raw[0] != 0xB5 or raw[1] != 0x62:
        return None
    cls, mid = raw[2], raw[3]
    payload_len = int.from_bytes(raw[4:6], 'little')
    if len(raw) < 6 + payload_len + 2:
        return None
    payload = raw[6:6 + payload_len]
    if cls != 0x0A or mid != 0x1D or len(payload) < 12:
        return None
    spec_len = int.from_bytes(payload[2:4], 'little')
    return {
        '_ubx': True, 'class': f'0x{cls:02X}', 'id': f'0x{mid:02X}',
        'rang': payload[0], 'type': payload[1], 'specLen': spec_len,
        'freOffect': int.from_bytes(payload[4:8], 'little', signed=True),
        'fre': int.from_bytes(payload[8:12], 'little'),
        'data': list(payload[12:12 + spec_len]),
    }


def parse_clk099_xo_oc(fields):
    """CLK099 XO/OC 频偏值格式：XO=3.219/4.411/0.000/5.007
    XO 顺序：OCXO/54M, DCO/54M, 50M/54M, DCO/10M；
    OC 顺序：DCO/10M,LT_976k/10M,125M/10M（前可选）。"""
    for key, base in (('XO', ['50M', 'SYNCE', 'DCO', 'OCXO']),
                      ('OC', ['DCO', 'SYNCE', 'DCO_976k', 'LT_976k', '125M'])):
        val = fields.get(key)
        if isinstance(val, str) and '/' in val:
            nums = val.split('/')
            if key == 'XO':
                # 格式 a/b/c/d：OCXO_XO, DCO_XO, 50M_XO, DCO_976k(或DCO/10M)
                names = ['OCXO_XO', 'DCO_XO', '50M_XO', 'DCO_10M_XO']
            else:
                names = ['DCO_OCXO', 'SYNCE_OCXO', 'DCO_976k_OCXO',
                         'LT_976k_OCXO', '125M_OCXO']
                nums = nums[-len(names):] if len(nums) > len(names) else nums
            for n, num in zip(names, nums):
                try:
                    fields[n] = float(num)
                except ValueError:
                    pass
    # Temp: in/out
    temp = fields.get('Temp')
    if isinstance(temp, str) and '/' in temp:
        t_in, t_out = temp.split('/')[:2]
        try:
            fields['in_temp'] = float(t_in)
            fields['out_temp'] = float(t_out)
        except ValueError:
            pass
    return fields


def parse_clk100_gns(stripped):
    """CLK100 GNS/GPS 卫星行：
    GNS Ant View Lock Fix State , PRN cno1 cno2 cno3 lock_state , ..."""
    segments = [s.strip() for s in stripped.split(',')]
    if not segments:
        return None
    header = segments[0].split()
    if len(header) < 6 or header[0] not in ('GNS', 'GPS'):
        return None
    try:
        sv = []
        for seg in segments[1:]:
            p = seg.split()
            if len(p) == 5:
                sv.append({'prn': int(p[0]), 'cno1': int(p[1]),
                           'cno2': int(p[2]), 'cno3': int(p[3]),
                           'lock_state': int(p[4])})
            elif len(p) == 3:
                sv.append({'prn': int(p[0]), 'cno1': int(p[1]),
                           'lock_state': int(p[2])})
        return {
            'type': header[0], 'ant': int(header[1]), 'view': int(header[2]),
            'lock': int(header[3]), 'fix': int(header[4]),
            'state': int(header[5]), 'sv_info': sv,
        }
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# GNSS 卫星行（gnsssvinfo）与欺骗检测
# ----------------------------------------------------------------------------
def map_prn(prn):
    if prn == 0:
        return prn, ''
    for base, mx, typ in PRN_MAP:
        if prn <= mx:
            return prn - base, typ
    return prn, ''


def parse_gnss_line(line):
    """解析 gnsssvinfo 单行：
    时间, <15 字段状态>, PRN used ele azi cno [cno1 cno2], ...
    9 字段旧格式自动补齐 15 字段。"""
    res = GNSS_LINE_RE.search(line.strip())
    if not res:
        return None
    try:
        ts = datetime.strptime(res.group('time').replace(',', ' '),
                               '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    info = res.group('data').split(',')
    try:
        status = [int(x) for x in info[0].split()]
        if len(status) == 9:
            status = [*status[:5], 0, 0, *status[5:7], 0, 0, 0, 0,
                      *status[7:]]
        elif len(status) != 15:
            return None
        result = {
            'timestamp': ts.strftime('%Y-%m-%d %H:%M:%S'),
            'status': {
                'Ref': status[0], 'View': status[1], 'Lock': status[2],
                'Fix': status[3], 'AGC': status[4], 'AGC1': status[5],
                'AGC2': status[6], 'JamInd': status[7], 'JamStatus': status[8],
                'JamInd1': status[9], 'JamStatus1': status[10],
                'JamInd2': status[11], 'JamStatus2': status[12],
                'Spoofing': status[13], 'Drift': status[14]},
            'satellites': [],
        }
        for sat in info[1:]:
            p = [int(x) if x.isdigit() else 0 for x in sat.split()]
            if len(p) == 5:
                prn, used, ele, azi, cno = p
                cno1 = cno2 = ''
            elif len(p) == 7:
                prn, used, ele, azi, cno, cno1, cno2 = p
            else:
                continue
            ele = ele if ele < 100 else 0
            pn, typ = map_prn(prn)
            if not typ:
                continue
            result['satellites'].append({
                'SatType': typ, 'Prn': pn, 'Used': used, 'Ele': ele,
                'Azi': azi, 'Cno': cno, 'Cno1': cno1, 'Cno2': cno2})
        return result
    except (ValueError, IndexError):
        return None


def evaluate_spoofing(sats, width=2.0, min_cluster=4, min_group=5,
                      ele_span=30.0, floor=20.0, sep_gap=10.0,
                      dominance=0.8, cno_high=35.0, margin=5.0):
    pairs = [(s['Cno'], s['Ele']) for s in sats
             if isinstance(s.get('Cno'), (int, float)) and s['Cno'] >= floor
             and isinstance(s.get('Ele'), (int, float))]
    if len(pairs) < min_group:
        return 0, 0.0
    pairs.sort(key=lambda p: p[0])
    max_c = pairs[-1][0]
    top = [(c, e) for c, e in pairs if c >= max_c - width]
    if len(top) < min_cluster:
        return 0, 0.0
    eles = [e for _, e in top]
    if max(eles) - min(eles) < ele_span:
        return 0, 0.0
    below = [c for c, _ in pairs if c < max_c - width]
    cluster_avg = sum(c for c, _ in top) / len(top)
    group_avg = sum(c for c, _ in pairs) / len(pairs)
    separated = bool(below) and (max_c - width - max(below)) >= sep_gap \
        and cluster_avg > group_avg
    if below:
        dominant = (len(top) / len(pairs) >= dominance and cluster_avg >= cno_high
                    and cluster_avg - group_avg >= margin)
    else:
        dominant = len(top) / len(pairs) >= dominance and cluster_avg >= cno_high
    if not (separated or dominant):
        return 0, 0.0
    # 仰角-C/N0 正相关排除（正常高仰角信号更强）
    if len(top) >= 3:
        mc, me = cluster_avg, sum(eles) / len(top)
        num = sum((c - mc) * (e - me) for c, e in top)
        dc = sum((c - mc) ** 2 for c, _ in top)
        de = sum((e - me) ** 2 for e in eles)
        if dc > 0 and de > 0 and num / math.sqrt(dc * de) > 0.3:
            return 0, 0.0
    conf = round(0.5 * len(top) / len(pairs)
                 + 0.5 * min(max(cluster_avg - group_avg, 0) / 10.0, 1.0), 4)
    return len(top), conf


# ----------------------------------------------------------------------------
# 日志合并（gz 轮转 / bz2 / 去重）
# ----------------------------------------------------------------------------
def read_text_member(path):
    """读单个文本/gz/bz2 文件，损坏返回 None。
    按 magic bytes 优先识别压缩（.bak.N 等伪装后缀的 gzip 也能读）。"""
    try:
        with open(path, 'rb') as probe:
            head = probe.read(3)
    except OSError:
        return None
    low = path.name.lower()
    try:
        if head[:2] == b'\x1f\x8b' or low.endswith('.gz'):
            with gzip.open(path, 'rt', encoding='utf-8', errors='ignore') as f:
                return f.read()
        if head[:3] == b'BZh' or low.endswith('.bz2'):
            with bz2.open(path, 'rt', encoding='utf-8', errors='ignore') as f:
                return f.read()
        return path.read_text(encoding='utf-8', errors='ignore')
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        print(f"  [warn] 损坏文件跳过 {path.name}: {e}")
        return None


COMP_SUFFIXES = ('.gz', '.bz2', '.gunzipped')
ARCH_SUFFIXES = ('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2',
                 '.7z', '.rar')
_SLOT_RE = re.compile(r'^(\d+(?:-\d+){2,3})')
_ROT_RE = re.compile(r'\.(\d+)$')


def _strip_comp(name):
    low = name.lower()
    for s in COMP_SUFFIXES:
        if low.endswith(s):
            return name[:-len(s)]
    return name


def _rotation_no(name):
    """轮转序号：foo.txt.02 → 2；无序号（当前文件 .txt）返回 -1。"""
    m = _ROT_RE.search(_strip_comp(name))
    return int(m.group(1)) if m else -1


def _clock_group_key(p, base):
    """时钟流分组键：(父目录, 槽位, 形态)。

    同一目录下 1-1-3-1 与 1-1-4-1 槽位的同名时钟文件不合并；
    PLAT/BSP 等不同进程前缀（形态）也不合并。
    文件名中采集序号/时间戳等数字段归一化后视为同一流。
    """
    n = _strip_comp(p.name).lower()
    n = _ROT_RE.sub('', n)                     # 去轮转序号 .01/.02
    n = _strip_comp(n)                         # 形如 .01.gz 已去两层
    m = _SLOT_RE.match(n)
    slot = m.group(1) if m else ''
    shape = n[m.end():] if m else n
    i = shape.find(base)
    if i >= 0:
        shape = shape[:i] + shape[i + len(base):]
    for s in ('.txt', '.log'):
        if shape.endswith(s):
            shape = shape[:-len(s)]
    shape = re.sub(r'\d+', '#', shape)         # 采集序号/时间戳归一
    return str(p.parent), slot, shape


def merge_clock_files(directory):
    """按 mList 基名在目录中检索并合并轮转文件。

    返回 {基名 或 基名@组标签: 合并文本}：
    - 按 (父目录, 槽位前缀, 进程形态) 分组，不同槽位/进程不混流；
    - 组内按轮转序号倒序拼接（.0N 越大越旧，无后缀 .txt 为当前文件排最后）；
    - .gz/.bz2/.gunzipped 与解压副本去重（保留带压缩标记版本），坏文件跳过。
    """
    directory = Path(directory)
    merged = {}
    for base in MLIST:
        # 收集所有匹配文件（含归档解压后的递归结果）
        candidates = [p for p in directory.rglob(f'*{base}*')
                      if p.is_file()
                      and not p.name.lower().endswith(ARCH_SUFFIXES)
                      and 'all' not in p.name.lower() and 'db' not in p.name.lower()]
        if not candidates:
            continue
        # 去重：foo 与 foo.gz/.bz2/.gunzipped 同时存在时，保留带压缩后缀的成员
        comp_bases = {_strip_comp(p.name).lower() for p in candidates
                      if p.name.lower().endswith(COMP_SUFFIXES)}
        candidates = [p for p in candidates
                      if p.name.lower().endswith(COMP_SUFFIXES)
                      or p.name.lower() not in comp_bases]
        # 按 (父目录, 槽位, 形态) 分组：不同网元/槽位/进程不合并
        groups = {}
        for p in candidates:
            groups.setdefault(_clock_group_key(p, base), []).append(p)
        for (parent, slot, shape), files in sorted(groups.items()):
            files.sort(key=lambda p: (_rotation_no(p.name) < 0,
                                      -_rotation_no(p.name), p.name))
            parts, line_src = [], []
            for p in files:
                t = read_text_member(p)
                if not t:
                    continue
                parts.append(t)
                for local_no, _ in enumerate(t.splitlines(), 1):
                    line_src.append((p.name, local_no))
            if not parts:
                continue
            text = ''.join(parts)
            if len(groups) == 1:
                key = base
            else:
                rel = str(Path(parent).relative_to(directory)).replace('\\', '/')
                tag = slot or shape.strip('._-# ') or '/'.join(rel.split('/')[-2:])
                key = f'{base}@{tag}'
            merged[key] = {'text': text, 'line_src': line_src}
    return merged


# ----------------------------------------------------------------------------
# 时间修复
# ----------------------------------------------------------------------------
def fix_timestamp(raw_lines):
    """复位段时间修复：基于 tick 差值校正 2021 假时间，支持多次复位。"""
    parsed = []
    for raw in raw_lines:
        m = LOG_HEADER_RE.search(raw)
        if not m:
            parsed.append({'clk': None, 'dt': None, 'tick': None, 'raw': raw})
            continue
        dt = parse_time(m.group('time'))
        tm = TICK_RE.search(m.group('data'))
        parsed.append({'clk': int(m.group('clk')), 'dt': dt,
                       'tick': int(tm.group(1)) if tm else None, 'raw': raw})

    segments, seg, last_dt = [], [], None
    for item in parsed:
        if item['dt'] and last_dt and item['dt'] < last_dt:
            if seg:
                segments.append(seg)
            seg = []
        seg.append(item)
        last_dt = item['dt'] or last_dt
    if seg:
        segments.append(seg)

    fixed = 0
    for seg in segments:
        reset = [i for i in seg if i['dt'] and i['dt'].year == RESET_YEAR]
        normal = [i for i in seg if i['dt'] and i['dt'].year != RESET_YEAR]
        if not reset or not normal:
            continue
        ra = next((i for i in reset if i['tick'] is not None), None)
        na = next((i for i in normal if i['tick'] is not None), None)
        if ra is None or na is None or na['tick'] <= ra['tick']:
            continue
        tick_diff = (na['tick'] - ra['tick']) / 100.0
        t1z = na['dt'] - timedelta(seconds=tick_diff)
        delta = t1z - ra['dt']
        cap = na['dt'] + timedelta(seconds=3)
        for i in reset:
            new_dt = i['dt'] + delta
            if new_dt > cap:
                continue
            old = i['dt'].strftime('%Y-%m-%d,%H:%M:%S')
            new = new_dt.strftime('%Y-%m-%d,%H:%M:%S')
            i['raw'] = i['raw'].replace(old, new, 1)
            i['dt'] = new_dt
            fixed += 1
    return [i['raw'] for i in parsed], fixed


# ----------------------------------------------------------------------------
# 阈值校验
# ----------------------------------------------------------------------------
# 关键字段默认阈值（status 正常枚举 / absolute 范围），可被 YAML 字典覆盖
# ----------------------------------------------------------------------------
# OCXO 频偏红线（Hz）：锁定态下持续超过即视为晶振老化/劣化
OCXO_FCV_LIMIT = 50.0
# OCXO 频偏物理合理上限（Hz）：超过此值视为失锁/未收敛时的填充或寄存器值
# （如失锁瞬间的 -1163.92、未收敛的 +573.9），不是真实频偏，禁止据此判老化
OCXO_FCV_MAX_PHYSICAL = 500.0
# 主备相位差（Clk085 Dif，ns）非物理/寄存器哨兵阈值：|Dif|≥1 亿 ns（100ms）
# 视为“无参考/无效”填充（如 ±1000000000、-134218494=0x08000002），不判相位差超差
DIF_INVALID_ABS = 1.0e8
# 真实 OCXO 老化判定：有效超差点在多少个不同自然日（或月）出现
OCXO_AGING_MIN_POINTS = 10
OCXO_AGING_MIN_DAYS = 2

DEFAULT_RULES = {
    'sts': {'status': [2, 3]},
    'PllStat': {'status': [2, 3]},
    'PllSts': {'status': [2, 3]},
    'FixType': {'status': [4, 5]},
    'FixFlg': {'status': [1]},
    'Ant': {'status': [1]},
    'UartErr': {'status': [0]},
    'PpsStat': {'status': [0]},
    'PpsSts': {'status': [0]},
    'SerialSts': {'status': [0]},
    'Flg': {'status': [0, 2]},
    'Dif': {'absolute': (-1500, 1500)},
    'TE': {'absolute': (-100, 100)},
    'Pdv': {'absolute': (0, 1000)},
    'DegVal': {'absolute': (-100, 100)},
    'cur_fcv': {'absolute': (-50.0, 50.0)},
    'avg_temp': {'absolute': (20.0, 70.0)},
}


def validate_field(name, value, prev=None):
    """返回 (is_normal, reason)；is_normal 为 None 表示无规则。"""
    rule = DEFAULT_RULES.get(name)
    if rule is None or value is None:
        return None, ''
    if 'status' in rule and isinstance(value, (int, float)):
        if value not in rule['status']:
            return False, f'{name}={value} 不在正常状态 {rule["status"]}'
        return True, ''
    if 'absolute' in rule and isinstance(value, (int, float)):
        lo, hi = rule['absolute']
        if lo is not None and value < lo:
            return False, f'{name}={value} < {lo}'
        if hi is not None and value > hi:
            return False, f'{name}={value} > {hi}'
        return True, ''
    return None, ''


# ----------------------------------------------------------------------------
# 主解析
# ----------------------------------------------------------------------------
def parse_clock_text(text, fix=True, source_map=None):
    """解析单份时钟日志文本（已合并）。

    source_map: 可选，与 text 行一一对应的 [(来源文件名, 该文件内行号)]，
    用于把合并后的行号映射回具体轮转文件（--merge 证据溯源）。
    """
    raw_lines = text.splitlines()
    lines, fixed = fix_timestamp(raw_lines) if fix else (raw_lines, 0)

    # 一行多 Clk 拆分
    entries = []
    for line_no, line in enumerate(lines, 1):
        first = CLK_MARKER_RE.search(line)
        if first is None:
            # inputclock 无前缀行
            if '10ms=' in line or 'RefClk=' in line:
                m = re.search(r'(\d{4}-\d{2}-\d{2}[, ]\d{2}:\d{2}:\d{2})'
                              r'[,\s]*(.*)', line)
                if m:
                    entries.append((99, parse_time(m.group(1)), m.group(2), line_no))
            continue
        markers = list(CLK_MARKER_RE.finditer(line))
        if len(markers) == 1:
            m = LOG_HEADER_RE.search(line)
            if m:
                entries.append((int(m.group('clk')), parse_time(m.group('time')),
                                m.group('data'), line_no))
        else:
            for i, mk in enumerate(markers):
                end = markers[i + 1].start() if i + 1 < len(markers) else len(line)
                sub = line[mk.start():end]
                m = LOG_HEADER_RE.search(sub)
                if m:
                    entries.append((int(m.group('clk')), parse_time(m.group('time')),
                                    m.group('data'), line_no))

    records = []
    alarms, switches, resets, health, ocxo = [], [], [], [], []
    for clk, dt, data, line_no in entries:
        ts = dt.strftime('%Y-%m-%d %H:%M:%S') if dt else None
        rec = {'clk': clk, 'timestamp': ts, 'line_no': line_no,
               'raw': data.strip()[:500], 'fields': {}}
        if source_map and line_no - 1 < len(source_map):
            _src_name, _src_lineno = source_map[line_no - 1]
            rec['source_file'] = _src_name
            rec['source_line_no'] = _src_lineno
        fields = {}
        anomaly = []

        ubx = parse_ubx(data)
        if ubx:
            rec['fields'] = ubx
            records.append(rec)
            continue

        if clk == 32:
            am = ALARM_RE.search(data)
            rm = ALARM_REF_RE.search(data)
            if am:
                ev = {'timestamp': ts, 'alarm_type': am.group(1),
                      'action': am.group(2),
                      'ref': int(rm.group(1)) if rm else None}
                alarms.append(ev)
                rec['fields'] = ev
        elif clk == 33:
            fields = extract_kv(data)
            if 'Ref sw end' in data:
                fields['Ref_sw_end'] = True
            switches.append({'timestamp': ts, 'from_ref': fields.get('BetId'),
                             'to_ref': fields.get('CurId'),
                             'end': fields.get('Ref_sw_end', False)})
            rec['fields'] = fields
        elif clk == 99:
            fields = parse_clk099_xo_oc(extract_kv(data))
            if isinstance(fields.get('ClkHealth'), int):
                bm = CLKHEALTH_VBPD1_BITS if 'M_R10' in fields else CLKHEALTH_VBP_BITS
                bits = decode_bitmask(fields['ClkHealth'], bm)
                health.append({'timestamp': ts,
                               'clk_health_hex': hex(fields['ClkHealth']),
                               'bits_set': bits})
                if bits:
                    anomaly.append('ClkHealth: ' + '; '.join(bits))
            for key, bm in (('Lock', LOCK_BITS), ('Loss', LOSS_BITS),
                            ('Drift', DRIFT_BITS)):
                if isinstance(fields.get(key), int) and fields[key] != 0:
                    bits = decode_bitmask(fields[key], bm)
                    if bits:
                        anomaly.append(f'{key}: ' + '; '.join(bits))
            rec['fields'] = fields
        elif clk == 94:
            fields = extract_kv(data)
            if 'ReasonId' in fields:
                resets.append({'timestamp': ts,
                               'reason_id': fields.get('ReasonId')})
            rec['fields'] = fields
        elif clk == 36:
            fields = extract_kv(data)
            if fields.get('Flg') == -1:
                anomaly.append('参考源差异异常 Flg=-1（相位差>1.5us 或频偏>2倍）')
            rec['fields'] = fields
        elif clk == 100:
            stripped = data.strip()
            gns = parse_clk100_gns(stripped)
            row = None
            is_bbox = False
            if gns:
                row = gns
            elif '=' not in stripped and len(stripped.split()) in (6, 8):
                p = stripped.split()
                try:
                    fcv3 = float(p[2])
                    row = {'cur_fcv': float(p[0]), 'max_fcv': float(p[1]),
                           'avg_fcv_locked': None if fcv3 >= 99999.0 else fcv3,
                           'ref_sts': int(p[3]), 'active_ref': int(p[4]),
                           'avg_temp': float(p[5])}
                    if len(p) == 8:
                        row['osc_temp'] = float(p[6])
                        ov = float(p[7])
                        row['osc_voltage'] = None if abs(ov) >= 9999.0 else ov
                    is_bbox = True
                except ValueError:
                    row = None
            else:
                row = extract_kv(data)
            if row is not None:
                rec['fields'] = row
                cur = row.get('cur_fcv')
                if isinstance(cur, (int, float)):
                    if abs(cur) >= OCXO_FCV_MAX_PHYSICAL:
                        # 失锁/未收敛填充值（含固定重复的 -1163.9 等），不是真实频偏
                        row['fcv_invalid'] = True
                        anomaly.append(
                            f'OCXO失锁未收敛(频偏值无效) cur_fcv={cur}Hz '
                            f'ref_sts={row.get("ref_sts")}')
                    elif abs(cur) > OCXO_FCV_LIMIT:
                        row['fcv_over_limit'] = True
                        anomaly.append(f'OCXO 频偏 {cur}Hz 超 ±50')
                if row.get('ref_sts') == 8:
                    anomaly.append('OCXO 自由运行')
                # 黑匣子位置格式入 OCXO 序列；统一 schema（顶层字段 + fields 镜像）
                if is_bbox and isinstance(row.get('cur_fcv'), (int, float)):
                    ocxo.append({'timestamp': ts, 'fields': row, **row})
        elif clk == 21:
            # 位置格式：Ref Lock FixT FixF Agc MPhase APhase CurDa RecDa
            parts = data.strip().split()
            if len(parts) >= 9 and '=' not in data:
                names = ['Ref', 'Lock', 'FixT', 'FixF', 'Agc', 'MPhase',
                         'APhase', 'CurDa', 'RecDa']
                rec['fields'] = {n: auto_value(v)
                                 for n, v in zip(names, parts)}
            else:
                rec['fields'] = extract_kv(data)
        elif clk in (27, 28):
            parts = data.strip().split()
            if len(parts) == 5 and '=' not in data:
                try:
                    rec['fields'] = {'PllStat': int(parts[0]),
                                     'MPhase': int(parts[1]),
                                     'cFCV': float(parts[2]),
                                     'rFCV': float(parts[3]),
                                     'Toffset': int(parts[4])}
                except ValueError:
                    rec['fields'] = extract_kv(data)
            else:
                rec['fields'] = extract_kv(data)
        elif clk == 95:
            parts = data.strip().split()
            if '=' not in data and len(parts) == 3:
                try:
                    rec['fields'] = {'serial': int(parts[0]),
                                     'pull_min': float(parts[1]),
                                     'pull_max': float(parts[2]),
                                     '_fmt': 'ocxo_blackbox'}
                except ValueError:
                    rec['fields'] = extract_kv(data)
            else:
                rec['fields'] = extract_kv(data)
        else:
            rec['fields'] = extract_kv(data)

        # 阈值校验（只针对数值字段）
        for k, v in rec['fields'].items():
            if isinstance(v, (int, float)) and not k.startswith('_'):
                # cur_fcv 由 CLK100 专门判定（含失锁哨兵、锁定态），此处不重复
                if k == 'cur_fcv':
                    continue
                # 主备相位差 Dif 的非物理/寄存器哨兵值（无参考），不判真实超差
                if k == 'Dif' and abs(v) >= DIF_INVALID_ABS:
                    anomaly.append(f'相位差哨兵值(无参考) Dif={v}')
                    continue
                ok, reason = validate_field(k, v)
                if ok is False:
                    anomaly.append(reason)
        if anomaly:
            rec['anomaly'] = anomaly
        records.append(rec)

    return {
        'total_records': len(records),
        'timestamp_fixed': fixed,
        'records': records,
        'anomalies': [r for r in records if r.get('anomaly')],
        'alarms': alarms,
        'ref_switches': switches,
        'reset_reasons': resets,
        'clk99_health_events': health,
        'ocxo_records': ocxo,
    }


def parse_clock_file(path, fix=True):
    path = Path(path)
    text = read_text_member(path)
    if text is None:
        return None
    result = parse_clock_text(text, fix=fix)
    result['file'] = str(path)
    return result


def parse_gnss_text(text):
    """从 gnsssvinfo 文本解析卫星行并做欺骗检测。"""
    entries = []
    for line in text.splitlines():
        e = parse_gnss_line(line)
        if e:
            entries.append(e)
    # 按 (SatType) 分组逐时刻评估
    suspicions = []
    for e in entries:
        by_type = {}
        for s in e['satellites']:
            by_type.setdefault(s['SatType'], []).append(s)
        for typ, sats in by_type.items():
            cnt, conf = evaluate_spoofing(sats)
            if cnt > 0:
                suspicions.append({'timestamp': e['timestamp'], 'sat_type': typ,
                                   'consistent_sats': cnt, 'confidence': conf})
    return {'gnss_records': len(entries),
            'spoofing_suspicions': suspicions,
            'last_status': entries[-1]['status'] if entries else None}


# ----------------------------------------------------------------------------
# 摘要（brief）与时钟分诊（triage）
# ----------------------------------------------------------------------------
def _monthly(events, key=lambda x: x.get('timestamp')):
    """按自然月统计事件数（key 取 timestamp 字符串）。"""
    out = {}
    for e in events:
        t = key(e) or ''
        if len(t) >= 7:
            out[t[:7]] = out.get(t[:7], 0) + 1
    return dict(sorted(out.items()))


def _ocxo_summary(result):
    """从 CLK100 黑匣子记录汇总 OCXO 频偏，区分真实超差与失锁无效值。"""
    recs = result.get('ocxo_records', [])
    over, invalid = [], []
    for r in recs:
        f = r.get('fields', r)
        cur = f.get('cur_fcv')
        if not isinstance(cur, (int, float)):
            continue
        if f.get('fcv_invalid') or abs(cur) >= OCXO_FCV_MAX_PHYSICAL:
            invalid.append((r.get('timestamp'), cur))
        elif f.get('fcv_over_limit') or abs(cur) > OCXO_FCV_LIMIT:
            over.append((r.get('timestamp'), cur))
    months = {}
    days = set()
    for t, x in over:
        if t:
            months[t[:7]] = months.get(t[:7], 0) + 1
            days.add(t[:10])
    aging = (len(over) >= OCXO_AGING_MIN_POINTS
             and len(days) >= OCXO_AGING_MIN_DAYS)
    return {
        'samples': len(recs),
        'over_limit_points': len(over),
        'unlocked_invalid_points': len(invalid),
        'over_limit_monthly': dict(sorted(months.items())),
        'over_limit_days': len(days),
        'fcv_min': min((x for _, x in over), default=None),
        'fcv_max': max((x for _, x in over), default=None),
        'aging': aging,
    }


def _last_clk001(result):
    """取最后一条 CLK001 接收机状态（锁星/定位/信号/天馈）。"""
    for r in reversed(result.get('records', [])):
        if r.get('clk') == 1:
            f = r.get('fields', {})
            if isinstance(f, dict) and ('LockBd' in f or 'FixType' in f):
                return {'timestamp': r.get('timestamp'),
                        'Ant': f.get('Ant'), 'LockBd': f.get('LockBd'),
                        'LockGps': f.get('LockGps'), 'FixType': f.get('FixType'),
                        'AveCno': f.get('AveCno'), 'MaxCno': f.get('MaxCno')}
    return None


def build_file_brief(result):
    """单个文件组（一个时钟流）的可读摘要。"""
    recs = result.get('records', [])
    ts = [r.get('timestamp') for r in recs if r.get('timestamp')]
    alarms = result.get('alarms', [])
    resets = result.get('reset_reasons', [])
    switches = result.get('ref_switches', [])
    alarm_counts = {}
    for a in alarms:
        alarm_counts[a.get('alarm_type')] = alarm_counts.get(
            a.get('alarm_type'), 0) + 1
    reset_counts = {}
    for r in resets:
        rid = r.get('reason_id')
        reset_counts[rid] = reset_counts.get(rid, 0) + 1
    # 真实切换次数：有目标参考源（to_ref/CurId）的行
    switch_count = sum(1 for s in switches if s.get('to_ref') is not None)
    return {
        'file': result.get('file'),
        'total_records': result.get('total_records', len(recs)),
        'time_range': [min(ts), max(ts)] if ts else [None, None],
        'alarm_counts': alarm_counts,
        'alarm_monthly': _monthly(alarms),
        'reset_reason_counts': reset_counts,
        'reset_monthly': _monthly(resets),
        'switch_events': switch_count,
        'switch_monthly': _monthly(
            [s for s in switches if s.get('to_ref') is not None]),
        'anomaly_count': len(result.get('anomalies', [])),
        'ocxo': _ocxo_summary(result),
        'last_clk001': _last_clk001(result),
    }


def _is_master_bsp(fb):
    f = (fb.get('file') or '')
    return 'clockstatuslog' in f and (
        'BSP_1-1-1-1' in f or 'HWM_APP' in f or '@' not in f)


def build_triage(file_briefs, gnss=None):
    """对一个网元（多文件组）做时钟分诊，给出标签、证据与末态判定。"""
    master = next((fb for fb in file_briefs if _is_master_bsp(fb)),
                  None)
    if master is None and file_briefs:
        master = max(file_briefs, key=lambda x: x['total_records'])
    ocxo = next((fb for fb in file_briefs
                 if 'clockocxolog' in (fb.get('file') or '')), None)
    if ocxo is None:
        ocxo = master  # CLK100 也可能并在主控流
    tags, evidence = [], []
    gnss_alarms = master['alarm_counts'].get('LockSatFail', 0) if master else 0
    rc = master['reset_reason_counts'] if master else {}
    gnss_resets = rc.get(1, 0) + rc.get(14, 0)
    gnss_locked_fail = bool(gnss_alarms or gnss_resets)
    if gnss_locked_fail:
        tags.append('GNSS_LOCK_FAIL')
        evidence.append(
            f'锁星失败告警 {gnss_alarms} 次，接收机复位(原因1/14) {gnss_resets} 次，'
            f'时钟源切换 {master["switch_events"]} 次')
    if ocxo and ocxo['ocxo']['aging']:
        ox = ocxo['ocxo']
        tags.append('OCXO_AGING')
        evidence.append(
            f'OCXO 锁定态频偏持续超 ±50Hz 共 {ox["over_limit_points"]} 点、'
            f'{ox["over_limit_days"]} 个自然日，极值 {ox["fcv_min"]}~{ox["fcv_max"]}Hz'
            .replace('None', '—'))

    # 采集末态健康度
    last = master.get('last_clk001') if master else None
    last_note = None
    if last:
        lockbd = last.get('LockBd')
        fix = last.get('FixType')
        cno = last.get('AveCno')
        healthy = (isinstance(lockbd, (int, float)) and lockbd >= 8
                   and fix in (4, 5) and isinstance(cno, (int, float))
                   and cno >= 35)
        last_note = {'healthy': healthy, **last}
    if not tags:
        verdict = 'OK'
        status = '采集末态时钟正常'
    elif 'GNSS_LOCK_FAIL' in tags:
        if last_note and not last_note['healthy']:
            status = '采集时仍存在锁星异常'
        elif gnss_alarms or gnss_resets:
            status = '间歇性锁星故障（采集当刻短暂恢复，历史反复，未根治）'
        else:
            status = '历史锁星故障'
    else:
        status = 'OCXO 老化（持续劣化，需更换硬件）'
    spoof = gnss.get('spoofing_suspicions', []) if gnss else []
    high_conf = [s for s in spoof if s.get('confidence', 0) >= 0.8]
    return {
        'verdict': 'OK' if not tags else 'FAULT',
        'tags': tags,
        'evidence': evidence,
        'last_status': last_note,
        'status_note': status,
        'spoofing_high_confidence': len(high_conf),
        'spoofing_suspicions': len(spoof),
    }


def build_brief(out):
    """整份 parse_clock 输出的可读摘要 + 分诊结论（逐网元调用）。"""
    file_briefs = [build_file_brief(f) for f in out.get('files', [])]
    triage = build_triage(file_briefs, out.get('gnss'))
    return {'files': file_briefs, 'triage': triage,
            'gnss': out.get('gnss')}


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="BBU 时钟日志结构化解析")
    ap.add_argument('input', nargs='?', help='时钟日志文件或目录（--merge-sites 模式可不传）')
    ap.add_argument('--glob', default='clockstatuslog*',
                    help='文件模式（目录模式默认 clockstatuslog*）')
    ap.add_argument('--merge', action='store_true',
                    help='目录模式：按 mList 自动合并 gz 轮转后解析')
    ap.add_argument('--merge-sites', nargs='+', metavar='CLOCK_JSON',
                    help='合并多个站点 parse_clock.py 输出的时钟 JSON（files[] 拼接、'
                         'gnss.spoofing_suspicions 列表拼接、gnss_records 求和），'
                         '输出单份 JSON；多站点汇总喂给 render_report --clock 目录模式')
    ap.add_argument('--json', help='输出 JSON 路径')
    ap.add_argument('--brief', help='输出可读摘要 JSON（计数/末态/趋势/分诊），不写大 JSON')
    ap.add_argument('--triage', action='store_true',
                    help='只输出分诊结论（标签/证据/末态），与 --brief 配合或单独打印')
    ap.add_argument('--no-fix-timestamp', action='store_true',
                    help='关闭 2021 复位时间修复')
    args = ap.parse_args()
    for _s in (sys.stdout, sys.stderr):  # Windows GBK 控制台下避免 UnicodeEncodeError
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

    src = Path(args.input) if args.input else None
    if args.merge_sites:
        src = None
    elif src is None or not src.exists():
        raise SystemExit(f'输入不存在: {args.input}')
    fix = not args.no_fix_timestamp

    if args.merge_sites:
        # M5: 多站点 clock JSON 合并（files[] 拼接、gnss 拼接/求和），输出单份 JSON
        files = []
        gnss_records = 0
        gnss_suspicions = []
        gnss_last_status = None
        for p in map(Path, args.merge_sites):
            if not p.exists():
                raise SystemExit(f'时钟 JSON 不存在: {p}')
            d = json.loads(p.read_text(encoding='utf-8'))
            files.extend(d.get('files', []) or [])
            g = d.get('gnss') or {}
            gnss_records += g.get('gnss_records', 0) or 0
            sus = g.get('spoofing_suspicions') or []
            if not isinstance(sus, list):
                sus = [sus] if sus else []  # 类型兜底：int/单对象归一为列表
            gnss_suspicions.extend(sus)
            if g.get('last_status'):
                gnss_last_status = g['last_status']
        out = {'files': files}
        if gnss_suspicions or gnss_records:
            out['gnss'] = {'gnss_records': gnss_records,
                           'spoofing_suspicions': gnss_suspicions,
                           'last_status': gnss_last_status}
        if args.json:
            Path(args.json).write_text(
                json.dumps(out, ensure_ascii=False, indent=2, default=str),
                encoding='utf-8')
            print(f'✅ 多站点时钟合并: {len(files)} 份 → {args.json}')
            print(f"  GNSS: {gnss_records} 行, 欺骗嫌疑 {len(gnss_suspicions)} 次")
        else:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    files, results = [], []
    if src.is_file():
        files = [src]
    elif args.merge:
        merged = merge_clock_files(src)
        if not merged:
            raise SystemExit(f'目录下未找到时钟文件（mList）: {src}')
        for key, bundle in merged.items():
            text = bundle['text'] if isinstance(bundle, dict) else bundle
            line_src = bundle.get('line_src') if isinstance(bundle, dict) else None
            r = parse_clock_text(text, fix=fix, source_map=line_src)
            r['file'] = f'<merged:{key}>'
            results.append(r)
        # gnsssvinfo 欺骗检测（多槽位/进程分组时取记录最多的一份）
        gnss_result = None
        for k, bundle in merged.items():
            if k == 'gnsssvinfo' or k.startswith('gnsssvinfo@'):
                t = bundle['text'] if isinstance(bundle, dict) else bundle
                r = parse_gnss_text(t)
                if r and (gnss_result is None
                          or r.get('gnss_records', 0)
                          > gnss_result.get('gnss_records', 0)):
                    gnss_result = r
    else:
        files = sorted(p for p in src.glob(args.glob) if p.is_file())

    for f in files:
        r = parse_clock_file(f, fix=fix)
        if r:
            results.append(r)
    if not files and not args.merge:
        raise SystemExit(f'未找到时钟日志: {src}')

    # 文件模式下顺带找同目录 gnsssvinfo
    if src.is_file() and src.name.startswith('clockstatuslog'):
        for g in src.parent.glob('*gnsssvinfo*'):
            t = read_text_member(g)
            if t:
                gnss_result = parse_gnss_text(t)
                break
        else:
            gnss_result = None
    elif not args.merge:
        gnss_result = None

    out = {'files': results}
    if gnss_result:
        out['gnss'] = gnss_result

    brief = build_brief(out) if (args.brief or args.triage) else None

    if args.json:
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8')
        print(f'✅ 解析完成: {len(results)} 份 → {args.json}')
        for r in results:
            print(f"  {r['file']}: {r['total_records']} 条, "
                  f"修复 {r['timestamp_fixed']}, 告警 {len(r['alarms'])}, "
                  f"复位 {len(r['reset_reasons'])}, 异常 {len(r['anomalies'])}")
        if gnss_result:
            print(f"  GNSS: {gnss_result['gnss_records']} 行, "
                  f"欺骗嫌疑 {len(gnss_result['spoofing_suspicions'])} 次")
    if args.brief:
        Path(args.brief).write_text(
            json.dumps(brief, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8')
        tr = brief['triage']
        print(f'✅ 摘要 → {args.brief}')
        print(f"  分诊: {tr['verdict']} {('、'.join(tr['tags'])) or ''} "
              f"— {tr['status_note']}")
        for ev in tr['evidence']:
            print(f'    · {ev}')
    if args.triage and not args.brief:
        print(json.dumps(brief['triage'], ensure_ascii=False, indent=2,
                         default=str))
    if not args.json and not args.brief and not args.triage:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
