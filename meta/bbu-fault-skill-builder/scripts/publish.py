#!/usr/bin/env python3
"""publish.py — 一键分发：临时构建(校验+生成+回归) → 验证后替换 → 打包 → 原子同步安装位

用法：
    python3 publish.py --profile <fault-profile.yaml> --path <skills根> --cases-dir <案例库根> \
        [--zip-out <输出目录>] [--changelog <仓库根 CHANGELOG.md>] \
        [--install-dir <安装位根>] [--sync-skills <子skill目录> ...] \
        [--skip-regression] [--update] [--force] [--dry-run] [--json <result.json>]

流程（任一步失败即停，只输出失败阶段与日志尾部）：
  1. build_child_skill.py   校验 profile → 脚手架生成 → SKILL.md 规范校验 → 全案例回归
  2. package_skill.py       打包 zip + sha256，附 CHANGELOG 摘要（--changelog 提供时）
  3. 同步安装位             --install-dir 提供时，将生成的子 skill 镜像到 <install-dir>/<skill>
  4. 同步已有子 skill       --sync-skills 提供时，对每个子 skill 跑 skill_sync.py --sync

--dry-run：只打印将执行的动作（构建/打包/同步命令），不实际执行。
"""
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


def tail(path, count=12):
    try:
        return "\n".join(Path(path).read_text(encoding="utf-8", errors="replace")
                         .splitlines()[-count:])
    except OSError:
        return ""


