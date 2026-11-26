#!/usr/bin/env python3
"""规则库导入器（builder 侧工具，不分发给子 Skill）。

把「黑匣子规则识别库」/「蜂鸟(HCC REPORT)规则库」的 xlsx/csv 解析为候选
fault-profile.yaml（mode: triage，规则全部 verified: false，只挂 analysis.steps）。

用法：
  python import_rule_library.py --source 规则.xlsx --out out/profile.yaml
  python import_rule_library.py --source 规则目录/ --out out_dir/ [--json meta.json]
选项：--type auto|blackbox|hummingbird  --board <板型>  --json <meta.json>
      --baseline <旧profile>  --merge-into <profile>  --list-groups  --only-groups <正则>
      --step-by group|sheet  --no-json-profile  --per-file（目录输入默认即按文件拆分）
分组：黑匣子按「故障呈现」（缺省回退「检查规则」），蜂鸟按「规则名称」。

零三方依赖：xlsx 复用 search_evidence.iter_xlsx_rows；csv 按 utf-8-sig→gbk→gb18030 解码。
"""
import argparse
import csv
import hashlib
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_evidence import iter_xlsx_rows  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 规范字段 -> 表头候选（去空白后精确匹配，其次包含匹配）；新增列变体只需改此表
BLACKBOX_COLS = {
    "keyword": ["关键字"],
    "mode": ["检测规则"],
    "path": ["日志路径和文件名", "日志路径"],
    "present": ["故障呈现"],
    "desc": ["故障描述"],
    "check": ["检查规则", "检查文件"],
    "advice": ["维修建议"],
    "auto": ["自动检查"],
    "block": ["业务工序是否拦截", "返修工序是否拦截"],
}
HUMMING_COLS = {
    "name": ["规则名称"],
    "detail": ["详细信息"],
    "impact": ["具体影响"],
    "advice": ["处理建议"],
    "rid": ["规则ID"],
    "rtype": ["规则类型"],
    "module": ["模块"],
    "hw": ["硬件类型"],
    "upgrade": ["影响升级"],
    "unavail": ["业务不可用"],
}
HUMMING_TERMS = ["时钟", "运行日志", "版本", "license", "FLASH", "SSD", "内存", "分区表",
                 "文件系统", "晶振", "OCXO", "TA值", "taValueLteIr", "升级失败", "反复复位",
                 "FLASH空间", "序列号"]
MAX_ALL_OF = 8
MAX_ANCHORS = 6
SPLIT_NL = re.compile(r"[\r\n;；]+")
LOG_NAME_RE = re.compile(r"[A-Za-z0-9_\-.]+\.(?:log|txt|xml|cfg|csv|dat|json|bin)\b", re.I)
BRACKET_RE = re.compile(r"[【\[]([^】\]]{1,40})[】\]]")
QUOTE_RE = re.compile(r"[\"'“”‘’]([A-Za-z0-9_\-.]{2,40})[\"'“”‘’]")


def norm(s):
    return re.sub(r"\s+", "", str(s or ""))


def map_header(cells, colmap):
    """表头 -> {字段: 列号}；取不到的字段缺省。"""
    heads = [norm(c) for c in cells]
    out = {}
    for field, cands in colmap.items():
        for cand in cands:
            if cand in heads:
                out[field] = heads.index(cand)
                break
        else:
            for cand in cands:
                hit = next((i for i, h in enumerate(heads) if cand in h), None)
                if hit is not None:
                    out[field] = hit
                    break
    return out


def sig_blackbox(m):
    return "keyword" in m and ("path" in m or "present" in m)


def sig_humming(m):
    return "name" in m and "detail" in m


