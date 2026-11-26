#!/usr/bin/env python3
"""Validate and test in staging, then atomically promote a diagnostic child skill."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scaffold_child_skill import TOOLKIT_VERSION, load_profile  # noqa: E402
from run_regression import discover_cases  # noqa: E402


def tail(path, count=12):
    try:
        return "\n".join(Path(path).read_text(encoding="utf-8", errors="replace")
                         .splitlines()[-count:])
    except OSError:
        return ""


def run(stage, cmd, logs):
    out, err = logs / f"{stage}.out", logs / f"{stage}.err"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with out.open("w", encoding="utf-8") as so, err.open("w", encoding="utf-8") as se:
        proc = subprocess.run([sys.executable, *map(str, cmd)], stdout=so, stderr=se, env=env)
    if proc.returncode:
        raise RuntimeError(f"{stage} 失败（退出码 {proc.returncode}）\n"
                           f"{tail(err) or tail(out)}")


def _case_as_profile(case_dir, data, cases_root):
    """把磁盘 case.yaml 映射为 profile 的展示索引；断言始终只读 case.yaml。"""
    exp = data.get("expected") or {}
    input_cfg = data.get("input") or {}
    archive = input_cfg.get("archive") or input_cfg.get("unpacked_dir") or "input"
    rel_archive = (case_dir.relative_to(cases_root) / archive).as_posix()
    item = {
        "id": str(data.get("id") or case_dir.name),
        "type": data.get("kind", "positive"),
        "archive": rel_archive,
        "description": data.get("description", ""),
        "expected_root_cause": exp.get("root_cause"),
        "expected_evidence": list(exp.get("hit_rules") or []),
        "not_expected_evidence": list(exp.get("not_hit_rules") or []),
    }
    if data.get("notes"):
        item["notes"] = data["notes"]
    return item


def effective_profile(profile, cases_dir, skill_name):
    """以案例库为唯一执行源，生成写入子 Skill 的 profile 案例索引。

    旧 profile 中同 ID 的登记若与 case.yaml 冲突则拒绝构建；缺失/额外项只提示，
    因为磁盘案例库已经取代手工维护的 cases 清单。
    """
    import yaml

    cases_root = Path(cases_dir).resolve()
    found = discover_cases(cases_root, skill_name)
    if not found:
        raise RuntimeError(f"案例库中未发现 {skill_name} 的 case.yaml: {cases_root / skill_name}")
    derived = [_case_as_profile(cd, data, cases_root) for cd, data in found]
    by_id = {c["id"]: c for c in derived}
    if len(by_id) != len(derived):
        raise RuntimeError("案例库存在重复 case id")

    registered = list(profile.get("cases") or [])
    conflicts = []
    for old in registered:
        cid = str(old.get("id") or "")
        new = by_id.get(cid)
        if not new:
            continue
        checks = (
            ("type", old.get("type"), new["type"]),
            ("expected_root_cause", old.get("expected_root_cause"), new["expected_root_cause"]),
            ("expected_evidence", list(old.get("expected_evidence") or []), new["expected_evidence"]),
            ("not_expected_evidence", list(old.get("not_expected_evidence") or []),
             new["not_expected_evidence"]),
        )
        for field, before, after in checks:
            if before is not None and before != after:
                conflicts.append(f"案例 {cid} 的 {field}：profile={before!r}，case.yaml={after!r}")
    if conflicts:
        raise RuntimeError("profile 与 case.yaml 登记冲突；请只修改 case.yaml：\n" + "\n".join(conflicts))

    old_ids = {str(c.get("id") or "") for c in registered}
    new_ids = set(by_id)
    sync = {
        "source": str(cases_root / skill_name),
        "cases": len(derived),
        "ignored_missing_in_library": sorted(old_ids - new_ids),
        "imported_from_library": sorted(new_ids - old_ids),
    }
    profile = dict(profile)
    profile["cases"] = derived
    return profile, sync


def promote(staged, dest, backup):
    """同文件系统目录切换；新目录切换失败时恢复旧目录。"""
    had_old = dest.exists()
    if had_old:
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(dest, backup)
    try:
        os.replace(staged, dest)
    except Exception:
        if had_old and backup.exists() and not dest.exists():
            os.replace(backup, dest)
        raise


def save_logs(logs, target):
    """复制完整阶段日志；允许 target 位于 logs 内（publish 的临时日志目录）。"""
    logs, target = Path(logs).resolve(), Path(target).resolve()
    files = [p for p in logs.rglob("*")
             if p.is_file() and p != target and target not in p.parents]
    target.mkdir(parents=True, exist_ok=True)
    for p in files:
        q = target / p.relative_to(logs)
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, q)
    return str(target)


def _sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def write_attestation(skill_dir: Path, skill_name: str, version: str,
                      profile: dict, regression: dict | None, coverage: dict | None,
                      e2e: dict | None, cases_dir: str | None) -> Path:
    manifest_path = skill_dir / "assets" / "skill-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    case_files = []
    if cases_dir:
        case_files = sorted(Path(cases_dir).glob(f"{skill_name}/**/case.yaml"))
    cases = profile.get("cases") or []
    counts = {"positive": 0, "negative": 0, "boundary": 0, "other": 0}
    for c in cases:
        kind = str((c or {}).get("type") or "other")
        counts[kind if kind in counts else "other"] += 1
    attestation = {
        "schema": "build-attestation/1",
        "skill": skill_name,
        "version": version,
        "domain": manifest.get("domain") or {},
        "engine": manifest.get("engine") or {"name": "diagnostic-engine",
                                              "version": TOOLKIT_VERSION},
        "profile_sha256": _sha256_file(skill_dir / "assets" / "fault-profile.yaml")
        if (skill_dir / "assets" / "fault-profile.yaml").exists() else "",
        "rules_sha256": _sha256_file(skill_dir / "assets" / "evidence-rules.yaml")
        if (skill_dir / "assets" / "evidence-rules.yaml").exists() else "",
        "cases": {
            "manifest_sha256": _sha256_bytes("\n".join(
                _sha256_file(p) for p in case_files).encode("utf-8")),
            "files": len(case_files),
            **counts,
        },
        "regression": regression,
        "coverage": coverage,
        "e2e": e2e,
        "builder": {"name": "diagnostic-skill-builder", "version": TOOLKIT_VERSION},
        "built_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "verification": {
            "status": "case_regression" if regression is not None else "none",
            "ok": bool(regression is not None and regression.get("failed", 1) == 0
                       and regression.get("errors", 1) == 0),
        },
    }
    target = skill_dir / "assets" / "build-attestation.json"
    target.write_text(json.dumps(attestation, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def main():
    ap = argparse.ArgumentParser(description="临时构建、全量验证后原子发布诊断子 Skill")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--path", required=True, help="子 Skill 安装根目录")
    ap.add_argument("--cases-dir", help="案例库根目录；交付前默认必跑回归")
    ap.add_argument("--skip-regression", action="store_true", help="仅生成，明确标记未验证")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", help="写入构建摘要 JSON")
    ap.add_argument("--log-dir", help="保留完整阶段日志；未指定时失败也会保留到 <path>/.bbu-build-logs/<skill>")
    ap.add_argument("--domain-pack", help="域包 id 或路径；默认 bbu")
    args = ap.parse_args()
    if not args.skip_regression and not args.cases_dir:
        ap.error("交付构建需 --cases-dir；仅生成请显式使用 --skip-regression")
    if args.cases_dir and not Path(args.cases_dir).is_dir():
        ap.error(f"案例库不存在: {args.cases_dir}")

    profile_path = Path(args.profile).resolve()
    profile = load_profile(profile_path)
    name = profile["skill"]["name"]
    path_root = Path(args.path).resolve()
    path_root.mkdir(parents=True, exist_ok=True)
    dest = path_root / name
    default_failure_logs = path_root / ".bbu-build-logs" / name

    attestation_profile = profile
    with tempfile.TemporaryDirectory(prefix=f".{name}-build-", dir=path_root) as td:
        tmp = Path(td)
        logs = tmp / "logs"
        logs.mkdir()
        staging = tmp / "skills"
        staged_dest = staging / name
        backup = tmp / "previous" / name
        result = {"ok": False, "skill": name, "path": str(dest)}
        try:
            if dest.exists() and not (args.update or args.force):
                raise RuntimeError(f"目标已存在: {dest}；更新请用 --update，重建请用 --force")
            if dest.exists() and args.update:
                shutil.copytree(dest, staged_dest,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))

            build_profile_path = profile_path
            case_sync = None
            if args.cases_dir:
                effective, case_sync = effective_profile(profile, args.cases_dir, name)
                attestation_profile = effective
                import yaml
                build_profile_path = tmp / "effective-profile.yaml"
                build_profile_path.write_text(yaml.safe_dump(
                    effective, allow_unicode=True, sort_keys=False), encoding="utf-8")

            cmd = [HERE / "scaffold_child_skill.py", name, "--profile", build_profile_path,
                   "--path", staging]
            if args.domain_pack:
                cmd += ["--domain-pack", args.domain_pack]
            if args.update:
                cmd.append("--update")
            if args.force:
                cmd.append("--force")
            run("scaffold", cmd, logs)
            run("validate", [HERE / "validate_skill_md.py", staged_dest], logs)

            regression = None
            coverage = None
            e2e = None
            if not args.skip_regression:
                regression_json = logs / "regression.json"
                run("regression", [HERE / "run_regression.py", "--cases-dir", args.cases_dir,
                                   "--skill", name, "--skills-dir", staging,
                                   "--workdir", tmp / "regression-work",
                                   "--e2e", "auto", "--json", regression_json], logs)
                raw = json.loads(regression_json.read_text(encoding="utf-8"))
                regression = {k: raw.get(k, 0) for k in ("total", "passed", "failed", "errors")}
                coverage = raw.get("coverage")
                e2e = raw.get("e2e")

            version = str((profile.get("skill") or {}).get("version") or "0.1.0")
            attestation = write_attestation(staged_dest, name, version, attestation_profile,
                                            regression, coverage, e2e, args.cases_dir)

            # 回归会执行子技能脚本，Python 可能生成 __pycache__；交付前清理运行缓存。
            for cache in list(staged_dest.rglob("__pycache__")):
                shutil.rmtree(cache, ignore_errors=True)
            for pattern in ("*.pyc", "*.pyo"):
                for artifact in staged_dest.rglob(pattern):
                    artifact.unlink(missing_ok=True)

            # 只有全部验证通过才触碰正式目录。
            promote(staged_dest, dest, backup)
            result.update({
                "ok": True, "verified": not args.skip_regression,
                "regression": regression, "coverage": coverage, "e2e": e2e,
                "case_sync": case_sync,
                "attestation": str(dest / "assets" / "build-attestation.json"),
                "domain": (json.loads((dest / "assets" / "skill-manifest.json")
                                      .read_text(encoding="utf-8")).get("domain")
                           if (dest / "assets" / "skill-manifest.json").exists() else {}),
                "note": "判定、覆盖率与关键案例端到端报告均已通过" if regression is not None
                        else "仅生成，未验证，不可作为已验证技能交付",
            })
            if args.log_dir:
                result["logs"] = save_logs(logs, args.log_dir)
        except Exception as exc:
            result["error"] = str(exc)
            try:
                result["logs"] = save_logs(logs, args.log_dir or default_failure_logs)
            except OSError:
                pass
        text = json.dumps(result, ensure_ascii=False)
        if args.json:
            out_json = Path(args.json)
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(text, file=sys.stdout if result["ok"] else sys.stderr)
        return 0 if result["ok"] else 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
