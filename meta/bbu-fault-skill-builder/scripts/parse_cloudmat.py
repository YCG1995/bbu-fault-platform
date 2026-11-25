#!/usr/bin/env python3
"""
parse_cloudmat.py — CloudMAT 工装拷机测试日志结构化解析

输入：单个 CloudMAT 纯文本日志（文件名形如
  <条码>_<开始时间>_<结果>_<时长秒>@<槽位>_.log）。
输出：JSON 结构化结果（基本信息、itemList、测试结果、失败项及失败原因、
  结果位图对位、拷机表字段、ftp 上传路径）。

用法：
    python3 parse_cloudmat.py <cloudmat.log> [--json out.json]
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

LINE_RE = re.compile(
    r"^(?P<lineno>\d{1,6})\s+轮次(?P<cycle>\d+)\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"【(?P<level>[^】]+)】\s+(?P<rest>.*)$"
)


def find_python_literal(text: str, opener="[", closer="]"):
    """从文本中提取首个平衡的 list/dict 字面量并 literal_eval。"""
    start = text.find(opener)
    if start < 0:
        return None, -1
    depth = 0
    quote = None
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                frag = text[start:i + 1]
                try:
                    return ast.literal_eval(frag), i
                except Exception:
                    return None, -1
    return None, -1


CALL_RE = re.compile(
    r"调用[:：]\s*(?P<iface>[^,，]+?)(?:接口)?\s*[,，]\s*参数[:：]\s*\[(?P<params>.*?)\]"
    r"\s*[,，]\s*返回值[:：]\s*(?P<retval>.*)$"
)


def _try_struct(text: str):
    """尝试把 params/返回值 文本解析为结构化字面量，失败保留原文。"""
    text = text.strip()
    for op, cl in (("[", "]"), ("{", "}")):
        if text.startswith(op):
            v, _ = find_python_literal(text, op, cl)
            if v is not None:
                return v
    return text


def parse_basic(rest: str, info: dict, filename: str):
    m = re.search(r"测试工程\s*TFP\s*[:：]?\s*(\S+)", rest)
    if m:
        info["tfp"] = m.group(1).strip("`'，,。")
    m = re.search(r"(?:产品|产品/子类)\s*[:：]?\s*([A-Za-z0-9_ /]+?)(?:\s{2,}|$|,|，|;|；)", rest)
    if m and "product" not in info:
        info["product"] = m.group(1).strip()
    m = re.search(r"条码\s*[:：]?\s*(\d{6,})", rest)
    if m:
        info["barcode"] = m.group(1)
    m = re.search(r"槽位\s*[:：]?\s*([0-9_]+)", rest)
    if m:
        info["slot"] = m.group(1)
    m = re.search(r"运行方式\s*[:：]?\s*(\w+)", rest)
    if m:
        info["run_mode"] = m.group(1)
    m = re.search(r"测试脚本\s*[:：]?\s*(\S+)", rest)
    if m:
        info["test_script"] = m.group(1)
    m = re.search(r"CloudMAT:测试结果[:：]\s*(\S+)", rest)
    if m:
        info["result"] = m.group(1).strip("，,。")
    m = re.search(r"测试时长[:：]\s*(\d+)\s*秒", rest)
    if m:
        info["duration_seconds"] = int(m.group(1))
    m = re.search(r"失败日志上传(?:ftp|FTP)[:：]?\s*(\S+)", rest)
    if m:
        info["ftp_upload"] = m.group(1).strip("，,。")
    # 文件名兜底
    if "barcode" not in info:
        m = re.match(r"(\d{6,})_", filename)
        if m:
            info["barcode"] = m.group(1)
    m = re.search(r"_(\d+)@([\d_]+)_", filename)
    if m:
        info.setdefault("duration_seconds", int(m.group(1)))
        info.setdefault("slot", m.group(2))


def parse(path: Path):
    out = {"file": path.name, "basic_info": {}, "item_list": [], "item_count": None,
           "failed_items": [], "bitmaps": [], "cycling_table": None,
           "interface_calls": [], "warnings": []}
    item_list = []
    # 收集失败项：name -> dict
    failed = {}
    item_results = []  # (lineno, cycle, module, result)
    bitmaps = []
    calls = []

    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            m = LINE_RE.match(line)
            if not m:
                rest = line
                ts = None
                lineno = None
            else:
                lineno = int(m.group("lineno"))
                ts = m.group("ts")
                rest = m.group("rest")
            parse_basic(rest, out["basic_info"], path.name)

            # itemList
            if "itemList" in rest and not item_list:
                lst, _ = find_python_literal(rest, "[", "]")
                if isinstance(lst, list) and lst:
                    item_list = [str(x).strip() for x in lst]

            # 单轮结果
            mr = re.search(r"(.+?):.*?第(\d+)轮结束，结果：(TestItemResult\.(?:Fail|Success))", rest)
            if mr:
                module = mr.group(1).strip()
                cycle = int(mr.group(2))
                result = mr.group(3)
                item_results.append((lineno, cycle, module, result))
                if result == "TestItemResult.Fail":
                    rec = failed.setdefault(module, {
                        "name": module, "fail_rounds": [], "reasons": [],
                        "first_fail_line": lineno})
                    if cycle not in rec["fail_rounds"]:
                        rec["fail_rounds"].append(cycle)

            # 失败原因行：【级别】模块:内容（通过标准 或 保存失败参数表）
            mc = re.search(r"【[^】]+】\s*(?P<module>[^:：]+)[:：](?P<body>.*)$", line)
            if mc and ("通过标准" in mc.group("body") or "保存失败参数表" in mc.group("body")):
                module = mc.group("module").strip()
                body = mc.group("body").strip()
                rec = failed.setdefault(module, {"name": module, "fail_rounds": [],
                                                  "reasons": [], "first_fail_line": lineno})
                if "保存失败参数表" in body:
                    tail = body.split("保存失败参数表", 1)[1].lstrip("—- ")
                    rec["reasons"].append({"line_no": lineno, "timestamp": ts,
                                           "raw": ("保存失败参数表 " + tail)[:1000]})
                else:
                    rec["reasons"].append({"line_no": lineno, "timestamp": ts,
                                           "raw": body[:1000]})

            # 拷机表
            if "保存拷机表数据为" in rest:
                d, _ = find_python_literal(rest, "{", "}")
                if isinstance(d, dict):
                    out["cycling_table"] = {str(k): str(v) for k, v in d.items()}

            # 接口调用返回值（失败项接口调用日志，CloudMAT/VSWe 工装通用）
            if "调用" in rest and "返回值" in rest:
                mc = CALL_RE.search(line)
                if mc:
                    calls.append({
                        "line_no": lineno, "timestamp": ts,
                        "interface": mc.group("iface").strip(),
                        "params": _try_struct(mc.group("params")),
                        "retval": _try_struct(mc.group("retval")),
                    })

            # 结果位图：优先取含锚点关键字行的 0/1 长串，避免把普通二进制串误当位图
            anchor = bool(re.search(r"getItemResultInfo|结果位图", rest))
            min_len = 20 if anchor else 40
            for mb in re.finditer(rf"(?<![01])([01]{{{min_len},}})(?![01])", line):
                bm = mb.group(1)
                bitmaps.append({"line_no": lineno, "timestamp": ts, "bitmap": bm, "length": len(bm)})

    out["item_list"] = item_list
    out["item_count"] = len(item_list) or None

    # 位图对位：优先取长度等于 item_count 的最后一个
    for bm in bitmaps:
        if item_list and bm["length"] == len(item_list):
            bm["mapped_failed_items"] = [
                {"index": i, "name": item_list[i]}
                for i, ch in enumerate(bm["bitmap"]) if ch == "1" and i < len(item_list)]
    out["bitmaps"] = bitmaps

    # 失败项汇总：连续失败轮次区间
    for rec in failed.values():
        rounds = sorted(rec["fail_rounds"])
        rec["first_fail_round"] = rounds[0] if rounds else None
        rec["last_fail_round"] = rounds[-1] if rounds else None
        rec["consecutive_from_round"] = (
            rounds[0] if rounds and rounds == list(range(rounds[0], rounds[-1] + 1)) else None)
        rec["fail_round_count"] = len(rounds)
    out["failed_items"] = sorted(failed.values(), key=lambda r: r["first_fail_line"] or 0)
    out["interface_calls"] = calls

    # 若位图已对位，用其校验失败项集合（防止关键字漏抓）
    mapped = []
    for bm in bitmaps:
        mapped.extend(x["name"] for x in bm.get("mapped_failed_items", []))
    if mapped:
        names = {r["name"] for r in out["failed_items"]}
        missing = [n for n in mapped if n not in names]
        if missing:
            out["warnings"].append(f"位图显示失败但未在单轮结果中解析到的测试项: {missing}")
    if not item_list:
        out["warnings"].append("未解析到 itemList，位图无法对位")
    return out


def main():
    ap = argparse.ArgumentParser(description="CloudMAT 拷机日志结构化解析")
    ap.add_argument("logfile")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()
    for _s in (sys.stdout, sys.stderr):  # Windows GBK 控制台下避免 UnicodeEncodeError
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    path = Path(args.logfile)
    if not path.exists():
        sys.exit(f"文件不存在: {path}")
    result = parse(path)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
        print(f"结果: {result['basic_info'].get('result')}；"
              f"测试项 {result['item_count']}；失败项 {len(result['failed_items'])}；"
              f"位图 {len(result['bitmaps'])}；接口调用 {len(result['interface_calls'])}"
              f" → {args.json_out}")
        for fi in result["failed_items"]:
            print(f"  FAIL {fi['name']} 轮次{fi['fail_rounds'][:10]}"
                  f"{'...' if len(fi['fail_rounds']) > 10 else ''} 原因{len(fi['reasons'])}条")
    else:
        print(text)
    for w in result["warnings"]:
        print("WARN:", w)


if __name__ == "__main__":
    main()