def read_rows(path, warnings):
    """统一读取 -> [(sheet, 行号, cells)]；失败记警告返回 None。"""
    suf = path.suffix.lower()
    try:
        if suf in (".xlsx", ".xlsm", ".xltx"):
            return list(iter_xlsx_rows(path))
        if suf == ".csv":
            raw = path.read_bytes()
            for enc in ("utf-8-sig", "gbk", "gb18030"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                warnings.append(f"{path.name}：csv 解码失败（utf-8-sig/gbk/gb18030），已跳过")
                return None
            return [(path.stem, i, row) for i, row in enumerate(csv.reader(io.StringIO(text)), 1)]
    except Exception as e:
        warnings.append(f"{path.name}：读取失败（{e}），已跳过")
        return None
    warnings.append(f"{path.name}：不支持的文件类型，已跳过")
    return None


def split_sheets(rows):
    out = {}
    for sname, rno, cells in rows:
        out.setdefault(sname, []).append((rno, cells))
    return out


def cell(cells, m, field):
    i = m.get(field)
    return str(cells[i]).strip() if i is not None and i < len(cells) else ""


def split_keywords(text):
    """换行/分号拆；逗号、顿号仅在括号外拆（保护 {1,3} 之类）。"""
    parts = []
    for chunk in SPLIT_NL.split(text):
        buf, depth = "", 0
        for ch in chunk:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            if ch in ",，、" and depth == 0:
                parts.append(buf)
                buf = ""
            else:
                buf += ch
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def to_pattern(frag):
    try:
        re.compile(frag)
        return frag
    except re.error:
        return re.escape(frag)


def path_to_globs(text):
    out = []
    for frag in split_keywords(text):
        base = re.split(r"[\\/]", frag.strip())[-1].strip()
        if not base:
            continue
        g = "**/" + base + ("" if any(c in base for c in "*?[") else "*")
        if g not in out:
            out.append(g)
    return out or ["**/*.log*"]


def fp_kw(all_of, any_of):
    """内容摘要用的关键字串（与 rule_library_diff 读 profile 时的格式一致）。"""
    return "；".join(all_of) + "|" + "；".join(any_of)


def parse_blackbox(sheets, fname, warnings, meta):
    rules, seen = [], {}
    for sname, rows in sheets.items():
        hi = next(((k, map_header(c, BLACKBOX_COLS)) for k, (_r, c) in enumerate(rows)
                   if sig_blackbox(map_header(c, BLACKBOX_COLS))), None)
        if not hi:
            meta["sheets"].append({"sheet": sname, "role": "skipped(无规则列签名)", "rules": 0})
            continue
        k, m = hi
        n = dup = 0
        for rno, cells in rows[k + 1:]:
            kw, chk = cell(cells, m, "keyword"), cell(cells, m, "check")
            pres = cell(cells, m, "present") or cell(cells, m, "desc")
            if not (kw or chk or pres):
                continue
            title = pres or chk
            frags = split_keywords(kw)
            if len(frags) > MAX_ALL_OF:
                warnings.append(f"{fname}[{sname} L{rno}]：关键字 {len(frags)} 个，仅取前 {MAX_ALL_OF}")
            literal = "正则" not in cell(cells, m, "mode") and cell(cells, m, "mode") != ""
            short = [f for f in frags[:MAX_ALL_OF] if len(f) < 4 and f.isascii()]
            if short:
                warnings.append(f"{fname}[{sname} L{rno}]：关键字过短 {short}，OR 语义下噪声大，建议人工收窄")
            pats = [re.escape(f) if literal else to_pattern(f) for f in frags[:MAX_ALL_OF]]
            if not cell(cells, m, "path"):
                warnings.append(f"{fname}[{sname} L{rno}]：日志路径为空，回退 **/*.log*（需人工收窄）")
            globs = path_to_globs(cell(cells, m, "path"))
            ident = title
            digest = hashlib.md5("|".join(pats + globs + [chk]).encode()).hexdigest()
            if (ident, digest) in seen:
                dup += 1
                continue
            seen[(ident, digest)] = True
            rules.append({
                "group": title or "未命名", "title": title, "all_of": [], "any_of": pats,
                "globs": globs, "src": f"{fname}[{sname} L{rno}]", "sheet": sname,
                "extra": {k2: cell(cells, m, k2) for k2 in ("check", "advice", "auto", "block")
                          if cell(cells, m, k2)},
                "key": title, "sig": digest,
                "fp_kw": fp_kw([], pats), "fp_path": ",".join(globs), "fp_check": chk,
            })
            n += 1
        meta["sheets"].append({"sheet": sname, "role": "blackbox", "rules": n})
        if dup:
            warnings.append(f"{fname}[{sname}]：{dup} 条与已解析 sheet 重复，已合并")
    return rules


def anchors_for(text, name):
    found = []

    def add(x):
        if x and x not in found:
            found.append(x)
    for rx in (LOG_NAME_RE,):
        for mt in rx.finditer(text):
            add(mt.group(0))
    for mt in BRACKET_RE.finditer(text):
        add(mt.group(1).strip())
    for mt in QUOTE_RE.finditer(text):
        add(mt.group(1))
    for t in HUMMING_TERMS:
        if t.lower() in text.lower():
            add(t)
    found = found[:MAX_ANCHORS] or [name]
    return [re.escape(a) for a in found]


def parse_humming(sheets, fname, warnings, meta):
    rules, seen = [], set()
    for sname, rows in sheets.items():
        hi = next(((k, map_header(c, HUMMING_COLS)) for k, (_r, c) in enumerate(rows)
                   if sig_humming(map_header(c, HUMMING_COLS))), None)
        if not hi:
            meta["sheets"].append({"sheet": sname, "role": "skipped(无规则列签名)", "rules": 0})
            continue
        k, m = hi
        n = 0
        for rno, cells in rows[k + 1:]:
            name, detail = cell(cells, m, "name"), cell(cells, m, "detail")
            if not (name or detail):
                continue
            if (name, detail) in seen:
                continue
            seen.add((name, detail))
            any_of = anchors_for(detail or name, name)
            rules.append({
                "group": name or detail[:30], "title": name or detail[:30],
                "all_of": [], "any_of": any_of,
                "globs": ["**/*.log", "**/*.txt"], "src": f"{fname}[{sname} L{rno}]", "sheet": sname,
                "extra": {k2: cell(cells, m, k2) for k2 in
                          ("detail", "impact", "advice", "rid", "rtype", "module", "hw",
                           "upgrade", "unavail") if cell(cells, m, k2)},
                "key": name or detail[:30], "sig": hashlib.md5(detail.encode()).hexdigest(),
                "fp_kw": fp_kw([], any_of), "fp_path": "**/*.log,**/*.txt", "fp_check": detail,
            })
            n += 1
        meta["sheets"].append({"sheet": sname, "role": "hummingbird", "rules": n})
        if n == 0:
            warnings.append(f"{fname}[{sname}]：命中签名但无数据行（巡检故障报告/开发者确认属正常预期）")
    return rules


def detect_and_parse(path, forced="auto", warnings=None):
    """-> (type, rules, meta)；无法识别返回 (None, [], meta)。"""
    warnings = warnings if warnings is not None else []
    meta = {"file": path.name, "type": None, "sheets": []}
    rows = read_rows(path, warnings)
    if rows is None:
        return None, [], meta
    sheets = split_sheets(rows)

    def has(sig, cmap):
        return any(sig(map_header(c, cmap)) for rs in sheets.values() for _r, c in rs[:10])
    typ = forced
    if typ == "auto":
        typ = ("blackbox" if has(sig_blackbox, BLACKBOX_COLS)
               else "hummingbird" if has(sig_humming, HUMMING_COLS) else None)
    if typ is None:
        warnings.append(f"{path.name}：未识别到规则库签名（可能是度量字典等非规则表），已跳过")
        return None, [], meta
    meta["type"] = typ
    fn = parse_blackbox if typ == "blackbox" else parse_humming
    rules = fn(sheets, path.name, warnings, meta)
    if not rules:
        warnings.append(f"{path.name}：解析到 0 条规则")
    return typ, rules, meta


def infer_board(path):
    stem = path.stem
    mt = re.search(r"(VSW|VBP)[a-z]?\d*[A-Za-z]?\d*", stem, re.I)
    if "ITRAN" in stem.upper():
        return "ITRAN"
    if "SDR" in stem.upper():
        return "SDR"
    if mt:
        return mt.group(0)[:3].upper() + mt.group(0)[3:]
    return "待补充"


def make_slug(path, board, used):
    stem = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")
    if not stem:
        b = re.sub(r"[^a-z0-9]+", "-", board.lower()).strip("-")
        stem = b if b and b != "待补充" else hashlib.md5(path.stem.encode()).hexdigest()[:6]
    base = ("bbu-fault-" + stem)[:64].rstrip("-")
    slug, i = base, 2
    while slug in used:
        suffix = f"-{i}"
        slug = base[:64 - len(suffix)].rstrip("-") + suffix
        i += 1
    used.add(slug)
    return slug


KW_SPLIT_RE = re.compile(r"(?:是否|发生|存在|导致|出现|进行|及|的|和|与|或|了|在|被|将)+")
KW_STOP = {"故障", "异常", "检查", "单板", "失败", "错误", "问题", "没有", "不足", "损坏"}


def keywords_from(rules, limit=15):
    """按「出现在多少条规则标题中」降序挑选（同频保持首次出现顺序），去停用词。"""
    count, order = {}, []
    for r in rules:
        seen = set()
        for run in re.findall(r"[\u4e00-\u9fa5]{2,}", r["title"]):
            for chunk in KW_SPLIT_RE.split(run):
                if 2 <= len(chunk) <= 8 and chunk not in KW_STOP:
                    seen.add(chunk)
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,11}", r["title"]):
            seen.add(tok)
        for tok in seen:
            if tok not in count:
                count[tok] = 0
                order.append(tok)
            count[tok] += 1
    return sorted(order, key=lambda x: -count[x])[:limit]


