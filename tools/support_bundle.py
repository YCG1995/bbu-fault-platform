#!/usr/bin/env python3
"""生成可回传的现场支持包（默认脱敏，不包含数据库与密钥）。

三种模式：
  metadata  只收环境、健康、最近任务摘要、技能版本、质量统计
  task      额外收指定任务的计划/仲裁/报告/技能证据片段与复现清单（默认）
  full      额外收原始日志输入和完整任务目录（含敏感内容，需现场确认后使用）

用法：
  python3 tools/support_bundle.py --task T2026... --out support.zip
  python3 tools/support_bundle.py --task T2026... --mode full --out support-full.zip
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bbu import __version__  # noqa: E402
from bbu.ai import AIGateway  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.evaluation import RealCaseStore  # noqa: E402
from bbu.learning import ExperienceStore  # noqa: E402
from bbu.metrics import collect as collect_metrics, prometheus  # noqa: E402
from bbu.review import ReviewService  # noqa: E402
from bbu.skills import SkillRegistry  # noqa: E402
from bbu.util import iso_now, sha256_file  # noqa: E402

SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
                r"\s*[:=]\s*[\"']?[^\s\"',;]+"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <REDACTED>"),
    (re.compile(r"(?i)cookie\s*[:=]\s*[^\n]+"), "Cookie: <REDACTED>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-<REDACTED>"),
]


def clean_text(text: str, data_dir: Path, root: Path) -> str:
    text = text.replace(str(data_dir), "<DATA_DIR>").replace(str(root), "<PLATFORM_ROOT>")
    text = text.replace(str(Path.home()), "<HOME>")
    for pattern, repl in SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def clean_obj(value: Any, data_dir: Path, root: Path) -> Any:
    if isinstance(value, str):
        return clean_text(value, data_dir, root)
    if isinstance(value, dict):
        return {k: clean_obj(v, data_dir, root) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_obj(v, data_dir, root) for v in value]
    return value


class MetricShim:
    def __init__(self, db: Database, registry: SkillRegistry, gateway: AIGateway,
                 config: Config):
        self.db = db
        self.registry = registry
        self.gateway = gateway
        self.config = config
        self.worker = self

    def stats(self) -> dict[str, Any]:
        running = self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE status='running'")["n"]
        queued = self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE status='queued'")["n"]
        return {"max_workers": 1, "running": running, "queued": queued,
                "max_queue": 1, "available": 0}


def task_payload(db: Database, config: Config, task_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise SystemExit(f"任务不存在: {task_id}")
    inputs = json.loads(row["input_json"] or "[]")
    options = json.loads(row["options_json"] or "{}")
    task_dir = config.data_dir / "tasks" / task_id
    payload = {
        "task_id": task_id,
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "stage": row["stage"],
        "mode": row["mode"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "inputs": [{"name": i.get("name"), "size": i.get("size"),
                    "sha256": i.get("sha256")} for i in inputs],
        "options": options,
    }
    artifacts: dict[str, str] = {}
    for name in ("inventory.json", "plan.json", "routing.json", "replan.json",
                 "arbitration.json", "report.md", "report.html", "error.log"):
        path = task_dir / name
        if path.is_file():
            artifacts[name] = path.read_text(encoding="utf-8", errors="replace")
    skill_rows = db.all(
        "SELECT skill_name, skill_version, status, score, verdict_json, summary_json, "
        "result_json, error FROM task_skills WHERE task_id=? ORDER BY id", (task_id,))
    skills = []
    for skill in skill_rows:
        result = json.loads(skill["result_json"] or "{}")
        evidence = result.get("evidence") or []
        skills.append({
            "skill": skill["skill_name"], "version": skill["skill_version"],
            "status": skill["status"], "score": skill["score"],
            "error": skill["error"],
            "verdict": json.loads(skill["verdict_json"] or "{}"),
            "evidence_count": len(evidence),
            "evidence_snippets": [
                {k: e.get(k) for k in ("rule", "file", "line", "message", "snippet")}
                for e in evidence[:5]
            ],
            "limitations": (result.get("limitations") or [])[:5],
        })
    payload["artifacts"] = artifacts
    payload["skills"] = skills
    payload["task_dir"] = str(task_dir)
    return payload


def add_text(zf: zipfile.ZipFile, name: str, text: str) -> None:
    zf.writestr(name, text.encode("utf-8"))


def build(args: argparse.Namespace) -> Path:
    root = Path(args.root).resolve()
    config = Config(root)
    if args.data_dir:
        config.values["data_dir"] = str(Path(args.data_dir).resolve())
        config.ensure_dirs()
    db = Database(config.db_path)
    registry = SkillRegistry(db, config.skills_dir)
    registry.scan()
    gateway = AIGateway(db, config)
    experiences = ExperienceStore(db)
    review = ReviewService(db, config.data_dir)
    quality = review.quality()
    shim = MetricShim(db, registry, gateway, config)
    metrics = collect_metrics(shim)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    task_id = args.task or ""
    task = task_payload(db, config, task_id) if task_id else None
    if args.mode in ("task", "full") and not task:
        raise SystemExit("--mode task/full 需要 --task")

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="bbu-support-"))
    files: list[dict[str, Any]] = []

    def write_file(name: str, content: str) -> None:
        target = tmp / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        files.append({"name": name, "size": target.stat().st_size,
                      "sha256": sha256_file(target)})

    environment = {
        "generated_at": iso_now(), "platform_version": __version__,
        "python": sys.version, "os": platform.platform(), "hostname": "<REDACTED>",
        "root": str(root), "data_dir": str(config.data_dir),
        "config_public": config.public(),
        "config_flags": {k: config.get(k) for k in (
            "experience_include_unreviewed", "planner_enabled", "planner_replan",
            "auto_distill", "report_llm", "max_task_resume", "max_queue")},
        "schema_version": db.setting("schema_version", "unknown"),
        "db_bytes": config.db_path.stat().st_size if config.db_path.exists() else 0,
    }
    write_file("environment.json", json.dumps(clean_obj(environment, config.data_dir, root),
                                              ensure_ascii=False, indent=2))
    write_file("health.json", json.dumps(clean_obj({
        "metrics": metrics, "quality": quality, "experience": experiences.stats(),
        "skills": [{"name": s.name, "version": s.version, "status": s.status,
                    "verified": s.verified, "channel": s.channel,
                    "health": s.health} for s in registry.list_skills(True)],
        "models": db.all("SELECT id,name,provider,model,enabled,roles,priority FROM model_configs"),
    }, config.data_dir, root), ensure_ascii=False, indent=2, default=str))
    recent = db.all(
        "SELECT id,title,status,stage,mode,created_at,finished_at,error,verdict_json "
        "FROM tasks ORDER BY created_at DESC LIMIT 20")
    write_file("recent_tasks.json", json.dumps(clean_obj([
        {"id": r["id"], "title": r["title"], "status": r["status"], "stage": r["stage"],
         "mode": r["mode"], "created_at": r["created_at"], "finished_at": r["finished_at"],
         "error": r["error"],
         "verdict": json.loads(r["verdict_json"] or "{}")} for r in recent
    ], config.data_dir, root), ensure_ascii=False, indent=2))
    write_file("metrics.txt", prometheus(metrics))
    cases = RealCaseStore(db, config.data_dir).list()
    write_file("cases.json", json.dumps(clean_obj([
        {"id": c.get("id"), "name": c.get("name"), "split": c.get("split"),
         "status": c.get("status"), "origin": c.get("origin"),
         "expected_roots": c.get("expected_roots"),
         "sha256": c.get("dataset_sha256")} for c in cases
    ], config.data_dir, root), ensure_ascii=False, indent=2))

    if task:
        task_clean = clean_obj(task, config.data_dir, root)
        reproduce = {
            "task_id": task["task_id"], "title": task["title"],
            "description": task["description"], "mode": task["mode"],
            "include_canary": bool(task.get("options", {}).get("include_canary")),
            "inputs": task["inputs"],
        }
        write_file("task.json", json.dumps(reproduce, ensure_ascii=False, indent=2))
        write_file("task_detail.json", json.dumps(task_clean, ensure_ascii=False, indent=2, default=str))
        for name, text in (task.get("artifacts") or {}).items():
            write_file(f"task/{name}", clean_text(text, config.data_dir, root))
        if args.mode == "full":
            task_dir = Path(task["task_dir"])
            if task_dir.exists():
                for path in sorted(task_dir.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(task_dir)
                    if path.stat().st_size > args.max_file_mb * 1024 * 1024:
                        continue
                    target = tmp / "task" / "full" / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    files.append({"name": str(target.relative_to(tmp)),
                                  "size": target.stat().st_size,
                                  "sha256": sha256_file(target)})
            uploads = config.data_dir / "uploads" / task["task_id"]
            if uploads.exists():
                for path in sorted(uploads.rglob("*")):
                    if not path.is_file():
                        continue
                    target = tmp / "inputs" / path.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    files.append({"name": str(target.relative_to(tmp)),
                                  "size": target.stat().st_size,
                                  "sha256": sha256_file(target)})

    shutil.copy2(root / "tools" / "reproduce_task.py", tmp / "reproduce_task.py")
    files.append({"name": "reproduce_task.py",
                  "size": (tmp / "reproduce_task.py").stat().st_size,
                  "sha256": sha256_file(tmp / "reproduce_task.py")})
    readme = f"""# BBU 故障诊断平台现场支持包

