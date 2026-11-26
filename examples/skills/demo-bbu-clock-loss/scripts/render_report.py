#!/usr/bin/env python3
"""
render_report.py — 从 evidence.json（含 verdict）+ fault-profile 预填故障分析报告草稿

报告面向**非专业用户**，按 assets/report_template.md 固定为三大块（标题带序号，网元只显示名称、不含路径）：
  ## 1 结果统计 —— 总网元/正常/故障计数 + 各站点一句话汇总表（网元名称可点击跳转到详情）；
  ## 2 网元详情 —— 每个网元一节（### 2.N 短名）：故障根因（详述）、处理建议（详细步骤）、故障证据
                  （按文件分组的「规则/行号/时间戳/原文」表，短时间内的同类重复行自动省略）；
  ## 3 故障现象与甄别。
不保留「附：执行与数据完整性 / 局限与未验证项」等专业附录，避免干扰读者。
机器能确定的内容（计数、根因/处理动作、证据表）全部由脚本填好，需要人工/LLM
补写的位置用 <!-- TODO: ... --> 标记。MD 为事实源，HTML 由同一内容渲染（单文件、内联 CSS）。

用法：
    python3 render_report.py --evidence evidence.json --md report.md --html report.html \
        [--profile assets/fault-profile.yaml] [--manifest <workdir>/manifest.json] \
        [--cloudmat cloudmat.json] [--clock clock.json] \
        [--description "用户故障描述"] [--source "xxx.zip"] [--site <站点键>]
    python3 render_report.py --from-md report.md --html report.html --evidence evidence.json   # 补完 TODO 后重渲 HTML
    python3 render_report.py --verify report.md --evidence evidence.json [--html report.html]   # 交付门禁（0 通过 / 2 失败）
（Windows 下 python3 替换为 python；零三方依赖，profile 为 .yaml 时需 PyYAML）

不可变域：脚本填入的证据表行尾带 <!-- ev:EV4:HASH --> 指纹注释（HTML 渲染时剥除）；
--verify 重算指纹并与 evidence.json 重建的行集合比对，任何手改路径/行号/原文/时间戳都会被检出。
"""
import argparse
import hashlib
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
def TODO(hint):
    return f"<!-- TODO: {hint} -->"


def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


def load_any(path):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        if not _HAS_YAML:
            sys.exit(f"{p} 为 YAML 但当前环境缺少 PyYAML：pip install pyyaml（v2.9.6 起规则/配置统一 .yaml）")
        return yaml.safe_load(text)
    return json.loads(text)