def desc_ident(description, counter):
    """从 profile 规则 description（去掉「（来源 …）」）还原身份键，重名加 #n。"""
    title = re.sub(r"（来源 .*?）$", "", str(description))
    counter[title] = counter.get(title, 0) + 1
    return title if counter[title] == 1 else f"{title}#{counter[title]}"


def assign_idents(rules):
    cnt = {}
    for r in rules:
        cnt[r["title"]] = cnt.get(r["title"], 0) + 1
        r["ident"] = r["title"] if cnt[r["title"]] == 1 else f"{r['title']}#{cnt[r['title']]}"


def load_profile_any(path):
    """读已有 profile：统一 .yaml。"""
    p = Path(path)
    if p.suffix.lower() not in (".yaml", ".yml"):
        sys.exit(f"{p} 仅支持 .yaml 规则/配置")
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except ImportError:
        sys.exit(f"{p} 为 YAML 但缺少 PyYAML：pip install pyyaml")


def _num(s):
    m = re.search(r"\d+", str(s))
    return int(m.group(0)) if m else 0


def build_profile(path, typ, rules, board, slug, baseline=None, step_by="group"):
    assign_idents(rules)
    base_rule, base_step, rid_max, sid_max = {}, {}, 0, 0
    if baseline:
        cnt = {}
        for st in (baseline.get("analysis") or {}).get("steps") or []:
            base_step[st.get("title", "")] = st.get("id")
            sid_max = max(sid_max, _num(st.get("id")))
            for r in st.get("rules") or []:
                base_rule[desc_ident(r.get("description", ""), cnt)] = r.get("id")
                rid_max = max(rid_max, _num(r.get("id")))
    groups = {}
    for r in rules:
        key = (r.get("sheet") or "sheet") if step_by == "sheet" else r["title"]
        groups.setdefault(key, []).append(r)
    steps, n_rule, n_step = [], rid_max, sid_max
    used_ids = set()
    for gi, (gname, rs) in enumerate(groups.items(), 1):
        title = gname[:60]
        if baseline:
            sid = base_step.get(title)
            if not sid:
                n_step += 1
                sid = f"S{n_step}"
        else:
            sid = f"S{gi}"
        srules = []
        for r in rs:
            rid = base_rule.get(r["ident"]) if baseline else None
            if not rid or rid in used_ids:
                n_rule += 1
                rid = f"E{n_rule}"
            used_ids.add(rid)
            r["id"] = rid
            rule = {
                "id": rid, "step": sid,
                "description": f"{r['title']}（来源 {r['src']}）",
                "type": "text", "globs": r["globs"], "encoding": "auto", "format": "text",
                "all_of": r["all_of"], "any_of": r["any_of"], "none_of": [],
                "min_matches": 1, "context_lines": 2, "verified": False,
            }
            if r["all_of"] or r["any_of"]:
                rule["case_sensitive"] = False
            if r["extra"]:
                rule["library_meta"] = r["extra"]
            srules.append(rule)
        steps.append({"id": sid, "title": title,
                      "objective": "候选规则组：仅做证据检索，需人工在真实日志验证",
                      "rules": srules})
    dev = "SDR" if "SDR" in board.upper() else "ITBBU" if "ITRAN" in board.upper() else "BBU/OSS"
    todo = "待补充"
    return {
        "mode": "triage",
        "skill": {"name": slug, "title": f"规则库导入：{board}（{path.name}）", "version": "0.1.0"},
        "fault": {
            "name": f"{board} 规则库候选集（{typ}）", "aliases": [], "alarm_codes": [],
            "domain": {"device_types": [dev], "modes": [], "boards": [board],
                       "version_scope": todo, "collection_sources": ["device-tar"]},
            "triggers": [], "exclusions": [],
        },
        "symptoms": {"observable": [todo + "：由人工根据分组补充可感知现象"],
                     "description_keywords": keywords_from(rules),
                     "impact": todo, "time_pattern": todo, "lookalikes": []},
        "analysis": {"steps": steps},
        "root_causes": [], "cases": [], "temporal": [],
        "components": {"unpack": True, "search": True, "report": True,
                       "alarm_dict": False, "common_rules": True},
    }


