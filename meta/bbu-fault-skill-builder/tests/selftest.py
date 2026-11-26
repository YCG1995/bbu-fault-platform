#!/usr/bin/env python3
"""
selftest.py — 生成器全链路自测（合成案例，零外部数据）

基于模板示例 profile（CloudMAT 相位超差）合成 4 个案例，在临时目录中走完整链路：
    init_case → scaffold_child_skill → run_regression(unpack→search→verdict) → render_report

分节覆盖：
  §0  脚本单一源（模板不含副本）/模板无 __pycache__/SKILL.md 规范校验/check_env
  §1  profile --check：模板通过 + 8 类错误逐项报错
  §2  脚手架：产物/组件推断（含时钟域）/--force/--update（保留定制、组件移除）
      /版本戳/--inspect（sync/local-modified/missing/stale）/frontmatter 转义与规范/name 规则
  §3  合成案例回归（4 案例 verdict、覆盖率、关键案例端到端、事务化构建）
  §4  render_report：结果统计+网元详情新结构/计数/网元锚点/HTML 锚点/dict-sites/
      预检/指纹注释/--verify 正反/--from-md/--brief/--specialized 豁免/多站点汇总表/
      轮转归一/真实计数/默认不复制源包/manifest.summary.json
  §5  GBK 管道不崩
  §6  参考格式回归：合成 DCM/FM 包（magic-gzip/分槽位合并/xlsx/盘点）
  §7  unpack_logs：list-only/selective/目录/单文件/zip-slip/损坏成员/预算/
      深度截断/独立 gz·bz2/7z 缺工具/GBK 成员名
  §8  search_evidence：numeric_trend/epoch/associate/case_sensitive/csv/gbk/
      截断/错误隔离/时区/多站点 verdict/temporal_conflict
  §9  parse_clock 内容断言+单文件+时间修复开关+--glob；parse_cloudmat 结构化
  §10 run_regression（单例/失败路径/旧版清单）、init_case（kind/--clock）、
      scan_rule_candidates、render 扩展（--site/cloudmat+clock/冲突/无 profile）
  §11 merge-manifest / extract_globs / gen_specialized / extra_scripts / skill_sync
  §12 规则库导入：import_rule_library（黑匣子/蜂鸟、sheet 过滤、slug 去重、--check）
      + rule_library_diff（差异对比）
  §13 导入增强（baseline/merge-into/分组筛选）、triage 端到端闭环、告警码列、无 openpyxl

用法：
    python3 tests/selftest.py [--keep]      # --keep 保留临时目录便于排查
退出码非 0 即失败。
"""
import argparse
import bz2
import gzip
import io
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TEMPLATE_PROFILE = ROOT / "assets" / "templates" / "child-skill" / "assets" / "fault-profile.yaml"
SLUG = "bbu-fault-cloudmat-phase-offset"

LOG_HEAD = [
    "2026-09-10 08:00:00 CloudMAT:开始 CyclingTest，TFP=ORTE_IT_BBU_VSWc2_REWK_V100_V20231118，条码 722385500358，槽位 1_1_3_1",
    "2026-09-10 08:01:00 itemList: 相位超差测试, 电源功率测试, 看门狗复位测试",
]
PHASE_FAIL = [
    "2026-09-10 08:04:30 【失败参数】usPhaseOf5V 频偏: 1346.0, 通过标准: 0",
    "2026-09-10 08:05:00 相位超差测试:第0轮结束，结果：TestItemResult.Fail",
]
PHASE_FAIL_NO_VALUE = ["2026-09-10 08:05:00 相位超差测试:第0轮结束，结果：TestItemResult.Fail"]
POWER_FAIL = ["2026-09-10 08:06:00 电源功率测试:第0轮结束，结果：TestItemResult.Fail"]
PHASE_OK = ["2026-09-10 08:05:00 相位超差测试:第0轮结束，结果：TestItemResult.Success"]
TOTAL_FAIL = ["2026-09-10 12:00:00 CloudMAT:测试结果：失败"]

CASES = {
    "case-01-positive": dict(kind="positive", lines=LOG_HEAD + PHASE_FAIL + POWER_FAIL + TOTAL_FAIL,
                             expected={"root_cause": "R1", "hit_rules": ["E1", "E2", "E3", "E4"],
                                       "temporal_satisfied": ["T1", "T2"], "match_counts": {"E4": ">=1"},
                                       "evidence_contains": [{"rule": "E4", "all_of": ["usPhaseOf5V", "1346.0"], "file_suffix": ".log"}]}),
    "case-02-nested-prefix": dict(kind="boundary", nested=True, prefix="客户A/2026年10月/站点X",
                                  lines=LOG_HEAD + PHASE_FAIL + TOTAL_FAIL,
                                  expected={"root_cause": "R1", "hit_rules": ["E1", "E2", "E4"], "not_hit_rules": ["E3"],
                                            "temporal_satisfied": ["T1", "T2"]}),
    "case-03-negative-power": dict(kind="negative", lines=LOG_HEAD + PHASE_OK + POWER_FAIL + TOTAL_FAIL,
                                   expected={"root_cause": "NONE", "hit_rules": ["E1", "E3"], "not_hit_rules": ["E2", "E4"]}),
    "case-04-incomplete-r2": dict(kind="boundary", lines=LOG_HEAD + PHASE_FAIL_NO_VALUE + TOTAL_FAIL,
                                  expected={"root_cause": "R2", "hit_rules": ["E1", "E2"], "not_hit_rules": ["E4"],
                                            "temporal_satisfied": ["T2"], "temporal_violated": ["T1"]}),
}

FAILS = []
RECORDS = []
CURRENT_SECTION = "?"

import builtins as _builtins
_real_print = _builtins.print
_SECTION_RE = re.compile(r"^\[(\d+)\]\s*(.*)")


def print(*a, **k):  # noqa: A001 - 追踪当前节标题，供 --json 结构化输出
    global CURRENT_SECTION
    if a and isinstance(a[0], str):
        m = _SECTION_RE.match(a[0].lstrip("\n"))
        if m:
            CURRENT_SECTION = int(m.group(1))
    _real_print(*a, **k)


def check(cond, msg):
    print(("  [ok]  " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)
    RECORDS.append({"section": CURRENT_SECTION, "msg": msg, "ok": bool(cond)})
    return cond


def run(cmd, cwd=None, expect_rc=0):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, *map(str, cmd)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=cwd, env=env)
    if expect_rc is not None and r.returncode != expect_rc:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
    return r


def yaml_dump(data, indent=0):
    """最小 YAML 序列化（dict/list/str/int/bool），避免依赖 PyYAML。"""
    pad = "  " * indent
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out.append(yaml_dump(v, indent + 1))
            else:
                out.append(f"{pad}{k}: {yaml_scalar(v)}")
    elif isinstance(data, list):
        for v in data:
            if isinstance(v, dict):
                body = yaml_dump(v, indent + 1).split("\n")
                out.append(f"{pad}- {body[0].strip()}")
                out.extend(body[1:])
            else:
                out.append(f"{pad}- {yaml_scalar(v)}")
    return "\n".join(x for x in out if x != "")


def yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, dict)) and not v:
        return "[]" if isinstance(v, list) else "{}"
    return json.dumps(str(v), ensure_ascii=False)


# ----------------------------------------------------------------------------
# 参考格式合成（对齐 参考数据集 中观测到的真实结构）
# ----------------------------------------------------------------------------
CLK_NEW = ("Clk[099]2025-05-18,10:00:00 MCS M LOC 1PPS PD:45\n"
           "Clk[099]2025-05-18,11:00:00 ClkHealth=0x10000\n")
CLK_OLD = "Clk[099]2025-05-10,10:00:00 MCS M LOC 1PPS PD:46\n"


def _make_dcm_zip(path):
    """合成 DCM 包：<subnet>_<ne>/BspLog/{collectinfo.log, BSP_*.tar, 槽位前缀时钟}。

    覆盖参考数据集观测点：tar 内 .bak.N 实为 gzip（magic 识别）、
    多槽位同名时钟文件同目录共存、.bin 二进制登记。
    """
    inner = Path(tempfile.mkdtemp(prefix="bbu_dcm_"))
    binit = gzip.compress('"2025.05.18 10:00:00.000", ====BspBoardInit start'.encode())
    tar_path = inner / "BSP_1-1-1-1-HWM_APP.EXE_0.tar"
    with tarfile.open(tar_path, "w") as tf:
        for name, data in (("BSP/clockstatuslog.txt", CLK_NEW.encode()),
                           ("BSP/clockstatuslog.txt.01", CLK_OLD.encode()),
                           ("BSP/BoardInit.log.bak.1", binit),
                           ("BSP/verprotect.log.bak.1", b"\x9f\x00\xff\x13cipher"),
                           ("BSP/bspdata.bin", b"\x00\x01\x02\x03")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    root = "6323_9999999/BspLog"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{root}/collectinfo.log",
                    "inst 1-1-1-1-HWM_APP.EXE collect succ 2025-05-19 08:52:39\n")
        zf.write(tar_path, f"{root}/{tar_path.name}")
        zf.writestr(f"{root}/1-1-3-1-PLAT.EXE_BspLog_0_1_clockstatuslog.txt", CLK_NEW)
        zf.writestr(f"{root}/1-1-3-1-PLAT.EXE_BspLog_0_7_clockstatuslog.txt.01.gz",
                    gzip.compress(CLK_OLD.encode()))
        zf.writestr(f"{root}/1-1-4-1-PLAT.EXE_BspLog_0_1_clockstatuslog.txt", CLK_OLD)
        # 非 .log/.txt 命名的 gzip 文本（原 GZ_TEXT 白名单之外，检验内容路由）
        zf.writestr(f"{root}/misc_status.dat.gz",
                    gzip.compress("2025-05-19 10:00:00 misc status OK\n".encode()))
    shutil.rmtree(inner, ignore_errors=True)


def _make_xlsx(rows):
    """最小 OOXML xlsx（rows[0] 为表头），纯 zipfile 生成。"""
    shared, idx = [], {}
    body = []
    for rn, row in enumerate(rows, 1):
        cells = []
        for cn, val in enumerate(row):
            col = chr(ord("A") + cn)
            s = str(val)
            if s not in idx:
                idx[s] = len(shared)
                shared.append(s)
            cells.append(f'<c r="{col}{rn}" t="s"><v>{idx[s]}</v></c>')
        body.append(f'<row r="{rn}">' + "".join(cells) + "</row>")
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml",
                    f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/'
                    f'officeDocument/2006/relationships"><sheets>'
                    f'<sheet name="a1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                    'relationships"><Relationship Id="rId1" Type="x" '
                    'Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/sharedStrings.xml",
                    f'<sst xmlns="{ns}">'
                    + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        zf.writestr("xl/worksheets/sheet1.xml",
                    f'<worksheet xmlns="{ns}"><sheetData>{"".join(body)}</sheetData></worksheet>')
    return buf.getvalue()


