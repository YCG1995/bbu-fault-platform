#!/usr/bin/env python3
"""scan_rule_candidates.py — 从已解压日志自动扫描规则候选"""
import argparse
import bz2, csv, gzip, json, re, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_evidence import iter_xlsx_rows  # noqa: E402  同目录复用 OOXML 行解析
from log_format import classify_file       # noqa: E402  通用格式分类路由
ERR = re.compile(r"(ERROR|FAIL|Exception|Traceback|【错误】|失败|异常)", re.I)
MODULE_RE = re.compile(r"【([^】]+)】")
KWS = {
    "reboot": re.compile(r"(reboot|reset|复位|重启|看门狗)", re.I),
    "clock": re.compile(r"(1588|时钟|clock|频偏|相位|sync|同步)", re.I),
    "link": re.compile(r"(断链|link\s*down|offline|掉线|闪断)", re.I),
    "power": re.compile(r"(功率|power|电压|voltage|电流)", re.I),
    "temp": re.compile(r"(温度|temp|过热)", re.I),
}
VAL = re.compile(r"(频偏|相位差|功率|温度|电压|电流)[:：=\s]+([\-\d.]+)")
# 各关键词类目在规则建议中使用的可命中字面量
KW_LITS = {"reboot": ["复位", "reboot"], "clock": ["时钟", "1588"],
           "link": ["断链", "offline"], "power": ["功率"],
           "temp": ["温度", "过热"]}


def open_txt(p):
    cls = classify_file(p)
    if cls.get("kind") not in ("text", "compressed_text"):
        return None
    enc = cls.get("encoding") or "utf-8"
    comp = cls.get("compression")
    try:
        if comp == "gzip":
            return gzip.open(p, "rt", encoding=enc, errors="replace")
        if comp == "bzip2":
            return bz2.open(p, "rt", encoding=enc, errors="replace")
        return open(p, "r", encoding=enc, errors="replace")
    except OSError:
        return None


def fm_rows(p):
    """fm-*.csv / fm-*.xlsx → 逐行产出 [cells]（每张 sheet 首行为表头）。"""
    if p.name.lower().endswith(".csv"):
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            yield from csv.reader(f)
    else:
        seen_first = False
        for _s, rno, row in iter_xlsx_rows(p):
            if rno == 1 and seen_first:
                continue  # 后续 sheet 的表头
            seen_first = True
            yield row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--json", dest="out")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    for _s in (sys.stdout, sys.stderr):  # Windows GBK 控制台下避免 UnicodeEncodeError
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    root = Path(a.root)
    alarms = Counter()
    errors = defaultdict(list)
    kwhits = defaultdict(list)
    series = defaultdict(list)
    fc = 0
    for p in root.rglob("*"):
        if not p.is_file(): continue
        fc += 1
        low = p.name.lower()
        if ("fm-active" in low or "fm-history" in low) \
                and low.endswith((".csv", ".xlsx", ".xlsm")):
            try:
                rd = fm_rows(p)
                h = next(rd, [])
                hc = [str(x).strip().lstrip(chr(0xfeff)) for x in h]
                # "告警码"列优先精确匹配，避免误取"告警码名称"列
                ci = next((i for i, x in enumerate(hc) if x == "告警码"), None)
                if ci is None:
                    ci = next((i for i, x in enumerate(hc)
                               if "告警码" in x and "名称" not in x), None)
                ni = next((i for i, x in enumerate(hc)
                           if "名称" in x or "告警名" in x), None)
                for row in rd:
                    if not row: continue
                    c = str(row[ci]) if ci is not None and ci < len(row) else ""
                    nm = str(row[ni]) if ni is not None and ni < len(row) else ""
                    if c or nm: alarms[f"{c} {nm}".strip()] += 1
            except Exception: pass
            continue
        fh = open_txt(p)
        if fh is None: continue
        try:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line or len(line) > 2000: continue
                if ERR.search(line):
                    m = MODULE_RE.search(line)
                    mod = m.group(1) if m else (line.split(":")[0][:40] if ":" in line else "unknown")
                    errors[mod].append({"file": str(p), "line": ln, "text": line[:200]})
                for cat, pat in KWS.items():
                    if pat.search(line):
                        kwhits[cat].append({"file": str(p), "line": ln, "text": line[:200]})
                for vm in VAL.finditer(line):
                    try: series[vm.group(1)].append(float(vm.group(2)))
                    except ValueError: pass
        except (OSError, EOFError):  # 坏 gzip/读失败：跳过不中断
            pass
        finally:
            fh.close()
    out = {
        "scanned_files": fc,
        "alarm_codes": [{"code_name": k, "count": v} for k, v in alarms.most_common(a.top)],
        "error_clusters": [{"module": k, "count": len(v), "samples": v[:3]}
                           for k, v in sorted(errors.items(), key=lambda x: -len(x[1]))[:a.top]],
        "keyword_hits": {k: {"count": len(v), "samples": v[:3]} for k, v in kwhits.items() if v},
        "numeric_series": {k: {"count": len(v), "min": min(v), "max": max(v), "avg": round(sum(v)/len(v), 2)}
                           for k, v in series.items() if v},
        "rule_suggestions": [],
    }
    sug = []
    xlsx_seen = any(f["file"].lower().endswith((".xlsx", ".xlsm"))
                    for v in kwhits.values() for f in v) or \
        any(p.suffix.lower() in (".xlsx", ".xlsm")
            for p in root.rglob("fm-*") if p.is_file())
    for ac in out["alarm_codes"]:
        code = ac["code_name"].split()[0]
        sug.append({"id": f"S{len(sug) + 1}", "type": "text",
                    "description": f"告警 {ac['code_name']} x{ac['count']}",
                    "globs": ["**/fm-active_*", "**/fm-history*"],
                    "format": "xlsx" if xlsx_seen else "csv",
                    "columns": ["告警码"], "any_of": [code]})
    for cat, hits in kwhits.items():
        if not hits:
            continue
        # glob 从首个样本文件名推导（保留目录特征段）
        sample = hits[0]["file"]
        base = Path(sample).name
        glob = f"**/*{base.split('.')[0]}*" if "." in base else f"**/*{base}*"
        sug.append({"id": f"S{len(sug) + 1}", "type": "text",
                    "description": f"关键词类 {cat} x{len(hits)}（样本 {base}）",
                    "globs": [glob, "**/*.log"],
                    "any_of": KW_LITS.get(cat, [cat])})
    for ec in out["error_clusters"][:5]:
        sug.append({"id": f"S{len(sug) + 1}", "type": "text",
                    "description": f"错误模块 {ec['module']} x{ec['count']}",
                    "globs": ["**/*.log", "**/*.log.gz"], "any_of": [ec["module"]]})
    for fld, st in out["numeric_series"].items():
        sug.append({"id": f"S{len(sug) + 1}", "type": "numeric_trend",
                    "description": f"{fld} {st['min']}~{st['max']}",
                    "globs": ["**/*.log"], "value_pattern": f"{fld}[:：=\\s]+([\\-\\d.]+)",
                    "trend": "exceed_threshold", "threshold": st["max"], "sustained_count": 3})
    out["rule_suggestions"] = sug
    txt = json.dumps(out, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(txt, encoding="utf-8")
        print(f"扫描 {fc} 文件 → {a.out}")
        print(f"  告警 {len(out['alarm_codes'])} 类, 错误 {len(out['error_clusters'])} 类, "
              f"数值 {len(out['numeric_series'])} 类, 建议 {len(sug)} 条")
    else:
        print(txt)


if __name__ == "__main__":
    main()