def merge_profile(old, new, notes):
    """把新解析的候选并入已有 profile：人工改动优先，库侧只增补/标记，不静默删除。"""
    steps = (old.setdefault("analysis", {})).setdefault("steps", [])
    by_step = {s.get("id"): s for s in steps}
    old_rules = {r.get("id"): r for s in steps for r in (s.get("rules") or [])}
    new_ids = set()
    for nst in new["analysis"]["steps"]:
        for nr in nst["rules"]:
            new_ids.add(nr["id"])
            orule = old_rules.get(nr["id"])
            if orule is not None:
                lm = dict(nr.get("library_meta") or {})
                if (orule.get("library_meta") or {}).get("library_removed"):
                    notes.append(f"{nr['id']} 重新出现在规则库中，已清除 library_removed 标记")
                if lm:
                    orule["library_meta"] = lm
                else:
                    orule.pop("library_meta", None)
                if any(orule.get(k) != nr.get(k) for k in ("all_of", "any_of", "globs")):
                    notes.append(f"{nr['id']}（{nr['description'][:30]}…）规则库内容已变化，本地版本保留未覆盖")
                continue
            tgt = by_step.get(nst["id"])
            if tgt is None:
                tgt = {k: v for k, v in nst.items() if k != "rules"}
                tgt["rules"] = []
                steps.append(tgt)
                by_step[tgt["id"]] = tgt
            tgt.setdefault("rules", []).append(nr)
            notes.append(f"新增规则 {nr['id']}：{nr['description'][:40]}")
    for rid, r in old_rules.items():
        if rid not in new_ids and not (r.get("library_meta") or {}).get("library_removed"):
            r.setdefault("library_meta", {})["library_removed"] = True
            notes.append(f"{rid} 已不在规则库中，已标记 library_removed（未删除）")
    return old


