#!/usr/bin/env python3
"""Run the BBU child-skill pipeline once and emit a verified user-facing report."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ASSETS = SKILL / "assets"
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2",
                    ".tar.xz", ".txz", ".gz", ".bz2", ".xz", ".7z", ".rar")


class StageError(RuntimeError):
    pass


def asset(stem):
    for suffix in (".yaml", ".yml"):
        p = ASSETS / (stem + suffix)
        if p.exists():
            return p
    raise StageError(f"缺少必需资源: {stem}.yaml（规则/配置统一 .yaml）")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def log_tail(path, lines=10):
    try:
        return "\n".join(Path(path).read_text(encoding="utf-8", errors="replace")
                         .splitlines()[-lines:])
    except OSError:
        return ""


def run_stage(stage, cmd, logs):
    logs.mkdir(parents=True, exist_ok=True)
    out = logs / f"{stage}.stdout.log"
    err = logs / f"{stage}.stderr.log"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with out.open("w", encoding="utf-8") as so, err.open("w", encoding="utf-8") as se:
        proc = subprocess.run([sys.executable, *map(str, cmd)], stdout=so, stderr=se,
                              cwd=SKILL, env=env)
    if proc.returncode:
        detail = log_tail(err) or log_tail(out)
        raise StageError(f"{stage} 失败（退出码 {proc.returncode}）。日志: {err}\n{detail}")


def archive_name(path):
    name = path.name.lower()
    return any(name.endswith(s) for s in ARCHIVE_SUFFIXES)


def prepare_inputs(raw):
    inputs = [Path(p).expanduser().resolve() for p in raw]
    for p in inputs:
        if not p.exists():
            raise StageError(f"输入不存在: {p}")
    if len(inputs) > 1 and any(p.is_dir() for p in inputs):
        raise StageError("多路径输入仅支持文件；已解压目录请单独传入")
    if len(inputs) == 1 and inputs[0].is_dir():
        root = inputs[0]
        children = list(root.iterdir())
        # 外层 zip 被预先解开时，目录通常只含若干并列采集包；逐包交给 unpack 合并。
        if children and all(p.is_file() for p in children):
            return sorted(children), None
        return [root], root
    return inputs, None


def prepare_workdir(out, pre_unpacked):
    if pre_unpacked:
        try:
            if out.resolve().is_relative_to(pre_unpacked.resolve()):
                raise StageError("输出目录不能位于待分析目录内，以免把产物当作日志扫描")
        except AttributeError:  # pragma: no cover - Python 3.11+ normally provides this
            if str(pre_unpacked.resolve()) in str(out.resolve()):
                raise StageError("输出目录不能位于待分析目录内")
    work = out / "unpacked"
    marker = work / ".bbu-run-analysis-owned"
    if work.exists():
        if not marker.exists():
            raise StageError(f"拒绝覆盖非本脚本管理的目录: {work}；请更换 --out")
        shutil.rmtree(work)
    work.mkdir(parents=True)
    marker.write_text("managed by run_analysis.py\n", encoding="utf-8")
    return work


def optional_cloudmat(root, out, logs):
    script = HERE / "parse_cloudmat.py"
    if not script.exists():
        return None, []
    candidates = sorted(p for p in root.rglob("*.log") if p.is_file())
    if not candidates:
        return None, ["未找到 CloudMAT .log，跳过结构化摘要"]
    if len(candidates) > 1:
        return None, [f"检测到 {len(candidates)} 个 CloudMAT 日志；证据检索已完成，结构化摘要仅支持单日志"]
    target = out / "cloudmat.json"
    run_stage("parse_cloudmat", [script, candidates[0], "--json", target], logs)
    return target, []


# ---------------------------------------------------------------------------
# 增量缓存：unpack/search/clock/cloudmat/precheck 阶段产物可复用。
# 缓存键 = 输入（文件内容哈希 / 目录弱签名）+ rules/profile + 相关脚本 + 时区。
# 不缓存 unpacked 日志本身（体积大）；命中时恢复检索产物 + manifest。
# ---------------------------------------------------------------------------
def _sig_file(p):
    h = hashlib.sha256()
    h.update(b"F")
    h.update(str(p.stat().st_size).encode())
    h.update(p.read_bytes())
    return h.hexdigest()[:24]


def _sig_dir(p):
    h = hashlib.sha256()
    h.update(b"D")
    for sub in sorted(p.rglob("*")):
        if sub.is_file():
            rel = sub.relative_to(p).as_posix()
            st = sub.stat()
            h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()[:24]


def compute_sig(sources, rules, profile, scripts, local_tz_offset):
    h = hashlib.sha256()
    for src in sources:
        if src.is_dir():
            h.update(_sig_dir(src).encode())
        else:
            h.update(_sig_file(src).encode())
        h.update(src.name.encode())
    for p in (rules, profile, *scripts):
        if p.exists():
            h.update(p.name.encode())
            h.update(_sig_file(p).encode())
    h.update(str(local_tz_offset).encode())
    return h.hexdigest()


def restore_cached(cache_dir, sig, out, work, products):
    """按 sig.json 实际保存清单恢复产物；全部存在才命中。"""
    slot = cache_dir / sig
    meta = slot / "sig.json"
    if not (slot.is_dir() and meta.is_file()):
        return False
    try:
        names = json.loads(meta.read_text(encoding="utf-8")).get("products", [])
    except (OSError, json.JSONDecodeError):
        return False
    if not names:
        return False
    for name in names:
        if not (slot / name).is_file():
            return False
    for name in names:
        target = next((t for n, t in products if n == name), None)
        if target is None:
            return False
        shutil.copy2(slot / name, target)
    return True


def save_cache(cache_dir, sig, products):
    slot = cache_dir / sig
    slot.mkdir(parents=True, exist_ok=True)
    saved = []
    for name, target in products:
        if target.exists():
            shutil.copy2(target, slot / name)
            saved.append(name)
    (slot / "sig.json").write_text(
        json.dumps({"sig": sig, "products": saved}, ensure_ascii=False),
        encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="一键执行 BBU 日志分析并校验最终报告")
    ap.add_argument("inputs", nargs="+", help="日志包、单个日志或已解压目录；可多给文件")
    ap.add_argument("--description", required=True, help="用户故障描述原文")
    ap.add_argument("--out", help="输出目录（默认取首个输入的同级目录 <输入名>.analysis，不落在 skill 目录下）")
    ap.add_argument("--site", help="仅输出 verdict.groups 中的指定站点")
    ap.add_argument("--specialized", help="gen_specialized.py 生成的复核 JSON")
    ap.add_argument("--fm-align", help="fm_alarm_align.py 输出的 MD/JSON")
    ap.add_argument("--local-tz-offset", type=float, default=8.0,
                    help="日志时间转 UTC 的本地时区偏移（小时，默认 +8 东八区）；"
                         "跨时区采集请按日志实际时区显式传入")
    ap.add_argument("--no-cache", action="store_true",
                    help="禁用增量缓存（unpack/search/clock/cloudmat 全量重跑）")
    ap.add_argument("--no-precheck", action="store_true",
                    help="禁用 common-rules 采集完整性预检（存在 common-rules.yaml 时默认启用）")
    args = ap.parse_args()

    out = None
    logs = None
    try:
        # 显式输出目录尽早确定：即使后续输入/资源检查失败，也能留下失败摘要。
        if args.out:
            out = Path(args.out).expanduser().resolve()
            logs = out / "logs"
        rules = asset("evidence-rules")
        profile = asset("fault-profile")
        for name in ("search_evidence.py", "render_report.py"):
            if not (HERE / name).exists():
                raise StageError(f"缺少必需脚本: scripts/{name}")
        sources, pre_unpacked = prepare_inputs(args.inputs)
        if out is None:
            anchor = sources[0]
            out = anchor.parent / (anchor.stem + ".analysis")
            logs = out / "logs"
        out.mkdir(parents=True, exist_ok=True)
        work = prepare_workdir(out, pre_unpacked)

        # 阶段脚本（缓存键成分）：unpack/search/clock/cloudmat 相关，存在即计入
        stage_scripts = [HERE / n for n in ("unpack_logs.py", "search_evidence.py",
                                            "parse_clock.py", "parse_cloudmat.py",
                                            "log_format.py")]
        sig = compute_sig(sources, rules, profile, stage_scripts, args.local_tz_offset)
        cache_dir = out / ".cache"
        cache_products = [("evidence.json", out / "evidence.json"),
                          ("evidence.brief.md", out / "evidence.brief.md"),
                          ("manifest.json", work / "manifest.json"),
                          ("manifest.summary.json", work / "manifest.summary.json")]
        has_clock = (HERE / "parse_clock.py").exists()
        has_cloudmat = (HERE / "parse_cloudmat.py").exists()
        if has_clock:
            cache_products += [("clock.json", out / "clock.json"),
                               ("clock.brief.md", out / "clock.brief.md")]
        if has_cloudmat:
            cache_products.append(("cloudmat.json", out / "cloudmat.json"))
        common_rules = ASSETS / "common-rules.yaml"
        precheck = out / "precheck.json"
        use_precheck = common_rules.exists() and not args.no_precheck
        if use_precheck:
            cache_products.append(("precheck.json", precheck))

        cached = False
        if not args.no_cache and restore_cached(cache_dir, sig, out, work, cache_products):
            cached = True
        else:
            if (HERE / "unpack_logs.py").exists():
                for i, source in enumerate(sources):
                    cmd = [HERE / "unpack_logs.py", source, "-o", work,
                           "--include-rules", rules]
                    if i:
                        cmd.append("--merge-manifest")
                    run_stage(f"unpack_{i + 1:02d}", cmd, logs)
                manifest = work / "manifest.json"
                root = pre_unpacked or work
            else:
                # 纯 CloudMAT 子技能按组件裁剪后不包含 unpack_logs.py。
                # 仅接受一个非压缩日志，复制到隔离工作目录，避免扫描报告产物。
                if len(sources) != 1 or not sources[0].is_file() or archive_name(sources[0]):
                    raise StageError("当前子技能未包含解压组件；请传入单个非压缩日志文件")
                shutil.copy2(sources[0], work / sources[0].name)
                manifest = None
                root = work

            evidence = out / "evidence.json"
            brief = out / "evidence.brief.md"
            run_stage("search", [HERE / "search_evidence.py", root, "--rules", rules,
                                 "--json", evidence, "--brief", brief,
                                 "--local-tz-offset", str(args.local_tz_offset)], logs)
            ev = read_json(evidence)
            if not isinstance(ev, dict):
                raise StageError(f"检索结果无效: {evidence}")

            clock = None
            if has_clock:
                clock = out / "clock.json"
                run_stage("parse_clock", [HERE / "parse_clock.py", root, "--merge",
                                          "--json", clock, "--brief", out / "clock.brief.md"], logs)
            cloudmat, notes = optional_cloudmat(root, out, logs)
            if use_precheck:
                run_stage("precheck", [HERE / "search_evidence.py", root,
                                       "--rules", common_rules, "--json", precheck,
                                       "--local-tz-offset", str(args.local_tz_offset)], logs)
            if not args.no_cache:
                save_cache(cache_dir, sig, cache_products)

        # 从缓存恢复时 root 已不存在（unpacked 被重建清空），render 只需产物文件
        evidence = out / "evidence.json"
        ev = read_json(evidence)
        if not isinstance(ev, dict):
            raise StageError(f"检索结果无效: {evidence}")
        manifest = work / "manifest.json"
        manifest_path = manifest if manifest.exists() else None
        clock = out / "clock.json" if (has_clock and (out / "clock.json").exists()) else None
        cloudmat = out / "cloudmat.json" if (has_cloudmat and (out / "cloudmat.json").exists()) else None
        precheck_path = precheck if (use_precheck and precheck.exists()) else None
        notes = []
        if cached:
            notes.append("命中增量缓存（输入/规则/脚本未变化，unpack/search 未重跑）")

        report_base = out / "report"
        cmd = [HERE / "render_report.py", "--final", "--evidence", evidence,
               "--profile", profile, "--out", report_base,
               "--description", args.description,
               "--source", "；".join(str(p) for p in sources)]
        if manifest_path:
            cmd += ["--manifest", manifest_path]
        if clock:
            cmd += ["--clock", clock]
        if cloudmat:
            cmd += ["--cloudmat", cloudmat]
        if precheck_path:
            cmd += ["--precheck", precheck_path]
        if args.specialized:
            cmd += ["--specialized", args.specialized]
        if args.fm_align:
            cmd += ["--fm-align", args.fm_align]
        if args.site:
            cmd += ["--site", args.site]
        run_stage("render", cmd, logs)

        report_md = out / "report.md"
        report_html = out / "report.html"
        cmd = [HERE / "render_report.py", "--verify", report_md,
               "--evidence", evidence, "--html", report_html]
        if args.specialized:
            cmd += ["--specialized", args.specialized]
        if args.site:
            cmd += ["--site", args.site]
        run_stage("verify", cmd, logs)

        mf = (read_json(manifest_path, {}) or {}) if manifest_path else {}
        verdict = ev.get("verdict") or {}
        summary = {
            "ok": True,
            "cached": cached,
            "mode": ev.get("mode") or "normal",
            "status": "triage" if ev.get("mode") == "triage" else verdict.get("status", "unmatched"),
            "primary": verdict.get("primary", "NONE"),
            "sites": ev.get("detected_sites") or [s.get("root") for s in mf.get("sites", [])
                                                  if isinstance(s, dict)],
            "report_md": str(report_md),
            "report_html": str(report_html),
            "evidence_brief": str(out / "evidence.brief.md"),
            "manifest_summary": str(work / "manifest.summary.json") if (work / "manifest.summary.json").exists() else None,
            "warnings": (mf.get("warnings") or []) + notes,
            "unsupported_archives": len(mf.get("unsupported_archives") or []),
            "glob_fallback": len(ev.get("glob_fallback") or []),
        }
        (out / "run-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (StageError, OSError) as exc:
        error = {"ok": False, "error": str(exc), "logs": str(logs) if logs else None}
        if out is not None:
            try:
                out.mkdir(parents=True, exist_ok=True)
                (out / "run-summary.json").write_text(
                    json.dumps(error, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                # 输出目录不可写时仍保留 stderr 的机器可读错误，不能再抛出二次异常。
                pass
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