def _load_clock(path):
    """加载 --clock 输入：文件直接 load；目录则合并目录内全部时钟 JSON
    （多站点 clock JSON：files[] 拼接、gnss.spoofing_suspicions 列表拼接、gnss_records 求和，
    等价 parse_clock.py --merge-sites）。返回聚合形态（顶层 alarms/ref_switches/…）。"""
    if not path:
        return None
    p = Path(path)
    if p.is_dir():
        # 目录模式：只合并多站点时钟 JSON（文件名含 clock/brief，排除已合并产物与其他 JSON）
        jsons = sorted(x for x in p.iterdir()
                       if x.suffix.lower() == ".json"
                       and ("clock" in x.name.lower() or "brief" in x.name.lower()))
        if not jsons:
            print(f"[!] --clock 目录无时钟 JSON（{p}，需文件名含 clock/brief）", file=sys.stderr)
            return None
        files, gnss_records, gnss_sus, gnss_last = [], 0, [], None
        for j in jsons:
            try:
                d = json.loads(j.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(f"[!] --clock 跳过 {j.name}: {e}", file=sys.stderr)
                continue
            files.extend(d.get("files", []) or [])
            g = d.get("gnss") or {}
            gnss_records += g.get("gnss_records", 0) or 0
            sus = g.get("spoofing_suspicions") or []
            if not isinstance(sus, list):  # 类型兜底：误写成 int 时归一为列表
                sus = [sus] if sus else []
            gnss_sus.extend(sus)
            if g.get("last_status"):
                gnss_last = g["last_status"]
        merged = {"files": files}
        if gnss_records or gnss_sus:
            merged["gnss"] = {"gnss_records": gnss_records,
                              "spoofing_suspicions": gnss_sus,
                              "last_status": gnss_last}
        return _aggregate_clock(merged)
    d = load_any(path)
    return _aggregate_clock(d) if isinstance(d, dict) else None


def _aggregate_clock(clock):
    """把 parse_clock 输出（files[] 或顶层已聚合）归一为 render_report 消费的
    聚合形态：顶层 alarms/ref_switches/reset_reasons/clk99_health_events 拼接、
    timestamp_fixed 求和、gnss 透传。已是聚合形态则原样返回。"""
    if not isinstance(clock, dict):
        return None
    files = clock.get("files") or []
    if not files:
        return clock
    agg = {"alarms": [], "ref_switches": [], "reset_reasons": [],
           "clk99_health_events": [], "timestamp_fixed": 0}
    for f in files:
        if not isinstance(f, dict):
            continue
        for k in ("alarms", "ref_switches", "reset_reasons", "clk99_health_events"):
            agg[k].extend(f.get(k, []) or [])
        agg["timestamp_fixed"] += f.get("timestamp_fixed", 0) or 0
    if clock.get("gnss"):
        agg["gnss"] = clock["gnss"]
    return agg


def md_cell(s, limit=160):
    s = "" if s is None else str(s)
    s = s.replace("\n", " ").replace("|", "\\|")
    return s if len(s) <= limit else s[:limit] + "…"


def _split_md_row(line):
    """把 MD 表格行拆成单元格，尊重反斜杠转义的 \\|。
    先按未转义的 | 拆分，再把每个单元格内的 \\| 还原为 |。
    （旧实现先 split 再 replace 会把 \\| 误当分隔符拆开，导致含 | 的证据单元格对不上。）"""
    body = line.strip().strip("|")
    parts = re.split(r"(?<!\\)\|", body)
    return [p.strip().replace("\\|", "|") for p in parts]


def fpath(e):
    return str(e.get("file") or "").replace("\\", "/")


def code(s):
    return f"`{md_cell(s)}`" if s not in (None, "") else ""


def _site_names(manifest):
    """manifest.sites 为 [{'root':..., 'features':..., 'files':n}] 字典列表；
    归一化为站点名字符串列表（旧版若直接存字符串也兼容）。"""
    out = []
    for s in (manifest or {}).get("sites", []) or []:
        out.append(s.get("root") if isinstance(s, dict) else str(s))
    return out


def _version_file_note(manifest):
    """M6: manifest 缺版本类文件（summary.json / *running_version* / *software_package_version*）
    时返回占位提示（不阻断交付）；有则返回 None。"""
    if not manifest:
        return None
    files = [str(f) for f in (manifest.get("extracted_files") or [])]
    if not files:
        return None
    has_ver = any(
        f.endswith("summary.json") or "running_version" in f
        or "software_package_version" in f for f in files)
    if has_ver:
        return None
    missing = [k for k in ("summary.json", "running_version.csv")
               if not any(f.endswith(k) for f in files)]
    return (f"⚠ 版本文件缺失（采集包缺 {' / '.join(missing)}），"
            f"版本以目录名推断（未确认）")


def _ne_short(name):
    """网元显示名：只取名称，去掉目录/路径前缀（Windows/Unix 分隔符都处理）。"""
    s = str(name or "").replace("\\", "/").rstrip("/")
    return s.split("/")[-1] if "/" in s else s


def table(headers, rows, empty="（无）", fp_tag=None):
    """渲染 MD 表；fp_tag 非空时每行尾部追加指纹注释 <!-- ev:TAG:HASH -->（不可变域，--verify 校验）。"""
    if not rows:
        return [empty, ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        line = "| " + " | ".join(md_cell(c) for c in row) + " |"
        out.append(line + fp_comment(fp_tag, line) if fp_tag else line)
    return out + [""]


# ---------------------------------------------------------------------------
# 不可变域指纹：脚本填入的证据/时间轴/时序行带 <!-- ev:TAG:HASH -->，HASH=行文本 sha256[:12]
# --verify 重算比对；HTML 渲染前剥除
# ---------------------------------------------------------------------------
FP_RE = re.compile(r"\s*<!-- ev:([A-Za-z0-9_]+):([0-9a-f]{12}) -->\s*$")


def fp_hash(line_text):
    return hashlib.sha256(line_text.encode("utf-8")).hexdigest()[:12]


def fp_comment(tag, line_text):
    return f" <!-- ev:{tag}:{fp_hash(line_text)} -->"


def strip_fp(md):
    return "\n".join(FP_RE.sub("", ln) for ln in md.split("\n"))


def _fp_diff_summary(lineno, body, exp_text, act_hash):
    """指纹失配的行级诊断：diff body 与权威行文本（evidence.json 反查）。

    返回多行可打印字符串：差异集中在反斜杠层数 → 明确提示层数；否则输出截断 diff。"""
    import difflib
    out = [f"  · L{lineno}: 指纹失配（期望 hash {fp_hash(exp_text)}，实际 {act_hash}）"]
    nb = body.count("\\")
    ne = exp_text.count("\\")
    if nb != ne and abs(nb - ne) <= 4:
        out.append(f"    疑似反斜杠转义层数不一致（期望 {ne} 个 \\，实际 {nb} 个 \\）")
    # 字符级 diff（并排，截断到 200 字符避免刷屏）
    sm = difflib.SequenceMatcher(None, exp_text, body)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    if ops:
        parts = []
        for tag, i1, i2, j1, j2 in ops[:4]:
            e = exp_text[i1:i2]
            a = body[j1:j2]
            if tag in ("replace", "delete"):
                parts.append(f"-期望 {e[:60]!r}" if e else "")
            if tag in ("replace", "insert"):
                parts.append(f"+实际 {a[:60]!r}" if a else "")
        out.append("    diff: " + " | ".join(p for p in parts if p)[:240])
    out.append(f"    期望: `{exp_text[:160]}{'…' if len(exp_text) > 160 else ''}`")
    out.append(f"    实际: `{body[:160]}{'…' if len(body) > 160 else ''}`")
    return "\n".join(out)


def evidence_rows(ev):
    """脚本填入的全部不可变行（与 build_report 同源）：网元详情「故障证据」按文件分组的
    证据表（4 列：规则/行号/时间戳/原文）。返回 {hash: tag}。仅依赖 evidence.json（命中规则），
    不依赖专项复核结论，故 --verify 对单站点裁剪也安全（期望集合为报告行的超集）。"""
    groups = (ev.get("verdict") or {}).get("groups", {})
    keys = [k for k in groups if k != "ALL"] or ["ALL"]
    rows = {}
    for key in keys:
        for _f, rws, _dropped in ne_evidence(ev, key):
            for r in rws:
                line = "| " + " | ".join(md_cell(c) for c in r) + " |"
                rows[fp_hash(line)] = "EV4"
    return rows


def evidence_texts(ev):
    """证据行原文反查表：{hash: 权威行文本}。供 --verify 失配时输出行级 diff（M1 诊断）。"""
    groups = (ev.get("verdict") or {}).get("groups", {})
    keys = [k for k in groups if k != "ALL"] or ["ALL"]
    texts = {}
    for key in keys:
        for _f, rws, _dropped in ne_evidence(ev, key):
            for r in rws:
                line = "| " + " | ".join(md_cell(c) for c in r) + " |"
                texts.setdefault(fp_hash(line), line)
    return texts


# 时间戳归一（用于"短时间内同类重复行"去重）：Clk 行逗号时间 + 普通空格时间
_TS_NORM = re.compile(r"(?:Clk\[\d+\]\s*)?\d{4}-\d{2}-\d{2}[ ,T]\d{2}:\d{2}:\d{2}(?:,\d+)?")
# 每条规则在每个网元最多保留的证据行数（防止刷屏；真实命中数仍以 match_count 为准）
PER_RULE_CAP = 8


def _norm_line(s):
    return _TS_NORM.sub("T", str(s or ""))


def ne_evidence(ev, key):
    """某网元（key="ALL" 或某站点根）命中规则的证据，按文件分组、同类去重。

    返回 [(file, rows, dropped)]：
      rows 为 4 列单元格 [规则, 行号, 时间戳, 原文(code)]，行序与证据表一致（指纹同源）；
      dropped 为该文件因「短时间内同类重复」或超出每规则上限而省略的行数。
    """
    buckets = {}
    order = []
    dropped_by_file = {}
    for r in ev.get("results", []):
        if not r.get("hit"):
            continue
        rid = r["rule_id"]
        seen = set()
        kept = 0
        for e in r.get("evidence", []):
            site = e.get("site")
            if key != "ALL" and site and site != key:
                continue
            f = fpath(e)
            if not f:
                continue
            line = e.get("line") or ""
            nk = (rid, _norm_line(line))
            if nk in seen or kept >= PER_RULE_CAP:
                # 同类重复/超上限：按文件计省略数（即使该文件尚无保留行，也计入，
                # 否则跨文件重复会静默丢失导致"另有 N 条省略"偏低）
                dropped_by_file[f] = dropped_by_file.get(f, 0) + 1
                continue
            seen.add(nk)
            kept += 1
            if f not in buckets:
                buckets[f] = {"rows": []}
                order.append(f)
            buckets[f]["rows"].append(
                [rid, e.get("line_no"), e.get("timestamp") or "时间未知", code(line)])
    return [(f, buckets[f]["rows"], dropped_by_file.get(f, 0)) for f in order]


def spec_map(spec):
    """--specialized JSON → {组键: {primary,status,note,source}}。

    接受 {"primary","status","note","source","site"} 单对象，或
    {"sites": {"<组键>": {...}}} 多站点形式；"normal" 归一为 unmatched。"""
    if not isinstance(spec, dict):
        return {}
    sites = spec.get("sites")
    items = sites.items() if isinstance(sites, dict) else (
        [(spec.get("site") or "ALL", spec)])
    out = {}
    for k, s in items:
        if not isinstance(s, dict):
            continue
        status = s.get("status")
        if status == "normal":
            status = "unmatched"
        if not status:
            status = "matched" if s.get("primary") else "unmatched"
        out[k] = {"primary": s.get("primary"), "status": status,
                  "note": s.get("note", ""), "source": s.get("source", ""),
                  "events": s.get("events", []) or []}
    return out


def _spec_for(specmap, group_key):
    return specmap.get(group_key) or specmap.get("ALL")


def verify_report(md_path, ev, html_path=None, site_key=None, specialized=None):
    """交付门禁：TODO 清零 / 指纹行未改且来源于 evidence.json / 结论与 verdict（或
    --specialized 专项复核结论）一致 / HTML 与 MD 一致。返回 (ok, 逐项结果列表)。"""
    md = Path(md_path).read_text(encoding="utf-8")
    lines = md.split("\n")
    checks = []

    n_todo = md.count("<!-- TODO")
    checks.append(("TODO 已清零", n_todo == 0, f"残留 {n_todo} 处" if n_todo else "无残留"))

    expected = evidence_rows(ev)
    exp_texts = evidence_texts(ev)
    tampered, unknown, n_fp = [], [], 0
    for i, ln in enumerate(lines, 1):
        m = FP_RE.search(ln)
        if not m:
            continue
        n_fp += 1
        body = ln[:m.start()]
        if fp_hash(body) != m.group(2):
            tampered.append((i, body, m.group(2), exp_texts.get(m.group(2), "")))
        elif m.group(2) not in expected:
            unknown.append(f"L{i}")
    if tampered:
        t_detail = [f"L{i}" for i, *_ in tampered]
        checks.append(("证据/时间轴行未被篡改", False,
                       f"被改动行: {', '.join(t_detail[:10])}" +
                       (f"（共 {len(t_detail)} 行）" if len(t_detail) > 10 else "")))
        # 诊断摘要：失配时默认输出前 3 行；--verify-detail 开启时全量输出
        show_all = verify_detail if "verify_detail" in globals() else False
        summary = []
        for item in (tampered[:3] if not show_all else tampered):
            lineno, body, act_hash, exp_text = item
            summary.append(_fp_diff_summary(lineno, body, exp_text or body, act_hash))
        checks.append(("指纹失配诊断", False, "\n".join(summary)))
    else:
        checks.append(("证据/时间轴行未被篡改", True, f"校验 {n_fp} 行"))
    checks.append(("证据行均来源于 evidence.json", not unknown, f"非本 evidence 产生的行: {', '.join(unknown[:10])}" if unknown else "一致"))
    checks.append(("存在指纹证据行", n_fp > 0 or not expected, "报告未含脚本填入的证据行（请用 render_report.py 生成）" if n_fp == 0 and expected else "有"))

    if ev.get("mode") == "triage":
        bad = re.search(r"- \*\*根因\*\*：", md) is not None
        checks.append(("分诊线索报告不含确定性根因", not bad,
                       "triage 报告不得给出「根因」条目" if bad else "一致"))
        checks.append(("分诊横幅存在", "分诊线索报告" in md,
                       "报告缺少分诊声明" if "分诊线索报告" not in md else "有"))
    verdict = ev.get("verdict") or {}
    groups = verdict.get("groups", {})
    gk = site_key if site_key in groups else "ALL"
    g = groups.get(gk, {})
    v_status = g.get("status", verdict.get("status"))
    v_primary = g.get("primary", verdict.get("primary"))
    smap = spec_map(specialized)
    sp = _spec_for(smap, gk)
    basis = "verdict"
    if sp:
        # 专项脚本复核优先：期望结论取自 specialized（verdict 宽松命中但专项精确判定不成立时豁免）
        basis = "specialized"
        v_status, v_primary = sp["status"], sp["primary"]
    m_status = re.search(r"\*\*判定状态\*\*：`([a-z_]+)`", md)
    r_status = m_status.group(1) if m_status else None
    checks.append((f"判定状态与 {basis} 一致", v_status is None or r_status == v_status,
                   f"报告 {r_status} vs {basis} {v_status}"))
    if sp and "专项脚本复核" not in md:
        checks.append(("专项脚本复核说明", False, "使用 --specialized 时报告须含「专项脚本复核」块"))
    if v_status == "matched":
        m_rc = re.search(r"- \*\*根因\*\*：([A-Z]\d+)\b", md)
        r_primary = m_rc.group(1) if m_rc else None
        checks.append((f"首选根因与 {basis} 一致", r_primary == v_primary,
                       f"报告 {r_primary} vs {basis} {v_primary}"))
    elif v_status in ("unmatched", "temporal_conflict"):
        bad = re.search(r"- \*\*根因\*\*：[A-Z]\d+\b", md) is not None
        checks.append(("未归因（无确定性根因）", not bad,
                       f"{basis} 未成立但报告给出了确定性根因" if bad else "一致"))

    if html_path:
        hp = Path(html_path)
        if not hp.exists():
            checks.append(("HTML 存在", False, f"{hp} 不存在"))
        else:
            h = hp.read_text(encoding="utf-8")
            missing = []
            for ln in lines:
                m = FP_RE.search(ln)
                if not m or m.group(1) == "TL":  # 时间轴在 HTML 中以 .timeline 组件呈现，不逐格比对
                    continue
                for c in _split_md_row(ln[:m.start()].strip()):
                    c = c.strip("`")
                    # M2：与 _inline 同源还原反斜杠实体，再 escape 与 HTML 内容比对
                    if c and html.escape(c.replace("&#92;", "\\"), quote=False) not in h:
                        missing.append(c[:40])
                        break
            checks.append(("HTML 含全部 MD 证据单元格", not missing, f"缺失 {len(missing)} 处，如 {missing[:3]}" if missing else "一致"))
            checks.append(("HTML 无 TODO 高亮", 'class="todo"' not in h, "HTML 仍含 TODO（需重新渲染）" if 'class="todo"' in h else "无"))
            if r_status:
                same = f"<code>{r_status}</code>" in h
                checks.append(("HTML 判定状态与 MD 一致", same, "一致" if same else "HTML 判定状态缺失/不一致（需 --from-md 重新渲染）"))
    return all(ok for _, ok, _ in checks), checks


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------
def gh_anchor(heading):
    """GitHub 风格标题锚点：小写、去标点（保留下划线/连字符/中文字符）、空格转 -。
    结果统计表的 [网元](#锚点) 在原始 MD（GitHub/编辑器预览）与生成的 HTML 中都有效。"""
    s = re.sub(r"[^\w\s-]", "", str(heading).strip().lower())
    return re.sub(r"\s+", "-", s)


def build_ne_items(groups, smap, detected_sites, restrict):
    """汇总需要出详情的网元清单。多站点时逐站点；单站点（仅 ALL 或无分组）时出一个。
    返回 [{name, key, g, sp, st, pr}]，st/pr 已应用专项复核（sp）覆盖。"""
    keys = [k for k in groups if k != "ALL"]
    if restrict:
        if restrict in groups:
            keys = [restrict]
        else:
            # 显式指定的站点不在分组中：仍单列该站点（未识别/未命中），便于核对；
            # specialized 中可能仍有该站点结论，须照常应用
            g = {"status": "unmatched", "primary": "NONE", "root_causes": []}
            sp = _spec_for(smap, restrict)
            st = sp["status"] if sp else "unmatched"
            pr = sp["primary"] if sp else "NONE"
            return [{"name": restrict, "key": restrict, "g": g, "sp": sp,
                     "st": st, "pr": pr}]
    if keys:
        items = []
        for k in keys:
            g = groups.get(k, {})
            sp = _spec_for(smap, k)
            st = sp["status"] if sp else g.get("status", "unmatched")
            pr = sp["primary"] if sp else g.get("primary", "NONE")
            items.append({"name": k, "key": k, "g": g, "sp": sp, "st": st, "pr": pr})
        # N3：多站点时，把采集包中已被识别但 verdict 未命中规则的站点也纳入统计，
        # 标为「规则未命中（待人工确认）」，避免"规则未命中"被误读为"站点正常"。
        # 组键与 manifest 根可能形态不同（短名 vs 完整相对路径），按短名归一再比对。
        known = set(keys) | {_ne_short(k) for k in keys}
        for ds in detected_sites:
            nm = _ne_short(ds)
            if nm in known or ds in known:
                continue
            items.append({"name": nm, "key": nm, "g": {"status": "unmatched",
                          "primary": "NONE", "root_causes": []}, "sp": None,
                          "st": "unmatched", "pr": "NONE", "miss_by_rule": True})
        if items:
            return items
    g = groups.get("ALL", {})
    sp = _spec_for(smap, "ALL")
    st = sp["status"] if sp else g.get("status", "unmatched")
    pr = sp["primary"] if sp else g.get("primary", "NONE")
    name = detected_sites[0] if len(detected_sites) == 1 else "本站点"
    return [{"name": name, "key": "ALL", "g": g, "sp": sp, "st": st, "pr": pr}]


def _short_rc(st, pr, rc_defs):
    if st == "matched":
        return md_cell(rc_defs.get(pr, {}).get("name", pr), 24)
    if st == "temporal_conflict":
        return "疑似（时序矛盾）"
    return "未发现本故障"


def _short_action(st, pr, rc_defs):
    if st == "matched":
        return md_cell((rc_defs.get(pr, {}).get("actions") or ["-"])[0], 30)
    return "人工排查（见详情）"


def _short_evidence(ev, key):
    """汇总表用一句话证据：命中规则含义 + 首条原文片段（不含文件路径），最多 2 条。"""
    parts = []
    for r in ev.get("results", []):
        if not r.get("hit"):
            continue
        if key != "ALL" and not any((e.get("site") or key) == key for e in r.get("evidence", [])):
            continue
        d = r.get("description") or r["rule_id"]
        snip = ""
        for e in r.get("evidence", []):
            if key != "ALL" and e.get("site") and e.get("site") != key:
                continue
            snip = md_cell(e.get("line", ""), 48)
            break
        parts.append(f"{d}（如 {snip}）" if snip else str(d))
        if len(parts) >= 2:
            break
    return "；".join(parts)


def _site_rule_stats(ev, key):
    """某网元逐条规则的命中统计行（非指纹）。多站点时用 hits_by_site 给该站点计数
    （未关联站点的命中与 verdict 分组语义一致，计入每个站点），避免误显示全局总数。"""
    out = []
    for r in ev.get("results", []):
        if key != "ALL" and not any((e.get("site") or key) == key for e in r.get("evidence", [])):
            continue
        if key == "ALL":
            cnt = r.get("match_count", 0)
        else:
            hbs = r.get("hits_by_site") or {}
            cnt = hbs.get(key, 0) + hbs.get("ALL", 0)
        out.append(f"{r['rule_id']} {'命中' if r.get('hit') else '未命中'} "
                   f"{cnt} 行")
    return "；".join(out)


def build_triage_report(ev, profile, manifest, description, source, draft=True):
    """分诊候选集（mode: triage）的线索报告：只列命中线索与证据，不给根因/判定。"""
    profile = profile or {}
    skill, fault = profile.get("skill", {}), profile.get("fault", {})
    title = skill.get("title") or fault.get("name") or "BBU 故障分析"
    if not str(title).endswith("报告"):
        title = f"{title}报告"
    results = ev.get("results", [])
    hits = [r for r in results if r.get("hit")]
    L = [f"# {title}", ""]
    if draft:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        L += [f"> 本报告由 render_report.py 依据 evidence.json 自动预填（{now}）；带 TODO 的位置需分析人员补充；"
              "证据的文件/行号/原文由脚本生成，请勿手改。", ""]
    L += ["> **分诊线索报告（候选规则集，规则均未经验证）**：以下命中仅为线索，**不构成根因结论**；"
          "须人工在原始日志中逐条核实，核实前不得归因。", "",
          f"> 分析对象：{fault.get('name', '') or '-'}；"
          f"数据来源：{(source or ev.get('root', '')) or '-'}"
          + (f"（提取模式：{manifest.get('mode')}）" if manifest and manifest.get("mode") else ""), "",
          "## 1 命中线索统计", "",
          f"**规则总数**：{len(results)}　　**命中线索**：{len(hits)}　　"
          f"**未验证命中**：{sum(1 for r in hits if r.get('verified') is False)}", ""]
    rows = [[r.get("step", ""), r["rule_id"], md_cell(r.get("description", ""), 60),
             f"{r.get('match_count', 0)}/≥{r.get('min_matches', 1)}",
             "未验证" if r.get("verified") is False else "已验证"] for r in hits]
    L += table(["步骤", "规则", "线索描述", "匹配数", "验证状态"], rows, "（未命中任何线索）")
    L += ["## 2 线索证据", ""]
    blocks = ne_evidence(ev, "ALL")
    if blocks:
        fi = 1
        for f, rws, dropped in blocks:
            L += [f"**文件 {fi}**：`{f}`", ""]
            L += table(["规则", "行号", "时间戳（UTC）", "原文（关键片段）"], rws, fp_tag="EV4")
            if dropped:
                L.append(f"（该文件另有 {dropped} 条同类记录已省略；真实命中数以规则统计为准。）")
            fi += 1
        L.append("")
    else:
        L += ["> 未发现命中线索。", ""]
    desc_txt = description or (TODO("用户描述") if draft else "（未提供）")
    L += ["## 3 人工核实结论", "",
          f"1. 用户描述的现象：{desc_txt}",
          "2. 逐条线索核实结果：" + (TODO("对每条命中线索说明已在日志中核实/排除及依据")
                                    if draft else "待人工在原始日志中核实；脚本不做判定。"),
          "3. 是否升级为已验证规则/新建根因：" + (TODO("未核实不得写根因；可建议回到 builder 补案例验证")
                                                 if draft else "未经案例验证前不得升级，请回到 builder 补案例。"), ""]
    return "\n".join(L), []


def build_report(ev, profile, manifest, cloudmat, clock, description, source,
                 site_key, precheck=None, specialized=None, fm_align_md=None, draft=True):
    if ev.get("mode") == "triage":
        return build_triage_report(ev, profile, manifest, description, source, draft)

    def todo(hint, final_text):
        return TODO(hint) if draft else final_text

    profile = profile or {}
    skill = profile.get("skill", {})
    fault = profile.get("fault", {})
    title = skill.get("title") or fault.get("name") or "BBU 故障分析"
    if not str(title).endswith("报告"):
        title = f"{title}报告"
    results = ev.get("results", [])
    by_rule = {r["rule_id"]: r for r in results}
    rc_defs = {rc.get("id"): rc for rc in profile.get("root_causes", []) or []}
    verdict = ev.get("verdict") or {}
    groups = verdict.get("groups", {})
    if site_key and site_key not in groups:
        print(f"[!] --site '{site_key}' 不在 verdict.groups（{list(groups)}），"
              f"回退 ALL 汇总组", file=sys.stderr)
    group_key = site_key if site_key in groups else "ALL"
    group = groups.get(group_key, {})
    smap = spec_map(specialized)
    sp = _spec_for(smap, group_key)
    v_status = group.get("status", verdict.get("status", "unmatched"))
    v_primary = group.get("primary", verdict.get("primary", "NONE"))
    # 专项脚本复核优先于 verdict（宽松命中但专项精确判定不成立的场景）
    status = sp["status"] if sp else v_status
    primary = sp["primary"] if sp else v_primary
    # 站点全集 = 证据检出站点 ∪ manifest 识别站点（ev.detected_sites 只含有命中的站点，
    # 仅靠它会把"已识别但规则未命中"的站点漏掉）；按短名去重，避免长短路径形态并存。
    detected_sites, _seen_ne = [], set()
    for d in list(ev.get("detected_sites") or []) + _site_names(manifest):
        if not d:
            continue
        s = _ne_short(d)
        if s in _seen_ne:
            continue
        _seen_ne.add(s)
        detected_sites.append(d)
    detected_sites.sort()
    items = build_ne_items(groups, smap, detected_sites, site_key)
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    badge = {"matched": "✅ 存在故障", "temporal_conflict": "⚠️ 疑似（时序矛盾）",
             "unmatched": "❓ 未发现本故障"}.get(status or "", status or "unknown")
    L = [f"# {title}", ""]
    if draft:
        L += [f"> 本报告由 render_report.py 依据 evidence.json 自动预填（{now}）；带 TODO 的位置需分析人员补充；"
              "证据的文件/行号/原文由脚本生成，请勿手改。", ""]
    version_scope = (fault.get("domain") or {}).get("version_scope") or "未确认"
    L += [f"> 分析对象：{fault.get('name', '') or '-'}；"
          f"站点/网元：{'、'.join(detected_sites) if detected_sites else '单站点/未识别'}；"
          f"数据来源：{(source or ev.get('root', '')) or '-'}；"
          f"适用版本：{version_scope}", ""]

    # M6: 版本文件缺失自动占位提示（summary.json / *running_version* / *software_package_version*）
    ver_note = _version_file_note(manifest)
    if ver_note:
        L.append(f"> 制式/版本：{ver_note}")
        L.append("")

    # 总判定状态（聚合；多站点时为 ALL 汇总结论，逐网元结论见详情）
    L += [f"> **判定状态**：`{status or 'unknown'}` — {badge}"]
    if sp:
        src_note = f"（来源：{sp['source']}）" if sp.get("source") else ""
        L.append(f"> **专项脚本复核**：{sp['note'] or '以专项脚本结论为准'}{src_note}"
                 f"；verdict 判定 {v_status}/{v_primary}，本报告以专项脚本复核为准。")
    L.append("")

    # ================= 1 结果统计 =================
    n_total = len(items)
    n_fault = sum(1 for it in items if it["st"] == "matched")
    # 正常站点分两类：判定为正常 vs 规则未命中（待人工确认，可能是格式不覆盖/需人工复核）
    n_unmatched = [it for it in items if it["st"] == "unmatched"]
    n_normal = sum(1 for it in n_unmatched if not it.get("miss_by_rule"))
    n_missrule = sum(1 for it in n_unmatched if it.get("miss_by_rule"))
    n_suspect = sum(1 for it in items if it["st"] == "temporal_conflict")
    L += ["## 1 结果统计", "",
          f"**总网元数量**：{n_total}　　**正常站点数量**：{n_normal}"
          + (f"　　**规则未命中（待人工确认）**：{n_missrule}" if n_missrule else "")
          + f"　　**故障站点数量**：{n_fault}"
          + (f"　　**疑似（时序矛盾）**：{n_suspect}" if n_suspect else ""), ""]
    rows = []
    for j, it in enumerate(items, 1):
        sh = _ne_short(it["name"])
        se = _short_evidence(ev, it["key"])
        if it.get("miss_by_rule"):
            rc_cell = "规则未命中（待人工确认）"
            act_cell = "人工复核（未命中规则不代表正常）"
            ev_cell = "未发现命中证据（可能因格式未被规则覆盖，需人工确认）"
        else:
            rc_cell = _short_rc(it["st"], it["pr"], rc_defs)
            act_cell = _short_action(it["st"], it["pr"], rc_defs)
            ev_cell = (md_cell(se, 90) if not draft else md_cell(se, 90) + " "
                       + TODO("压缩为一句话（示例：sts=9 且 abs(cFCV)>1000），不含文件路径")) if se else "未发现命中证据"
        # 锚点与「### 2.j <sh>」标题的 GitHub 自动锚点一致，MD/HTML 中都可跳转
        rows.append([f"[{sh}](#{gh_anchor(f'2.{j} {sh}')})", rc_cell, act_cell, ev_cell])
    L += ["各站点情况（点击网元名称可跳转至对应详情）：", ""]
    L += table(["网元名称", "故障根因", "处理建议", "证据"], rows)

    # ================= 2 网元详情 =================
    L += ["## 2 网元详情", ""]
    for j, it in enumerate(items, 1):
        name, key, g, sp_i = it["name"], it["key"], it["g"], it["sp"]
        sh = _ne_short(name)
        st, pr = it["st"], it["pr"]
        pdef = rc_defs.get(pr, {})
        pitem = next((r for r in g.get("root_causes", []) if r.get("id") == pr), {})
        conf_txt = {"high": "高", "medium": "中", "low": "低"}.get(
            pdef.get("confidence", pitem.get("confidence", "medium")), "中")
        site_v_pr = g.get("primary", "NONE")
        L += [f"### 2.{j} {sh}", ""]

        # ---- 故障根因 ----
        L += [f"#### 2.{j}.1 故障根因", ""]
        if st == "matched":
            mechanism = (pdef.get("mechanism") or pdef.get("explanation")
                         or "已满足下列判定条件；更具体的物理机理未在规则库中登记，不作推测。")
            L += [f"- **根因**：{pr} {pdef.get('name', pitem.get('name', ''))}"
                  f"（置信度：{conf_txt}" + ("；**低置信度，必须人工复核**" if conf_txt == "低" else "") + "）",
                  f"- **根因详述**：{todo('用大白话说明故障现象、发生原因、为什么会导致该故障', mechanism)}",
                  f"- **判定依据**：条件 `{pdef.get('condition') or pitem.get('condition', '')}` 成立"
                  + (f"，时序约束 {'、'.join(pitem.get('temporal_required', []))} 全部满足"
                     if pitem.get("temporal_required") else "")
                  + f"；命中规则：{'、'.join(g.get('hit_rules') or []) or '见下方故障证据'}。"]
            if sp_i:
                L.append(f"- **专项脚本复核**：{sp_i['note'] or '以专项脚本结论为准'}"
                         + (f"（来源：{sp_i['source']}）" if sp_i.get("source") else ""))
            if g.get("others"):
                L.append(f"- 其他同时满足的根因：{'、'.join(g['others'])}（按 profile 取首选，详见处理建议）")
        elif st == "temporal_conflict":
            L += [f"- **根因**：未判定（布尔条件成立但时序约束不满足：{'、'.join(g.get('temporal_conflict', []))}）",
                  "- 在时序矛盾解释清楚前**不得直接归因**。",
                  f"- 时序矛盾说明：{todo('说明可能原因（时区/日志截断/非同一事件）', '需核对时区、日志完整性及事件是否属于同一网元；矛盾消除前不能确定根因。')}"]
            if sp_i:
                L.append(f"- **专项脚本复核**：{sp_i['note'] or '以专项脚本结论为准'}")
        else:
            if it.get("miss_by_rule"):
                L += ["- **根因**：规则未命中（该站点在采集包中被识别，但未命中任何本故障规则证据）",
                      "- **提示**：规则未命中不代表站点正常——可能因日志格式未被规则覆盖（如 SDR cDa 格式）、"
                      "证据类型不符或确属正常，需结合原始日志人工复核后再下结论。"]
            else:
                L += ["- **根因**：未发现本故障的已知根因。"]
            if site_v_pr not in (None, "NONE") and site_v_pr != pr:
                L.append(f"- **候选根因（未归因）**：{site_v_pr} {rc_defs.get(site_v_pr, {}).get('name', '')}"
                         " —— verdict 条件成立，但专项脚本复核未确认。")
            if sp_i:
                L.append(f"- **专项脚本复核**：{sp_i['note'] or '以专项脚本结论为准'}"
                         + (f"（来源：{sp_i['source']}）" if sp_i.get("source") else "")
                         + f"；verdict 判定 {g.get('status')}/{site_v_pr}。")
            L.append("- 已排除的根因与依据：")
            for r in g.get("root_causes", []):
                L.append(f"  - {r.get('id')} {r.get('name', '')}：`{r.get('condition')}`"
                         f" → `{r.get('substituted')}` = {r.get('value')}")
            L.append(f"- 人工排查建议：{todo('给出排查方向，禁止强行归因', '核对未命中规则所需日志是否完整；补齐采集后重跑，仍无命中则转其他故障流程。')}")
        L.append("")

        # ---- 处理建议 ----
        L += [f"#### 2.{j}.2 处理建议", ""]
        if st == "matched":
            acts = pdef.get("actions", []) or []
            if acts:
                L.append("建议按以下步骤处理：")
                for i, a in enumerate(acts, 1):
                    L.append(f"{i}. {a}")
            else:
                L.append(todo("补充有序处理步骤", "规则库未登记处理步骤；请由故障负责人补充，勿自行换件或修改配置。"))
            if pdef.get("prerequisites"):
                L.append("- **前提条件**：" + "；".join(map(str, pdef["prerequisites"])))
            if pdef.get("risks"):
                L.append(f"- **风险与注意事项**：{pdef['risks']}")
            if pdef.get("rollback"):
                L.append(f"- **回退方案**：{pdef['rollback']}")
            ver = "；".join(
                f"在 {'/'.join(v.get('globs', []) or [])} 中{'应出现' if v.get('expect', 'present') != 'absent' else '不应出现'} "
                f"{', '.join(v.get('patterns', []) or [])}"
                + (f"；{v.get('hold_time', '')}" if v.get("hold_time") else "")
                for v in pdef.get("verify", []) or [])
            if ver:
                L.append(f"- **恢复验证**：{ver}")
            if pdef.get("escalation"):
                L.append(f"- **升级路径**：{pdef['escalation']}")
            if draft:
                L.append(f"- 现场补充：{TODO('结合现网确认备件、操作窗口、责任人与预期恢复时间')}")
        else:
            L += [f"- 未定位确定性根因，暂不建议更换备件；人工排查方向：{todo('结合证据给出排查方向', '先补齐日志并核对采集完整性，再按相似故障流程排查。')}",
                  "- 若排查后确认属于其他故障，请按对应故障处理流程执行。"]
        L.append("")

        # ---- 故障证据（按文件分组）----
        L += [f"#### 2.{j}.3 故障证据", ""]
        blocks = ne_evidence(ev, key)
        if blocks:
            if st != "matched":
                L += ["> 以下为命中规则的候选证据；专项脚本复核未确认或时序矛盾时，**不作为归因依据**。", ""]
            fi = 1
            for f, rws, dropped in blocks:
                L += [f"**文件 {fi}**：`{f}`", ""]
                L += table(["规则", "行号", "时间戳（UTC）", "原文（关键片段）"], rws, fp_tag="EV4")
                if dropped:
                    L.append(f"（该文件另有 {dropped} 条短时间内同类记录，已省略；真实命中数以规则统计为准。）")
                fi += 1
            L.append("规则命中统计：" + (_site_rule_stats(ev, key) or "无") + "。")
            trunc = [r["rule_id"] for r in results if r.get("evidence_truncated")
                     and (key == "ALL" or any((e.get("site") or key) == key for e in r.get("evidence", [])))]
            if trunc:
                L.append(f"（规则 {'、'.join(trunc)} 命中行较多，证据表仅展示前若干条代表，"
                         "完整明细见 evidence.json；同类记录可用图表替代表格呈现。）")
            if sp_i and sp_i.get("events"):
                L.append("专项脚本关键时间：")
                for e in sp_i["events"]:
                    if e.get("timestamp"):
                        L.append(f"- {e['timestamp']} {e.get('desc', '专项关键事件')}")
            L.append("")
        else:
            miss = [[r["rule_id"], r.get("files_scanned", 0),
                     f"匹配 {r.get('match_count', 0)} 行（门限 {r.get('min_matches', 1)}）"
                     + (f"；错误：{r.get('error')}" if r.get("error") else "")]
                    for r in results if not r.get("hit")
                    and (key == "ALL" or any((e.get("site") or key) == key for e in r.get("evidence", []))
                         or not any(e.get("site") for rr in results for e in rr.get("evidence", [])))]
            L += ["> 未发现命中本故障的证据。", ""]
            L += table(["规则", "扫描文件数", "未命中说明"], miss, "（无）")
            L.append("")

    # ================= 3 故障现象与甄别（补充） =================
    observed = "；".join(
        f"{r.get('description') or r['rule_id']}（{r.get('match_count', 0)} 条）"
        for r in results if r.get("hit")) or "未命中本故障规则；这不等同于证明设备正常。"
    differentiation = "；".join(
        f"{lk.get('fault')}——{lk.get('differentiator')}"
        for lk in profile.get("symptoms", {}).get("lookalikes", []) or [])
    L += ["## 3 故障现象与甄别", "",
          f"1. 用户描述的现象：{description or todo('用户描述', '未提供')}",
          f"2. 日志/告警中观察到的现象：{todo('依据证据概括告警码/级别/时间/单板状态/测试项结果', observed)}",
          "3. 与相似故障的甄别："
          + (differentiation or todo("甄别依据", "规则库未登记相似故障甄别依据。")), ""]
    if cloudmat:
        bi = cloudmat.get("basic_info", {})
        L += ["CloudMAT 结构化摘要：",
              f"- 结果：{bi.get('result')}；TFP：{bi.get('tfp')}；条码：{bi.get('barcode')}；"
              f"槽位：{bi.get('slot')}；时长：{bi.get('duration_seconds')}s"]
        for it in cloudmat.get("failed_items", [])[:10]:
            L.append(f"- 失败项 {it.get('name')}：失败轮次 {it.get('fail_rounds')}；"
                     f"原因：{md_cell('；'.join(map(str, it.get('reasons', [])[:2])))}")
        L.append("")
    if clock:
        L += ["时钟结构化摘要：",
              f"- 告警 {len(clock.get('alarms', []))} 条；参考源切换 {len(clock.get('ref_switches', []))} 次；"
              f"复位原因 {len(clock.get('reset_reasons', []))} 条；ClkHealth 事件 {len(clock.get('clk99_health_events', []))}；"
              f"欺骗嫌疑 {len((clock.get('gnss') or {}).get('spoofing_suspicions', []))}；"
              f"假时间修复 {clock.get('timestamp_fixed', 0)}", ""]
    if fm_align_md:
        # N4：FM 告警时间窗对齐结果（fm_alarm_align.py --md 输出）注入第 3 节
        L.append(fm_align_md.strip())
        L.append("")

    # 注：不保留「附：执行与数据完整性 / 局限与未验证项」等专业附录（面向非专业用户的报告无需干扰）。
    return "\n".join(L), []




# ---------------------------------------------------------------------------
# MD → HTML（极简渲染，复用 report_template.html 的样式）
# ---------------------------------------------------------------------------
def _inline(s):
    # M2：还原 numeric_trend 证据行的规范反斜杠实体（&#92; → \），再 escape 保证 HTML 显示为 \；
    # 实体在 MD 事实源中保持字面，指纹/HTML 一致性都基于还原后字节。
    s = s.replace("&#92;", "\\")
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    # Markdown 链接：结果统计表「网元名称」→ 网元详情锚点（[name](#ne-name)）
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    # TODO 注释渲染为可见标记（HTML 读者可直接看到待补位置）
    s = re.sub(r"&lt;!--\s*TODO:\s*(.*?)\s*--&gt;",
               r'<span class="todo">[TODO] \1</span>', s)
    s = s.replace("&lt;!--", "<!--").replace("--&gt;", "-->")
    return s


def _fm_align_to_md(path):
    """把 fm_alarm_align.py 的输出（--md 文件直接返回，--json 文件渲染为表格）转成
    可注入报告第 3 节的 MD 文本；无输入/读取失败返回 None。"""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        print(f"[!] --fm-align 不存在：{p}", file=sys.stderr)
        return None
    try:
        txt = p.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[!] --fm-align 读取失败：{e}", file=sys.stderr)
        return None
    stripped = txt.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        # JSON：渲染为对齐表格
        try:
            d = json.loads(txt)
        except json.JSONDecodeError as e:
            print(f"[!] --fm-align JSON 解析失败：{e}", file=sys.stderr)
            return None
        return _fm_align_json_to_md(d)
    # 已是 MD 片段
    return stripped


def _fm_align_json_to_md(d):
    L = ["FM 告警时间窗对齐（匹配窗 {}h）：".format(d.get("window_h", 48))]
    alarms = d.get("alarms", [])
    if not alarms:
        note = (d.get("notes") or ["未发现时钟相关告警"])[0]
        return L[0] + f" 未发现时钟相关告警（{note}）"
    rows = []
    for it in alarms:
        a = it.get("alarm", {})
        st = {"in_window": "✓ 与证据吻合", "outside_window": "⚠ 偏离证据时间",
              None: "—"}.get(a.get("matched"))
        rows.append([it.get("file", ""), _mdcell(a.get("name", ""), 20),
                     a.get("code") or "-", a.get("time") or "-", st])
    L.append("| 告警文件 | 告警名称 | 告警码 | 告警时间 | 与证据对齐 |")
    L.append("| --- | --- | --- | --- | --- |")
    for r in rows:
        L.append("| " + " | ".join(_mdcell(x, 40) for x in r) + " |")
    if d.get("notes"):
        L.append("- 注：" + "；".join(d["notes"]))
    return "\n".join(L)


def _mdcell(s, n):
    s = str(s).replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def md_to_html(md, css, title):
    lines = strip_fp(md).split("\n")
    out, i, n = [], 0, len(lines)
    toc = []
    while i < n:
        ln = lines[i]
        if ln.startswith("```"):
            buf = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            out.append("<pre>" + html.escape("\n".join(buf)) + "</pre>"); i += 1; continue
        if ln.startswith("> ```"):
            buf = []
            i += 1
            while i < n and not lines[i].startswith("> ```"):
                buf.append(lines[i][2:] if lines[i].startswith("> ") else lines[i]); i += 1
            out.append("<pre>" + html.escape("\n".join(buf)) + "</pre>"); i += 1; continue
        m = re.match(r"^(#{1,4}) (.*)", ln)
        if m:
            lvl = len(m.group(1))
            if lvl == 2:
                toc.append(re.sub(r"<.*?>", "", m.group(2)))
                out.append(f'<h2 id="s{len(toc)}">{_inline(m.group(2))}</h2>')
            elif lvl == 3:
                # 网元详情标题（### 2.N 名称）：id 用 GitHub 锚点规则，
                # 与结果统计表 [网元](#…) 链接一致，MD/HTML 都可跳转
                out.append(f'<h3 id="{gh_anchor(m.group(2))}">{_inline(m.group(2))}</h3>')
            else:
                out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if ln.startswith("|"):
            rows = []
            while i < n and lines[i].startswith("|"):
                rows.append(lines[i]); i += 1
            cells = [_split_md_row(r) for r in rows if not re.match(r"^\|[-| ]+\|$", r)]
            if cells:
                h = "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
                body = "".join("<tr>" + "".join(f"<td class=\"{'hit' if c in ('命中', '满足', '成立') else 'miss' if c in ('未命中', '**不满足**', '不成立') else ''}\">{_inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
                out.append(f"<table><tr>{h}</tr>{body}</table>")
            continue
        if ln.startswith("> "):
            buf = []
            while i < n and lines[i].startswith("> "):
                buf.append(lines[i][2:]); i += 1
            out.append('<p class="note">' + "<br>".join(_inline(b) for b in buf) + "</p>"); continue
        if re.match(r"^\s*- ", ln):
            items, stack = [], []
            while i < n and re.match(r"^\s*- ", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                items.append((indent, lines[i].lstrip()[2:])); i += 1
            out.append(_list(items)); continue
        if re.match(r"^\d+\. ", ln):
            buf = []
            while i < n and re.match(r"^\d+\. ", lines[i]):
                buf.append(re.sub(r"^\d+\. ", "", lines[i])); i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(b)}</li>" for b in buf) + "</ol>"); continue
        if ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    # h1 后插入目录（h2 锚点 s1..sN）
    if toc:
        nav = ('<nav class="toc">'
               + "".join(f'<a href="#s{k}">{html.escape(t)}</a>'
                         for k, t in enumerate(toc, 1)) + "</nav>")
        for j, frag in enumerate(out):
            if frag.startswith("<h1"):
                out.insert(j + 1, nav)
                break
    body = "\n".join(out)
    if not str(title).endswith("报告"):
        title = f"{title}报告"
    return (f'<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>{html.escape(title)}</title>\n'
            f"<style>\n{css}\n</style>\n</head>\n<body>\n<div class=\"page\">\n{body}\n</div>\n</body>\n</html>\n")


def _list(items):
    out, depth = ["<ul>"], 0
    base = items[0][0] if items else 0
    prev = base
    for indent, text in items:
        if indent > prev:
            out.append("<ul>")
        elif indent < prev:
            out.append("</ul>" * max(1, (prev - indent) // 2))
        out.append(f"<li>{_inline(text)}</li>")
        prev = indent
    out.append("</ul>" * (1 + max(0, (prev - base) // 2)))
    return "".join(out)


DEFAULT_CSS = ("body{margin:0;padding:32px 16px;background:#f5f6f7;color:#1f2329;font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;line-height:1.65}"
               ".page{max-width:960px;margin:0 auto;background:#fff;border:1px solid #dee0e3;border-radius:10px;padding:40px 48px}"
               "table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #dee0e3;padding:7px 10px;vertical-align:top}"
               "pre{background:#1f2329;color:#e6e6e6;padding:14px;border-radius:8px;overflow:auto}code{background:#f2f3f5;padding:1px 5px;border-radius:4px}"
               ".hit{color:#1a7f37;font-weight:600}.miss{color:#d83931;font-weight:600}.note{color:#646a73;font-size:13px}"
               "a{color:#2b5cff;text-decoration:none}a:hover{text-decoration:underline}h3{scroll-margin-top:20px}"
               ".todo{background:#fff3cd;color:#8a6d00;padding:0 4px;border-radius:3px;font-size:12px}"
               ".toc{font-size:13px;margin:8px 0 20px}.toc a{color:#2b5cff;text-decoration:none;margin-right:14px}"
               "@media print{body{background:#fff;padding:0}.page{border:none;padding:0;max-width:none}"
               "pre,table{page-break-inside:avoid}}")


def load_css(template_path):
    p = Path(template_path) if template_path else ASSETS / "report_template.html"
    if p.exists():
        m = re.search(r"<style>(.*?)</style>", p.read_text(encoding="utf-8"), re.S)
        if m:
            return m.group(1).strip()
    return DEFAULT_CSS


def main():
    ap = argparse.ArgumentParser(description="从 evidence.json 预填 MD/HTML 故障分析报告草稿")
    ap.add_argument("--evidence", required=True, help="search_evidence.py 输出的 evidence.json")
    ap.add_argument("--profile", help="fault-profile.yaml（默认 ../assets/fault-profile.yaml）")
    ap.add_argument("--manifest", help="unpack_logs.py 输出的 manifest.json")
    ap.add_argument("--cloudmat", help="parse_cloudmat.py 输出 JSON")
    ap.add_argument("--clock", help="parse_clock.py 输出 JSON，或含多站点 clock JSON 的目录"
                                    "（目录模式自动合并 files[]/gnss）")
    ap.add_argument("--precheck", help="common-rules 预检输出 JSON（search_evidence --rules common-rules.yaml）")
    ap.add_argument("--specialized", help="专项脚本复核结论 JSON：{\"primary\": \"R1\"|null, "
                    "\"status\": \"matched|unmatched|temporal_conflict|normal\", \"note\": ..., \"site\": 组键} "
                    "或 {\"sites\": {\"<组键>\": {...}}}；存在时结论与 --verify 以专项脚本为准")
    ap.add_argument("--fm-align", metavar="MD_OR_JSON",
                    help="fm_alarm_align.py 的输出（--md 文件或 --json 文件）；MD 直接注入报告第 3 节，"
                         "JSON 自动渲染为对齐表格")
    ap.add_argument("--description", default="", help="用户故障描述原文")
    ap.add_argument("--source", default="", help="数据来源（压缩包/日志文件名）")
    ap.add_argument("--site", help="只输出某个站点分组的结论（verdict.groups 的键）")
    ap.add_argument("--out", help="输出基名（等价 --md <out>.md --html <out>.html）；缺省输出到 --evidence 所在目录 report.md/report.html")
    ap.add_argument("--md", help="输出 MD 路径")
    ap.add_argument("--html", help="输出 HTML 路径（可选）")
    ap.add_argument("--html-template", help="取样式用的 report_template.html（默认 ../assets/）")
    ap.add_argument("--from-md", metavar="REPORT_MD",
                    help="补全 TODO 后，由已编辑的 MD 重新渲染 HTML（--html 必填；时间轴取自 --evidence），不重建 MD")
    ap.add_argument("--verify", metavar="REPORT_MD",
                    help="交付门禁：校验已补全的 MD（TODO 清零/指纹行未改/结论与 verdict 一致/HTML 一致），"
                         "退出码 0 通过、2 失败；配合 --evidence [--html] [--site]")
    ap.add_argument("--verify-detail", action="store_true",
                    help="--verify 失配时输出全部行级 diff（默认仅输出前 3 行诊断摘要）")
    ap.add_argument("--final", action="store_true",
                    help="生成无 TODO 的最终用户报告；缺失的专家说明使用明确兜底，不作推测")
    args = ap.parse_args()
    _utf8_stdio()
    if args.verify:
        ev = load_any(args.evidence)
        if ev is None:
            sys.exit(f"evidence 不存在: {args.evidence}")
        if not Path(args.verify).exists():
            sys.exit(f"报告不存在: {args.verify}")
        globals()["verify_detail"] = args.verify_detail
        ok, checks = verify_report(args.verify, ev, args.html, args.site,
                                   specialized=load_any(args.specialized))
        for name, passed, note in checks:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}：{note}")
        print(f"[{'OK' if ok else 'x'}] 报告校验{'通过，可交付' if ok else '未通过，禁止交付'}: {args.verify}")
        sys.exit(0 if ok else 2)
    if args.from_md:
        if not args.html:
            sys.exit("--from-md 需配合 --html 指定输出")
        ev = load_any(args.evidence) or {}
        md = Path(args.from_md).read_text(encoding="utf-8")
        prof_path = args.profile or ASSETS / "fault-profile.yaml"
        title = ((load_any(prof_path) or {}).get("skill") or {}).get("title") or "BBU 故障分析"
        Path(args.html).write_text(md_to_html(md, load_css(args.html_template), title), encoding="utf-8")
        print(f"[OK] HTML 由 MD 重新渲染: {args.html}（MD 残留 TODO {md.count('<!-- TODO')} 处）")
        return
    if args.out:
        base = Path(args.out)
        base = base.with_suffix("")  # 误带后缀则剥离
        if not args.md:
            args.md = str(base) + ".md"
        if not args.html:
            args.html = str(base) + ".html"
    if not args.md and not args.out:
        # 默认输出到待分析文件（evidence.json）所在目录，基名 report
        base = Path(args.evidence).resolve().parent / "report"
        args.md = str(base) + ".md"
        if not args.html:
            args.html = str(base) + ".html"
    if not args.md:
        sys.exit("必须提供 --md 或 --out")

    ev = load_any(args.evidence)
    if ev is None:
        sys.exit(f"evidence 不存在: {args.evidence}")
    prof_path = args.profile or ASSETS / "fault-profile.yaml"
    profile = load_any(prof_path)
    if profile is None:
        print(f"[!] 未找到 profile（{prof_path}），处理方案/步骤标题将缺省", file=sys.stderr)
    md, _events = build_report(ev, profile, load_any(args.manifest), load_any(args.cloudmat),
                              _load_clock(args.clock), args.description, args.source, args.site,
                              precheck=load_any(args.precheck),
                              specialized=load_any(args.specialized),
                              fm_align_md=_fm_align_to_md(args.fm_align),
                              draft=not args.final)
    Path(args.md).write_text(md, encoding="utf-8")
    todo_count = md.count("<!-- TODO")
    print(f"[OK] MD {'最终报告' if args.final else '报告草稿'}: {args.md}（TODO {todo_count} 处）")
    if args.html:
        title = ((profile or {}).get("skill") or {}).get("title") or "BBU 故障分析"
        Path(args.html).write_text(md_to_html(md, load_css(args.html_template), title), encoding="utf-8")
        print(f"[OK] HTML 报告: {args.html}")


if __name__ == "__main__":
    main()
