#!/usr/bin/env python3
"""离线复现一个诊断任务（不需要真实模型，确定性路径）。

用法：
  python3 tools/reproduce_task.py --task-file support/task.json \
      --input-dir support/inputs --out-dir data/repro/<task_id>

support/task.json 由 tools/support_bundle.py 生成，包含故障描述、模式、输入文件
清单与原始 SHA256。脚本会在临时 SQLite 与临时任务目录里重新跑一遍 TaskPipeline，
并把复现结果写到 out-dir；不会写入生产库，也不会调用真实模型。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("BBU_PLATFORM_ROOT") or Path(__file__).resolve().parents[1])
if not (ROOT / "bbu").exists():
    candidate = Path.cwd()
    if (candidate / "bbu").exists():
        ROOT = candidate
sys.path.insert(0, str(ROOT))



def load_task(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("description"):
        raise SystemExit("task.json 缺少 description")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="离线复现 BBU 诊断任务")
    parser.add_argument("--task-file", required=True, help="support bundle 中的 task.json")
    parser.add_argument("--input-dir", default=None, help="原始输入目录，默认 task.json 同级的 inputs")
    parser.add_argument("--out-dir", required=True, help="复现输出目录")
    parser.add_argument("--root", default=str(ROOT), help="平台根目录（含 bbu/、skills/）")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not (root / "bbu").exists():
        raise SystemExit(f"平台根目录不正确（缺少 bbu/）: {root}")
    sys.path.insert(0, str(root))
    from bbu.ai import AIGateway
    from bbu.config import Config
    from bbu.db import Database
    from bbu.learning import ExperienceStore
    from bbu.pipeline import TaskPipeline
    from bbu.skills import SkillRegistry, SkillRunner
    from bbu.util import iso_now, new_id, sha256_file, write_json
    task_meta = load_task(Path(args.task_file))
    input_dir = Path(args.input_dir) if args.input_dir else Path(args.task_file).resolve().parent / "inputs"
    if not input_dir.exists():
        raise SystemExit(f"输入目录不存在: {input_dir}")

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    config = Config(root)
    config.values.update({
        "data_dir": str(work / "data"),
        "skills_dir": str(root / "skills"),
        "planner_enabled": False,
        "planner_replan": False,
        "report_llm": False,
        "auto_distill": False,
        "experience_include_unreviewed": False,
        "max_llm_calls": 0,
        "max_workers": 1,
    })
    config.ensure_dirs()
    db = Database(config.db_path)
    user = db.one("SELECT id,username FROM users ORDER BY id LIMIT 1")
    if not user:
        db.execute(
            "INSERT INTO users(username,display_name,role,password_hash,created_at) "
            "VALUES(?,?,?,?,?)", ("reproduce", "复现用户", "admin", "x", iso_now()))
        user = db.one("SELECT id,username FROM users ORDER BY id LIMIT 1")

    task_id = str(task_meta.get("task_id") or new_id("R"))
    upload_dir = config.data_dir / "uploads" / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    inputs = []
    for item in task_meta.get("inputs") or []:
        name = Path(str(item.get("name") or "")).name
        if not name:
            continue
        src = input_dir / name
        if not src.exists():
            raise SystemExit(f"输入缺失: {src}")
        target = upload_dir / name
        shutil.copy2(src, target)
        inputs.append({"name": name, "stored_name": target.name,
                       "stored_path": str(target), "size": target.stat().st_size,
                       "sha256": sha256_file(target)})
    if not inputs:
        raise SystemExit("task.json 中没有可用输入文件")

    db.execute(
        "INSERT OR REPLACE INTO tasks(id,user_id,title,description,status,stage,progress,mode,"
        "input_json,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, user["id"], task_meta.get("title") or "离线复现", task_meta["description"],
         "queued", "queued", 0, task_meta.get("mode") or "fast",
         json.dumps(inputs, ensure_ascii=False),
         json.dumps({"mode": task_meta.get("mode") or "fast",
                     "include_canary": bool(task_meta.get("include_canary"))},
                    ensure_ascii=False), iso_now()))
    registry = SkillRegistry(db, config.skills_dir)
    registry.scan()
    gateway = AIGateway(db, config)
    runner = SkillRunner(config)
    pipeline = TaskPipeline(db, config, registry, gateway, runner,
                            experiences=ExperienceStore(db))
    result = pipeline.run(task_id, threading.Event())
    task = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    summary = {
        "task_id": task_id,
        "status": task["status"],
        "verdict": json.loads(task["verdict_json"] or "{}"),
        "report_md": task["report_md"],
        "report_html": task["report_html"],
        "result": result,
        "inputs": [{"name": i["name"], "sha256": i["sha256"]} for i in inputs],
    }
    write_json(out_dir / "reproduction.json", summary)
    print(json.dumps({"ok": task["status"] == "done", "task_id": task_id,
                      "status": task["status"], "report": task["report_html"],
                      "out_dir": str(out_dir)}, ensure_ascii=False, indent=2))
    return 0 if task["status"] == "done" else 2


if __name__ == "__main__":
    raise SystemExit(main())
