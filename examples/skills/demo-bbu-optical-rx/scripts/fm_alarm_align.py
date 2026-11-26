#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fm_alarm_align.py — FM 告警时间窗对齐工具（N4）

把 fm-active / fm-history 告警表（csv/xlsx）中的「时钟相关告警码」与
规则证据 JSON（evidence.json，含命中时间戳）做时间窗对齐，判断：
  - 告警是否落在证据时间窗内（吻合）
  - 是否早于/晚于证据时间（错位，可能因告警留存/采集延迟）
  - 采集包中根本没有该告警（无佐证）

输出：
  --json <file>   对齐结果 JSON（每文件一行/每告警一条）
  --md   <file>   可追加到报告「3 故障现象与甄别」的 MD 片段
  --window <h>    匹配时间窗（小时），默认 48h

依赖：纯 stdlib（xlsx 复用同目录 search_evidence.iter_xlsx_rows），随 report 组件分发。

用法：
  python3 fm_alarm_align.py <解压目录> --evidence evidence.json \
      --json fm_align.json --md fm_align.md
"""
import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from search_evidence import iter_xlsx_rows
except ImportError:
    iter_xlsx_rows = None

# 时钟相关告警：命中「告警名称」或「告警码」任一即视为时钟类（与 alarm-code-dictionary 对齐）
CLOCK_ALARM_NAMES = (
    "时钟", "同步", "失锁", "1588", "ocxo", "晶振", "参考源", "时钟源",
    "相位", "1pps", "gnss", "锁星", "卫星", "时间同步", "ptp", "bds", "gps",
)
CLOCK_ALARM_CODES = {
    "198092217", "198096802", "198092215", "198096830", "198096831",
    "198096834", "198096835", "198096836", "198096837", "198096841",
    "198097004", "198096840", "198096839",
}
TIME_FM = (
    "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y%m%d%H%M%S",
)
TIME_EV = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S UTC",
    "%Y-%m-%d,%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
)


def _utf8():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


def _is_fm(low):
    return any(k in low for k in ("fm-active", "fm_history", "fm_active",
                                  "fm-history", "currentalarm", "historyalarm",
                                  "历史告警", "当前告警", "活动告警"))


def _dt(raw, fmts):
    if not raw:
        return None
    s = str(raw).strip()
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    # 数字时间戳（Excel 序列号）
    try:
        from datetime import timedelta
        v = float(s)
        if 20000 < v < 60000:  # Excel 日期序列号
            return datetime(1899, 12, 30) + timedelta(days=v)
    except (ValueError, TypeError):
        pass
    return None


def _alarm_name_col(header):
    """告警名称列：兼容「告警名称/告警名/告警码名称」（真实网管导出为「告警码名称」）。"""
    for j, x in enumerate(header):
        s = str(x).strip()
        if "告警名称" in s or "告警名" in s or s == "告警码名称":
            return j
    return None


def _alarm_code_col(header):
    """告警码列：优先精确「告警码」；其次含「告警码」但不含「名称」；最后任意含「告警码」。"""
    heads = [str(x).strip() for x in header]
    for pred in (lambda s: s == "告警码",
                 lambda s: "告警码" in s and "名称" not in s,
                 lambda s: "告警码" in s):
        for j, s in enumerate(heads):
            if pred(s):
                return j
    return None


def _time_col(header):
    for j, x in enumerate(header):
        s = str(x)
        if "时间" in s or "时刻" in s:
            return j
    return None


def _read_csv(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        return rows, "csv"
    except Exception:
        for enc in ("gb18030", "big5"):
            try:
                with open(path, encoding=enc, newline="") as f:
                    rows = list(csv.reader(f))
                return rows, f"csv({enc})"
            except Exception:
                continue
    return None, None


def _read_xlsx(path):
    if iter_xlsx_rows is None:
        return None, "xlsx(无search_evidence)"
    try:
        rows = [[("" if c is None else str(c)) for c in cells]
                for _s, _n, cells in iter_xlsx_rows(path) if any(str(c).strip() for c in cells)]
        return rows, "xlsx"
    except Exception as e:
        return None, f"xlsx(err:{e})"


def _clock_hit(name, code):
    if code and str(code).strip() in CLOCK_ALARM_CODES:
        return True
    if name:
        nl = str(name).lower()
        return any(k in nl for k in CLOCK_ALARM_NAMES)
    return False


def align_dir(directory, evidence_json, window_h=48, local_tz_offset=8):
    """扫描 fm 告警文件，与证据时间对齐。返回 dict + notes。
    local_tz_offset: FM 告警时间的本地时区相对 UTC 的偏移（小时）；
                     search_evidence 已把证据时间转成 UTC，故比较前先把 FM 本地时间减该偏移。"""
    directory = Path(directory)
    from datetime import timedelta
    fm_to_utc = timedelta(hours=local_tz_offset)
    fm_files = [p for p in directory.rglob("*")
                if p.is_file() and _is_fm(p.name.lower())
                and p.suffix.lower() in (".csv", ".xlsx", ".xlsm")]
    ev_ts = _evidence_times(evidence_json)
    window = window_h * 3600
    notes, out = [], []
    for f in fm_files:
        low = f.name.lower()
        if low.endswith(".csv"):
            rows, how = _read_csv(f)
        else:
            rows, how = _read_xlsx(f)
        if rows is None:
            if "无search_evidence" in str(how):
                notes.append(f"{f.name}：缺少同目录 search_evidence.py，无法读取 .xlsx，已跳过")
            elif "err" in str(how):
                notes.append(f"{f.name}：读取失败（{how}）")
            continue
        header = rows[0] if rows else []
        ni, ci, ti = _alarm_name_col(header), _alarm_code_col(header), _time_col(header)
        if ni is None and ci is None:
            notes.append(f"{f.name}：未找到「告警名称/告警码」列，跳过")
            continue
        clock_rows = []
        for r in rows[1:]:
            name = r[ni] if ni is not None and ni < len(r) else ""
            code = r[ci] if ci is not None and ci < len(r) else ""
            if not _clock_hit(name, code):
                continue
            ts = _dt(r[ti], TIME_FM) if ti is not None and ti < len(r) else None
            clock_rows.append({"name": name, "code": str(code).strip(),
                               "time": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None,
                               "raw_time": r[ti] if ti is not None and ti < len(r) else None})
        if not clock_rows:
            notes.append(f"{f.name}：未发现时钟相关告警（或告警为空表）")
            continue
        # 时间窗对齐：最近证据时间 + 距离
        for a in clock_rows:
            a["matched"] = None
            a["gap_s"] = None
            if a["time"] and ev_ts:
                at = _dt(a["time"], TIME_EV)
                if at:
                    at = at - fm_to_utc  # FM 本地时间 → UTC
                    best = min(ev_ts, key=lambda x: abs((x - at).total_seconds()))
                    gap = abs((best - at).total_seconds())
                    a["gap_s"] = int(gap)
                    a["nearest_evidence"] = best.strftime("%Y-%m-%d %H:%M:%S")
                    a["matched"] = "in_window" if gap <= window else "outside_window"
            out.append({"file": f.name, "how": how, "alarm": a})
    return {"window_h": window_h, "files": len(fm_files), "alarms": out,
            "notes": notes, "has_evidence": bool(ev_ts)}


def _evidence_times(evidence_json):
    """收集证据 JSON 中所有命中时间戳（UTC naive）。"""
    ts = []
    if not evidence_json:
        return ts
    ev = json.loads(evidence_json.read_text(encoding="utf-8"))
    for r in ev.get("results", []):
        for e in r.get("evidence", []):
            if e.get("timestamp"):
                dt = _dt(e["timestamp"], TIME_EV)
                if dt:
                    ts.append(dt)
    # 专项关键时间（events）也并入
    for g in (ev.get("verdict") or {}).get("groups", {}).values():
        for e in (g.get("events") or []) if isinstance(g, dict) else []:
            if e.get("timestamp"):
                dt = _dt(e["timestamp"], TIME_EV)
                if dt:
                    ts.append(dt)
    return ts


def to_md(align, out=None):
    L = []
    if not align or not align.get("alarms"):
        L.append("FM 告警时间窗对齐：未发现时钟相关告警记录"
                 + (f"（{align['notes'][0]}）" if align.get("notes") else "。"))
    else:
        L.append("FM 告警时间窗对齐（"
                 + ("已识别证据时间" if align.get("has_evidence") else "无证据时间，仅列告警")
                 + f"，匹配窗 {align['window_h']}h）：")
        rows = []
        for it in align["alarms"]:
            a = it["alarm"]
            st = {"in_window": "✓ 与证据吻合", "outside_window": "⚠ 偏离证据时间",
                  None: "—"}.get(a.get("matched"))
            rows.append([it["file"], md_cell(a["name"], 20), a.get("code") or "-",
                         a.get("time") or a.get("raw_time") or "-", st])
        if not rows:
            rows = [["—", "—", "—", "—", "—"]]
        L += _table(["告警文件", "告警名称", "告警码", "告警时间", "与证据对齐"], rows)
        if align.get("notes"):
            L += ["- 注：" + "；".join(align["notes"])]
    L.append("")
    return "\n".join(L)


def md_cell(s, n):
    s = str(s).replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _table(head, rows):
    L = ["| " + " | ".join(head) + " |",
         "| " + " | ".join(["---"] * len(head)) + " |"]
    for r in rows:
        L.append("| " + " | ".join(md_cell(x, 40) for x in r) + " |")
    return L


def main():
    _utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", help="已解压日志目录（扫描 fm-* 告警表）")
    ap.add_argument("--evidence", help="evidence.json 路径（提供证据时间做窗口对齐）")
    ap.add_argument("--window", type=int, default=48, help="匹配时间窗（小时，默认 48）")
    ap.add_argument("--local-tz-offset", type=int, default=8,
                    help="FM 告警时间本地时区相对 UTC 的偏移（小时，默认 +8；与 search_evidence --local-tz-offset 一致）")
    ap.add_argument("--json", dest="out_json")
    ap.add_argument("--md", dest="out_md")
    a = ap.parse_args()

    evp = Path(a.evidence) if a.evidence else None
    if evp and not evp.exists():
        print(f"[!] --evidence 不存在：{evp}", file=sys.stderr)
        evp = None
    align = align_dir(a.directory, evp, a.window, a.local_tz_offset)

    if a.out_json:
        Path(a.out_json).write_text(
            json.dumps(align, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] FM 告警对齐 JSON → {a.out_json}")
    if a.out_md:
        Path(a.out_md).write_text(to_md(align), encoding="utf-8")
        print(f"[OK] FM 告警对齐 MD → {a.out_md}")
    if not a.out_json and not a.out_md:
        print(json.dumps(align, ensure_ascii=False, indent=2))
    # 发现「偏离证据时间」的时钟告警时提示（仅提示，非阻断）
    n_out = sum(1 for it in align["alarms"] if it["alarm"].get("matched") == "outside_window")
    if n_out:
        print(f"[!] {n_out} 条时钟告警偏离证据时间窗，请结合告警采集/留存差异人工核对", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