- 生成时间：{iso_now()}
- 平台版本：{__version__}
- 模式：{args.mode}
- 任务：{task_id or '（未指定）'}

## 怎么回传
把这个 zip 直接发回即可。默认模式（metadata/task）不包含原始日志、数据库和密钥；
`full` 模式包含原始输入，回传前请确认现场允许。

## 怎么复现
```bash
unzip {out.name} -d support
python3 support/reproduce_task.py --task-file support/task.json \\
  --input-dir support/inputs --out-dir data/repro \\
  --root /path/to/bbu-fault-platform
```
复现使用临时 SQLite 与确定性路径，不调用真实模型。task 模式下没有原始日志，
需要把现场原始日志放到 `support/inputs/` 后再运行；full 模式已自带。

## 包内文件
- `environment.json`：版本、操作系统、脱敏配置
- `health.json`：队列、任务、技能、模型健康、成熟度门禁
- `metrics.txt`：Prometheus 文本指标
- `recent_tasks.json`：最近任务摘要（已脱敏）
- `task.json` / `task_detail.json`：故障描述、输入指纹、计划/仲裁/证据片段
- `task/`：报告与计划产物
- `manifest.json`：文件清单与 SHA256

## 已知限制
- `task` 模式不含原始日志，只含输入指纹与证据片段。
- `full` 模式含原始日志，可能包含站点标识、IP、单板序列号，回传前请确认。
- 真实案例没有标签时不能计算现网准确率；合成案例结果只代表规则闭环。
"""
    write_file("README.md", readme)
    manifest = {
        "created_at": iso_now(), "platform_version": __version__, "mode": args.mode,
        "task_id": task_id, "contains_raw_logs": args.mode == "full",
        "files": files,
    }
    (tmp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(tmp.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(tmp).as_posix())
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps({"ok": True, "out": str(out), "mode": args.mode, "task": task_id,
                      "files": len(files) + 1,
                      "size": out.stat().st_size,
                      "sha256": sha256_file(out)}, ensure_ascii=False, indent=2))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 BBU 现场支持包")
    parser.add_argument("--task", default="", help="任务 ID（task/full 模式必填）")
    parser.add_argument("--mode", choices=("metadata", "task", "full"), default="task")
    parser.add_argument("--out", default="", help="输出 zip，默认 data/support-<时间>.zip")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--max-file-mb", type=int, default=200,
                        help="full 模式单个文件上限（MB）")
    args = parser.parse_args()
    if not args.out:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.out = str(Config(Path(args.root)).data_dir / f"support-{args.mode}-{stamp}.zip")
    build(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