def mirror_dir(src, dst):
    """先完整复制到同文件系统临时目录，再切换安装位；失败保留/恢复旧版本。"""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if dst == src or src in dst.parents or dst in src.parents:
        raise RuntimeError(f"安装位与构建目录相互包含，拒绝镜像: {src} -> {dst}")
    if not src.is_dir():
        raise RuntimeError(f"待同步目录不存在: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    txn = Path(tempfile.mkdtemp(prefix=f".{dst.name}-sync-", dir=dst.parent))
    staged, backup = txn / "new", txn / "old"
    try:
        # 复制失败发生在这里时，正式安装位尚未被触碰。
        shutil.copytree(src, staged, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.pyo"))
        had_old = dst.exists()
        if had_old:
            os.replace(dst, backup)
        try:
            os.replace(staged, dst)
        except Exception:
            if had_old and backup.exists() and not dst.exists():
                os.replace(backup, dst)
            raise
    finally:
        shutil.rmtree(txn, ignore_errors=True)


def persist_logs(logs, target):
    """保留完整阶段日志，不删除目标目录中已有文件。"""
    logs, target = Path(logs), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for p in logs.rglob("*"):
        if p.is_file():
            q = target / p.relative_to(logs)
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, q)
    return str(target)


def run(stage, cmd, logs):
    out, err = logs / f"{stage}.out", logs / f"{stage}.err"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with out.open("w", encoding="utf-8") as so, err.open("w", encoding="utf-8") as se:
        proc = subprocess.run([sys.executable, *map(str, cmd)], stdout=so, stderr=se, env=env)
    if proc.returncode:
        raise RuntimeError(f"{stage} 失败（退出码 {proc.returncode}）\n"
                           f"{tail(err) or tail(out)}")


def main():
    ap = argparse.ArgumentParser(description="一键构建并分发 BBU 故障子 Skill")
    ap.add_argument("--profile", required=True, help="已确认的 fault-profile.yaml")
    ap.add_argument("--path", required=True, help="子 Skill 安装根目录（构建目标）")
    ap.add_argument("--cases-dir", help="案例库根目录；交付前默认必跑回归")
    ap.add_argument("--zip-out", help="打包输出目录（默认 <path>/dist）")
    ap.add_argument("--changelog", help="仓库根 CHANGELOG.md，打包时附入 zip（不落盘技能目录）")
    ap.add_argument("--install-dir", help="安装位根目录；提供时将子 skill 镜像同步过去")
    ap.add_argument("--sync-skills", nargs="*", help="已有子 skill 目录列表；逐个跑 skill_sync.py --sync")
    ap.add_argument("--skip-regression", action="store_true", help="仅构建，明确标记未验证")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的动作，不实际执行")
    ap.add_argument("--json", help="写入发布结果 JSON")
    ap.add_argument("--log-dir", help="保留完整发布阶段日志；未指定时失败也会保留到 <path>/.bbu-publish-logs/<skill>")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, AttributeError):
                pass

    profile_path = Path(args.profile).resolve()
    if not profile_path.is_file():
        sys.exit(f"profile 不存在: {profile_path}")
    try:
        name = load_profile(profile_path)["skill"]["name"]
    except Exception as exc:
        sys.exit(f"profile 读取失败: {exc}")

    dest = Path(args.path).resolve() / name
    zip_out = Path(args.zip_out).resolve() if args.zip_out else Path(args.path).resolve() / "dist"
    changelog = Path(args.changelog).resolve() if args.changelog else None
    if changelog and not changelog.is_file():
        sys.exit(f"--changelog 不存在: {changelog}")
    install_dir = Path(args.install_dir).resolve() if args.install_dir else None
    # 安装位目标目录允许尚不存在（镜像时自动创建）；输入类路径才做存在性校验

    zip_name = f"{name}-v{TOOLKIT_VERSION}.zip"
    actions = []
    actions.append(("build", [HERE / "build_child_skill.py", "--profile", str(profile_path),
                              "--path", str(Path(args.path).resolve())] +
                             (["--cases-dir", str(Path(args.cases_dir).resolve())] if args.cases_dir else []) +
                             (["--skip-regression"] if args.skip_regression else []) +
                             (["--update"] if args.update else []) +
                             (["--force"] if args.force else [])))
    pkg = [HERE / "package_skill.py", "--skill", str(dest), "--out", str(zip_out),
           "--name", zip_name]
    if changelog:
        pkg += ["--extra", f"{changelog}:CHANGELOG.md"]
    actions.append(("package", pkg))
    if install_dir:
        actions.append(("sync-install", ["mirror", f"{dest}/", f"{install_dir}/{name}/",
                                         "(rsync -a --delete 语义，排除 __pycache__/*.pyc；纯 Python 实现，跨平台)"]))
    for sk in args.sync_skills or []:
        sp = Path(sk).resolve()
        if not sp.is_dir():
            sys.exit(f"--sync-skills 目录不存在: {sp}")
        actions.append(("sync-skill", [HERE / "skill_sync.py", "--sync", str(sp)]))

    if args.dry_run:
        print("=== dry-run：将执行以下动作 ===")
        for stage, cmd in actions:
            print(f"[{stage}] " + " ".join(str(c) for c in cmd))
        result = {"ok": True, "dry_run": True, "skill": name, "zip": str(zip_out / zip_name),
                  "actions": [s for s, _ in actions]}
        if args.json:
            Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    with tempfile.TemporaryDirectory(prefix="bbu-publish-") as td:
        logs = Path(td)
        try:
            build_cmd = actions[0][1] + ["--log-dir", str(logs / "build-logs")]
            run("build", build_cmd, logs)
            build_raw = json.loads((logs / "build.out").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1])
            run("package", actions[1][1], logs)
            digest = ""
            for ln in (logs / "package.out").read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.strip().startswith("sha256"):
                    digest = ln.strip().split()[-1]
            for stage, cmd in actions[2:]:
                if stage == "sync-install":
                    mirror_dir(dest, install_dir / name)
                else:
                    run(stage, cmd, logs)
            result = {
                "ok": True, "skill": name, "dest": str(dest),
                "verified": not args.skip_regression,
                "build": build_raw,
                "zip": str(zip_out / zip_name),
                "sha256": digest,
                "install_dir": str(install_dir) if install_dir else None,
                "synced_skills": list(args.sync_skills or []),
            }
            if args.log_dir:
                result["logs"] = persist_logs(logs, args.log_dir)
        except Exception as exc:
            result = {"ok": False, "skill": name, "error": str(exc)}
            try:
                default_logs = Path(args.path).resolve() / ".bbu-publish-logs" / name
                result["logs"] = persist_logs(logs, args.log_dir or default_logs)
            except OSError:
                pass
        if args.json:
            Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False),
              file=sys.stdout if result["ok"] else sys.stderr)
        return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
