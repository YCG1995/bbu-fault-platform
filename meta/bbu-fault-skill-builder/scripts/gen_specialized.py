#!/usr/bin/env python3
"""
gen_specialized.py — 由专项脚本结论生成 render_report.py 的 --specialized JSON

背景：当子 Skill 的标准 evidence-rules 为"宽松命中"而专项脚本（如 ocxo_aging_analyzer）
做精确判定时，结论以专项脚本为准。本工具把专项脚本的每站点结论归一成
render_report --specialized 可直接读取的 {"sites": {...}}（或单对象）格式，
避免人工手写含完整站点根路径的 JSON。

输入结论支持三种形态（见 --conclusions 说明）：
  A) 单对象  {"primary": null|"R1", "status": "normal|matched|unmatched|temporal_conflict",
              "note": "...", "site": "<verdict组键>", "source": "专项脚本名"}
  B) 已分组  {"sites": {"<verdict组键>": {...同一字段...}, ...}}
  C) 列表    [ {...同 A...}, ... ]

归一规则（与 render_report.spec_map 完全一致）：
  - status == "normal"  → "unmatched"
  - status 缺失         → 按 primary 推导（primary 非空 → matched；否则 unmatched）
  - primary / status / note / source / events 逐项透传（events 供报告「专项脚本关键时间」展示）

用法：
    python3 scripts/gen_specialized.py --out specialized.json \
        --conclusions concl.json
    python3 scripts/gen_specialized.py --out specialized.json \
        --from-special special_13195742.json special_13196231.json
    python3 scripts/gen_specialized.py --out specialized.json \
        --site 13196231 --conclusion '{"primary":null,"status":"unmatched","note":"专项脚本判定正常","events":[{"timestamp":"...","desc":"..."}]}'
（Windows 下 python3 替换为 python；零三方依赖）
"""
import argparse
import json
import sys
from pathlib import Path


def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


def normalize(item):
    """单条专项结论 → render_report --specialized 的站点子对象（与 spec_map 对齐）。"""
    if not isinstance(item, dict):
        sys.exit(f"专项结论应为对象，收到: {item!r}")
    status = item.get("status")
    if status == "normal":
        status = "unmatched"
    if not status:
        status = "matched" if item.get("primary") else "unmatched"
    return {"primary": item.get("primary"),
            "status": status,
            "note": item.get("note", ""),
            "source": item.get("source", ""),
            "events": item.get("events", [])}


def collect(data):
    """把 A/B/C 三种输入统一为 [(site_key, 归一子对象)]。"""
    out = []
    if isinstance(data, dict) and isinstance(data.get("sites"), dict):
        for k, s in data["sites"].items():
            out.append((k, normalize(s)))
    elif isinstance(data, list):
        for s in data:
            out.append((s.get("site") or "ALL", normalize(s)))
    elif isinstance(data, dict):
        out.append((data.get("site") or "ALL", normalize(data)))
    else:
        sys.exit(f"无法识别的结论结构: {data!r}")
    return out


def from_special_files(files, site_override=None):
    """读取专项脚本输出的 special_<网元>.json，按站点映射为 render_report 结论。

    special_*.json 约定结构（各专项脚本产出）：
      {"summary": {"primary_condition": <int|None>, "condition_text": "...", ...},
       "result": {"occurrence_time": "...", "description": "...", ...}, ...}
    映射：
      primary_condition 1/2 → primary="R1"，3/4 → primary="R3"（或由 profile 根因顺序推断）；
      None → primary=None；status 命中 → matched / 未命中 → unmatched；
      note = condition_text/description；events = [occurrence_time+description]。

    站点键：优先取 site_override（--site）；未指定时从文件名推断（special_<键>.json 的 <键>）。
    """
    out = []
    for p in files:
        fp = Path(p)
        if not fp.exists():
            sys.exit(f"专项 JSON 不存在: {p}")
        d = json.loads(fp.read_text(encoding="utf-8"))
        sm = d.get("summary") or {}
        rs = d.get("result") or {}
        pc = sm.get("primary_condition")
        # primary_condition → R1/R3（1/2→R1，3/4→R3）；None → null
        primary = None
        if pc is not None:
            primary = "R1" if int(pc) in (1, 2) else "R3"
        # 站点键：--site 覆盖 > 文件名 special_<键>.json
        stem = fp.stem
        key = site_override or (stem[len("special_"):] if stem.startswith("special_") else stem)
        note = sm.get("condition_text") or rs.get("description") or ""
        events = []
        if rs.get("occurrence_time") or rs.get("description"):
            events.append({"timestamp": rs.get("occurrence_time") or "",
                           "desc": rs.get("description") or ""})
        status = "matched" if pc is not None else "unmatched"
        out.append((key, {"primary": primary, "status": status, "note": note,
                          "source": fp.name, "events": events}))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="由专项脚本结论生成 render_report --specialized JSON（归一 normal→unmatched）")
    ap.add_argument("--out", required=True, help="输出 specialized.json 路径")
    ap.add_argument("--conclusions", help="结论 JSON 文件路径（A/B/C 三形态）")
    ap.add_argument("--site", help="单条结论所属 verdict 组键（配合 --conclusion）")
    ap.add_argument("--conclusion", help="单条结论内联 JSON（配合 --site）")
    ap.add_argument("--from-special", nargs="+", metavar="SPECIAL_JSON",
                    help="直接读取专项脚本输出的 special_*.json（可多个），自动按站点映射"
                         "（primary_condition 1/2→R1、3/4→R3，None→unmatched）；"
                         "站点键从文件名 special_<键>.json 推断，可用 --site 覆盖")
    args = ap.parse_args()
    _utf8_stdio()

    items = []
    if args.from_special:
        items = from_special_files(args.from_special, site_override=args.site)
    elif args.conclusions:
        p = Path(args.conclusions)
        if not p.exists():
            sys.exit(f"结论文件不存在: {p}")
        items = collect(json.loads(p.read_text(encoding="utf-8")))
    elif args.conclusion:
        data = json.loads(args.conclusion)
        if isinstance(data, dict) and "sites" in data:
            items = collect(data)
        else:
            items = [(args.site or data.get("site") or "ALL", normalize(data))]
    else:
        sys.exit("必须提供 --from-special、--conclusions 或 --conclusion（可配 --site）")

    if not items:
        sys.exit("结论为空，未生成任何站点映射")
    # 多站点 → {"sites": {...}}；单站点且组键为 ALL → 单对象（render 亦兼容）
    if len(items) == 1 and items[0][0] == "ALL":
        out = items[0][1]
    else:
        out = {"sites": dict(items)}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    print(f"专项复核 JSON → {args.out}")
    for k, s in items:
        print(f"  [{k}] primary={s['primary']} status={s['status']}"
              + (f" note={s['note']}" if s.get("note") else "")
              + (f" source={s['source']}" if s.get("source") else ""))


if __name__ == "__main__":
    main()
