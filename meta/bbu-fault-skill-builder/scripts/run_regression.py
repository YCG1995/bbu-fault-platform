#!/usr/bin/env python3
"""
run_regression.py — BBU 故障子 Skill 案例库一键回归

案例库与 skill 解耦，存放于 skill 之外，每个 case 是独立目录：

    <cases-root>/<skill-slug>/<case-id>/
      ├── case.yaml              # 案例元数据 + 结构化预期（唯一断言来源）
      ├── input/                 # 输入：归档（zip/tar/gz/bz2）或解压后的目录树
      └── expected/              # 可选：期望产物（报告片段等，case.yaml 内断言为准）

用法：
    # 全量回归：判定 + 根因/规则覆盖门禁；auto 对关键案例跑一键报告门禁
    python3 run_regression.py --cases-dir <cases-root> --skills-dir <skills根> \\
        --skill <skill-slug> --e2e auto --json report.json
    # 回归单个 case 目录
    python3 run_regression.py --case <case-id 目录> --skills-dir <skills根>
    # 初始化一个新案例骨架
    python3 init_case.py --cases-dir <cases-root> --skill <slug> --id <case-id>

稳定性原则（断言不得依赖易变内容）：
  - 不比对绝对路径：用 file_suffix（路径后缀）或 file_basename
  - 不比对行号：层级变化会改变行号
  - 证据只断言关键字 all_of（子串）与命中次数 match_counts（支持 >= / == / <=）
  - 每次回归在全新临时工作目录执行，结果可复现；记录归档 sha256
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_evidence import eval_condition, related_temporal  # noqa: E402 判定同源

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


def _utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def load_yaml_or_json(path):
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() in (".yaml", ".yml"):
        if not _HAS_YAML:
            raise SystemExit("案例为 YAML 但环境缺少 PyYAML，请用 JSON 或安装 pyyaml")
        return yaml.safe_load(text)
    return json.loads(text)


def run(cmd, cwd=None):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                       encoding="utf-8", errors="replace", env=env)
    return r.returncode, r.stdout, r.stderr


def fhash(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()[:16]


def hash_input(path):
    """对归档文件或目录（按相对路径排序）计算稳定摘要。"""
    if path.is_file():
        return fhash(path)
    h = hashlib.sha256()
    for q in sorted(q for q in path.rglob("*") if q.is_file()):
        h.update(str(q.relative_to(path)).encode("utf-8"))
        h.update(b"\0")
        with open(q, "rb") as f:
            for c in iter(lambda: f.read(1 << 20), b""):
                h.update(c)
        h.update(b"\0")
    return h.hexdigest()[:16]


def eval_count(spec, actual):
    """比较表达式：'1' / '>=1' / '==2' / '<=3'。"""
    if isinstance(spec, int):
        return actual == spec
    m = re.fullmatch(r"\s*(>=|<=|==|>|<)?\s*(\d+)\s*", str(spec))
    if not m:
        return False
    op, n = m.group(1) or "==", int(m.group(2))
    return {">=": actual >= n, "<=": actual <= n, "==": actual == n,
            ">": actual > n, "<": actual < n}[op]


def norm_path(p):
    """证据路径归一化：统一为正斜杠，消除绝对路径与层级差异。"""
    return (p or "").replace("\\", "/")


# ---------------------------------------------------------------------------
# 案例发现 / skill 定位
# ---------------------------------------------------------------------------
def discover_cases(cases_dir, skill_slug=None):
    root = Path(cases_dir)
    if skill_slug:
        root = root / skill_slug
    found = []
    for cf in sorted(root.rglob("case.yaml")) + sorted(root.rglob("case.yml")):
        data = load_yaml_or_json(cf)
        if data is None:
            continue
        if skill_slug and data.get("skill") and data["skill"] != skill_slug:
            continue
        found.append((cf.parent, data))
    return found


def locate_skill(skill_dir, skills_dir, slug):
    if skill_dir:
        sd = Path(skill_dir)
        return sd if (sd / "scripts").exists() else None
    if skills_dir and slug:
        sd = Path(skills_dir) / slug
        if (sd / "scripts").exists():
            return sd
        for q in Path(skills_dir).iterdir():
            if q.is_dir() and q.name.endswith(slug) and (q / "scripts").exists():
                return q
    return None


# ---------------------------------------------------------------------------
# 阶段执行
# ---------------------------------------------------------------------------
def stage_unpack(skill_dir, input_path, cw):
    ud = cw / "unpacked"
    if input_path.is_dir():
        ud.mkdir(parents=True, exist_ok=True)
        for q in input_path.rglob("*"):
            if q.is_file():
                dest = ud / q.relative_to(input_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(q, dest)
        return {"ok": True, "manifest": {}}
    rules = skill_dir / "assets" / "evidence-rules.yaml"  # 统一 yaml
    rc, _, err = run([sys.executable, str(skill_dir / "scripts" / "unpack_logs.py"),
                      str(input_path), "-o", str(ud),
                      "--include-rules", str(rules)])
    if rc != 0:
        return {"ok": False, "error": f"unpack: {err[:300]}"}
    mf = ud / "manifest.json"
    manifest = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    return {"ok": True, "manifest": manifest}


def stage_search(skill_dir, ud, cw):
    ej = cw / "evidence.json"
    rf = skill_dir / "assets" / "evidence-rules.yaml"  # 统一 yaml
    rc, _, err = run([sys.executable, str(skill_dir / "scripts" / "search_evidence.py"),
                      str(ud), "--rules", str(rf), "--json", str(ej)])
    if rc != 0:
        return {"ok": False, "error": f"search: {err[:300]}"}
    return {"ok": True, "evidence": json.loads(ej.read_text(encoding="utf-8"))}


def stage_parse_clock(skill_dir, ud, cw):
    cj = cw / "clock.json"
    rc, _, err = run([sys.executable, str(skill_dir / "scripts" / "parse_clock.py"),
                      str(ud), "--merge", "--json", str(cj)])
    if rc != 0:
        return {"ok": False, "error": f"parse_clock: {err[:300]}"}
    return {"ok": True,
            "clock": json.loads(cj.read_text(encoding="utf-8")) if cj.exists() else None}


def eval_root_cause(ev, profile):
    """优先采用 search_evidence 输出的 verdict（与子 Skill 判定同源）；
    旧版 evidence 无 verdict 时按 profile 本地计算（直接复用 search_evidence
    的 eval_condition / related_temporal，保证判定逻辑同源不分叉）。"""
    verdict = ev.get("verdict")
    if verdict and verdict.get("primary") is not None:
        return verdict["primary"]
    hit = set(ev.get("hit_rules", []))
    tok = set(ev.get("temporal_satisfied", []))
    temporal_defs = ev.get("temporal", []) or []
    for rcdef in profile.get("root_causes", []) or []:
        value, _sub, _err = eval_condition(rcdef.get("condition", ""), hit)
        if not value:
            continue
        if all(t in tok for t in related_temporal(rcdef, temporal_defs)):
            return rcdef["id"]
    return "NONE"


def load_profile(skill_dir):
    p = Path(skill_dir) / "assets" / "fault-profile.yaml"
    return load_yaml_or_json(p) if p.exists() else {}


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------
def check(ok, step, expected, actual):
    return {"step": step, "ok": bool(ok), "expected": expected, "actual": actual}


def assert_evidence(case_exp, ev, checks):
    hit_rules = ev.get("hit_rules", [])
    by_rule = {}
    for r in ev.get("results", ev.get("rules", [])) or []:
        by_rule.setdefault(r.get("id") or r.get("rule_id"), r)

    for eid in case_exp.get("hit_rules", []):
        ok = eid in hit_rules
        checks.append(check(ok, f"hit_rule:{eid}", "命中", "命中" if ok else "未命中"))
    for eid in case_exp.get("not_hit_rules", []):
        ok = eid not in hit_rules
        checks.append(check(ok, f"not_hit_rule:{eid}", "不命中", "未命中" if ok else "命中"))
    for tid in case_exp.get("temporal_satisfied", []):
        ok = tid in ev.get("temporal_satisfied", [])
        checks.append(check(ok, f"temporal:{tid}", "满足", "满足" if ok else "不满足"))
    for tid in case_exp.get("temporal_violated", []):
        ok = tid in ev.get("temporal_violated", [])
        checks.append(check(ok, f"temporal_violated:{tid}", "违反", "违反" if ok else "未违反"))
    for rid, spec in (case_exp.get("match_counts") or {}).items():
        rule = by_rule.get(rid, {})
        actual = rule.get("match_count", 0) if rule else 0
        checks.append(check(eval_count(spec, actual), f"match_count:{rid}", spec, actual))
    for i, ec in enumerate(case_exp.get("evidence_contains") or []):
        rid = ec.get("rule")
        rule = by_rule.get(rid, {})
        evidence = rule.get("evidence", []) if rule else []
        needles = ec.get("all_of", [])
        f_suffix = ec.get("file_suffix")
        f_base = ec.get("file_basename")
        matched = False
        for e in evidence:
            line = e.get("line", "")
            fp = norm_path(e.get("file", ""))
            if needles and not all(n in line for n in needles):
                continue
            if f_suffix and not fp.endswith(norm_path(f_suffix)):
                continue
            if f_base and Path(fp).name != f_base:
                continue
            matched = True
            break
        checks.append(check(matched, f"evidence:{rid}#{i}",
                            {"all_of": needles, "file_suffix": f_suffix,
                             "file_basename": f_base}, f"{len(evidence)} 条证据"))


def assert_manifest(case_exp, manifest, checks):
    want = case_exp.get("manifest")
    if not want:
        return
    key_map = {"warnings": "warnings", "unsupported": "unsupported_archives",
               "skipped": "skipped", "corrupt": "skipped"}
    for key, spec in want.items():
        if key == "sites":
            actual = len(manifest.get("sites", []))
        elif key in key_map:
            actual = len(manifest.get(key_map[key], []) or [])
        else:
            actual = manifest.get(key)
        ok = eval_count(spec, actual) if isinstance(actual, int) else actual == spec
        checks.append(check(ok, f"manifest:{key}", spec, actual))


def _clock_aggregate(clock):
    agg = {"alarms": [], "ref_switches": [], "reset_reasons": [],
           "health_bits": [], "anomalies": [], "ocxo_count": 0,
           "timestamp_fixed": 0, "total_records": 0,
           "gnss_records": 0, "spoofing": []}
    for f in clock.get("files") or []:
        agg["alarms"].extend(f.get("alarms", []))
        agg["ref_switches"].extend(f.get("ref_switches", []))
        agg["reset_reasons"].extend(f.get("reset_reasons", []))
        agg["timestamp_fixed"] += f.get("timestamp_fixed", 0)
        agg["total_records"] += f.get("total_records", 0)
        agg["ocxo_count"] += len(f.get("ocxo_records", []))
        for h in f.get("clk99_health_events", []):
            agg["health_bits"].extend((h.get("bits_set") or {}).keys())
        for r in f.get("anomalies", []):
            anomalies = r.get("anomaly") if isinstance(r.get("anomaly"), list) else [r.get("anomaly")]
            agg["anomalies"].extend(a for a in anomalies if a)
    g = clock.get("gnss") or {}
    agg["gnss_records"] = g.get("gnss_records", 0)
    agg["spoofing"] = g.get("spoofing_suspicions", [])
    return agg


def _match_events(actual, wanted):
    for w in wanted:
        if not any(all(ev.get(k) == v for k, v in w.items()) for ev in actual):
            return False
    return True


def assert_clock(case_exp, clock, checks):
    want = case_exp.get("parse_clock")
    if not want:
        return
    if clock is None:
        checks.append(check(False, "parse_clock", "存在时钟解析结果", "无"))
        return
    agg = _clock_aggregate(clock)
    if "alarms" in want:
        ok = _match_events(agg["alarms"], want["alarms"])
        checks.append(check(ok, "clock:alarms", want["alarms"],
                            "命中" if ok else agg["alarms"]))
    if "reset_reasons" in want:
        ok = _match_events(agg["reset_reasons"], want["reset_reasons"])
        checks.append(check(ok, "clock:reset_reasons", want["reset_reasons"],
                            "命中" if ok else agg["reset_reasons"]))
    for bit in want.get("health_bits", []):
        checks.append(check(bit in agg["health_bits"], f"clock:health_bit:{bit}",
                            bit, "置位" if bit in agg["health_bits"] else "未置位"))
    for an in want.get("anomaly_contains", []):
        ok = any(an in a for a in agg["anomalies"])
        checks.append(check(ok, f"clock:anomaly:{an}", an, "存在" if ok else "不存在"))
    if "spoofing_suspicions" in want:
        n = len(agg["spoofing"])
        checks.append(check(eval_count(want["spoofing_suspicions"], n),
                            "clock:spoofing", want["spoofing_suspicions"], n))
    if "timestamp_fixed" in want:
        checks.append(check(eval_count(want["timestamp_fixed"], agg["timestamp_fixed"]),
                            "clock:timestamp_fixed", want["timestamp_fixed"],
                            agg["timestamp_fixed"]))
    for k in ("total_records", "ocxo_count", "gnss_records"):
        if k in want:
            checks.append(check(eval_count(want[k], agg[k]), f"clock:{k}", want[k], agg[k]))


# ---------------------------------------------------------------------------
# 单 case 执行
# ---------------------------------------------------------------------------
USE_CACHE = True  # --no-cache 关闭；缓存键 = 输入+案例定义+工具箱 digest


def _toolkit_sig(skill_dir):
    """子 skill 工具箱签名：版本戳 digest 优先，否则受管脚本内容哈希。"""
    stamp = Path(skill_dir) / "assets" / "toolkit-version.json"
    try:
        d = json.loads(stamp.read_text(encoding="utf-8"))
        if d.get("digest"):
            return d["digest"]
    except Exception:
        pass
    h = hashlib.sha256()
    for p in sorted((Path(skill_dir) / "scripts").glob("*.py")):
        h.update(p.name.encode()); h.update(fhash(p).encode())
    return h.hexdigest()[:16]


def _case_sig(case_dir, input_path, skill_dir):
    case_file = next(case_dir.glob("case.y*ml"), None)
    return {"input": hash_input(input_path),
            "case": fhash(case_file) if case_file else "",
            "toolkit": _toolkit_sig(skill_dir)}


def _load_cache(workdir, cid, sig):
    try:
        d = json.loads((workdir / ".cache" / f"{cid}.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return d.get("result") if d.get("sig") == sig else None


def _save_cache(workdir, cid, sig, result):
    cdir = workdir / ".cache"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{cid}.json").write_text(
        json.dumps({"sig": sig, "result": result}, ensure_ascii=False),
        encoding="utf-8")


def run_case(case_dir, case_data, skill_dir, workdir):
    cid = case_data.get("id", case_dir.name)
    input_cfg = case_data.get("input", {})
    arc_rel = input_cfg.get("archive") or input_cfg.get("unpacked_dir")
    input_path = (case_dir / arc_rel) if arc_rel else case_dir / "input"
    if not input_path.exists():
        return {"id": cid, "status": "ERROR", "error": f"输入不存在: {input_path}"}
    sig = _case_sig(case_dir, input_path, skill_dir) if USE_CACHE else None
    if sig is not None:
        hit = _load_cache(workdir, cid, sig)
        if hit is not None:
            hit = dict(hit)
            hit["cached"] = True
            return hit
    cw = workdir / cid
    if cw.exists():
        shutil.rmtree(cw)
    cw.mkdir(parents=True)

    result = {"id": cid, "skill": case_data.get("skill"),
              "kind": case_data.get("kind", ""),
              "description": case_data.get("description", ""),
              "input_hash": hash_input(input_path), "checks": [], "status": "PASS"}
    exp = case_data.get("expected", {})
    pipeline = list(case_data.get("pipeline", ["unpack", "search"]))
    if exp.get("parse_clock") and "parse_clock" not in pipeline:
        pipeline.append("parse_clock")

    ud = cw / "unpacked"
    if "unpack" in pipeline:
        r = stage_unpack(skill_dir, input_path, cw)
        if not r["ok"]:
            result["status"] = "ERROR"
            result["error"] = r["error"]
            return result
        result["checks"].append(check(True, "stage:unpack", "成功", "成功"))
        assert_manifest(exp, r["manifest"], result["checks"])

    if "search" in pipeline:
        r = stage_search(skill_dir, ud, cw)
        if not r["ok"]:
            result["status"] = "ERROR"
            result["error"] = r["error"]
            return result
        ev = r["evidence"]
        result["hit_rules"] = ev.get("hit_rules", [])
        result["temporal_satisfied"] = ev.get("temporal_satisfied", [])
        result["checks"].append(check(True, "stage:search", "成功", "成功"))
        assert_evidence(exp, ev, result["checks"])
        if exp.get("root_cause") is not None:
            arc = eval_root_cause(ev, load_profile(skill_dir))
            result["actual_root_cause"] = arc
            result["checks"].append(check(arc == exp["root_cause"], "root_cause",
                                          exp["root_cause"], arc))

    if "parse_clock" in pipeline:
        r = stage_parse_clock(skill_dir, ud, cw)
        if not r["ok"]:
            result["status"] = "ERROR"
            result["error"] = r["error"]
            return result
        assert_clock(exp, r["clock"], result["checks"])

    if any(not c["ok"] for c in result["checks"]):
        result["status"] = "FAIL"
    if sig is not None:  # PASS/FAIL 入缓存；ERROR（环境问题）不缓存
        _save_cache(workdir, cid, sig, result)
    return result


# ---------------------------------------------------------------------------
# 旧版 JSON 清单（向后兼容）
# ---------------------------------------------------------------------------
def run_legacy(cases_json, workdir):
    cases = json.loads(Path(cases_json).read_text(encoding="utf-8")).get("cases", [])
    results = []
    for c in cases:
        cd = Path(c["archive"])
        data = {"id": c["id"], "description": c.get("description", ""),
                "input": {"archive": str(cd)},
                "expected": {
                    "root_cause": c.get("expected_root_cause"),
                    "hit_rules": c.get("expected_evidence", []),
                    "not_hit_rules": c.get("not_expected_evidence", [])}}
        results.append(run_case(cd.parent, data, Path(c["skill_dir"]), workdir))
    return results


# ---------------------------------------------------------------------------
# 覆盖率与关键案例端到端门禁
# ---------------------------------------------------------------------------
def coverage_report(profile, case_pairs):
    """以 case.yaml 的期望为依据，检查每个根因及其 condition 规则均被案例覆盖。"""
    roots = profile.get("root_causes") or []
    root_ids = {rc.get("id") for rc in roots}
    positive = {rid: [] for rid in root_ids}
    covered = {rid: set() for rid in root_ids}
    for cd, data in case_pairs:
        exp = data.get("expected") or {}
        rid = exp.get("root_cause")
        cid = str(data.get("id") or cd.name)
        if rid in root_ids and data.get("kind", "positive") != "negative":
            positive[rid].append(cid)
            # condition 中的否定分支靠 not_hit_rules 验证，不能要求它必须正向命中。
            covered[rid].update(exp.get("hit_rules") or [])
            covered[rid].update(exp.get("not_hit_rules") or [])
    errors, rows = [], {}
    for rc in roots:
        rid = rc.get("id")
        required = set(re.findall(r"\b[A-Z]\d+\b", rc.get("condition", "") or ""))
        missing = sorted(required - covered[rid])
        if not positive[rid]:
            errors.append(f"根因 {rid} 没有非负例案例")
        if missing:
            errors.append(f"根因 {rid} 的 condition 规则未被其案例验证: {missing}")
        rows[rid] = {"cases": positive[rid], "required_rules": sorted(required),
                     "covered_rules": sorted(covered[rid]), "missing_rules": missing}
    negative_cases = sum(1 for _cd, data in case_pairs
                         if (data.get("expected") or {}).get("root_cause") == "NONE")
    warnings = [] if negative_cases else ["未登记 expected.root_cause=NONE 的反例，建议补充"]
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "root_causes": rows, "negative_cases": negative_cases}


def select_e2e(case_pairs, results, mode):
    if mode == "all":
        return list(zip(case_pairs, results))
    chosen, seen_roots = [], set()
    for (cd, data), result in zip(case_pairs, results):
        exp = data.get("expected") or {}
        rid = exp.get("root_cause")
        kind = data.get("kind", "positive")
        tags = {str(x) for x in data.get("tags") or []}
        needed = kind in ("negative", "multisite") or "conflict" in tags
        if rid and rid != "NONE" and rid not in seen_roots:
            needed = True
        if needed:
            chosen.append(((cd, data), result))
            if rid and rid != "NONE":
                seen_roots.add(rid)
    return chosen


def stage_e2e(skill_dir, case_dir, data, workdir):
    input_cfg = data.get("input") or {}
    arc_rel = input_cfg.get("archive") or input_cfg.get("unpacked_dir")
    input_path = (case_dir / arc_rel) if arc_rel else case_dir / "input"
    out = workdir / "e2e"
    if out.exists():
        shutil.rmtree(out)
    script = skill_dir / "scripts" / "run_analysis.py"
    if not script.exists():
        return {"ok": False, "error": f"缺少一键入口: {script}"}
    rc, stdout, stderr = run([sys.executable, str(script), str(input_path),
                              "--description", str(data.get("description") or "案例回归"),
                              "--out", str(out), "--no-cache"])
    summary = {}
    sp = out / "run-summary.json"
    if sp.exists():
        try:
            summary = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    expected = (data.get("expected") or {}).get("root_cause")
    actual = summary.get("primary")
    ok = (rc == 0 and summary.get("ok") is True
          and (out / "report.md").is_file() and (out / "report.html").is_file()
          and (expected is None or actual == expected))
    error = ""
    if not ok:
        error = (f"run_analysis 端到端失败: rc={rc}, expected={expected}, actual={actual}; "
                 f"{(stderr or stdout)[-500:]}")
    return {"ok": ok, "expected_root_cause": expected, "actual_root_cause": actual,
            "out": str(out), "error": error}


def apply_e2e(case_pairs, results, mode, args):
    selected = select_e2e(case_pairs, results, mode)
    chosen_ids = {id(result) for _pair, result in selected}
    passed = failed = 0
    for (cd, data), result in selected:
        if result.get("status") != "PASS":
            result["e2e"] = {"ok": False, "skipped": "快速判定未通过，未执行端到端"}
            continue
        sd = locate_skill(args.skill_dir, args.skills_dir, data.get("skill"))
        e2e = stage_e2e(sd, cd, data, Path(args.workdir) / result["id"]) if sd else {
            "ok": False, "error": "无法定位子 skill"}
        result["e2e"] = e2e
        if e2e["ok"]:
            passed += 1
        else:
            failed += 1
            result["status"] = "FAIL"
            result["error"] = e2e["error"]
    skipped = sum(1 for r in results if id(r) not in chosen_ids)
    return {"mode": mode, "selected": len(selected), "passed": passed,
            "failed": failed, "skipped": skipped}


def main():
    ap = argparse.ArgumentParser(description="BBU 案例库回归（标准化 case 目录）")
    ap.add_argument("--cases-dir", help="案例库根（其下 <skill>/<case>/case.yaml）")
    ap.add_argument("--skill", help="只回归指定 skill slug（配合 --cases-dir）")
    ap.add_argument("--case", dest="case_dir", help="只回归单个 case 目录")
    ap.add_argument("--skills-dir", help="skills 安装根目录（用于定位子 skill）")
    ap.add_argument("--skill-dir", help="直接指定子 skill 目录（优先于 --skills-dir）")
    ap.add_argument("--cases", help="（兼容旧版）案例清单 JSON")
    ap.add_argument("--workdir", default="bbu_regression_work",
                    help="回归临时工作目录（相对路径，默认当前目录下）")
    ap.add_argument("--json", dest="out")
    ap.add_argument("--no-cache", action="store_true",
                    help="关闭增量缓存（输入/案例/工具箱未变时默认复用上次结果）")
    ap.add_argument("--e2e", choices=("none", "auto", "all"), default="none",
                    help="端到端报告门禁：auto=每个根因一例+反例/多站点/冲突；all=全部案例")
    a = ap.parse_args()
    if a.no_cache:
        global USE_CACHE
        USE_CACHE = False
    _utf8_stdio()

    workdir = Path(a.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    case_pairs = []
    full_library = False

    if a.cases:
        results = run_legacy(a.cases, workdir)
    elif a.case_dir:
        cd = Path(a.case_dir)
        yc = cd / "case.yaml"
        if not yc.exists():
            yc = next(cd.glob("case.y*ml"), None)
        if yc is None or not yc.exists():
            raise SystemExit(f"案例目录缺少 case.yaml/case.yml: {cd}")
        data = load_yaml_or_json(yc)
        sd = locate_skill(a.skill_dir, a.skills_dir, data.get("skill"))
        if not sd:
            raise SystemExit(f"无法定位子 skill（{data.get('skill')}），请用 --skills-dir/--skill-dir")
        case_pairs = [(cd, data)]
        results = [run_case(cd, data, sd, workdir)]
    elif a.cases_dir:
        case_pairs = discover_cases(Path(a.cases_dir), a.skill)
        if not case_pairs:
            raise SystemExit(f"未在 {a.cases_dir} 下发现 case.yaml")
        full_library = True
        results = []
        for cd, data in case_pairs:
            sd = locate_skill(a.skill_dir, a.skills_dir, data.get("skill"))
            if not sd:
                results.append({"id": data.get("id", cd.name), "status": "ERROR",
                                "error": f"无法定位子 skill: {data.get('skill')}"})
                continue
            results.append(run_case(cd, data, sd, workdir))
    else:
        ap.error("需提供 --cases-dir / --case 或（兼容）--cases")

    coverage = None
    if full_library and a.skill:
        sd = locate_skill(a.skill_dir, a.skills_dir, a.skill)
        if sd:
            coverage = coverage_report(load_profile(sd), case_pairs)
            for error in coverage["errors"]:
                print(f"[COVERAGE] {error}")
            for warning in coverage["warnings"]:
                print(f"[COVERAGE-WARN] {warning}")

    e2e = {"mode": a.e2e, "selected": 0, "passed": 0, "failed": 0, "skipped": len(results)}
    if a.e2e != "none" and case_pairs:
        e2e = apply_e2e(case_pairs, results, a.e2e, a)

    p = sum(1 for r in results if r["status"] == "PASS")
    f = sum(1 for r in results if r["status"] == "FAIL")
    e = sum(1 for r in results if r["status"] == "ERROR")
    for r in results:
        icon = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERR"}[r["status"]]
        cached = " (cached)" if r.get("cached") else ""
        print(f"[{icon}] {r['id']} ({r.get('kind','')}) {r.get('description','')}{cached}")
        if r["status"] != "PASS":
            if r.get("error"):
                print(f"   ERROR: {r['error']}")
            for c in r.get("checks", []):
                if not c["ok"]:
                    print(f"   [x] {c['step']}: 期望={c['expected']} 实际={c['actual']}")
    print(f"\n回归: {p}/{len(results)} 通过, {f} 失败, {e} 错误")
    if a.e2e != "none":
        print(f"端到端: {e2e['passed']}/{e2e['selected']} 通过，{e2e['failed']} 失败，"
              f"{e2e['skipped']} 个快速案例无需端到端")
    if coverage:
        print(f"覆盖率门禁: {'PASS' if coverage['ok'] else 'FAIL'}")
    if a.out:
        Path(a.out).write_text(json.dumps(
            {"total": len(results), "passed": p, "failed": f, "errors": e,
             "coverage": coverage, "e2e": e2e, "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
    if f or e or (coverage and not coverage["ok"]) or e2e["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