def _make_xlsx_sheets(sheets):
    """多 sheet OOXML xlsx：sheets = [("sheet名", [row,...]), ...]，每 sheet 独立表头。
    rows[0] 为该 sheet 表头。用于验证 sheet 级过滤与跨 sheet 类型探测。"""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared, idx = [], {}
    def si(s):
        s = str(s)
        if s not in idx:
            idx[s] = len(shared)
            shared.append(s)
        return idx[s]
    sx, rels, sht_elems = [], [], []
    for i, (sname, rows) in enumerate(sheets, 1):
        body = []
        for rn, row in enumerate(rows, 1):
            cells = [f'<c r="{chr(ord("A") + cn)}{rn}" t="s"><v>{si(v)}</v></c>'
                     for cn, v in enumerate(row)]
            body.append(f'<row r="{rn}">' + "".join(cells) + "</row>")
        sx.append(f'<worksheet xmlns="{ns}"><sheetData>{"".join(body)}</sheetData></worksheet>')
        sht_elems.append(f'<sheet name="{sname}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(f'<Relationship Id="rId{i}" Type="x" Target="worksheets/sheet{i}.xml"/>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml",
                    f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/'
                    f'officeDocument/2006/relationships"><sheets>'
                    + "".join(sht_elems) + "</sheets></workbook>")
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                    'relationships">' + "".join(rels) + "</Relationships>")
        zf.writestr("xl/sharedStrings.xml",
                    f'<sst xmlns="{ns}">' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        for i, x in enumerate(sx, 1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", x)
    return buf.getvalue()


def _make_fm_zip(path):
    """合成 fm-active 导出：zip 内单个中文名 .xlsx（对齐真实网管导出）。"""
    rows = [["告警码", "告警名称", "告警时间"],
            ["198092217", "系统时钟异常", "2025-05-19 13:08:33"],
            ["200201041", "基站DU退服", "2025-05-19 13:08:24"]]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("fm-active-告警监控-test.xlsx", _make_xlsx(rows))


def _make_gbk_name_zip(path, gbk_name: bytes, content: bytes):
    """手工构造最小 zip（stored）：文件名按 GBK 字节写入且无 UTF-8 标志位，
    验证 fix_zip_filename 的编码探测。"""
    crc, n, c = zlib.crc32(content), len(gbk_name), len(content)
    local = b"PK\x03\x04" + struct.pack("<HHHHHIIIHH", 20, 0, 0, 0, 0, crc, c, c, n, 0)
    central = (b"PK\x01\x02" + struct.pack("<HHHHHHIIIHHHHHII",
                                          20, 20, 0, 0, 0, 0, crc, c, c,
                                          n, 0, 0, 0, 0, 0, 0) + gbk_name)
    eocd = b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, 1, 1,
                                       len(central), len(local) + n + c, 0)
    path.write_bytes(local + gbk_name + content + central + eocd)


def _make_twosite_dir(root):
    """双站点目录：<root>/<subnet>_<ne>/BspLog/collectinfo.log ×2。"""
    for tag in ("6321_1111111", "6322_2222222"):
        d = root / tag / "BspLog"
        d.mkdir(parents=True, exist_ok=True)
        (d / "collectinfo.log").write_text(
            "inst 1-1-1-1 collect succ\n", encoding="utf-8")


def make_case(cases_dir, cid, spec):
    r = run([SCRIPTS / "init_case.py", "--cases-dir", cases_dir, "--skill", SLUG, "--id", cid, "--kind", spec["kind"], "--force"])
    check(r.returncode == 0, f"init_case {cid}")
    case_dir = cases_dir / SLUG / cid
    log_name = "722385500358_20260910120000_失败_38406@1_1_3_1_.log"
    inner_dir = Path(tempfile.mkdtemp(prefix="bbu_inner_"))
    log_path = inner_dir / spec.get("prefix", "") / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(spec["lines"]) + "\n", encoding="utf-8")
    archive = case_dir / "input" / "case.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in inner_dir.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(inner_dir).as_posix())
    if spec.get("nested"):
        outer = case_dir / "input" / "outer.zip"
        with zipfile.ZipFile(outer, "w") as zf:
            zf.write(archive, "二次打包/case.zip")
        archive.unlink()
        archive = outer
    shutil.rmtree(inner_dir, ignore_errors=True)
    case_yaml = {"id": cid, "skill": SLUG, "description": f"合成案例 {cid}", "kind": spec["kind"],
                 "input": {"archive": f"input/{archive.name}"}, "pipeline": ["unpack", "search"],
                 "expected": spec["expected"]}
    (case_dir / "case.yaml").write_text(yaml_dump(case_yaml) + "\n", encoding="utf-8")
    return case_dir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--json", dest="json_path", metavar="PATH",
                    help="输出结构化结果（节/条目/通过/失败/退出码），供 CI 或 Windows 下读取")
    ap.add_argument("--only",
                    help="只跑指定节（逗号分隔，如 --only 8；依赖节自动带上）")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

    tmp = Path(tempfile.mkdtemp(prefix="bbu_selftest_"))
    skills_dir, cases_dir, work = tmp / "skills", tmp / "cases", tmp / "work"
    print(f"临时目录: {tmp}")

    # 1. --check：模板 profile 通过；错误 profile 逐项报错
    _SECTION_DEPS = {2: set(), 3: {2}, 4: {3}, 5: set(), 6: set(), 7: {6},
                     8: {6}, 9: {6, 8}, 10: {2, 3, 6, 8, 9}, 12: set()}
    _SECTION_DEPS[8].add(2)  # §8 用 sd 的 fault-profile.yaml 测 --profile
    _SECTION_DEPS[13] = set()
    _want = set(range(0, 14))
    if args.only:
        _want, _stack = set(), [int(x) for x in args.only.split(",")]
        while _stack:
            _n = _stack.pop()
            if _n not in _want:
                _want.add(_n)
                _stack += list(_SECTION_DEPS.get(_n, set()))
    sd = ev_path = ev4 = fmt_dir = fmt_work = dcm_zip = fm_zip = fmt_rules = clock_json = mani = sdir = twodir = ev_ms = ev_cf = cmj = None

    def _s0():
        print("\n[0] 脚本单一源 / 模板干净 / 规范校验 / 环境自检")
        tdir = ROOT / "assets" / "templates" / "child-skill" / "scripts"
        check(not tdir.exists(), "模板不含 scripts 副本（脚本唯一源在 builder scripts/，生成时复制）")
        for name in ("unpack_logs.py", "search_evidence.py", "parse_clock.py", "scan_rule_candidates.py", "log_format.py",
                     "gen_specialized.py", "fm_alarm_align.py", "run_analysis.py", "parse_cloudmat.py", "render_report.py"):
            check((SCRIPTS / name).exists(), f"builder scripts/{name} 存在（分发源）")
        junk = [p for p in (ROOT / "assets" / "templates").rglob("*") if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")]
        check(not junk, f"模板目录无 __pycache__/pyc（{len(junk)} 个）")
        try:
            import yaml as _y
            cr = _y.safe_load((ROOT / "assets" / "shared" / "common-rules.yaml").read_text(encoding="utf-8"))
            check(isinstance(cr, dict) and "rules" in cr,
                  "shared/common-rules.yaml 存在且含 rules（规则/配置统一 yaml）")
        except ImportError:
            print("  [skip] 无 PyYAML，跳过 common-rules 校验")
        r = run([SCRIPTS / "validate_skill_md.py", ROOT])
        check(r.returncode == 0, "主 SKILL.md 符合 Agent Skills 规范")
        r = run([SCRIPTS / "check_env.py", "--json", tmp / "env.json"])
        check(r.returncode == 0 and (tmp / "env.json").exists(), "check_env.py 自检通过并输出 JSON")
        # 规范校验器反例：非法 name / 超长 description / 未知字段
        bad = tmp / "bad-skill-name"
        bad.mkdir(exist_ok=True)
        (bad / "SKILL.md").write_text("---\nname: Bad--Name-\ndescription: " + "x" * 1100 + "\nfoo: 1\n---\n# t\n", encoding="utf-8")
        r = run([SCRIPTS / "validate_skill_md.py", bad], expect_rc=1)
        check(r.returncode == 1 and "name 非法" in r.stdout and "超长" in r.stdout and "未知字段" in r.stdout,
              "validate_skill_md 报出非法 name/超长 description/未知字段")
    if 0 in _want: _s0()

    def _s1():
        print("\n[1] profile 校验")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", TEMPLATE_PROFILE, "--check"])
        check(r.returncode == 0 and "校验通过" in r.stdout, "模板 profile --check 通过")
        # 分诊型子 skill（mode: "triage"）允许无 analysis.steps / root_causes
        triage_prof = {
            "skill": {"name": "bbu-fault-some-triage", "title": "通用分诊"},
            "fault": {"name": "通用分诊", "aliases": ["排查"], "alarm_codes": []},
            "symptoms": {"description_keywords": ["排查"]},
            "mode": "triage",
            "components": {"unpack": True},
        }
        triage_path = tmp / "triage-profile.yaml"
        triage_path.write_text(json.dumps(triage_prof, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", triage_path, "--check"])
        check(r.returncode == 0 and "校验通过" in r.stdout,
              "分诊型 profile（mode: triage）允许无 steps/root_causes，--check 通过")
        bad = json.loads(json.dumps(_load_profile_json()))
        bad["root_causes"][0]["condition"] = "E1 and E99"
        bad["root_causes"][0]["confidence"] = "sure"
        bad["analysis"]["steps"][0]["decision"]["on_hit"]["goto"] = "S9"
        bad["analysis"]["steps"][0]["rules"][0]["all_of"] = ["(unclosed"]
        bad["analysis"]["steps"][1]["rules"][0]["verified"] = False
        bad["temporal"].append({"id": "T1", "first": "E1", "second": "E1"})
        bad["components"] = {"foo": True}
        bad_path = tmp / "bad-profile.yaml"
        bad_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", bad_path, "--check"], expect_rc=1)
        for token in ("E99", "confidence 非法", "goto 引用不存在: S9", "正则无法编译", "verified: false", "时序约束 ID 重复: T1", "first 与 second 相同", "components 未知键: foo"):
            check(token in r.stderr, f"--check 报出: {token}")

        # 新字段负例：execution_mode 非法、quick_signatures 空 any_of、category 空串
        bad2 = json.loads(json.dumps(_load_profile_json()))
        bad2["skill"]["execution_mode"] = "hybrid"
        bad2["category"] = ""
        bad2["quick_signatures"] = [
            {"id": "Q1", "file_hints": [".log"], "any_of": ["x"]},
            {"id": "Q1", "file_hints": [".log"], "any_of": []},
        ]
        bad2_path = tmp / "bad2-profile.yaml"
        bad2_path.write_text(json.dumps(bad2, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", bad2_path, "--check"], expect_rc=1)
        for token in ("execution_mode 非法", "category 为空", "quick_signatures id 重复", "缺少 any_of"):
            check(token in r.stderr, f"--check 报出新字段问题: {token}")

        # 2. 脚手架
    if 1 in _want: _s1()

    def _s2():
        nonlocal sd
        print("\n[2] 脚手架生成")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", TEMPLATE_PROFILE, "--path", skills_dir])
        check(r.returncode == 0, "scaffold 成功")
        sd = skills_dir / SLUG
        for rel in ("SKILL.md", "assets/evidence-rules.yaml", "assets/fault-profile.yaml", "scripts/search_evidence.py",
                    "scripts/render_report.py", "scripts/unpack_logs.py", "scripts/parse_cloudmat.py", "references/fault-profile.md"):
            check((sd / rel).exists(), f"产物存在: {rel}")
        check(not (sd / "assets" / "evidence-rules.json").exists() and not (sd / "assets" / "fault-profile.json").exists(),
              "仅 yaml（子 skill 不含 .json 副本）")
        check(not (sd / "scripts" / "parse_clock.py").exists(), "非时钟类未复制 parse_clock.py")
        skill_md = (sd / "SKILL.md").read_text(encoding="utf-8")
        check("{{" not in skill_md, "SKILL.md 无模板残留")
        check("run_analysis.py" in skill_md and "verdict" in skill_md, "SKILL.md 含一键运行与 verdict 约束")
        check("交付门禁" in skill_md and "一键分析" in skill_md, "SKILL.md 含自动门禁与一键入口")
        for token in ("不读脚本源码", "evidence.brief.md", "run-summary.json",
                      "--specialized", "故障原因", "证明依据", "解决办法"):
            check(token in skill_md, f"SKILL.md 含: {token}")
        check(not list(sd.rglob("__pycache__")) and not list(sd.rglob("*.pyc")), "子 Skill 无 __pycache__/pyc")
        r = run([SCRIPTS / "validate_skill_md.py", sd])
        check(r.returncode == 0, "子 Skill frontmatter 符合 Agent Skills 规范（仅 name/description）")
        check("compatibility:" in skill_md and "metadata:" in skill_md,
              "frontmatter 含 compatibility/metadata（通用技能契约）")
        # 别名含 YAML 特殊字符（: # "）时 frontmatter 仍可解析
        spicy = json.loads(json.dumps(_load_profile_json()))
        spicy["skill"]["name"] = "bbu-fault-spicy"
        spicy["fault"]["aliases"] = ['告警: "1588" #时钟', "a: b"]
        spicy["fault"]["alarm_codes"] = [{"code": "198096831", "name": "1588时钟源异常"}]
        sp = tmp / "spicy.yaml"
        sp.write_text(json.dumps(spicy, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", sp, "--path", skills_dir])
        r2 = run([SCRIPTS / "validate_skill_md.py", skills_dir / "bbu-fault-spicy"])
        spicy_md = (skills_dir / "bbu-fault-spicy" / "SKILL.md").read_text(encoding="utf-8") if r.returncode == 0 else ""
        check(r.returncode == 0 and r2.returncode == 0 and "198096831" in spicy_md,
              "别名含 : # \" 的 profile 生成的 frontmatter 可解析，description 含告警码锚点")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", sp, "--check"])
        spicy["skill"]["name"] = "bbu-fault--bad-"
        sp.write_text(json.dumps(spicy, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", sp, "--check"], expect_rc=1)
        check("skill.name 非法" in r.stderr, "--check 拒绝连续/尾部连字符的 skill.name")
        import yaml as _y
        rules_doc = _y.safe_load((sd / "assets" / "evidence-rules.yaml").read_text(encoding="utf-8"))
        check([rc["id"] for rc in rules_doc.get("root_causes", [])] == ["R1", "R2"], "evidence-rules.yaml 含 root_causes")
        # --update 以子 skill 自身 profile 为输入（历史 SameFileError）；无实际变更 → 幂等短路
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", sd / "assets" / "fault-profile.yaml", "--path", skills_dir, "--update"])
        check(r.returncode == 0 and "已是最新" in r.stdout,
              "--update 以自身 profile 为输入且无变更 → 幂等短路")
        r = run([SCRIPTS / "scaffold_child_skill.py", sd.name, "--path", skills_dir, "--update"])
        check(r.returncode == 0 and "已是最新" in r.stdout,
              "--update 省略 --profile 时复用子 skill 内 profile，无变更同样短路")

        # 版本戳：全新即第 1 代；无变更 update 不递增；inspect 同步；本地改动=rc1；缺失=rc2
        stamp_file = sd / "assets" / "toolkit-version.json"
        check(stamp_file.exists(), "版本戳 toolkit-version.json 已写入")
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
        check(stamp["generation"] == 1 and stamp["digest"].startswith("tk-")
              and stamp["files"].get("scripts/search_evidence.py"),
              "版本戳: 全新生成=第1代，幂等 update 不递增 + 受管文件哈希")
        skill_md2 = (sd / "SKILL.md").read_text(encoding="utf-8")
        check(stamp["digest"] in skill_md2 and "diagnostic-skill-builder v" in skill_md2,
              "SKILL.md 尾部含版本戳注释")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--inspect", sd])
        check(r.returncode == 0 and "同步" in r.stdout, "--inspect 全新子 skill 判定同步（rc=0）")
        (sd / "scripts" / "search_evidence.py").write_text("# tampered\n", encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--inspect", sd])
        check(r.returncode == 1 and "local-modified" in r.stdout,
              "--inspect 本地改动检出（rc=1 可交付）")
        (sd / "scripts" / "search_evidence.py").unlink()
        r = run([SCRIPTS / "scaffold_child_skill.py", "--inspect", sd])
        check(r.returncode == 2 and "缺失" in r.stdout, "--inspect 检出缺失（rc=2）")
        shutil.copy2(SCRIPTS / "search_evidence.py", sd / "scripts" / "search_evidence.py")

        # --adopt-extra-scripts：非受管脚本一键写入 profile.components.extra_scripts
        (sd / "scripts" / "my_special_tool.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--inspect", sd, "--adopt-extra-scripts"])
        prof_text = (sd / "assets" / "fault-profile.yaml").read_text(encoding="utf-8")
        check("my_special_tool.py" in r.stdout and "my_special_tool.py" in prof_text,
              "--adopt-extra-scripts 把非受管脚本写入 components.extra_scripts")
        # 采纳后需 update 才能纳入版本戳；update 后 inspect 不再报不受管且应同步（仅 profile 变化，无其他本地改动）
        r = run([SCRIPTS / "scaffold_child_skill.py", sd.name, "--path", skills_dir, "--update"])
        check(r.returncode == 0, "采纳后 --update 成功")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--inspect", sd])
        check(not any("my_special_tool" in ln and "不受管" in ln for ln in r.stdout.splitlines()),
              "采纳后 --inspect 不再把该脚本报为不受管")

        # 冲突保护：已存在目录无 --force/--update 报错；--force 覆盖重建
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", TEMPLATE_PROFILE,
                 "--path", skills_dir], expect_rc=None)
        check(r.returncode != 0, "目标已存在且无 --force/--update 时报错")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", TEMPLATE_PROFILE,
                 "--path", skills_dir, "--force"])
        check(r.returncode == 0 and (sd / "SKILL.md").exists(), "--force 覆盖重建")

        # --update 保留用户定制报告模板
        rt = sd / "assets" / "report_template.md"
        rt.write_text(rt.read_text(encoding="utf-8") + "\n<!-- 本地定制 -->\n",
                      encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile",
                 sd / "assets" / "fault-profile.yaml", "--path", skills_dir, "--update"])
        check(r.returncode == 0 and "本地定制" in rt.read_text(encoding="utf-8"),
              "--update 保留定制报告模板")

        # 组件移除：显式关 parse_cloudmat → update 删脚本；原 profile 恢复
        import yaml as _y
        profj = _y.safe_load((sd / "assets" / "fault-profile.yaml").read_text(encoding="utf-8"))
        profj.setdefault("components", {})["parse_cloudmat"] = False
        profj_path = tmp / "prof_nocm.yaml"
        profj_path.write_text(json.dumps(profj, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", profj_path,
                 "--path", skills_dir, "--update"])
        check(r.returncode == 0 and not (sd / "scripts" / "parse_cloudmat.py").exists(),
              "--update 组件移除 → 脚本删除")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", TEMPLATE_PROFILE,
                 "--path", skills_dir, "--update"])
        check(r.returncode == 0 and (sd / "scripts" / "parse_cloudmat.py").exists(),
              "--update 恢复原 profile → 组件脚本回装")
        # 恢复后再跑一次同样的 update → 幂等短路，generation 不递增
        gen_before = json.loads((sd / "assets" / "toolkit-version.json").read_text(encoding="utf-8"))["generation"]
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", TEMPLATE_PROFILE,
                 "--path", skills_dir, "--update"])
        gen_after = json.loads((sd / "assets" / "toolkit-version.json").read_text(encoding="utf-8"))["generation"]
        check(r.returncode == 0 and "已是最新" in r.stdout and gen_after == gen_before,
              "无变更重复 --update 幂等短路、generation 保持不变")

        # 报告模板漂移：--inspect rc=1 且 --diff 给出行级差异
        r = run([SCRIPTS / "scaffold_child_skill.py", "--inspect", sd, "--diff", "assets/report_template.md"])
        check(r.returncode == 1 and "tpl-drift" in r.stdout and "本地定制" in r.stdout
              and "diff assets/report_template.md" in r.stdout,
              "--inspect 报告模板漂移（rc=1）+ --diff 行级差异")

        # stale 检出：戳中哈希与 builder 源不同（磁盘未动 → stale 而非 local-modified）
        fake = tmp / "fakeskill"
        (fake / "scripts").mkdir(parents=True)
        (fake / "assets").mkdir(parents=True)
        old_content = b"# old builder copy\n"
        (fake / "scripts" / "unpack_logs.py").write_bytes(old_content)
        import hashlib
        fake_stamp = {"builder": "bbu-fault-skill-builder", "version": "1.0.0",
                      "digest": "tk-old", "generation": 1,
                      "files": {"scripts/unpack_logs.py":
                                hashlib.sha256(old_content).hexdigest()}}
        (fake / "assets" / "toolkit-version.json").write_text(
            json.dumps(fake_stamp), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--inspect", fake], expect_rc=None)
        check(r.returncode == 2 and "stale" in r.stdout, "--inspect 检出 stale（rc=2）")

        # 时钟域 profile → 组件推断复制 parse_clock + clk 字典
        clock_prof = json.loads(json.dumps(_load_profile_json()))
        clock_prof["skill"]["name"] = "bbu-fault-clk-demo"
        clock_prof["skill"]["title"] = "时钟演示"
        clock_prof["fault"]["name"] += "（clockstatuslog）"
        cp_path = tmp / "clock-profile.yaml"
        cp_path.write_text(json.dumps(clock_prof, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", cp_path,
                 "--path", skills_dir])
        cd = skills_dir / "bbu-fault-clk-demo"
        check(r.returncode == 0 and (cd / "scripts" / "parse_clock.py").exists()
              and (cd / "assets" / "clk-field-dictionary.yaml").exists(),
              "时钟域 profile → parse_clock + clk_dict 组件")

        # quick-signatures 产物：默认模板 profile 生成 quick-signatures.yaml，含 Q1 与 category
        qs = _y.safe_load((sd / "assets" / "quick-signatures.yaml").read_text(encoding="utf-8"))
        check([q["id"] for q in qs.get("quick_signatures", [])] == ["Q1", "Q2"]
              and qs.get("category") == "工装拷机类",
              "quick-signatures.yaml 含浅度特征与 category")
        # agent 模式：execution_mode=agent 时 SKILL.md 含 agent 编排块，script 模式不含
        agent_prof = json.loads(json.dumps(_load_profile_json()))
        agent_prof["skill"]["name"] = "bbu-fault-agent-demo"
        agent_prof["skill"]["title"] = "复杂故障演示"
        agent_prof["skill"]["execution_mode"] = "agent"
        ap_path = tmp / "agent-profile.yaml"
        ap_path.write_text(json.dumps(agent_prof, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", ap_path,
                 "--path", skills_dir])
        agent_md = (skills_dir / "bbu-fault-agent-demo" / "SKILL.md").read_text(
            encoding="utf-8") if r.returncode == 0 else ""
        check("agent 编排模式" in agent_md, "execution_mode=agent 渲染 agent 编排块")

        # 3. 合成案例 + 回归
    if 2 in _want: _s2()

    def _s3():
        nonlocal ev_path, ev4
        print("\n[3] 合成案例回归")
        for cid, spec in CASES.items():
            make_case(cases_dir, cid, spec)
        report = tmp / "regression.json"
        r = run([SCRIPTS / "run_regression.py", "--cases-dir", cases_dir, "--skill", SLUG, "--skills-dir", skills_dir,
                 "--workdir", work, "--json", report], expect_rc=None)
        print("\n".join("    " + ln for ln in r.stdout.strip().splitlines()))
        check(r.returncode == 0, "run_regression 全部通过")
        if report.exists():
            rep = json.loads(report.read_text(encoding="utf-8"))
            by_id = {x["id"]: x for x in rep["results"]}
            for cid, spec in CASES.items():
                check(by_id.get(cid, {}).get("actual_root_cause") == spec["expected"]["root_cause"],
                      f"{cid} verdict={spec['expected']['root_cause']}")
        ev_path = work / "case-01-positive" / "evidence.json"
        if ev_path.exists():
            ev = json.loads(ev_path.read_text(encoding="utf-8"))
            v = ev.get("verdict", {})
            check(v.get("status") == "matched" and v.get("primary") == "R1", "正例 verdict.status=matched/R1")
            r1 = next(x for x in v["groups"]["ALL"]["root_causes"] if x["id"] == "R1")
            check(r1["temporal_required"] == ["T1", "T2"] and all(r1["temporal_ok"].values()), "R1 关联时序 T1/T2 全满足")
            check("E1" in ev["hit_rules"] and ev["results"][0].get("min_matches") == 1, "results 含 min_matches")
        ev4 = work / "case-04-incomplete-r2" / "evidence.json"
        if ev4.exists():
            v = json.loads(ev4.read_text(encoding="utf-8"))["verdict"]
            r2 = next(x for x in v["groups"]["ALL"]["root_causes"] if x["id"] == "R2")
            check(r2["temporal_required"] == ["T2"] and r2["established"], "R2 显式 temporal 只要求 T2")
        build_json = tmp / "build-child-result.json"
        r = run([SCRIPTS / "build_child_skill.py", "--profile", TEMPLATE_PROFILE,
                 "--path", skills_dir, "--cases-dir", cases_dir, "--update", "--json", build_json])
        build_result = json.loads(build_json.read_text(encoding="utf-8")) if build_json.exists() else {}
        check(r.returncode == 0 and build_result.get("verified") is True
              and build_result.get("regression", {}).get("passed") == len(CASES),
              "build_child_skill 一键生成 + 规范校验 + 全案例回归")
        check((build_result.get("coverage") or {}).get("ok") is True
              and (build_result.get("e2e") or {}).get("passed", 0) >= 2,
              "构建门禁含根因/规则覆盖率与每根因关键案例端到端报告")
        import yaml as _y
        effective_cases = (_y.safe_load((sd / "assets" / "fault-profile.yaml").read_text(encoding="utf-8"))
                           .get("cases") or [])
        check({c.get("id") for c in effective_cases} == set(CASES),
              "生成 profile 的案例索引由磁盘 case.yaml 重建，不再沿用旧手工清单")

        # 事务化构建：staging 已完成生成，但回归失败时正式目录和本地文件保持不变。
        sentinel = sd / "atomic-sentinel.txt"
        sentinel.write_text("old-install\n", encoding="utf-8")
        broken = json.loads(json.dumps(_load_profile_json()))
        broken["cases"] = []
        broken["analysis"]["steps"][0]["rules"][0]["all_of"] = ["SELFTEST_绝不可能命中_9F27"]
        broken_path = tmp / "broken-regression-profile.yaml"
        broken_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        broken_json = tmp / "broken-build-result.json"
        r = run([SCRIPTS / "build_child_skill.py", "--profile", broken_path,
                 "--path", skills_dir, "--cases-dir", cases_dir, "--update",
                 "--json", broken_json], expect_rc=None)
        broken_result = json.loads(broken_json.read_text(encoding="utf-8")) if broken_json.exists() else {}
        check(r.returncode != 0 and sentinel.read_text(encoding="utf-8") == "old-install\n",
              "回归在 staging 失败后正式子 Skill 不被覆盖")
        check(bool(broken_result.get("logs")) and Path(broken_result["logs"]).is_dir(),
              "构建失败自动保留完整阶段日志")
        sentinel.unlink()

        # 4. 报告渲染
    if 3 in _want: _s3()

    def _s4():
        print("\n[4] 报告渲染")
        if ev_path.exists():
            md, html_out = tmp / "report.md", tmp / "report.html"
            r = run([sd / "scripts" / "render_report.py", "--evidence", ev_path, "--md", md, "--html", html_out,
                     "--manifest", work / "case-01-positive" / "unpacked" / "manifest.json",
                     "--description", "VSWc2 返修拷机失败，相位超差", "--source", "case.zip"])
            check(r.returncode == 0, "render_report 成功")
            if md.exists():
                text = md.read_text(encoding="utf-8")
                for sec in ("## 1 结果统计", "## 2 网元详情", "#### 2.1.1 故障根因",
                            "#### 2.1.2 处理建议", "#### 2.1.3 故障证据", "## 3 故障现象与甄别"):
                    check(sec in text, f"MD 含节: {sec}")
                check("总网元数量" in text and "正常站点数量" in text and "故障站点数量" in text,
                      "结果统计含总网元/正常/故障计数")
                check("**判定状态**" in text, "顶部含「判定状态」行")
                check("R1" in text and "1346.0" in text and "{{" not in text, "MD 含根因/证据原文且无模板残留")
                check("<!-- TODO" in text, "MD 含 TODO 标记供人工补全")
                # 旧八节/时间轴结构已移除
                for gone in ("## 一、结论", "## 二、处理方法", "### 时间轴",
                             "### 根因判定表达式", "<!-- TIMELINE -->", "**是否存在故障**"):
                    check(gone not in text, f"旧结构已移除: {gone}")
            if html_out.exists():
                h = html_out.read_text(encoding="utf-8")
                check("<table>" in h and "1346.0" in h, "HTML 含表格/证据")
                check('class="todo"' in h and "<nav" in h and 'id="s1"' in h,
                      "HTML TODO 可见化 + 目录锚点")
                check('id="21-' in h and '<a href="#21-' in h, "HTML 含网元详情锚点与汇总表跳转链接（GitHub 规则锚点）")
            text = md.read_text(encoding="utf-8") if md.exists() else ""
            check("附：执行与数据完整性" not in text and "局限与未验证项" not in text
                  and "检索覆盖" not in text,
                  "专业附录（数据完整性/局限/检索覆盖）已移除")

            # 交付门禁 --verify：草稿（有 TODO）失败 → 补全 TODO + --from-md 重渲 → 通过 → 篡改证据行 → 失败
            rr = sd / "scripts" / "render_report.py"
            check("<!-- ev:EV4:" in text and "<!-- ev:TL:" not in text, "MD 证据行带指纹注释（已无时间轴指纹）")
            h0 = html_out.read_text(encoding="utf-8") if html_out.exists() else ""
            check("<!-- ev:" not in h0, "HTML 已剥除指纹注释")
            r = run([rr, "--verify", md, "--evidence", ev_path, "--html", html_out], expect_rc=2)
            check(r.returncode == 2 and "TODO 已清零：残留" in r.stdout, "--verify 草稿（含 TODO）退出码 2")
            import re as _re
            filled = _re.sub(r"<!-- TODO:.*?-->", "已补全", text)
            md_ok = tmp / "report_ok.md"
            md_ok.write_text(filled, encoding="utf-8")
            html_ok = tmp / "report_ok.html"
            r = run([rr, "--from-md", md_ok, "--html", html_ok, "--evidence", ev_path])
            check(r.returncode == 0 and html_ok.exists() and 'class="todo"' not in html_ok.read_text(encoding="utf-8"),
                  "--from-md 由补全后的 MD 重渲 HTML（无 TODO 高亮）")
            r = run([rr, "--verify", md_ok, "--evidence", ev_path, "--html", html_ok])
            check(r.returncode == 0 and "可交付" in r.stdout, "--verify 补全后 MD+HTML 通过（退出码 0）")
            lines = filled.split("\n")
            idx = next(i for i, ln in enumerate(lines) if "<!-- ev:EV4:" in ln)
            lines[idx] = _re.sub(r"\| (\d+) \|", r"| 9\1 |", lines[idx], count=1)
            md_bad = tmp / "report_tampered.md"
            md_bad.write_text("\n".join(lines), encoding="utf-8")
            r = run([rr, "--verify", md_bad, "--evidence", ev_path], expect_rc=2)
            check(r.returncode == 2 and "被改动行" in r.stdout, "--verify 检出被篡改的证据行号（退出码 2）")
            # 不传 --out/--md 时默认输出到 --evidence 所在目录 report.md/report.html
            ev_dir = tmp / "ev_default"
            ev_dir.mkdir(exist_ok=True)
            ev_path_cp = ev_dir / "evidence.json"
            ev_path_cp.write_text(Path(ev_path).read_text(encoding="utf-8"), encoding="utf-8")
            r = run([rr, "--evidence", str(ev_path_cp)])
            check(r.returncode == 0 and (ev_dir / "report.md").exists() and (ev_dir / "report.html").exists(),
                  "未传 --out/--md 时默认输出到 evidence 所在目录（report.md + report.html）")
            final_md = tmp / "report_final.md"
            r = run([rr, "--final", "--evidence", ev_path, "--md", final_md,
                     "--description", "VSWc2 返修拷机失败，相位超差"])
            final_text = final_md.read_text(encoding="utf-8") if final_md.exists() else ""
            check(r.returncode == 0 and "<!-- TODO" not in final_text
                  and "render_report.py 依据" not in final_text
                  and "根因详述" in final_text and "处理建议" in final_text,
                  "--final 直接生成无 TODO、无运行过程叙述的用户报告")
            r = run([rr, "--verify", final_md, "--evidence", ev_path])
            check(r.returncode == 0, "--final 报告无需人工补写即可通过门禁")
            run_out = tmp / "one-command"
            positive_zip = cases_dir / SLUG / "case-01-positive" / "input" / "case.zip"
            r = run([sd / "scripts" / "run_analysis.py", positive_zip,
                     "--description", "VSWc2 返修拷机失败，相位超差", "--out", run_out])
            run_summary = json.loads((run_out / "run-summary.json").read_text(encoding="utf-8")) \
                if (run_out / "run-summary.json").exists() else {}
            check(r.returncode == 0 and run_summary.get("ok") is True
                  and run_summary.get("primary") == "R1"
                  and (run_out / "report.md").exists()
                  and "<!-- TODO" not in (run_out / "report.md").read_text(encoding="utf-8"),
                  "run_analysis 一条命令完成解压、检索、报告与门禁")
            missing_out = tmp / "one-command-missing-input"
            r = run([sd / "scripts" / "run_analysis.py", tmp / "不存在-9F27.zip",
                     "--description", "缺失输入", "--out", missing_out], expect_rc=None)
            error_summary = json.loads((missing_out / "run-summary.json").read_text(encoding="utf-8")) \
                if (missing_out / "run-summary.json").exists() else {}
            check(r.returncode == 2 and error_summary.get("ok") is False
                  and "Traceback" not in r.stderr and "输入不存在" in error_summary.get("error", ""),
                  "run_analysis 早期输入错误返回结构化摘要，不抛 logs 未初始化二次异常")
            r = run([sd / "scripts" / "run_analysis.py", positive_zip,
                     "--description", "VSWc2 返修拷机失败，相位超差"])
            default_out = positive_zip.parent / (positive_zip.stem + ".analysis")
            ds = json.loads((default_out / "run-summary.json").read_text(encoding="utf-8")) \
                if (default_out / "run-summary.json").exists() else {}
            check(r.returncode == 0 and ds.get("ok") is True
                  and str(default_out.resolve()) != str(ROOT.resolve())
                  and str(sd.resolve()) not in str(default_out.resolve()),
                  "run_analysis 省略 --out 时默认输出到输入同级 <输入名>.analysis，不进 skill 目录")
            r = run([sd / "scripts" / "run_analysis.py", positive_zip,
                     "--description", "VSWc2 返修拷机失败，相位超差", "--out", run_out])
            check(r.returncode == 0, "run_analysis 同输出目录重复执行幂等")
            run_summary2 = json.loads((run_out / "run-summary.json").read_text(encoding="utf-8"))
            check(run_summary2.get("cached") is True,
                  "run_analysis 输入未变化时命中增量缓存（cached=true，unpack/search 不重跑）")
            r = run([sd / "scripts" / "run_analysis.py", positive_zip,
                     "--description", "VSWc2 返修拷机失败，相位超差", "--out", run_out, "--no-cache"])
            run_summary3 = json.loads((run_out / "run-summary.json").read_text(encoding="utf-8"))
            check(r.returncode == 0 and run_summary3.get("cached") is False,
                  "run_analysis --no-cache 强制全量重跑（cached=false）")
            # 显式裁掉 unpack 的纯单日志子 Skill：一键入口不得再依赖 unpack_logs.py。
            single_profile = _load_profile_json()
            single_profile["skill"]["name"] = "bbu-fault-cloudmat-single"
            single_profile["components"] = {"unpack": False}
            single_profile_path = tmp / "cloudmat-single-profile.yaml"
            single_profile_path.write_text(json.dumps(single_profile, ensure_ascii=False), encoding="utf-8")
            r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", single_profile_path,
                     "--path", skills_dir])
            single_sd = skills_dir / "bbu-fault-cloudmat-single"
            single_log = next((work / "case-01-positive" / "unpacked").rglob("*.log"), None)
            single_out = tmp / "one-command-no-unpack"
            if r.returncode == 0 and single_log:
                r = run([single_sd / "scripts" / "run_analysis.py", single_log,
                         "--description", "单日志分析", "--out", single_out])
            single_summary = json.loads((single_out / "run-summary.json").read_text(encoding="utf-8")) \
                if (single_out / "run-summary.json").exists() else {}
            check(r.returncode == 0 and single_summary.get("ok") is True
                  and not (single_sd / "scripts" / "unpack_logs.py").exists(),
                  "run_analysis 支持无 unpack 组件的纯单日志子 Skill")
            # v2.9.1: publish --dry-run 预览 build/package/sync 动作；package --extra 附 CHANGELOG
            pub_profile = _load_profile_json()
            pub_profile["skill"]["name"] = "bbu-fault-pub-skill"
            pub_profile_path = tmp / "pub-profile.yaml"
            pub_profile_path.write_text(json.dumps(pub_profile, ensure_ascii=False), encoding="utf-8")
            pub_skills = tmp / "pub-skills"
            pub_skills.mkdir(exist_ok=True)
            pub_cases = tmp / "pub-cases"
            pub_cases.mkdir(exist_ok=True)
            pub_zip = tmp / "pub-dist"
            pub_install = tmp / "pub-install"
            (pub_install).mkdir(exist_ok=True)
            pub_cl = tmp / "CL.md"
            pub_cl.write_text("# changelog\n", encoding="utf-8")
            r = run([SCRIPTS / "publish.py", "--profile", str(pub_profile_path),
                     "--path", str(pub_skills), "--cases-dir", str(pub_cases),
                     "--zip-out", str(pub_zip), "--changelog", str(pub_cl),
                     "--install-dir", str(pub_install), "--dry-run"])
            check(r.returncode == 0 and "dry-run" in r.stdout
                  and "package_skill.py" in r.stdout and "build_child_skill.py" in r.stdout
                  and "sync-install" in r.stdout,
                  "publish --dry-run 预览 build/package/sync-install 动作")

            # 原子安装：新目录切换失败时恢复旧安装位；随后正常切换成功。
            import importlib.util as _ilu
            from unittest import mock as _mock
            spec = _ilu.spec_from_file_location("bbu_publish_under_test", SCRIPTS / "publish.py")
            pub_mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(pub_mod)
            mirror_src, mirror_dst = tmp / "mirror-src", tmp / "mirror-dst"
            mirror_src.mkdir(); mirror_dst.mkdir()
            (mirror_src / "new.txt").write_text("new\n", encoding="utf-8")
            (mirror_dst / "old.txt").write_text("old\n", encoding="utf-8")
            real_replace = pub_mod.os.replace
            def _flaky_replace(a, b):
                if Path(a).name == "new":
                    raise OSError("模拟安装切换失败")
                return real_replace(a, b)
            failed = False
            with _mock.patch.object(pub_mod.os, "replace", side_effect=_flaky_replace):
                try:
                    pub_mod.mirror_dir(mirror_src, mirror_dst)
                except OSError:
                    failed = True
            check(failed and (mirror_dst / "old.txt").exists()
                  and not (mirror_dst / "new.txt").exists(),
                  "publish 安装位切换失败自动恢复旧版本")
            pub_mod.mirror_dir(mirror_src, mirror_dst)
            check((mirror_dst / "new.txt").exists() and not (mirror_dst / "old.txt").exists(),
                  "publish 安装位验证后原子切换成功")

            # 真实 publish：构建/覆盖率/端到端/打包/安装位/完整日志一次走通。
            real_pub_root = tmp / "real-pub-skills"
            real_pub_install = tmp / "real-pub-install"
            real_pub_logs = tmp / "real-pub-logs"
            real_pub_json = tmp / "real-pub-result.json"
            r = run([SCRIPTS / "publish.py", "--profile", TEMPLATE_PROFILE,
                     "--path", real_pub_root, "--cases-dir", cases_dir,
                     "--zip-out", tmp / "real-pub-dist", "--install-dir", real_pub_install,
                     "--log-dir", real_pub_logs, "--json", real_pub_json], expect_rc=None)
            real_pub = json.loads(real_pub_json.read_text(encoding="utf-8")) if real_pub_json.exists() else {}
            check(r.returncode == 0 and real_pub.get("ok") is True
                  and Path(real_pub.get("zip", "")).is_file()
                  and (real_pub_install / SLUG / "SKILL.md").is_file(),
                  "publish 真实闭环：验证后构建、打包并原子同步安装位")
            check((real_pub_logs / "build-logs" / "scaffold.out").is_file()
                  and (real_pub_logs / "package.out").is_file(),
                  "publish --log-dir 保留 build 与 package 完整阶段日志")
            mini = tmp / "mini-skill"
            mini.mkdir(exist_ok=True)
            (mini / "SKILL.md").write_text("---\nname: mini\n---\n# mini\n", encoding="utf-8")
            (mini / "a.txt").write_text("x\n", encoding="utf-8")
            r = run([SCRIPTS / "package_skill.py", "--skill", str(mini),
                     "--out", str(pub_zip), "--name", "mini.zip",
                     "--extra", f"{pub_cl}:CHANGELOG.md"])
            import zipfile
            with zipfile.ZipFile(pub_zip / "mini.zip") as zf:
                names = zf.namelist()
            check(r.returncode == 0 and any(n.endswith("CHANGELOG.md") for n in names)
                  and any(n.endswith("a.txt") for n in names),
                  "package_skill --extra 将附加文件写入 zip（不落盘技能目录）")
            wrong = filled.replace("- **根因**：R1", "- **根因**：R2", 1)
            md_wrong = tmp / "report_wrong_rc.md"
            md_wrong.write_text(wrong, encoding="utf-8")
            r = run([rr, "--verify", md_wrong, "--evidence", ev_path], expect_rc=2)
            check(r.returncode == 2 and "首选根因与 verdict 一致" in r.stdout and "FAIL" in r.stdout,
                  "--verify 检出结论与 verdict 不一致")

            # --specialized 专项复核：verdict=matched/R1 但专项判定正常 → verify 以专项为准
            spec = tmp / "spec.json"
            spec.write_text(json.dumps({"primary": None, "status": "unmatched",
                                        "note": "专项脚本判定正常"}), encoding="utf-8")
            md_sp = tmp / "report_sp.md"
            r = run([rr, "--evidence", ev_path, "--md", md_sp, "--specialized", spec])
            t_sp = md_sp.read_text(encoding="utf-8") if md_sp.exists() else ""
            check(r.returncode == 0 and "专项脚本复核" in t_sp and "候选根因（未归因）" in t_sp,
                  "--specialized 生成复核块与候选根因（未归因）行")
            md_sp_ok = tmp / "report_sp_ok.md"
            md_sp_ok.write_text(_re.sub(r"<!-- TODO:.*?-->", "已补全", t_sp), encoding="utf-8")
            r = run([rr, "--verify", md_sp_ok, "--evidence", ev_path, "--specialized", spec])
            check(r.returncode == 0, "--verify 以 specialized 为基准通过（verdict 冲突豁免）")
            r = run([rr, "--verify", md_sp_ok, "--evidence", ev_path], expect_rc=2)
            check(r.returncode == 2 and "与 verdict 一致" in r.stdout,
                  "无 specialized 时同一报告被 verdict 门禁拦下")

            # M6: manifest 缺版本文件 → 报告自动占位提示（不阻断交付）
            mani_nover = tmp / "manifest_nover.json"
            mani_nover.write_text(json.dumps({
                "mode": "included", "sites": [{"root": "siteA"}],
                "extracted_files": ["siteA/clockstatuslog.txt"]}), encoding="utf-8")
            md_m6 = tmp / "report_m6.md"
            r = run([rr, "--evidence", ev_path, "--manifest", mani_nover, "--md", md_m6])
            t6 = md_m6.read_text(encoding="utf-8") if md_m6.exists() else ""
            check(r.returncode == 0 and "⚠ 版本文件缺失" in t6
                  and "summary.json" in t6 and "版本以目录名推断" in t6,
                  "M6: 缺版本文件自动占位提示（⚠ 版本文件缺失…未确认）")
            mani_ver = tmp / "manifest_ver.json"
            mani_ver.write_text(json.dumps({
                "mode": "included", "sites": [{"root": "siteA"}],
                "extracted_files": ["siteA/summary.json"]}), encoding="utf-8")
            md_m6v = tmp / "report_m6v.md"
            r = run([rr, "--evidence", ev_path, "--manifest", mani_ver, "--md", md_m6v])
            t6v = md_m6v.read_text(encoding="utf-8") if md_m6v.exists() else ""
            check(r.returncode == 0 and "⚠ 版本文件缺失" not in t6v,
                  "M6: 有 summary.json 时不提示缺失")

            # 多站点：结果统计汇总表每个站点一行，且网元名是锚点链接
            ev_ms = json.loads(ev_path.read_text(encoding="utf-8"))
            g_all = ev_ms["verdict"]["groups"]["ALL"]
            ev_ms["verdict"]["groups"] = {
                "ALL": g_all,
                "siteA_1": dict(g_all, status="matched", primary="R1"),
                "siteB_2": dict(g_all, status="unmatched", primary="NONE")}
            ev_ms["verdict"]["grouped_by_site"] = True
            # N3：detected_sites 含一个未命中规则的站点（siteC_3），应被补进统计并标「待人工确认」
            ev_ms["detected_sites"] = ["siteA_1", "siteB_2", "siteC_3"]
            ev_ms_path = tmp / "ev_ms.json"
            ev_ms_path.write_text(json.dumps(ev_ms), encoding="utf-8")
            md_ms = tmp / "report_ms.md"
            r = run([rr, "--evidence", ev_ms_path, "--md", md_ms])
            t_ms = md_ms.read_text(encoding="utf-8") if md_ms.exists() else ""
            n_links = sum(1 for ln in t_ms.split("\n")
                          if "[siteA_1](" in ln or "[siteB_2](" in ln or "[siteC_3](" in ln)
            n_detail = sum(1 for ln in t_ms.split("\n")
                           if ln.startswith("### 2.1 siteA_1") or ln.startswith("### 2.2 siteB_2")
                           or ln.startswith("### 2.3 siteC_3"))
            check(r.returncode == 0 and "结果统计" in t_ms and "网元详情" in t_ms
                  and n_links == 3 and n_detail == 3,
                  "多站点 → 汇总表三行锚点 + 三网元详情小节")
            check("规则未命中（待人工确认）" in t_ms and "待人工确认" in t_ms,
                  "N3: 未命中规则站点补进统计并标「待人工确认」")
            # N4: --fm-align 注入（JSON 自动渲染表格）
            fm_align = tmp / "fm_align.json"
            fm_align.write_text(json.dumps({
                "window_h": 48, "has_evidence": True, "alarms": [
                    {"file": "fm-history.csv", "alarm": {"name": "系统时钟不可用",
                     "code": "198092217", "time": "2026-07-23 02:32:17", "matched": "in_window"}},
                    {"file": "fm-history.csv", "alarm": {"name": "同步丢失",
                     "code": "198096802", "time": "2026-08-25 10:00:00", "matched": "outside_window"}},
                ], "notes": []}), encoding="utf-8")
            md_fa = tmp / "report_fm.md"
            r = run([rr, "--evidence", ev_ms_path, "--md", md_fa, "--fm-align", str(fm_align)])
            t_fa = md_fa.read_text(encoding="utf-8") if md_fa.exists() else ""
            check(r.returncode == 0 and "FM 告警时间窗对齐" in t_fa and "✓ 与证据吻合" in t_fa
                  and "⚠ 偏离证据时间" in t_fa and "系统时钟不可用" in t_fa,
                  "N4: --fm-align 把告警对齐表格注入报告第 3 节")
            # search_evidence --brief 精简摘要
            brief = tmp / "evidence.brief.md"
            r = run([sd / "scripts" / "search_evidence.py", work / "case-01-positive" / "unpacked",
                     "--rules", sd / "assets" / "evidence-rules.yaml", "--json", tmp / "ev_brief.json", "--brief", brief])
            bt = brief.read_text(encoding="utf-8") if brief.exists() else ""
            check(r.returncode == 0 and "**verdict**" in bt and "| E4 |" in bt and "1346.0" in bt,
                  "search_evidence --brief 输出 verdict + 逐规则表 + 首条证据")

            # manifest.sites 为 dict 列表时不崩（DCM 类有站点包的真实形态）+ --precheck 接入
            mani_path = work / "case-01-positive" / "unpacked" / "manifest.json"
            mani = json.loads(mani_path.read_text(encoding="utf-8"))
            mani["sites"] = [{"root": "6321_2595975", "features": ["collectinfo.log"], "files": 10}]
            mani2 = tmp / "manifest_sites.json"
            mani2.write_text(json.dumps(mani), encoding="utf-8")
            prec = tmp / "precheck.json"
            prec.write_text(json.dumps({"hit_rules": ["C1"],
                                        "results": [{"rule_id": "C1", "hit": True, "match_count": 3},
                                                    {"rule_id": "C4", "hit": False, "match_count": 0,
                                                     "description": "活动告警存在"}]}), encoding="utf-8")
            r = run([sd / "scripts" / "render_report.py", "--evidence", ev_path, "--md", tmp / "report2.md",
                     "--manifest", mani2, "--precheck", prec])
            check(r.returncode == 0, "manifest 含 dict 站点不崩")
            t2 = (tmp / "report2.md").read_text(encoding="utf-8")
            check("6321_2595975" in t2 and "公共预检" not in t2,
                  "dict 站点名归一化（6321_2595975 作网元名）；预检结果随附录已移除")

            # 轮转文件归一 + 真实命中计数 + 默认不复制源包 + manifest.summary.json
            rot = tmp / "rot_site"
            rot.mkdir(exist_ok=True)
            (rot / "clockstatuslog.txt").write_text(
                "".join(f"2026-10-13 0{i//10}:{i%60:02d}:00 sts=3 normal lock\n" for i in range(60)),
                encoding="utf-8")
            (rot / "clockstatuslog.txt.01").write_text(
                "2026-10-12 23:59:00 sts=3 normal lock\n" * 5, encoding="utf-8")
            rot_rules = tmp / "rot-rules.yaml"
            rot_rules.write_text(json.dumps({"rules": [
                {"id": "E1", "globs": ["**/clockstatuslog*"], "any_of": ["sts=3"],
                 "min_matches": 1}], "root_causes": [
                {"id": "R1", "name": "t", "condition": "E1", "actions": ["a"]}]}),
                encoding="utf-8")
            rot_ev = tmp / "rot_ev.json"
            r = run([sd / "scripts" / "search_evidence.py", rot,
                     "--rules", rot_rules, "--json", rot_ev])
            re_j = json.loads(rot_ev.read_text(encoding="utf-8")) if rot_ev.exists() else {}
            r1 = (re_j.get("results") or [{}])[0]
            check(r.returncode == 0 and not re_j.get("detected_sites")
                  and not (re_j.get("verdict") or {}).get("grouped_by_site"),
                  "轮转文件不产生伪站点（detected_sites 空、grouped_by_site=False）")
            check(r1.get("match_count") == 65 and r1.get("evidence_count") == 50
                  and r1.get("evidence_truncated") is True,
                  "match_count=真实命中总数 65，evidence 收 50 条并标记截断")
            rzip = tmp / "rot.zip"
            with zipfile.ZipFile(rzip, "w") as zf:
                zf.write(rot / "clockstatuslog.txt", "clockstatuslog.txt")
                zf.write(rot / "clockstatuslog.txt.01", "clockstatuslog.txt.01")
                zf.writestr("siteA/clockstatuslog.txt", "x")
                zf.writestr("siteA/fm-active.csv", "y")
            rwork = tmp / "rot_work"
            r = run([sd / "scripts" / "unpack_logs.py", rzip, "-o", rwork])
            check("按需提取" in r.stderr, "全量解压（无 --include-rules）时 stderr 打印按需提取提示")
            rmani = json.loads((rwork / "manifest.json").read_text(encoding="utf-8")) \
                if (rwork / "manifest.json").exists() else {}
            check(r.returncode == 0 and not (rwork / "rot.zip").exists(),
                  "默认不复制源包进 workdir")
            check((rwork / "manifest.summary.json").exists()
                  and json.loads((rwork / "manifest.summary.json").read_text(encoding="utf-8")).get("sites"),
                  "manifest.summary.json 旁路摘要已生成")
            s_sites = json.loads((rwork / "manifest.summary.json").read_text(encoding="utf-8"))["sites"]
            check(len(s_sites) == 1 and "siteA" in str(s_sites[0].get("root")),
                  "detect_sites：轮转文件不计特征，仅 siteA 成双特征站点")
            # ---- C2/C10：多网元外层容器不吞叶子，顶层 fm 包不锚定容器根 ----
            import importlib.util
            _spec = importlib.util.spec_from_file_location(
                "unk_mod_c2", sd / "scripts" / "unpack_logs.py")
            unk_mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(unk_mod)
            c2_rels = []
            for ne in ("6323_2595757", "6321_2595975", "6321_2625542"):
                c2_rels += [
                    f"log.zip_unpacked/DCM_x_{ne}.zip_unpacked/{ne}/BspLog/collectinfo.log",
                    f"log.zip_unpacked/DCM_x_{ne}.zip_unpacked/{ne}/BspLog/BSP_t_unpacked/BSP/clockstatuslog.txt",
                ]
            for fn in ("fm-active-告警监控-x.zip_unpacked/fm-active-告警监控-x.xlsx",
                       "fm-history-所有-y.zip_unpacked/fm-history-所有-y.xlsx"):
                c2_rels.append("log.zip_unpacked/" + fn)
            c2_sites, _ = unk_mod.detect_sites(c2_rels)
            c2_roots = [v["root"].split("/")[-1] for v in c2_sites.values()]
            check(len(c2_sites) == 3
                  and all(ne in c2_roots for ne in
                          ("6323_2595757", "6321_2595975", "6321_2625542"))
                  and "log.zip_unpacked" not in c2_roots,
                  "C2: 多网元识别为 3 个叶子网元根，容器不吞并、fm 包不锚定")
            rwork2 = tmp / "rot_work2"
            r = run([sd / "scripts" / "unpack_logs.py", rzip, "-o", rwork2, "--copy-source"])
            check(r.returncode == 0 and (rwork2 / "rot.zip").exists(),
                  "--copy-source 恢复旧行为（源包复制进 workdir）")

        # 5. GBK 管道：模拟 Windows 中文控制台（PYTHONIOENCODING=gbk 且不开 UTF-8 模式）
    if 4 in _want: _s4()

    def _s5():
        print("\n[5] GBK 管道输出")
        env = dict(os.environ, PYTHONIOENCODING="gbk", PYTHONUTF8="0")
        r = subprocess.run([sys.executable, str(SCRIPTS / "scaffold_child_skill.py"), "--profile", str(TEMPLATE_PROFILE), "--check"],
                           capture_output=True, env=env)
        check(r.returncode == 0 and b"Traceback" not in r.stderr, "GBK 管道下 scaffold --check 不崩")

        # 6. 参考格式回归：合成 DCM/FM 包验证格式支持（对齐参考数据集实测结构）
    if 5 in _want: _s5()

    def _s6():
        nonlocal fmt_dir, fmt_work, dcm_zip, fm_zip, fmt_rules, clock_json, mani
        print("\n[6] 参考格式回归（合成 DCM/FM 包）")
        fmt_dir, fmt_work = tmp / "fmt", tmp / "fmt_work"
        fmt_dir.mkdir(exist_ok=True)

        dcm_zip = fmt_dir / "DCM_test_6323_9999999_BspLog.zip"
        _make_dcm_zip(dcm_zip)
        r = run([SCRIPTS / "unpack_logs.py", dcm_zip, "-o", fmt_work / "dcm"])
        check(r.returncode == 0, "DCM 合成包解包")
        mani = json.loads((fmt_work / "dcm" / "manifest.json").read_text(encoding="utf-8"))
        extracted = mani["extracted_files"]
        check(len(mani["sites"]) == 1,
              f"DCM 站点识别==1（BspLog 下嵌套 BSP 不拆伪站点）: {mani['sites']}")
        check(any(x.endswith("BoardInit.log.bak.1.gunzipped") for x in extracted),
              ".bak.N magic-gzip → .gunzipped")
        check(any(x.endswith("bspdata.bin") for x in mani["binary_or_opaque_files"]),
              ".bin 登记 binary_or_opaque")
        check(any(x.endswith("collectinfo.log") for x in extracted), "collectinfo.log 提取")
        check(any(x.endswith("clockstatuslog.txt.01") for x in extracted),
              "tar 内 .gz 成员解压保留轮转名")

        clock_json = fmt_work / "clock.json"
        r = run([SCRIPTS / "parse_clock.py", fmt_work / "dcm", "--merge", "--json", clock_json])
        check(r.returncode == 0, "parse_clock --merge")
        cd = json.loads(clock_json.read_text(encoding="utf-8"))
        names = [f["file"] for f in cd["files"] if f["file"].startswith("<merged:clockstatuslog")]
        check(any("@1-1-3-1" in n for n in names) and any("@1-1-4-1" in n for n in names)
              and any("BSP" in n for n in names), f"clockstatuslog 按槽位/目录分组: {names}")
        slot = next(f for f in cd["files"] if "@1-1-3-1" in f["file"])
        check(slot["total_records"] == 3, "槽位合并条数=轮转文件合计(.01.gz 去重后 3 条)")

        fmt_rules = fmt_work / "rules.yaml"
        fmt_rules.write_text(json.dumps({"rules": [
            {"id": "F1", "type": "text", "globs": ["**/BoardInit.log.bak*"],
             "any_of": ["BspBoardInit"]},
            {"id": "F2", "type": "text", "globs": ["**/collectinfo.log"], "any_of": ["succ"]},
            {"id": "F3", "type": "text", "globs": ["**/misc_status*"],
             "any_of": ["status OK"]},
            {"id": "F4", "type": "text", "globs": ["**/bspdata*"], "any_of": ["xxx"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        ev_json = fmt_work / "ev.json"
        r = run([SCRIPTS / "search_evidence.py", fmt_work / "dcm", "--rules", fmt_rules,
                 "--json", ev_json])
        ev = json.loads(ev_json.read_text(encoding="utf-8"))
        by_id = {x["rule_id"]: x for x in ev["results"]}
        check(by_id["F1"]["files_scanned"] == 1 and by_id["F1"]["match_count"] == 1
              and by_id["F1"]["evidence"][0]["file"].endswith("BoardInit.log.bak.1"),
              "伪装后缀 gzip 可检索且 .gunzipped 同源副本去重（证据指向原件）")
        check(by_id["F2"]["match_count"] >= 1, "collectinfo succ 行命中")
        check(by_id["F3"]["match_count"] >= 1,
              ".dat.gz 等任意后缀 gzip 文本按内容路由可检索")
        f4skip = by_id["F4"].get("skipped_files", [])
        check(by_id["F4"]["match_count"] == 0
              and any(s["file"].endswith("bspdata.bin") and s["kind"] == "binary"
                      for s in f4skip), "命中 glob 的二进制记入 skipped_files")

        # 通用格式盘点（log_format.py）：manifest 字段 + CLI
        kc = mani.get("file_kind_counts", {})
        check(kc.get("text", 0) >= 1 and kc.get("compressed_text", 0) >= 1
              and kc.get("binary", 0) >= 1 and kc.get("ciphered", 0) >= 1
              and kc.get("archive", 0) >= 1,
              f"manifest file_kind_counts 覆盖各类型: {kc}")
        check(any(o["path"].endswith("bspdata.bin") for o in mani.get("opaque_files", [])),
              "opaque_files 含 .bin 明细")
        inv_json = fmt_work / "inv.json"
        r = run([SCRIPTS / "log_format.py", fmt_work / "dcm", "--json", inv_json, "--quiet"])
        check(r.returncode == 0, "log_format 目录盘点")
        inv = json.loads(inv_json.read_text(encoding="utf-8"))
        check(inv["by_kind"].get("compressed_text", 0) >= 2
              and inv["by_kind"].get("ciphered", 0) >= 1,
              f"inventory 分类: {inv['by_kind']}")
        r = run([SCRIPTS / "log_format.py", dcm_zip, "--json", inv_json, "--quiet"])
        inv2 = json.loads(inv_json.read_text(encoding="utf-8"))
        check(r.returncode == 0 and inv2["total_files"] >= 5,
              "log_format zip 直读盘点（不解包）")

        fm_zip = fmt_dir / "fm-active-告警监控-test.zip"
        _make_fm_zip(fm_zip)
        r = run([SCRIPTS / "unpack_logs.py", fm_zip, "-o", fmt_work / "fm"])
        fmani = json.loads((fmt_work / "fm" / "manifest.json").read_text(encoding="utf-8"))
        check(len(fmani["extracted_files"]) == 1
              and fmani["extracted_files"][0].endswith(".xlsx"),
              "xlsx 整文件提取（不展开内部 XML）")
        check(len(fmani["archives_extracted"]) == 1, "xlsx 未登记为已解归档")
        fmt_rules.write_text(json.dumps({"rules": [
            {"id": "X1", "type": "text", "globs": ["**/*.xlsx"], "any_of": ["系统时钟异常"]},
            {"id": "X2", "type": "text", "globs": ["**/fm-*"], "format": "xlsx",
             "columns": ["告警码"], "any_of": ["198092217"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "search_evidence.py", fmt_work / "fm", "--rules", fmt_rules,
                 "--json", ev_json])
        ev = json.loads(ev_json.read_text(encoding="utf-8"))
        by_id = {x["rule_id"]: x for x in ev["results"]}
        check(by_id["X1"]["match_count"] >= 1, "xlsx 行检索命中告警名")
        check(by_id["X2"]["match_count"] == 1
              and by_id["X2"]["evidence"][0]["line"] == "198092217",
              "xlsx columns 列过滤命中告警码")
        check(by_id["X2"]["evidence"][0]["timestamp"] == "2025-05-19 05:08:33 UTC",
              "xlsx 行时间戳提取(+8→UTC)")

        # 7. unpack_logs 场景专项
    if 6 in _want: _s6()

    def _s7():
        print("\n[7] unpack_logs 专项")
        u_dir = fmt_work / "unpack_extra"
        u_dir.mkdir(exist_ok=True)

        r = run([SCRIPTS / "unpack_logs.py", dcm_zip, "--list-only"])
        check(r.returncode == 0 and '"members"' in r.stdout
              and "collectinfo.log" in r.stdout, "--list-only zip 成员清单")
        nested_tar = next((fmt_work / "dcm").rglob("*.tar"), None)
        if check(nested_tar is not None, "DCM 内含嵌套 tar"):
            r = run([SCRIPTS / "unpack_logs.py", nested_tar, "--list-only"])
            check(r.returncode == 0 and "clockstatuslog" in r.stdout, "--list-only tar")

        sel_dir = u_dir / "sel"
        r = run([SCRIPTS / "unpack_logs.py", dcm_zip, "-o", sel_dir,
                 "--include-glob", "**/collectinfo.log"])
        sm = json.loads((sel_dir / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and sm["mode"] == "selective"
              and any(x.endswith("collectinfo.log") for x in sm["extracted_files"])
              and len(sm["not_matched_not_extracted"]) > 0,
              "--include-glob 选择性提取 + 未匹配登记")
        sel2 = u_dir / "sel2"
        r = run([SCRIPTS / "unpack_logs.py", dcm_zip, "-o", sel2,
                 "--include-rules", fmt_rules])
        sm2 = json.loads((sel2 / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and sm2["mode"] == "selective"
              and any("BoardInit" in x for x in sm2["extracted_files"]),
              "--include-rules 从规则收集 globs")

        r = run([SCRIPTS / "unpack_logs.py", fmt_work / "dcm", "-o", u_dir / "dmani"])
        dm = json.loads((u_dir / "dmani" / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and dm["mode"] == "pre-unpacked"
              and dm["total_extracted_files"] > 0, "目录输入 → pre-unpacked manifest")
        single = fmt_dir / "one.log"
        single.write_text("2025-05-19 hello\n", encoding="utf-8")
        sdir1 = u_dir / "single"
        r = run([SCRIPTS / "unpack_logs.py", single, "-o", sdir1])
        sm3 = json.loads((sdir1 / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and sm3["total_extracted_files"] == 1
              and sm3["extracted_files"] == ["one.log"], "单非归档文件输入登记")

        evil = fmt_dir / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../evil_st.txt", "escape")
            zf.writestr("safe.txt", "ok")
        edir = u_dir / "evil"
        r = run([SCRIPTS / "unpack_logs.py", evil, "-o", edir])
        em = json.loads((edir / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and not (u_dir / "evil_st.txt").exists()
              and any("unsafe" in str(s.get("reason", "")) for s in em["skipped"]),
              "zip-slip ../ 成员拒绝且记 skipped")
        check((edir / "evil.zip_unpacked" / "safe.txt").exists(), "安全成员正常提取")

        badm = fmt_dir / "badmem.zip"
        with zipfile.ZipFile(badm, "w") as zf:
            zf.writestr("x.log.gz", gzip.compress(b"hi there")[:5])  # 截断 gzip
            zf.writestr("good.log", "fine")
        bdir = u_dir / "badmem"
        r = run([SCRIPTS / "unpack_logs.py", badm, "-o", bdir])
        bm = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and (bm["warnings"] or bm["skipped"]),
              "损坏成员容错（warnings/skipped 非空，整包不中断）")
        check(any(x.endswith("good.log") for x in bm["extracted_files"]),
              "损坏成员不影响其他成员")

        lim = u_dir / "limit"
        r = run([SCRIPTS / "unpack_logs.py", dcm_zip, "-o", lim, "--max-files", "2"])
        lm = json.loads((lim / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and lm["warnings"],
              "--max-files 预算熔断记 warnings")

        gz_f = fmt_dir / "only.log.gz"
        gz_f.write_bytes(gzip.compress(b"log line\n"))
        gz_d = u_dir / "gz"
        r = run([SCRIPTS / "unpack_logs.py", gz_f, "-o", gz_d])
        check(r.returncode == 0 and (gz_d / "only.log").exists()
              and (gz_d / "only.log").read_text().strip() == "log line",
              "独立 .gz 文件解压")
        bz_f = fmt_dir / "b.log.bz2"
        bz_f.write_bytes(bz2.compress(b"bz line\n"))
        bz_d = u_dir / "bz"
        r = run([SCRIPTS / "unpack_logs.py", bz_f, "-o", bz_d])
        check(r.returncode == 0 and (bz_d / "b.log").exists()
              and (bz_d / "b.log").read_text().strip() == "bz line",
              "独立 .bz2 文件解压")

        # 三层嵌套 + --max-depth 1 截断
        inner_z = fmt_dir / "inner.zip"
        with zipfile.ZipFile(inner_z, "w") as zf:
            zf.writestr("deep.log", "deep")
        mid_z = fmt_dir / "mid.zip"
        with zipfile.ZipFile(mid_z, "w") as zf:
            zf.write(inner_z, "inner.zip")
        outer_z = fmt_dir / "outer3.zip"
        with zipfile.ZipFile(outer_z, "w") as zf:
            zf.write(mid_z, "mid.zip")
        dp = u_dir / "depth"
        r = run([SCRIPTS / "unpack_logs.py", outer_z, "-o", dp, "--max-depth", "1"])
        dm2 = json.loads((dp / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and any("递归层数" in w for w in dm2["warnings"]),
              "--max-depth 截断记 warnings")

        fake7z = fmt_dir / "f.7z"
        fake7z.write_bytes(b"\x37\x7a\xbc\xaf\x27\x1cfake7z")
        d7 = u_dir / "z7"
        r = run([SCRIPTS / "unpack_logs.py", fake7z, "-o", d7], expect_rc=None)
        m7 = json.loads((d7 / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and (m7["warnings"] or m7["unsupported_archives"]),
              "7z 无工具/坏包 graceful（warnings/unsupported 非空）")

        _make_gbk_name_zip(fmt_dir / "gbkname.zip", "告警日志.log".encode("gbk"),
                           b"log line\n")
        gd = u_dir / "gbk"
        r = run([SCRIPTS / "unpack_logs.py", fmt_dir / "gbkname.zip", "-o", gd])
        gm = json.loads((gd / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0
              and any("告警日志" in x for x in gm["extracted_files"]),
              f"GBK 成员名修复（{gm['extracted_files']}）")

        xz_f = fmt_dir / "x.log.xz"
        xz_f.write_bytes(lzma.compress(b"xz line\n"))
        xd = u_dir / "xz"
        r = run([SCRIPTS / "unpack_logs.py", xz_f, "-o", xd])
        check(r.returncode == 0 and (xd / "x.log").exists()
              and (xd / "x.log").read_text().strip() == "xz line",
              "独立 .xz 文件解压（lzma）")
        xls_f = fmt_dir / "old.xls"
        xls_f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
        xl_d = u_dir / "xls"
        r = run([SCRIPTS / "unpack_logs.py", xls_f, "-o", xl_d])
        xm = json.loads((xl_d / "manifest.json").read_text(encoding="utf-8"))
        check(r.returncode == 0
              and any(o.get("subtype") == "ole2" for o in xm["opaque_files"]),
              ".xls 按 OLE2 登记 opaque")
        # 长路径防护冒烟（helper 单测，不造 260 字符文件系统对象）
        sys.path.insert(0, str(SCRIPTS))
        import log_format as _lf
        lp = _lf.long_path("D:\\" + "x" * 250 + "\\a.log")
        check(not __import__("os").name == "nt"
              or lp.startswith("\\\\?\\"), "long_path 长路径加 \\\\?\\ 前缀")

        # 8. search_evidence 专项
    if 7 in _want: _s7()

    def _s8():
        nonlocal sdir, twodir, ev_ms, ev_cf
        print("\n[8] search_evidence 专项")
        sdir = fmt_work / "search"
        sdir.mkdir(exist_ok=True)
        (sdir / "trend.log").write_text("\n".join(
            f"2025-05-19 10:0{i}:00 temp={v}" for i, v in enumerate([10, 55, 80, 99]))
            + "\n", encoding="utf-8")
        # F1 回归：abs_threshold 绝对值阈值（含负值失效）运行时用例
        (sdir / "trendabs.log").write_text("\n".join(
            ["2025-05-19 10:00:00 v=-1098.900", "2025-05-19 10:01:00 v=999.999",
             "2025-05-19 10:02:00 v=-1200.500"]) + "\n", encoding="utf-8")
        (sdir / "epoch.log").write_text("1768000000 event start\n", encoding="utf-8")
        (sdir / "assoc.log").write_text(
            "2025-05-19 10:00:00 slot=1-1-3-1 board reset\n", encoding="utf-8")
        (sdir / "case.log").write_text("errornow flag\n", encoding="utf-8")
        (sdir / "gbk.log").write_bytes("复位成功 gbk行\n".encode("gbk"))
        (sdir / "alarm.csv").write_text(
            "告警码,告警名称,时间\n9001,断链告警,2025-05-19 10:00:00\n"
            "9002,正常恢复,2025-05-19 10:01:00\n", encoding="utf-8")
        (sdir / "many.log").write_text("\n".join(
            f"2025-05-19 10:0{i}:00 HITME" for i in range(5)) + "\n", encoding="utf-8")
        (sdir / "conflict.log").write_text(
            "2025-05-19 10:00:00 bbb\n2025-05-19 10:05:00 aaa\n", encoding="utf-8")
        site_file = sdir / "6330_1234567" / "BspLog"
        site_file.mkdir(parents=True)
        (site_file / "sassoc.log").write_text(
            "2025-05-19 10:00:00 board reset\n", encoding="utf-8")
        twodir = fmt_work / "twosite"
        _make_twosite_dir(twodir)

        srules = fmt_work / "search_rules.yaml"
        srules.write_text(json.dumps({"rules": [
            {"id": "N_TREND", "type": "numeric_trend", "globs": ["**/trend.log"],
             "value_pattern": "temp=(\\d+)", "trend": "exceed_threshold",
             "threshold": 50},
            {"id": "N_TREND_HI", "type": "numeric_trend", "globs": ["**/trend.log"],
             "value_pattern": "temp=(\\d+)", "threshold": 500},
            {"id": "N_SUSTAIN", "type": "numeric_trend", "globs": ["**/trend.log"],
             "value_pattern": "temp=(\\d+)", "trend": "sustained_above",
             "threshold": 5, "sustained": 3},
            {"id": "N_ABS", "type": "numeric_trend", "globs": ["**/trendabs.log"],
             "value_pattern": "v=(-?[0-9.]+)", "trend": "exceed_threshold",
             "abs_threshold": 1000, "sustained_count": 1},
            {"id": "N_ABS_HI", "type": "numeric_trend", "globs": ["**/trendabs.log"],
             "value_pattern": "v=(-?[0-9.]+)", "trend": "exceed_threshold",
             "abs_threshold": 2000, "sustained_count": 1},
            {"id": "E_EPOCH", "globs": ["**/epoch.log"], "any_of": ["event start"],
             "time_extract": {"pattern": "(\\d{10})", "group": 1, "epoch": True}},
            {"id": "E_ASSOC", "globs": ["**/assoc.log"], "any_of": ["board reset"],
             "associate_by": {"pattern": "slot=([\\d-]+)"}},
            {"id": "E_SITE", "globs": ["**/sassoc.log"], "any_of": ["board reset"],
             "associate_by": {"site": True}},
            {"id": "E_CS_T", "globs": ["**/case.log"], "any_of": ["ERRORNOW"],
             "case_sensitive": True},
            {"id": "E_CS_F", "globs": ["**/case.log"], "any_of": ["ERRORNOW"]},
            {"id": "E_CSV", "globs": ["**/alarm.csv"], "format": "csv",
             "columns": ["告警名称"], "any_of": ["断链"]},
            {"id": "E_GBK", "globs": ["**/gbk.log"], "any_of": ["复位"],
             "encoding": "gbk"},
            {"id": "E_MANY", "globs": ["**/many.log"], "any_of": ["HITME"]},
            {"id": "E_BADRX", "globs": ["**/assoc.log"], "any_of": ["(unclosed"]},
        ], "root_causes": [{"id": "R1", "condition": "E_MANY"}]},
            ensure_ascii=False), encoding="utf-8")
        sev = fmt_work / "sev.json"
        r = run([SCRIPTS / "search_evidence.py", sdir, "--rules", srules,
                 "--max-evidence-per-rule", "2", "--json", sev])
        check(r.returncode == 0, "专项检索 rc=0")
        ev2 = json.loads(sev.read_text(encoding="utf-8"))
        b2 = {x["rule_id"]: x for x in ev2["results"]}
        check(b2["N_TREND"]["hit"] is True and b2["N_TREND"]["samples"],
              f"numeric_trend exceed_threshold 命中（{b2['N_TREND'].get('trend_detail')}）")
        check(b2["N_TREND_HI"]["hit"] is False, "numeric_trend 阈值未达 → 未命中")
        check(b2["N_SUSTAIN"]["hit"] is True, "numeric_trend sustained_above 连续点数")
        check(b2["N_ABS"]["hit"] is True and "|v| > 1000" in b2["N_ABS"].get("trend_detail", ""),
              f"F1: abs_threshold 绝对值阈值命中（{b2['N_ABS'].get('trend_detail')}）")
        check(b2["N_ABS_HI"]["hit"] is False,
              "F1: abs_threshold 未达 → 未命中（|v| 不超 2000）")
        # F2 回归：export_rules 导出白名单必须透传 abs_threshold（否则 profile 有值、导出后丢失）
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "scaffold_child_skill_abs", SCRIPTS / "scaffold_child_skill.py")
        _scaff = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_scaff)
        _exp = _scaff.export_rules({"analysis": {"steps": [{"id": "S1", "rules": [
            {"id": "E6", "description": "t", "type": "numeric_trend", "globs": ["x"],
             "value_pattern": "v", "trend": "exceed_threshold", "abs_threshold": 1000}]}]}})
        check(_exp[0].get("abs_threshold") == 1000,
              "F2: export_rules 导出白名单保留 abs_threshold")
        te_ts = b2["E_EPOCH"]["evidence"][0]["timestamp"]
        check(te_ts and te_ts.startswith("2026-01"), f"epoch 时间戳→UTC（{te_ts}）")
        check(b2["E_ASSOC"]["evidence"][0]["associate"] == "1-1-3-1",
              "associate_by pattern 分组提取")
        check(b2["E_SITE"]["evidence"][0]["associate"] == "6330_1234567",
              "associate_by site:true 取站点根")
        check(b2["E_CS_T"]["match_count"] == 0 and b2["E_CS_F"]["match_count"] == 1,
              "case_sensitive 开/关语义")
        check(b2["E_CSV"]["match_count"] == 1
              and b2["E_CSV"]["evidence"][0]["line"] == "断链告警",
              "format:csv + columns 列过滤")
        check(b2["E_GBK"]["hit"] is True and "gbk" in b2["E_GBK"]["encodings"],
              "显式 encoding=gbk 解码")
        check(b2["E_MANY"]["match_count"] == 5 and b2["E_MANY"]["evidence_count"] == 2
              and b2["E_MANY"]["evidence_truncated"] is True
              and "E_MANY" in (ev2.get("verdict") or {}).get("truncated", []),
              "--max-evidence-per-rule 截断：match_count 真实总数 + evidence_truncated")
        check(b2["E_BADRX"].get("error") and b2["E_ASSOC"]["hit"] is True,
              "非法正则规则隔离（error 不拖累其他规则）")

        # --local-tz-offset：同一行本地时间在 offset=0 下不换算
        tz_rules = fmt_work / "tz_rules.yaml"
        tz_rules.write_text(json.dumps({"rules": [
            {"id": "E_TZ", "globs": ["**/assoc.log"], "any_of": ["board reset"]}]},
            ensure_ascii=False), encoding="utf-8")
        ev_tz = fmt_work / "evtz.json"
        run([SCRIPTS / "search_evidence.py", sdir, "--rules", tz_rules,
             "--local-tz-offset", "0", "--json", ev_tz])
        tz0 = json.loads(ev_tz.read_text(encoding="utf-8"))["results"][0]
        run([SCRIPTS / "search_evidence.py", sdir, "--rules", tz_rules,
             "--local-tz-offset", "8", "--json", ev_tz])
        tz8 = json.loads(ev_tz.read_text(encoding="utf-8"))["results"][0]
        check(tz0["evidence"][0]["timestamp"].startswith("2025-05-19 10:00")
              and tz8["evidence"][0]["timestamp"].startswith("2025-05-19 02:00"),
              "--local-tz-offset 换算（0→原样，8→-8h）")

        # 多站点 verdict 分组
        ms_rules = fmt_work / "ms_rules.yaml"
        ms_rules.write_text(json.dumps({"rules": [
            {"id": "E_MS", "globs": ["**/collectinfo.log"], "any_of": ["succ"],
             "associate_by": {"site": True}}],
            "root_causes": [{"id": "R1", "condition": "E_MS"}]},
            ensure_ascii=False), encoding="utf-8")
        ev_ms = fmt_work / "evms.json"
        r = run([SCRIPTS / "search_evidence.py", twodir, "--rules", ms_rules,
                 "--json", ev_ms])
        msv = json.loads(ev_ms.read_text(encoding="utf-8"))["verdict"]
        check(r.returncode == 0 and msv.get("grouped_by_site") is True
              and len(msv.get("groups", {})) >= 3,
              f"多站点 verdict 分组（groups={list(msv.get('groups', {}))}）")

        # temporal_conflict：E7 晚于 E8 出现 → 约束违反 → 状态 temporal_conflict
        cf_rules = fmt_work / "cf_rules.yaml"
        cf_rules.write_text(json.dumps({"rules": [
            {"id": "E7", "globs": ["**/conflict.log"], "any_of": ["aaa"]},
            {"id": "E8", "globs": ["**/conflict.log"], "any_of": ["bbb"]}],
            "temporal": [{"id": "T9", "first": "E7", "second": "E8",
                          "max_gap_minutes": 60}],
            "root_causes": [{"id": "R9", "condition": "E7 and E8",
                             "temporal": ["T9"]}]}, ensure_ascii=False),
            encoding="utf-8")
        ev_cf = fmt_work / "evcf.json"
        r = run([SCRIPTS / "search_evidence.py", sdir, "--rules", cf_rules,
                 "--json", ev_cf])
        cfv = json.loads(ev_cf.read_text(encoding="utf-8"))["verdict"]
        check(r.returncode == 0 and cfv.get("status") == "temporal_conflict",
              f"时序矛盾 → temporal_conflict（{cfv.get('status')}）")

        # 证据指纹 + --profile 直取规则 + xz 透明解压
        check(ev2.get("file_hashes") and all(
            len(v) == 64 for v in ev2["file_hashes"].values()),
            f"file_hashes sha256 指纹（{len(ev2.get('file_hashes', {}))} 个文件）")
        r = run([SCRIPTS / "search_evidence.py", sdir, "--profile",
                 sd / "assets" / "fault-profile.yaml", "--json", ev_tz])
        pev = json.loads(ev_tz.read_text(encoding="utf-8"))
        check(r.returncode == 0 and pev.get("results")
              and pev["results"][0].get("step"),
              "--profile 直取内嵌规则（step 标注保留）")
        (sdir / "xz.log.xz").write_bytes(
            lzma.compress("2025-05-19 10:00:00 xz内容行\n".encode()))
        xz_rules = fmt_work / "xz_rules.yaml"
        xz_rules.write_text(json.dumps({"rules": [
            {"id": "E_XZ", "globs": ["**/xz.log*"], "any_of": ["xz内容行"]}]},
            ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "search_evidence.py", sdir, "--rules", xz_rules,
                 "--json", ev_tz])
        xr = json.loads(ev_tz.read_text(encoding="utf-8"))["results"][0]
        check(r.returncode == 0 and xr["match_count"] == 1,
              ".xz 压缩文本透明解压可检索")

        # M3: 含转义字符的 numeric_trend 证据 → render → verify（正/反）+ --verify-detail 诊断
        # 合成 numeric_trend 日志：value_detail 含正则转义 \|（原文反斜杠+管道）
        esc_dir = fmt_work / "esc"
        esc_dir.mkdir(exist_ok=True)
        (esc_dir / "trend.log").write_text(
            "2025-05-19 10:00:00 temp=88 |v| > 1000\n"
            "2025-05-19 10:01:00 temp=95 |v| > 1000\n", encoding="utf-8")
        esc_rules = fmt_work / "esc_rules.yaml"
        esc_rules.write_text(json.dumps({"rules": [
            {"id": "E_ESC", "type": "numeric_trend", "globs": ["**/trend.log"],
             "description": r"频偏超过 1000（\|v\| 正则转义）",
             "value_pattern": r"temp=(\d+)", "trend": "exceed_threshold",
             "threshold": 80}]},
            ensure_ascii=False), encoding="utf-8")
        esc_ev = fmt_work / "esc_ev.json"
        r = run([SCRIPTS / "search_evidence.py", esc_dir, "--rules", esc_rules,
                 "--json", esc_ev])
        esc_data = json.loads(esc_ev.read_text(encoding="utf-8"))
        esc_hit = next(x for x in esc_data["results"] if x["rule_id"] == "E_ESC")
        esc_line = esc_hit["evidence"][0]["line"]
        check(r.returncode == 0 and esc_hit["hit"] is True and esc_line.count("&#92;") >= 2,
              f"M3: numeric_trend 证据反斜杠规范化为实体（line={esc_line[:60]!r}）")
        # render → verify（正向通过）
        esc_md = fmt_work / "esc_report.md"
        r = run([sd / "scripts" / "render_report.py", "--evidence", esc_ev,
                 "--md", esc_md])
        check(r.returncode == 0 and esc_md.exists()
              and "频偏超过 1000" in esc_md.read_text(encoding="utf-8"),
              "M3: 含转义 numeric_trend 证据渲染 MD")
        # 补全 TODO 后 verify 通过
        esc_ok = fmt_work / "esc_report_ok.md"
        esc_ok.write_text(re.sub(r"<!-- TODO:.*?-->", "已补全",
                                 esc_md.read_text(encoding="utf-8")), encoding="utf-8")
        r = run([sd / "scripts" / "render_report.py", "--verify", esc_ok,
                 "--evidence", esc_ev], expect_rc=2)
        # 注：无 HTML 时 verify 只校验 TODO/指纹/结论；numeric_trend 证据行指纹应通过
        check(r.returncode == 0, "M3: 规范化证据 verify 通过（正例）")
        # 反向：把实体改回裸反斜杠（模拟人类重写层数错误）→ verify 失败 + M1 诊断
        esc_bad = fmt_work / "esc_report_bad.md"
        esc_bad.write_text(esc_ok.read_text(encoding="utf-8").replace("&#92;", "\\"),
                           encoding="utf-8")
        r = run([sd / "scripts" / "render_report.py", "--verify", esc_bad,
                 "--evidence", esc_ev, "--verify-detail"], expect_rc=2)
        check(r.returncode == 2 and "被改动行" in r.stdout
              and ("疑似反斜杠转义层数不一致" in r.stdout or "指纹失配" in r.stdout),
              "M3: 转义层数被改 → verify 失败并输出 M1 诊断")

        # 9. parse_clock / parse_cloudmat 专项
    if 8 in _want: _s8()

    def _s9():
        nonlocal cmj
        print("\n[9] parse_clock / parse_cloudmat 专项")
        clk_lines = [
            "Clk[032]2025-05-18,10:00:00 SysClk -a ref:2",
            "Clk[033]2025-05-18,10:01:00 CurId=3 BetId=2 Ref sw end=1",
            "Clk[099]2025-05-18,10:02:00 ClkHealth=0x1",
        ]
        clkf = sdir / "clockstatuslog.txt"
        clkf.write_text("\n".join(clk_lines) + "\n", encoding="utf-8")
        cj = fmt_work / "clk1.json"
        r = run([SCRIPTS / "parse_clock.py", clkf, "--json", cj])
        cd1 = json.loads(cj.read_text(encoding="utf-8")) if r.returncode == 0 else {}
        f0 = (cd1.get("files") or [{}])[0]
        check(r.returncode == 0 and len(f0.get("alarms", [])) >= 1
              and len(f0.get("ref_switches", [])) >= 1
              and len(f0.get("clk99_health_events", [])) >= 1,
              "单文件解析：告警/参考源切换/ClkHealth 事件提取")
        fixf = sdir / "clockstatuslog.fix.txt"
        fixf.write_text("Clk[099]2021-01-01,00:00:10 boot Tick=100\n"
                        "Clk[099]2025-05-18,10:00:01 normal Tick=200\n",
                        encoding="utf-8")
        fj = fmt_work / "clkfix.json"
        run([SCRIPTS / "parse_clock.py", fixf, "--json", fj])
        fx = json.loads(fj.read_text(encoding="utf-8"))
        run([SCRIPTS / "parse_clock.py", fixf, "--json", fj, "--no-fix-timestamp"])
        fnf = json.loads(fj.read_text(encoding="utf-8"))
        check(fx["files"][0]["timestamp_fixed"] == 1
              and fnf["files"][0]["timestamp_fixed"] == 0,
              "2021 假时间修复（--no-fix-timestamp 关闭）")
        (sdir / "myclock.log").write_text("\n".join(clk_lines) + "\n",
                                         encoding="utf-8")
        gj = fmt_work / "clkglob.json"
        r = run([SCRIPTS / "parse_clock.py", sdir, "--glob", "myclock*",
                 "--json", gj])
        gd = json.loads(gj.read_text(encoding="utf-8"))
        check(r.returncode == 0 and len(gd.get("files", [])) == 1
              and "myclock" in gd["files"][0]["file"], "--glob 自定义文件模式")

        cm_log = fmt_dir / "cm.log"
        cm_log.write_text("\n".join([
            "1 轮次0 2026-09-10 08:00:00 【信息】 CloudMAT:开始 CyclingTest，"
            "TFP=ORTE_X，条码 722385500358，槽位 1_1_3_1",
            "2 轮次0 2026-09-10 08:05:00 【失败】 相位超差测试:第0轮结束，"
            "结果：TestItemResult.Fail",
            "3 轮次0 2026-09-10 12:00:00 【信息】 CloudMAT:测试结果：失败",
            "4 轮次0 2026-09-10 12:05:00 【数据】 相位超差测试:调用:BspPhsOffsetGet接口,"
            "参数:[1,2],返回值:[{'result': 1, 'data': 15500}]",
            "5 轮次0 2026-09-10 12:06:00 【数据】 随机短串 0000000000000000000000000",
            "6 轮次0 2026-09-10 12:07:00 【数据】 getItemResultInfo 结果位图 "
            "0011011000100111010101010110010100110101",
        ]) + "\n", encoding="utf-8")
        cmj = fmt_work / "cm.json"
        r = run([SCRIPTS / "parse_cloudmat.py",
                 cm_log, "--json", cmj])
        cm = json.loads(cmj.read_text(encoding="utf-8")) if r.returncode == 0 else {}
        bi = cm.get("basic_info", {})
        check(r.returncode == 0 and bi.get("result") == "失败"
              and bi.get("barcode") == "722385500358"
              and any("相位超差" in x.get("name", "") for x in cm.get("failed_items", [])),
              f"parse_cloudmat 结构化（result={bi.get('result')}, barcode={bi.get('barcode')}）")
        check(len(cm.get("interface_calls", [])) >= 1
              and cm["interface_calls"][0]["interface"] == "BspPhsOffsetGet"
              and cm["interface_calls"][0]["retval"][0].get("data") == 15500,
              "D1: parse_cloudmat 解析『调用:接口,参数,返回值』为 interface_calls")
        check(len(cm.get("bitmaps", [])) == 1
              and cm["bitmaps"][0]["length"] == 40
              and cm["bitmaps"][0]["bitmap"].startswith("001101100"),
              "B4: 位图仅收锚点行（getItemResultInfo），非锚点短 0/1 串被过滤")

        # ---- C1/C3/C6：失锁哨兵过滤 / Dif 哨兵 / brief 分诊 / schema 统一 ----
        # C1: 失锁填充频偏（-1163.92/ref_sts=9）不得判真实 OCXO 超差
        sentinel = []
        for i in range(12):
            sentinel.append(f"Clk[100]2025-05-{(i % 28) + 1:02d},10:00:00 "
                            f"-1163.92 -1155.90 9999.99 9 1 32.71 3276.4 -9999.99")
        sentinel.append("Clk[100]2025-05-01,00:00:00 -8.00 0.04 -8.00 2 1 32.35")
        sf = sdir / "clockocxolog.txt"
        sf.write_text("\n".join(sentinel) + "\n", encoding="utf-8")
        run([SCRIPTS / "parse_clock.py", sf, "--json", cj])
        sd2 = json.loads(cj.read_text(encoding="utf-8"))
        f0 = sd2["files"][0]
        oc = f0.get("ocxo_records", [])
        n_inv = sum(1 for x in oc if x.get("fields", {}).get("fcv_invalid"))
        n_over = sum(1 for x in oc if x.get("fields", {}).get("fcv_over_limit"))
        check(n_inv == 12 and n_over == 0,
              f"C1: 失锁哨兵频偏标 fcv_invalid 且不计真实超差（invalid={n_inv}, over={n_over}）")
        check(oc and all("fields" in x and "cur_fcv" in x for x in oc),
              "C6: ocxo_records 统一含 fields 镜像与顶层字段")
        sentinel_anom = " ".join(a for d in f0.get("anomalies", [])
                                 for a in d.get("anomaly", []))
        check("失锁未收敛" in sentinel_anom and "OCXO 频偏" not in sentinel_anom,
              "C1: 失锁哨兵只记『失锁未收敛(频偏值无效)』，不报真实频偏超差")
        # C1: 真实老化频偏（-171，锁定/快捕态）应计真实超差
        aging = []
        for d in range(1, 13):
            aging.append(f"Clk[100]2026-02-{d:02d},00:00:00 "
                         f"-171.56 19.65 -172.15 1 1 39.72")
        af = sdir / "clockocxolog_aging.txt"
        af.write_text("\n".join(aging) + "\n", encoding="utf-8")
        bj = fmt_work / "brief_aging.json"
        run([SCRIPTS / "parse_clock.py", af, "--brief", bj])
        b = json.loads(bj.read_text(encoding="utf-8"))
        check("OCXO_AGING" in b["triage"]["tags"]
              and b["files"][0]["ocxo"]["over_limit_points"] == 12,
              "C3: brief 判定锁定态持续频偏为 OCXO_AGING")
        # C1: Dif 哨兵（1e9）不判相位差超差，真实 4.5e7 保留
        df = sdir / "dif.txt"
        df.write_text("Clk[085]2025-05-01,00:00:00 Ref=1 Dif=1000000000\n"
                      "Clk[085]2025-05-01,00:01:00 Ref=1 Dif=45477040\n",
                      encoding="utf-8")
        run([SCRIPTS / "parse_clock.py", df, "--json", cj])
        dd = json.loads(cj.read_text(encoding="utf-8"))
        dif_anom = " ".join(a for r in dd["files"] for d in r.get("anomalies", [])
                            for a in d.get("anomaly", []))
        check("Dif=1000000000" in dif_anom and "哨兵" in dif_anom
              and "Dif=45477040" in dif_anom,
              "C1: Dif=1e9 哨兵不报超差、真实 4.5e7 保留")
        # C3: brief 分诊 GNSS 锁星失败
        gf = sdir / "clockstatuslog_gnss.txt"
        gf.write_text("\n".join([
            "Clk[032]2025-12-30,05:00:00 LockSatFail -a ref:1",
            "Clk[094]2025-12-30,05:01:00 ReasonId=1",
            "Clk[094]2025-12-30,05:02:00 ReasonId=14",
        ]) + "\n", encoding="utf-8")
        bj2 = fmt_work / "brief_gnss.json"
        run([SCRIPTS / "parse_clock.py", gf, "--brief", bj2])
        bg = json.loads(bj2.read_text(encoding="utf-8"))
        check(bg["triage"]["verdict"] == "FAULT"
              and "GNSS_LOCK_FAIL" in bg["triage"]["tags"],
              "C3: brief 分诊 GNSS_LOCK_FAIL")
        # C5: 合并轮转行号溯源到具体源文件
        c5 = fmt_work / "c5.json"
        dcm_bsp = fmt_work / "c5_bsp"
        (dcm_bsp / "6323_9999999" / "BspLog").mkdir(parents=True, exist_ok=True)
        # 直接对 merge 目录结果校验 source_file 字段存在
        run([SCRIPTS / "parse_clock.py", sdir, "--merge", "--json", c5])
        c5d = json.loads(c5.read_text(encoding="utf-8"))
        has_src = any(rec.get("source_file")
                      for f in c5d.get("files", []) for rec in f.get("records", []))
        check(has_src, "C5: --merge 记录带来源文件/内行号（source_file/source_line_no）")

        # M5: 多站点 clock JSON 合并（--merge-sites）+ gnss 类型固化（spoofing_suspicions: list）
        ms_a = fmt_work / "clock_siteA.brief.json"
        ms_a.write_text(json.dumps({
            "files": [{"file": "<merged:A>", "total_records": 10, "timestamp_fixed": 2,
                       "alarms": [{"name": "al1"}], "ref_switches": [], "reset_reasons": [],
                       "clk99_health_events": []}],
            "gnss": {"gnss_records": 5,
                     "spoofing_suspicions": [{"confidence": 0.9}],
                     "last_status": "ok"}}, ensure_ascii=False), encoding="utf-8")
        ms_b = fmt_work / "clock_siteB.brief.json"
        ms_b.write_text(json.dumps({
            "files": [{"file": "<merged:B>", "total_records": 8, "timestamp_fixed": 0,
                       "alarms": [], "ref_switches": [], "reset_reasons": [],
                       "clk99_health_events": []}],
            "gnss": {"gnss_records": 3,
                     "spoofing_suspicions": 3,  # 类型陷阱：误写成 int，必须归一为 list
                     "last_status": "ok"}}, ensure_ascii=False), encoding="utf-8")
        ms_out = fmt_work / "clock_merged.json"
        r = run([SCRIPTS / "parse_clock.py", "--merge-sites", str(ms_a), str(ms_b),
                 "--json", ms_out])
        ms = json.loads(ms_out.read_text(encoding="utf-8"))
        check(r.returncode == 0 and len(ms["files"]) == 2,
              "M5: --merge-sites 多站点 files[] 拼接")
        check(ms["gnss"]["gnss_records"] == 8
              and isinstance(ms["gnss"]["spoofing_suspicions"], list)
              and len(ms["gnss"]["spoofing_suspicions"]) == 2,
              "M5: gnss 合并（records 求和 + spoofing_suspicions 列表拼接/int→list 兜底）")
        # M5b: render_report --clock 目录模式自动合并多站点（过滤 clock/brief 命名）
        if sd.exists():
            rr = sd / "scripts" / "render_report.py"
            clk_dir = fmt_work / "clk_dir"
            clk_dir.mkdir(exist_ok=True)
            (clk_dir / "clock_siteA.brief.json").write_bytes(ms_a.read_bytes())
            (clk_dir / "clock_siteB.brief.json").write_bytes(ms_b.read_bytes())
            (clk_dir / "ignore.json").write_text("{}", encoding="utf-8")  # 非时钟 JSON 忽略
            ev5 = fmt_work / "ev5.json"
            ev5.write_text(json.dumps({"results": [],
                                       "verdict": {"groups": {}, "status": "unmatched",
                                                   "primary": "NONE"},
                                       "detected_sites": []}), encoding="utf-8")
            md5 = fmt_work / "rep5.md"
            r = run([rr, "--evidence", ev5, "--clock", clk_dir, "--md", md5])
            t5 = md5.read_text(encoding="utf-8") if md5.exists() else ""
            check(r.returncode == 0 and "告警 1 条" in t5 and "欺骗嫌疑 2" in t5
                  and "假时间修复 2" in t5,
                  "M5b: render_report --clock 目录模式合并多站点摘要")

        # 10. run_regression / init_case / scan_rule_candidates 专项
    if 9 in _want: _s9()

    def _s10():
        print("\n[10] run_regression / init_case / scan_rule_candidates 专项")
        r = run([SCRIPTS / "init_case.py", "--cases-dir", cases_dir, "--skill", SLUG,
                 "--id", "case-90-clock", "--kind", "corruption", "--clock"])
        cy = (cases_dir / SLUG / "case-90-clock" / "case.yaml")
        check(r.returncode == 0 and cy.exists()
              and "parse_clock" in cy.read_text(encoding="utf-8"),
              "init_case --clock 断言模板")
        r = run([SCRIPTS / "init_case.py", "--cases-dir", cases_dir, "--skill", SLUG,
                 "--id", "case-90-clock", "--kind", "corruption"], expect_rc=None)
        check(r.returncode != 0, "init_case 已存在目录无 --force 报错")
        r = run([SCRIPTS / "init_case.py", "--cases-dir", cases_dir, "--skill", SLUG,
                 "--id", "case-91-ms", "--kind", "multisite"])
        check(r.returncode == 0, "init_case kind=multisite")
        r = run([SCRIPTS / "init_case.py", "--cases-dir", cases_dir, "--skill", SLUG,
                 "--id", "Bad ID", "--kind", "positive"], expect_rc=None)
        check(r.returncode != 0 and "slug" in r.stderr, "init_case 非法 slug 拒绝")

        # --case 单例回归
        one_dir = cases_dir / SLUG / "case-01-positive"
        r = run([SCRIPTS / "run_regression.py", "--case", one_dir, "--skill-dir", sd,
                 "--workdir", tmp / "rw1", "--json", tmp / "rw1.json"])
        rep1 = json.loads((tmp / "rw1.json").read_text(encoding="utf-8"))
        check(r.returncode == 0 and len(rep1["results"]) == 1
              and rep1["results"][0]["status"] == "PASS", "--case/--skill-dir 单例回归")
        # 增量缓存：同输入+同工具箱 → 第二次命中 CACHED
        r = run([SCRIPTS / "run_regression.py", "--case", one_dir, "--skill-dir", sd,
                 "--workdir", tmp / "rw1", "--json", tmp / "rw1b.json"])
        rep1b = json.loads((tmp / "rw1b.json").read_text(encoding="utf-8"))
        check(rep1b["results"][0].get("cached") is True and "(cached)" in r.stdout,
              "回归增量缓存命中（cached）")
        r = run([SCRIPTS / "run_regression.py", "--case", one_dir, "--skill-dir", sd,
                 "--workdir", tmp / "rw1", "--no-cache"])
        check(r.returncode == 0 and "(cached)" not in r.stdout, "--no-cache 关闭缓存")

        # 故意失配案例 → FAIL + 非零退出
        wrong = cases_dir / SLUG / "case-99-wrong"
        shutil.copytree(one_dir, wrong)
        wcy = wrong / "case.yaml"
        wcy.write_text(wcy.read_text(encoding="utf-8").replace("R1", "R9"),
                       encoding="utf-8")
        r = run([SCRIPTS / "run_regression.py", "--case", wrong, "--skill-dir", sd,
                 "--workdir", tmp / "rw2"], expect_rc=None)
        check(r.returncode != 0 and "FAIL" in r.stdout, "期望失配 → FAIL + 非零退出")

        # 旧版 --cases JSON 清单入口
        case_zip = next(one_dir.rglob("*.zip"))
        legacy = tmp / "cases.json"
        legacy.write_text(json.dumps({"cases": [{
            "id": "legacy-01", "archive": str(case_zip), "skill_dir": str(sd),
            "expected_root_cause": "R1", "expected_evidence": ["E1", "E2"],
            "not_expected_evidence": []}]}, ensure_ascii=False), encoding="utf-8")
        r = run([SCRIPTS / "run_regression.py", "--cases", legacy,
                 "--workdir", tmp / "rw3"], expect_rc=None)
        check(r.returncode == 0 and "PASS" in r.stdout, "旧版 --cases 清单回归")

        # scan_rule_candidates：关键词 + 模块统计 + --top + fm 告警统计
        cand = fmt_work / "cand.json"
        r = run([SCRIPTS / "scan_rule_candidates.py", fmt_work / "dcm",
                 "--json", cand, "--top", "3"])
        cj2 = json.loads(cand.read_text(encoding="utf-8")) if r.returncode == 0 else {}
        check(r.returncode == 0 and cj2.get("keyword_hits"),
              f"候选扫描关键词/模块统计（{list(cj2)[:4]}）")
        cand_fm = fmt_work / "cand_fm.json"
        r = run([SCRIPTS / "scan_rule_candidates.py", fmt_work / "fm",
                 "--json", cand_fm])
        cf = json.loads(cand_fm.read_text(encoding="utf-8")) if r.returncode == 0 else {}
        check(r.returncode == 0 and (cf.get("alarm_codes") or cf.get("alarms")),
              f"fm xlsx 告警统计（{list(cf)[:4]}）")
        sugg = cf.get("rule_suggestions", [])
        check(bool(sugg) and all(s.get("id") for s in sugg)
              and any(s.get("format") == "xlsx" for s in sugg),
              "rule_suggestions 骨架含 id + xlsx 格式")

        # render 扩展：--site 分组 / --cloudmat / --clock / temporal_conflict / 无 profile
        rep3 = tmp / "rep3.md"
        r = run([sd / "scripts" / "render_report.py", "--evidence", ev_ms,
                 "--md", rep3, "--site", "6321_1111111"])
        t3 = rep3.read_text(encoding="utf-8") if rep3.exists() else ""
        check(r.returncode == 0 and "6321_1111111" in t3, "--site 输出指定站点分组")
        rep4 = tmp / "rep4.md"
        r = run([sd / "scripts" / "render_report.py", "--evidence", ev_cf,
                 "--md", rep4])
        t4 = rep4.read_text(encoding="utf-8") if rep4.exists() else ""
        check(r.returncode == 0 and "时序" in t4 and "未判定" in t4,
              "temporal_conflict 报告分支")
        rep5 = tmp / "rep5.md"
        r = run([sd / "scripts" / "render_report.py", "--evidence", ev_ms,
                 "--md", rep5, "--cloudmat", cmj, "--clock", clock_json,
                 "--profile", tmp / "nonexistent.json"], expect_rc=None)
        t5 = rep5.read_text(encoding="utf-8") if rep5.exists() else ""
        check(r.returncode == 0 and "CloudMAT 结构化摘要" in t5
              and "时钟结构化摘要" in t5, "--cloudmat/--clock 摘要块")
        check("未找到 profile" in r.stderr or "722385500358" in t5,
              "无 profile 时告警但仍渲染")

        # --out 同基名出 MD+HTML + 结论判定状态徽章
        r = run([sd / "scripts" / "render_report.py", "--evidence", ev_ms,
                 "--out", tmp / "rep_pair"])
        check(r.returncode == 0 and (tmp / "rep_pair.md").exists()
              and (tmp / "rep_pair.html").exists(), "--out 一键出 MD+HTML")
        check("判定状态" in (tmp / "rep_pair.md").read_text(encoding="utf-8"),
              "结论判定状态徽章行")

    if 10 in _want: _s10()

    if 11 in _want:
        def _s11():
            print("\n[11] 新增能力：merge-manifest / extract_globs / gen_specialized / extra_scripts")
            unk = SCRIPTS / "unpack_logs.py"
            gs = SCRIPTS / "gen_specialized.py"

            # merge-manifest：两个小包合并到同一根（站点并集、sources 追加、幂等去重）
            za = tmp / "a.zip"
            zb = tmp / "b.zip"
            with zipfile.ZipFile(za, "w") as z:
                z.writestr("siteA/clockstatuslog.txt", "2026-10-13 00:00:00 sts=3 lock\n")
                z.writestr("siteA/fm-active.csv", "x")
            with zipfile.ZipFile(zb, "w") as z:
                z.writestr("siteB/clockstatuslog.txt", "2026-10-14 00:00:00 sts=3 lock\n")
                z.writestr("siteB/fm-active.csv", "y")
            mw = tmp / "merge_work"
            r = run([unk, za, "-o", mw])
            check(r.returncode == 0, "merge-manifest：首包解压")
            r = run([unk, zb, "-o", mw, "--merge-manifest"])
            mj = json.loads((mw / "manifest.json").read_text(encoding="utf-8")) \
                if (mw / "manifest.json").exists() else {}
            check(r.returncode == 0 and len(mj.get("sources", [])) == 2
                  and mj.get("mode") == "merged",
                  "merge-manifest：sources 追加为 2 份、mode=merged")
            roots = [str(s.get("root")) for s in mj.get("sites", [])]
            check(any("siteA" in r_ for r_ in roots) and any("siteB" in r_ for r_ in roots),
                  "merge-manifest：站点并集含 siteA/siteB")
            ex_all = mj.get("extracted_files", [])
            check(len(ex_all) == len(set(ex_all)), "merge-manifest：extracted_files 并集无重复")
            ms = json.loads((mw / "manifest.summary.json").read_text(encoding="utf-8"))
            check(ms.get("mode") == "merged", "merge 后 manifest.summary.json 同步为 merged")
            # 重复合并：站点根不重复膨胀、文件仍为并集（追加 sources 记录但内容不重算双份）
            r = run([unk, zb, "-o", mw, "--merge-manifest"])
            mj2 = json.loads((mw / "manifest.json").read_text(encoding="utf-8"))
            roots2 = [str(s.get("root")) for s in mj2.get("sites", [])]
            check(roots2.count("siteB") <= 1 and len(mj2.get("extracted_files", []))
                  == len(set(mj2.get("extracted_files", []))),
                  "merge-manifest：重复合并站点根/文件并集不重复")

            # extract_globs：规则未命中但 extract_globs 仍提取
            eg = tmp / "eg-rules.yaml"
            eg.write_text(json.dumps({"rules": [
                {"id": "E1", "globs": ["**/nofile*"], "any_of": ["x"],
                 "extract_globs": ["**/summary.json"]}]}), encoding="utf-8")
            ez = tmp / "eg.zip"
            with zipfile.ZipFile(ez, "w") as z:
                z.writestr("d/summary.json", '{"neList": []}')
                z.writestr("d/other.log", "x")
            ew = tmp / "eg_work"
            r = run([unk, ez, "-o", ew, "--include-rules", eg])
            mt = (ew / "manifest.json").read_text(encoding="utf-8") \
                if (ew / "manifest.json").exists() else ""
            check(r.returncode == 0 and "summary.json" in mt,
                  "extract_globs：规则未命中仍提取 summary.json")

            # gen_specialized：normal→unmatched、按 primary 推导 status
            out = tmp / "gs.json"
            r = run([gs, "--out", out, "--site", "13196231",
                     "--conclusion", '{"primary": null, "status": "normal", "note": "专项判定正常"}'])
            gj = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
            check(r.returncode == 0 and gj.get("sites", {}).get("13196231", {}).get("status") == "unmatched",
                  "gen_specialized：normal→unmatched、写入 sites 组键")
            r = run([gs, "--out", out, "--site", "s1", "--conclusion", '{"primary": "R2"}'])
            gj = json.loads(out.read_text(encoding="utf-8"))
            check(gj.get("sites", {}).get("s1", {}).get("status") == "matched",
                  "gen_specialized：无 status 时按 primary 推导 matched")

            # extra_scripts：声明后纳入受管清单、--inspect 不误报不受管
            if sd.exists() and (sd / "assets" / "fault-profile.yaml").exists():
                pr = (sd / "assets" / "fault-profile.yaml").read_text(encoding="utf-8")
                if not re.search(r"^components:\s*\n[ \t]+extra_scripts:", pr, re.M):
                    pr = pr.rstrip() + "\ncomponents:\n  extra_scripts:\n    - scripts/ocxo_aging_analyzer.py\n"
                    (sd / "assets" / "fault-profile.yaml").write_text(pr, encoding="utf-8")
                (sd / "scripts" / "ocxo_aging_analyzer.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
                r = run([SCRIPTS / "scaffold_child_skill.py", sd.name, "--profile",
                         sd / "assets" / "fault-profile.yaml", "--path", str(sd.parent), "--update"])
                st = json.loads((sd / "assets" / "toolkit-version.json").read_text(encoding="utf-8"))
                check(any("ocxo_aging_analyzer" in f for f in st.get("files", {})),
                      "extra_scripts：--update 后纳入受管清单/版本戳")
                r = run([SCRIPTS / "scaffold_child_skill.py", "--inspect", str(sd)])
                check(not any("ocxo_aging_analyzer" in ln and "不受管" in ln
                              for ln in r.stdout.splitlines()),
                      "extra_scripts：--inspect 不再把声明脚本报为不受管")

            # B1：gen_specialized 透传 events（render_report 消费端依赖）
            out = tmp / "gs_ev.json"
            r = run([gs, "--out", out, "--site", "s1", "--conclusion",
                     '{"primary": null, "status": "unmatched", "note": "n", "events": [{"timestamp": "2026-09-06 05:41:39", "desc": "频跳窗口"}]}'])
            gj = json.loads(out.read_text(encoding="utf-8"))
            check(r.returncode == 0 and gj.get("sites", {}).get("s1", {}).get("events")
                  and gj["sites"]["s1"]["events"][0]["desc"] == "频跳窗口",
                  "B1 gen_specialized：events 透传")

            # M4: --from-special 自动读取 special_*.json（命中/未命中两站点映射正确）
            sp1 = tmp / "special_13195742.json"
            sp1.write_text(json.dumps({
                "summary": {"primary_condition": 3, "condition_text": "OCXO 频偏超阈值"},
                "result": {"occurrence_time": "2026-10-26 13:08:33",
                           "description": "R3 命中：老化频偏"}}), encoding="utf-8")
            sp2 = tmp / "special_13196231.json"
            sp2.write_text(json.dumps({
                "summary": {"primary_condition": None, "condition_text": "未发现异常"},
                "result": {"occurrence_time": "", "description": "正常"}}), encoding="utf-8")
            out = tmp / "gs_sp.json"
            r = run([gs, "--out", out, "--from-special", str(sp1), str(sp2)])
            sj = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
            sites = sj.get("sites", {})
            check(r.returncode == 0 and sites.get("13195742", {}).get("primary") == "R3"
                  and sites.get("13195742", {}).get("status") == "matched",
                  "M4 --from-special：primary_condition=3 → primary=R3/status=matched")
            check(sites.get("13196231", {}).get("primary") is None
                  and sites.get("13196231", {}).get("status") == "unmatched",
                  "M4 --from-special：primary_condition=None → primary=null/status=unmatched")
            check(sites.get("13195742", {}).get("events")
                  and sites["13195742"]["events"][0]["timestamp"] == "2026-10-26 13:08:33",
                  "M4 --from-special：events 从 result 提取 occurrence_time/description")

            # M4b: --from-special 站点键 --site 覆盖（单站点输出为单对象）
            out = tmp / "gs_sp2.json"
            r = run([gs, "--out", out, "--from-special", str(sp2), "--site", "SITE_X"])
            s2 = json.loads(out.read_text(encoding="utf-8"))
            check(r.returncode == 0 and s2.get("sites", {}).get("SITE_X", {}).get("status") == "unmatched",
                  "M4b --from-special：--site 覆盖文件名推断键")

            # M7: skill_sync --reclaim --diff 行级差异输出
            if sd.exists():
                # 在子 skill 的共享脚本里制造 +5/-5 差异（临时改内容，跑完不回收）
                tgt = sd / "scripts" / "log_format.py"
                orig = tgt.read_text(encoding="utf-8")
                tgt.write_text(orig + "\n# M7 diff 探测占位 +5\n# M7 diff 探测占位 +5\n"
                                       "# M7 diff 探测占位 +5\n# M7 diff 探测占位 +5\n# M7 diff 探测占位 +5\n",
                               encoding="utf-8")
                try:
                    r = run([SCRIPTS / "skill_sync.py", "--reclaim", str(sd), "--diff"])
                    check(r.returncode == 0 and "(+" in r.stdout
                          and "+ # M7 diff 探测占位" in r.stdout,
                          "M7: --reclaim --diff 输出行级 +/- 差异（占位行可见）")
                finally:
                    tgt.write_text(orig, encoding="utf-8")


            # B0：时钟逗号分隔时间戳可解析（TIME_FORMATS/TIME_LOOKS 逗号 + time_extract.formats）
            clk_zip = tmp / "clk.zip"
            with zipfile.ZipFile(clk_zip, "w") as z:
                z.writestr("ne1/BSP/clockstatuslog.txt",
                           "Clk[000]2026-09-06,05:41:39 Ref= 1 sts=9 cFCV=9999\n"
                           "Clk[000]2026-09-06,05:47:29 Ref= 1 sts=9 cFCV=9999\n")
                z.writestr("ne2/BSP/clockstatuslog.txt",
                           "Clk[000]2026-09-06,06:00:00 Ref= 1 sts=9 cFCV=9999\n")
            cw = tmp / "clk_work"
            cw.mkdir(exist_ok=True)
            (cw / "clockstatuslog.txt").write_text(
                "Clk[000]2026-09-06,05:41:39 Ref= 1 sts=9 cFCV=9999\n"
                "Clk[000]2026-09-06,06:00:00 Ref= 1 sts=9 cFCV=9999\n", encoding="utf-8")
            c_rule = {"rules": [{"id": "C1", "globs": ["**/clockstatuslog.txt"],
                                 "all_of": ["sts=9"], "min_matches": 1,
                                 "time_extract": {"pattern": r"(\d{4}-\d{2}-\d{2},\d{2}:\d{2}:\d{2})",
                                                  "group": 1,
                                                  "formats": ["%Y-%m-%d,%H:%M:%S"]}}]}
            cr = tmp / "clk-rules.yaml"
            cr.write_text(json.dumps(c_rule), encoding="utf-8")
            cv = tmp / "clk_ev.json"
            r = run([SCRIPTS / "search_evidence.py", cw, "--rules", cr, "--json", cv])
            cj = json.loads(cv.read_text(encoding="utf-8")) if cv.exists() else {}
            ev_lines = [e.get("timestamp") for e in (cj.get("results") or [{}])[0].get("evidence", [])]
            check(r.returncode == 0 and all(t for t in ev_lines),
                  "B0 search_evidence：时钟逗号时间戳解析为时间（非时间未知）")

            # B2：多站点时证据按站点独立配额，先扫描站点不占满全局上限
            b2d = tmp / "b2"
            (b2d / "siteA_1").mkdir(parents=True, exist_ok=True)
            (b2d / "siteB_2").mkdir(parents=True, exist_ok=True)
            for i in range(3):
                (b2d / "siteA_1" / "clockstatuslog.txt").write_text(
                    f"Clk[000]2026-09-06,0{i+1}:00:00 Ref= 1 sts=9 cFCV=9999\n", encoding="utf-8")
                (b2d / "siteB_2" / "clockstatuslog.txt").write_text(
                    f"Clk[000]2026-09-06,0{i+1}:10:00 Ref= 1 sts=9 cFCV=9999\n", encoding="utf-8")
            b2v = tmp / "b2_ev.json"
            r = run([SCRIPTS / "search_evidence.py", b2d, "--rules", cr, "--json", b2v, "--max-evidence-per-rule", "1"])
            b2j = json.loads(b2v.read_text(encoding="utf-8")) if b2v.exists() else {}
            b2_ev = (b2j.get("results") or [{}])[0].get("evidence", [])
            b2_sites = {e.get("site") for e in b2_ev}
            check(r.returncode == 0 and {"siteA_1", "siteB_2"} <= b2_sites,
                  "B2 search_evidence：证据按站点独立配额（多站点各有代表证据）")

            # B3：--progress-interval 生效（进度按指定间隔打印到 stderr）
            p3 = tmp / "p3"
            r = run([unk, za, "-o", p3, "--progress", "--progress-interval", "1"])
            check(r.returncode == 0 and ("processed" in r.stdout or "processed" in r.stderr),
                  "B3 unpack_logs：--progress-interval 生效（进度按间隔打印）")

            # B4：merge 输出 merged_delta；多包历史默认拒绝覆盖，force 显式覆盖
            m4 = tmp / "m4"
            run([unk, za, "-o", m4, "--merge-manifest"])
            r = run([unk, zb, "-o", m4, "--merge-manifest"])
            mj4 = json.loads(r.stdout) if r.stdout else {}
            check(r.returncode == 0 and mj4.get("merged_delta") == 1
                  and mj4.get("merged_from") == 2,
                  "B4 unpack_logs：merge 输出 merged_delta=1、merged_from=2")
            r = run([unk, zb, "-o", m4], expect_rc=3)  # 不带 merge/force → 拒绝覆盖
            check(r.returncode == 3 and "拒绝覆盖" in r.stderr
                  and json.loads((m4 / "manifest.json").read_text(encoding="utf-8"))
                      .get("sources") and len(json.loads(
                          (m4 / "manifest.json").read_text(encoding="utf-8"))["sources"]) == 2,
                  "B4 unpack_logs：默认拒绝覆盖多包历史（rc=3）且 manifest 不被改写")
            r = run([unk, zb, "-o", m4, "--force-overwrite"])  # 显式覆盖放行
            check(r.returncode == 0 and "force-overwrite" in r.stderr,
                  "B4 unpack_logs：--force-overwrite 显式覆盖并给提示")
            # 单包重跑（无 sources 历史）幂等，不拦截
            s4 = tmp / "s4"
            r0 = run([unk, za, "-o", s4]); r1 = run([unk, za, "-o", s4])
            check(r0.returncode == 0 and r1.returncode == 0,
                  "B4 unpack_logs：单包重跑幂等（无 sources 历史不拦截）")

            # 能力1/能力2：skill_sync.py --sync（元skill→子skill）/--reclaim（子skill→元skill）
            ss = SCRIPTS / "skill_sync.py"
            import hashlib as _hl
            _h = lambda p: _hl.sha256(p.read_bytes()).hexdigest()
            if ss.exists() and (sd / "assets" / "fault-profile.yaml").exists():
                sync_child = sd  # 复用已有 report 组件子 skill
                # 备份子 skill 原始脚本，测后还原
                bak = tmp / "sync_bak"
                bak.mkdir(exist_ok=True)
                shutil.copy2(sync_child / "scripts" / "search_evidence.py",
                             bak / "search_evidence.py")
                shutil.copy2(SCRIPTS / "search_evidence.py", bak / "builder_se.py")
                try:
                    # --sync：改元 skill 基础脚本 → sync 应把新版本同步到子 skill
                    (SCRIPTS / "search_evidence.py").write_text(
                        (SCRIPTS / "search_evidence.py").read_text(encoding="utf-8")
                        + "\n# sync-marker\n", encoding="utf-8")
                    h_new = _h(SCRIPTS / "search_evidence.py")
                    r = run([ss, "--sync", str(sync_child)])
                    h_child = _h(sync_child / "scripts" / "search_evidence.py")
                    check(r.returncode == 0 and h_child == h_new,
                          "能力1 skill_sync --sync：元 skill 基础脚本变更同步到子 skill")
                    # --reclaim：子 skill 再改基础脚本 → dry-run 列出差异，--apply 回灌元 skill（scripts+模板同源）
                    (sync_child / "scripts" / "search_evidence.py").write_text(
                        (sync_child / "scripts" / "search_evidence.py").read_text(encoding="utf-8")
                        + "\n# child-fix\n", encoding="utf-8")
                    h_fix = _h(sync_child / "scripts" / "search_evidence.py")
                    r = run([ss, "--reclaim", str(sync_child)])
                    check(r.returncode == 0 and "search_evidence.py" in r.stdout
                          and "APPLY" not in r.stdout,
                          "能力2 skill_sync --reclaim：dry-run 列出基础能力差异（未回灌）")
                    r = run([ss, "--reclaim", str(sync_child), "--apply"])
                    h_bs = _h(SCRIPTS / "search_evidence.py")
                    check(r.returncode == 0 and h_bs == h_fix
                          and "已回灌" in r.stdout,
                          "能力2 skill_sync --reclaim --apply：回灌元 skill（builder 单一源）")
                    # 子 skill 特有 profile 派生件不被回收（不在 reclaim 差异列表）
                    check("fault-profile" not in r.stdout,
                          "能力2 skill_sync --reclaim：子 skill 特有 profile 不回灌")
                finally:
                    shutil.copy2(bak / "search_evidence.py", SCRIPTS / "search_evidence.py")
                    shutil.copy2(bak / "search_evidence.py", sync_child / "scripts" / "search_evidence.py")

        _s11()

    if 12 in _want:
        def _s12():
            print("\n[12] 规则库导入：import_rule_library / rule_library_diff")
            import importlib.util
            imp = SCRIPTS / "import_rule_library.py"
            dif = SCRIPTS / "rule_library_diff.py"
            check(imp.exists() and dif.exists(), "§12: 导入器与差异脚本文件存在")
            rldir = tmp / "rl"; rldir.mkdir(exist_ok=True)
            # 合成黑匣子 xlsx：规则 sheet VSWd0 + 样本 sheet USB（无规则列）→ 验证 sheet 级过滤
            bb = _make_xlsx_sheets([
                ("VSWd0", [["关键字", "日志路径和文件名", "故障呈现", "检查规则"],
                           ["flash error", "/logs/BSP/BspLog.log", "FLASH损坏", "检查FLASH"],
                           ["usb fail; ssd err", "/logs/xxx.log", "USB异常", "检查USB"],
                           ["eid is (****)", "BspLog.log", "EID错误", ""]]),
                ("USB", [["print_req_error: I/O error", "", ""], ["kernel panic", "", ""]]),
            ])
            bbf = rldir / "bb.xlsx"; bbf.write_bytes(bb)
            bbout = rldir / "bb.yaml"
            r = run([imp, "--source", bbf, "--out", bbout,
                     "--type", "blackbox", "--board", "VSWd0"], expect_rc=None)
            check(r.returncode == 0 and bbout.exists(), "§12: 黑匣子导入产出候选 profile")
            bb_txt = bbout.read_text(encoding="utf-8")
            check('mode: "triage"' in bb_txt and "mode:" in bb_txt,
                  "§12: 候选 profile 含 mode: triage")
            check('verified: false' in bb_txt and "BspLog.log" in bb_txt and "FLASH损坏" in bb_txt,
                  "§12: profile 含 verified:false / 日志glob / 故障呈现文本")
            check(bb_txt.count('- id: "E') >= 3, "§12: 候选 profile 规则条数 ≥3（带引号 ID 形式）")
            # USB 样本 sheet 被过滤：不得产出 USB 相关规则
            check("print_req_error" not in bb_txt, "§12: 非规则 sheet(USB) 被按 sheet 过滤跳过")
            # --check 通过（triage 豁免）
            rc = run([SCRIPTS / "scaffold_child_skill.py",
                      "--profile", bbout, "--check"], expect_rc=None)
            check(rc.returncode == 0, "§12: scaffold --check 对黑匣子候选 profile 通过（triage）")
            # 合成蜂鸟 xlsx：首 sheet 为无规则列汇总 → 验证跨 sheet 探测
            hb = _make_xlsx_sheets([
                ("汇总", [["巡检网元总数", "开始时间", "结束时间", "耗时"], ["10", "10:00", "11:00", "1h"]]),
                ("规则", [["规则ID", "规则类型", "模块", "硬件类型", "规则名称", "详细信息", "具体影响", "处理建议"],
                          ["R1", "文件系统", "VBP", "ITRAN", "检查单板文件系统", "1.执行ushell命令检查FLASH空间", "", "格式化"]]),
                ("网元数据", [["网元名称", "网元ID", "版本"], ["NE1", "1", "V100"]]),
            ])
            hbf = rldir / "hb.xlsx"; hbf.write_bytes(hb)
            hbout = rldir / "hb.yaml"
            r = run([imp, "--source", hbf, "--out", hbout,
                     "--type", "hummingbird", "--board", "ITRAN"], expect_rc=None)
            check(r.returncode == 0 and hbout.exists(), "§12: 蜂鸟导入产出候选 profile")
            hb_txt = hbout.read_text(encoding="utf-8")
            check("FLASH空间" in hb_txt or "FLASH" in hb_txt, "§12: 蜂鸟候选含领域词锚点")
            rc = run([SCRIPTS / "scaffold_child_skill.py",
                      "--profile", hbout, "--check"], expect_rc=None)
            check(rc.returncode == 0, "§12: 蜂鸟候选 profile --check 通过")
            # 目录导入 → 每文件一个 profile，slug 不冲突
            rld = tmp / "rld"; rld.mkdir(exist_ok=True)
            (rld / "vswd0.xlsx").write_bytes(bb)
            # 合成 vswd0-0804（同板新版）→ slug 须不同
            bb804 = _make_xlsx_sheets([
                ("VSWd0", [["关键字", "日志路径和文件名", "故障呈现", "返修工序是否拦截"],
                           ["flash error", "/logs/BSP/BspLog.log", "FLASH损坏", "是"]]),
            ])
            (rld / "vswd0-0804.xlsx").write_bytes(bb804)
            rd_out = tmp / "rld_out"; rd_out.mkdir(exist_ok=True)
            r = run([imp, "--source", rld, "--out", rd_out,
                     "--json", str(tmp / "rl_meta.json")], expect_rc=None)
            names = sorted(p.name for p in rd_out.glob("*.yaml"))
            check(len(names) == 2 and len(set(names)) == 2,
                  f"§12: 目录导入每文件一个 profile 且 slug 不冲突（{names}）")
            # rule_library_diff：两个不同规则集 → added+removed
            dr = run([dif, rld / "vswd0.xlsx", rld / "vswd0-0804.xlsx",
                      "--json", str(tmp / "rl_diff.json")], expect_rc=None)
            check(dr.returncode == 0, "§12: rule_library_diff 运行成功")
            dj = json.loads((tmp / "rl_diff.json").read_text(encoding="utf-8"))
            check(dj["added"] or dj["removed"], "§12: diff JSON 含非空 added/removed")

        _s12()

    if 13 in _want:
        def _s13():
            _make_xlsx_multi = _make_xlsx_sheets
            def to_yaml_for_test(o):
                sys.path.insert(0, str(SCRIPTS))
                import import_rule_library as _m
                return _m.to_yaml(o) + chr(10)
            print("\n[13] 规则库导入增强 / triage 闭环 / 告警码列 / 无 openpyxl")
            imp, dif = SCRIPTS / "import_rule_library.py", SCRIPTS / "rule_library_diff.py"
            check(not (ROOT / "assets/templates/child-skill/scripts/import_rule_library.py").exists(),
                  "导入器不进模板目录")
            d = tmp / "rulelib"
            d.mkdir()
            hdr = ["故障呈现", "检查规则", "关键字", "日志路径和文件名", "维修建议"]
            bb_rows = [hdr,
                       ["SNR芯片故障", "查BspLog", "snr fail;bbu shelf eid is (***)", "/logs/BSP/BspLog.log", "换板"],
                       ["IPMI字节错误", "查ipmi", "ipmi byte error", "/logs/ipmi.log", "重启"],
                       ["风扇异常", "查fan", "fan{1,3} err", "/logs/fan.log", "换风扇"],
                       ["", "", "", "", ""]]
            sample = [["print_req_error: I/O error"], ["x"]]
            bb = _make_xlsx_multi([("VSWd0", bb_rows), ("USB", sample), ("Sheet1", bb_rows[:3])])
            (d / "vswd0.xlsx").write_bytes(bb)
            bb2_rows = [hdr, bb_rows[1][:2] + ["snr fail2"] + bb_rows[1][3:], bb_rows[2],
                        ["新增故障", "c", "newkw", "/logs/new.log", "m"]]
            (d / "vswd0-0804.xlsx").write_bytes(_make_xlsx_multi([("VSWd0", bb2_rows)]))
            hb_rows = [["规则ID", "规则类型", "模块", "规则名称", "详细信息", "处理建议"],
                       ["1", "t", "m", "升级所需FLASH空间是否不充足", "1.登陆主控板 2.检查FLASH空间 查看 run.log", "清理"],
                       ["2", "t", "m", "license是否未绑定", "检查【EID】与license", "绑定"]]
            hb = _make_xlsx_multi([("汇总", [["巡检网元总数", "耗时"], ["1", "2"]]), ("规则", hb_rows),
                                   ("巡检故障报告", [["规则ID", "规则名称", "详细信息"]])])
            hbf = tmp / "hb" / "HCC REPORT_SDR-V1-故障挖掘.xlsx"
            hbf.parent.mkdir()
            hbf.write_bytes(hb)
            (d / "Metrics.csv").write_bytes("Metrics对象,Metrics类型编号,Metrics名称\nA,1,温度\n".encode("gbk"))

            out1 = tmp / "bb1.yaml"
            r = run([imp, "--source", d / "vswd0.xlsx", "--out", out1, "--json", tmp / "bb1.json"])
            txt = out1.read_text(encoding="utf-8") if out1.exists() else ""
            check(r.returncode == 0 and 'mode: "triage"' in txt, "黑匣子导入 → mode: triage")
            check("skill" in out1.read_text(encoding="utf-8")
                  and (tmp / "bb1.json").exists() and not (tmp / "bb1.meta.json").exists(),
                  "§13: profile 仅 yaml，--json 元数据直接输出，不再撞名改写")
            check("verified: false" in txt and "BspLog.log" in txt and "SNR芯片故障" in txt,
                  "候选含 verified:false / 日志 glob / 故障呈现")
            check(txt.count('- id: "E') >= 3 and "样本" not in txt and "print_req_error" not in txt,
                  "规则 ≥3 且样本 sheet 被过滤")
            check(txt.count('(来源') == 3 or txt.count("来源 ") == 3, "双签名 sheet 去重（Sheet1 重复规则已合并）")
            r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", out1, "--check"])
            check(r.returncode == 0 or "PyYAML" in (r.stdout + r.stderr), "黑匣子候选 --check 通过（含非法正则转义）")

            out2 = tmp / "hb.yaml"
            r = run([imp, "--source", hbf, "--out", out2])
            t2 = out2.read_text(encoding="utf-8") if out2.exists() else ""
            check(r.returncode == 0 and "FLASH空间" in t2 and "any_of" in t2, "蜂鸟跨 sheet 探测 + 领域词锚点")
            r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", out2, "--check"])
            check(r.returncode == 0 or "PyYAML" in (r.stdout + r.stderr), "蜂鸟候选 --check 通过")

            od = tmp / "bb_dir"
            r = run([imp, "--source", d, "--out", od])
            ys = sorted(p.name for p in od.glob("*.yaml"))
            check(len(ys) == 2 and len(set(ys)) == 2, f"目录导入按文件拆分且 slug 不冲突：{ys}")
            check("Metrics" in r.stderr, "Metrics.csv 字典被跳过并提示")

            dj = tmp / "diff.json"
            r = run([dif, d / "vswd0.xlsx", d / "vswd0-0804.xlsx", "--json", dj])
            dd = json.loads(dj.read_text(encoding="utf-8"))
            check(dd["added"] and dd["removed"], "diff 含 added 与 removed")
            check(any(c["rule"] == "SNR芯片故障" for c in dd["changed"]), "diff 检出关键字变更（changed）")
            try:
                import yaml as _y
                prof = _y.safe_load(txt)
                check(prof["mode"] == "triage" and len(prof["analysis"]["steps"]) == 3
                      and prof["analysis"]["steps"][0]["rules"][0]["any_of"][0] == "snr fail",
                      "最小 YAML 序列化可被 PyYAML 还原")
            except ImportError:
                print("  [skip] 无 PyYAML，跳过 YAML 往返断言")
            jprof = tmp / "bb1.json"
            try:
                import yaml as _y
                jprof.write_text(json.dumps(_y.safe_load(txt), ensure_ascii=False), encoding="utf-8")
                for name, same in (("vswd0.xlsx", True), ("vswd0-0804.xlsx", False)):
                    r = run([dif, d / name, jprof, "--json", tmp / "rt.json"])
                    rt = json.loads((tmp / "rt.json").read_text(encoding="utf-8"))
                    n = len(rt["added"]) + len(rt["removed"]) + len(rt["changed"])
                    check((n == 0) == same, f"xlsx 与其候选 profile 往返 diff：{name} 差异 {n}")
            except ImportError:
                pass
            hbj = tmp / "hbp.yaml"
            r = run([imp, "--source", hbf, "--out", hbj])
            check("日志路径为空" not in r.stderr, "蜂鸟无路径列不触发空路径警告")
            emp = d / "empty-path.xlsx"
            emp.write_bytes(_make_xlsx_multi([("VSWd0", [hdr, ["无路径", "c", "kw1", "", "m"]])]))
            r = run([imp, "--source", emp, "--out", tmp / "emp.yaml"])
            check("日志路径为空" in r.stderr, "黑匣子空路径给出回退警告")
            r = run([imp, "--source", d, "--out", tmp / "x.yaml"], expect_rc=None)
            check(r.returncode != 0, "目录输入 + .yaml 输出 → 拒绝")

            # --- 导入器增强：baseline / merge-into / json 元数据 / 分组筛选 / step-by / 关键字频次 ---
            r = run([imp, "--source", d / "vswd0.xlsx", "--list-groups"])
            check("SNR芯片故障" in r.stdout and not list(d.glob("*.yaml")), "--list-groups 只列分组不写文件")
            r = run([imp, "--source", d / "vswd0.xlsx", "--out", tmp / "only.yaml", "--only-groups", "IPMI"])
            ot = (tmp / "only.yaml").read_text(encoding="utf-8")
            check("IPMI字节错误" in ot and "SNR芯片故障" not in ot, "--only-groups 只导出匹配分组")
            r = run([imp, "--source", d / "vswd0.xlsx", "--out", tmp / "sheet.yaml", "--step-by", "sheet"])
            st = (tmp / "sheet.yaml").read_text(encoding="utf-8")
            check(st.count('- id: "S') == 1 and st.count('- id: "E') == 3, "--step-by sheet 合并为 1 个步骤、规则 ID 不变")
            r = run([imp, "--source", d / "vswd0-0804.xlsx", "--out", tmp / "base2.yaml", "--baseline", out1])
            b2 = (tmp / "base2.yaml").read_text(encoding="utf-8")
            check('- id: "E4"' in b2 and b2.count('- id: "E') == 3, "--baseline：新增规则取 max+1，不复用旧编号")
            try:
                import yaml as _y
                bp = _y.safe_load(b2)
                ids = {x["description"].split("（来源")[0]: x["id"] for s in bp["analysis"]["steps"] for x in s["rules"]}
                op = _y.safe_load(txt)
                oids = {x["description"].split("（来源")[0]: x["id"] for s in op["analysis"]["steps"] for x in s["rules"]}
                check(all(ids[k] == oids[k] for k in ids if k in oids), "--baseline：同名规则保持旧 ID")
                mg = tmp / "merge.yaml"
                mg.write_text(txt, encoding="utf-8")
                mp = _y.safe_load(txt)
                mp["analysis"]["steps"][0]["rules"][0]["verified"] = True
                mp["analysis"]["steps"][0]["rules"][0]["any_of"] = ["HUMAN-EDIT"]
                mp.pop("mode")
                mg.write_text(json.dumps(mp, ensure_ascii=False), encoding="utf-8")
                mgy = tmp / "merge2.yaml"
                mgy.write_text(to_yaml_for_test(mp), encoding="utf-8")
                r = run([imp, "--source", d / "vswd0-0804.xlsx", "--merge-into", mgy])
                mm = _y.safe_load(mgy.read_text(encoding="utf-8"))
                allr = [x for s in mm["analysis"]["steps"] for x in s["rules"]]
                first = allr[0]
                check(first["verified"] is True and first["any_of"] == ["HUMAN-EDIT"] and "mode" not in mm,
                      "--merge-into：人工 verified/关键字/去 triage 均保留")
                check(any(x.get("verified") is False and "新增故障" in x["description"] for x in allr),
                      "--merge-into：新增规则 verified:false 追加")
                check(any((x.get("library_meta") or {}).get("library_removed") for x in allr),
                      "--merge-into：库中已删除规则标记 library_removed 而非删除")
                check((tmp / "merge2.yaml.bak").exists() and not mgy.with_suffix(".json").exists(),
                      "--merge-into 备份 yaml")
            except ImportError:
                print("  [skip] 无 PyYAML，跳过 baseline/merge 的 YAML 断言")
            r = run([dif, d / "vswd0.xlsx", d / "vswd0.xlsx", "--fail-on-change"])
            check(r.returncode == 0, "diff --fail-on-change：无差异退出 0")
            r = run([dif, d / "vswd0.xlsx", d / "vswd0-0804.xlsx", "--fail-on-change"], expect_rc=None)
            check(r.returncode == 1, "diff --fail-on-change：有差异退出 1")
            sys.path.insert(0, str(SCRIPTS))
            import import_rule_library as _irl
            kws = _irl.keywords_from([{"title": "单板SNR芯片及外设发生硬件故障"}, {"title": "SNR时钟异常"},
                                     {"title": "风扇转速异常"}])
            check(kws[0] == "SNR" and "异常" not in kws, f"关键字按频次挑选且去停用词：{kws[:4]}")

            # --- triage 端到端：scaffold → search → render → verify，router 默认跳过 ---
            sk_root = tmp / "skills_tr"
            r = run([SCRIPTS / "scaffold_child_skill.py", "--profile", out1, "--path", sk_root])
            sk = sk_root / "bbu-fault-vswd0"
            check(r.returncode == 0 and (sk / "SKILL.md").exists(), "triage 候选可真实 scaffold 生成子 Skill")
            sm = (sk / "SKILL.md").read_text(encoding="utf-8") if (sk / "SKILL.md").exists() else ""
            check("分诊候选集" in sm and "{{" not in sm and "**不得归因**" in sm
                  and "以 `verdict` 为判定基准" not in sm, "triage 子 SKILL.md 渲染 TRIAGE 块并去掉 verdict 判定条款")
            import yaml as _y
            er = _y.safe_load((sk / "assets" / "evidence-rules.yaml").read_text(encoding="utf-8"))
            check(er.get("mode") == "triage" and er["rules"][0].get("verified") is False,
                  "evidence-rules.yaml 带 mode 与 verified:false")
            tw = tmp / "tr_work" / "logs" / "BSP"
            tw.mkdir(parents=True)
            (tw / "BspLog.log").write_text("2026-09-10 08:00:00 snr fail on chip U1\\n", encoding="utf-8")
            ev_tr = tmp / "ev_tr.json"
            r = run([sk / "scripts" / "search_evidence.py", tmp / "tr_work", "--rules", sk / "assets" / "evidence-rules.yaml",
                     "--json", ev_tr, "--brief", tmp / "brief_tr.md"])
            evd = json.loads(ev_tr.read_text(encoding="utf-8")) if ev_tr.exists() else {}
            hit_unv = [x for x in evd.get("results", []) if x.get("hit") and x.get("verified") is False]
            check(evd.get("mode") == "triage" and "verdict" not in evd and hit_unv
                  and evd.get("unverified_hit_rules"), "triage 检索：mode 透传、无 verdict、命中规则带 verified:false")
            bt = (tmp / "brief_tr.md").read_text(encoding="utf-8") if (tmp / "brief_tr.md").exists() else ""
            check("分诊候选集" in bt and "HIT(未验证)" in bt, "triage brief 标注未验证命中")
            rp = tmp / "tr_report.md"
            r = run([sk / "scripts" / "render_report.py", "--evidence", ev_tr, "--md", rp, "--html", tmp / "tr_report.html",
                     "--description", "SNR 故障"])
            rmd = rp.read_text(encoding="utf-8") if rp.exists() else ""
            check("分诊线索报告" in rmd and "未发现本故障" not in rmd and "- **根因**：" not in rmd
                  and "BspLog.log" in rmd and "<!-- ev:EV4:" in rmd, "triage 报告：线索报告、无根因/未匹配措辞、含指纹证据")
            r = run([sk / "scripts" / "render_report.py", "--verify", rp, "--evidence", ev_tr], expect_rc=None)
            check(r.returncode == 2, "triage 报告 TODO 未清零 → --verify 拒绝")
            rp.write_text(re.sub(r"<!-- TODO:.*?-->", "已人工核实", rmd), encoding="utf-8")
            run([sk / "scripts" / "render_report.py", "--from-md", rp, "--html", tmp / "tr_report.html", "--evidence", ev_tr])
            r = run([sk / "scripts" / "render_report.py", "--verify", rp, "--evidence", ev_tr, "--html", tmp / "tr_report.html"])
            check(r.returncode == 0, "triage 报告补全 TODO 后 --verify 通过")
            cleared = re.sub(r"<!-- TODO:.*?-->", "已人工核实", rmd)
            check("snr fail" in cleared, "（前置）报告含可篡改的证据原文")
            rp.write_text(cleared.replace("snr fail", "snr FAKE", 1), encoding="utf-8")
            r = run([sk / "scripts" / "render_report.py", "--verify", rp, "--evidence", ev_tr], expect_rc=None)
            check(r.returncode == 2 and "未被篡改" in r.stdout, "triage 报告证据被篡改（TODO 已清）→ --verify 拒绝")
            rj = tmp / "router_tr.json"
            run([ROOT.parent / "bbu-fault-router" / "scripts" / "router.py", "--description", "SNR芯片故障",
                 "--skills-dir", sk_root, "--json", rj])
            rd = json.loads(rj.read_text(encoding="utf-8"))
            check("bbu-fault-vswd0" in rd.get("skipped_triage", []) and not rd["matched"] and not rd["candidates"],
                  "router 默认跳过 triage 候选并登记 skipped_triage")
            run([ROOT.parent / "bbu-fault-router" / "scripts" / "router.py", "--description", "SNR芯片故障",
                 "--skills-dir", sk_root, "--include-triage", "--json", rj])
            rd = json.loads(rj.read_text(encoding="utf-8"))
            check(rd["candidates"] and rd["candidates"][0]["triage"] and not rd["matched"],
                  "router --include-triage：仅作候选展示，不进入 matched")

            # run_analysis 一键分析：triage 摘要 status=triage、报告为线索报告并通过门禁
            ra_out = tmp / "ra_tr_out"
            r = run([sk / "scripts" / "run_analysis.py", tmp / "tr_work", "--description", "SNR 故障", "--out", ra_out])
            rs = json.loads((ra_out / "run-summary.json").read_text(encoding="utf-8")) if (ra_out / "run-summary.json").exists() else {}
            ra_md = (ra_out / "report.md").read_text(encoding="utf-8") if (ra_out / "report.md").exists() else ""
            check(r.returncode == 0 and rs.get("ok") is True and rs.get("status") == "triage" and rs.get("mode") == "triage"
                  and "分诊线索报告" in ra_md and "未发现本故障" not in ra_md and "<!-- TODO" not in ra_md,
                  "run_analysis 对 triage 候选：status=triage、线索报告、无 TODO 且通过门禁 :: " + (r.stderr or "")[-200:])
            # router / fm_alarm_align 的 xlsx 读取不再依赖 openpyxl
            azip = tmp / "alarm_pkg.zip"
            with zipfile.ZipFile(azip, "w") as zf:
                zf.writestr("fm-active-告警监控-t.xlsx", _make_xlsx([["告警码", "告警名称"], ["198096831", "x"]]))
            rj2 = tmp / "router_x.json"
            run([ROOT.parent / "bbu-fault-router" / "scripts" / "router.py", "--description", "x",
                 "--skills-dir", sk_root, "--archive", azip, "--json", rj2])
            rx = json.loads(rj2.read_text(encoding="utf-8"))
            check("198096831" in rx.get("archive_alarms", []) and "openpyxl_available" not in rx,
                  "router 纯 stdlib 读取 xlsx 告警码（无 openpyxl）")
            fmd = tmp / "fm_dir"
            fmd.mkdir()
            (fmd / "fm-active-t.xlsx").write_bytes(_make_xlsx([["告警码", "告警名称", "告警时间"],
                                                                ["198096831", "1588时钟源异常", "2026-09-10 08:00:00"]]))
            fmj = tmp / "fm_align.json"
            r = run([SCRIPTS / "fm_alarm_align.py", fmd, "--evidence", ev_tr, "--json", fmj])
            fmo = fmj.read_text(encoding="utf-8") if fmj.exists() else ""
            check(r.returncode == 0 and "198096831" in fmo, "fm_alarm_align 纯 stdlib 读取 xlsx（无 openpyxl）")
            # 真实网管导出的列布局：「告警码名称」在「告警码」之前，必须取到数字告警码列
            real_hdr = ["网元", "告警码名称", "关联", "资源类型", "告警码", "告警原因", "告警级别", "发生时间"]
            real_rows = [real_hdr, ["NE1(1)", "系统时钟不可用", "", "ITBBU 公共", "198092217", "", "严重", "2026-09-10 08:00:00"]]
            azip2 = tmp / "alarm_real.zip"
            with zipfile.ZipFile(azip2, "w") as zf:
                zf.writestr("fm-history-所有-t.xlsx", _make_xlsx(real_rows))
                zf.writestr("fm-active-t.csv", "\\n".join(",".join(r_) for r_ in real_rows).encode("utf-8").decode("utf-8"))
            run([ROOT.parent / "bbu-fault-router" / "scripts" / "router.py", "--description", "x",
                 "--skills-dir", sk_root, "--archive", azip2, "--json", rj2])
            rx = json.loads(rj2.read_text(encoding="utf-8"))
            check(rx["archive_alarms"] == ["198092217"], f"router 告警码列优先取「告警码」而非「告警码名称」：{rx['archive_alarms']}")
            fmd2 = tmp / "fm_dir_real"
            fmd2.mkdir()
            (fmd2 / "fm-history-所有-t.xlsx").write_bytes(_make_xlsx(real_rows))
            r = run([SCRIPTS / "fm_alarm_align.py", fmd2, "--evidence", ev_tr, "--json", fmj])
            fo = json.loads(fmj.read_text(encoding="utf-8"))
            check(any(a_["alarm"]["code"] == "198092217" and a_["alarm"]["name"] == "系统时钟不可用"
                      for a_ in fo.get("alarms", [])), "fm_alarm_align 兼容真实列布局（告警码名称/告警码）并识别时钟告警")

        _s13()

    print()
    result = "FAIL" if FAILS else "PASS"
    if FAILS:
        print(f"[FAIL] {len(FAILS)} 项失败：")
        for f in FAILS:
            print("  -", f)
    else:
        print("[OK] 自测全部通过")

    # 结构化结果（--json）：按节聚合，便于 CI/脚本/Windows 重定向后读取
    sections = {}
    for r in RECORDS:
        s = sections.setdefault(str(r["section"]),
                                {"section": r["section"], "total": 0, "passed": 0, "failed": 0})
        s["total"] += 1
        s["passed" if r["ok"] else "failed"] += 1
    report = {
        "tool": "bbu-fault-skill-builder selftest",
        "result": result,
        "total": len(RECORDS),
        "passed": sum(1 for r in RECORDS if r["ok"]),
        "failed": len(FAILS),
        "failures": FAILS,
        "sections": [sections[k] for k in sorted(sections, key=lambda x: (len(x), x))],
    }
    report["exit_code"] = 0 if result == "PASS" else 1
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        print(f"结构化结果已写入: {args.json_path}")
    # 固定的机器可读末行（Windows cmd /c 重定向到文件后可直接抓取，勿改名）
    print(f"SELFTEST_RESULT={result}: {len(FAILS)} 失败（共 {len(RECORDS)} 项）")

    if args.keep or FAILS:
        print(f"临时目录保留: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if result == "PASS" else 1)


def _load_profile_json():
    sys.path.insert(0, str(SCRIPTS))
    from scaffold_child_skill import load_profile  # noqa: E402
    return load_profile(TEMPLATE_PROFILE)


if __name__ == "__main__":
    main()