def to_yaml(obj, indent=0):
    """最小 YAML 序列化：标量 JSON 引号包裹，空容器用 []/{}。"""
    pad = "  " * indent
    lines = []

    def scalar(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return json.dumps(str(v), ensure_ascii=False)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(to_yaml(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: " + ("[]" if v == [] else "{}" if v == {} else scalar(v)))
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict) and v:
                sub = to_yaml(v, indent + 1).split("\n")
                lines.append(f"{pad}- " + sub[0].lstrip())
                lines.extend(sub[1:])
            elif isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}-")
                lines.append(to_yaml(v, indent + 1))
            else:
                lines.append(f"{pad}- " + ("[]" if v == [] else "{}" if v == {} else scalar(v)))
    return "\n".join(lines)


def collect_files(src):
    if src.is_file():
        return [src]
    return sorted(p for p in src.iterdir()
                  if p.is_file() and p.suffix.lower() in (".xlsx", ".xlsm", ".xltx", ".csv")
                  and not p.name.startswith("~$"))


def write_profile(dest, prof):
    dest.write_text(to_yaml(prof) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="规则库 -> 候选 fault-profile.yaml（triage）")
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", help="输出 yaml 文件或目录（--list-groups/--merge-into 时可省略）")
    ap.add_argument("--type", default="auto", choices=["auto", "blackbox", "hummingbird"])
    ap.add_argument("--board")
    ap.add_argument("--json", help="写出解析元数据 JSON")
    ap.add_argument("--per-file", action="store_true", help="（已废弃）目录输入默认已按文件拆分，本参数无实际作用")
    ap.add_argument("--baseline", help="旧 profile：相同规则复用旧 E/S 编号，新增规则取 max+1")
    ap.add_argument("--merge-into", metavar="PROFILE",
                    help="并入已有 profile（人工改动优先；新增追加；库中已删除的标 library_removed）")
    ap.add_argument("--no-json-profile", action="store_true",
                    help="（已废弃，无实际作用）")
    ap.add_argument("--list-groups", action="store_true", help="只列出分组（序号/标题/规则数），不写文件")
    ap.add_argument("--only-groups", metavar="REGEX", help="只导出标题匹配该正则的分组")
    ap.add_argument("--step-by", default="group", choices=["group", "sheet"],
                    help="步骤粒度：每个分组一个步骤（默认）或每个 sheet 一个步骤")
    a = ap.parse_args(argv)
    src = Path(a.source)
    if not src.exists():
        sys.exit(f"--source 不存在: {src}")
    files = collect_files(src)
    if not files:
        sys.exit("未找到 xlsx/csv 规则文件")
    if a.merge_into and not src.is_file():
        sys.exit("--merge-into 需要单个规则文件作为 --source")
    if not (a.list_groups or a.merge_into) and not a.out:
        sys.exit("需要 --out（或使用 --list-groups / --merge-into）")
    out = Path(a.out) if a.out else None
    if out and src.is_dir() and out.suffix.lower() in (".yaml", ".yml"):
        sys.exit("--source 为目录时 --out 须为目录（每个规则文件一个候选 profile）")
    single = bool(out) and src.is_file() and out.suffix.lower() in (".yaml", ".yml")
    if out and not single and not a.list_groups and not a.merge_into:
        out.mkdir(parents=True, exist_ok=True)
    only = re.compile(a.only_groups) if a.only_groups else None
    baseline = load_profile_any(a.baseline) if a.baseline else None
    used, warnings, metas, written = set(), [], [], []
    for f in files:
        typ, rules, meta = detect_and_parse(f, a.type, warnings)
        if only:
            rules = [r for r in rules if only.search(r["title"])]
        meta["rules"] = len(rules)
        metas.append(meta)
        if not rules:
            continue
        if a.list_groups:
            gs = {}
            for r in rules:
                gs[r["title"]] = gs.get(r["title"], 0) + 1
            print(f"== {f.name}（{typ}，{len(rules)} 规则 / {len(gs)} 组）")
            for i, (g, c) in enumerate(gs.items(), 1):
                print(f"  {i:>3}. [{c}] {g}")
            continue
        board = a.board or infer_board(f)
        slug = make_slug(f, board, used)
        if a.merge_into:
            dest = Path(a.merge_into)
            old = load_profile_any(dest)
            prof = build_profile(f, typ, rules, board, slug, old, a.step_by)
            notes = []
            merged = merge_profile(old, prof, notes)
            dest.with_name(dest.name + ".bak").write_bytes(dest.read_bytes())
            write_profile(dest, merged)
            meta.update(board=board, merge_notes=notes)
            for n in notes:
                print(f"[~] {n}")
            print(f"[+] 已并入 {dest}（备份 {dest.name}.bak，{len(notes)} 条变更提示）")
        else:
            prof = build_profile(f, typ, rules, board, slug, baseline, a.step_by)
            meta.update(board=board, slug=slug, groups=len(prof["analysis"]["steps"]))
            dest = out if single else out / f"{slug}.yaml"
            write_profile(dest, prof)
            print(f"[+] {f.name} -> {dest}（{typ}，{len(rules)} 规则 / {meta['groups']} 组）")
        written.append(dest)
    for w in warnings:
        print(f"[!] {w}", file=sys.stderr)
    if a.json:
        mp = Path(a.json)
        mp.write_text(json.dumps({"files": metas, "warnings": warnings,
                                            "outputs": [str(p) for p in written]},
                                           ensure_ascii=False, indent=2), encoding="utf-8")
    if written:
        print("\n待人工确认：\n  1. 按故障/分组确认拆分（一个故障一个子 Skill；可用 --list-groups/--only-groups 辅助）\n"
              "  2. 每条 all_of/any_of 关键字需在真实日志验证\n"
              "  3. 补 symptoms/actions/案例后去掉 mode: triage，走 Phase 2/3")
    return 0 if (written or a.list_groups) else 1


if __name__ == "__main__":
    sys.exit(main())
