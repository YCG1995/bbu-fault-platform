#!/usr/bin/env python3
"""把经过回归验证的推理语料写成“专家排查剧本”。

两种用途：
- 默认写数据库，本地立即生效；
- --export 写 knowledge/reasoning_playbooks.json，随离线包分发，平台启动自动加载。

经验来源标记 curated_reasoning，只提供排查路径，不预判根因。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.db import Database  # noqa: E402
from bbu.learning import ExperienceStore  # noqa: E402
from bbu.util import iso_now  # noqa: E402


def build_payloads(records: list[dict]) -> list[dict]:
    by_skill: dict[str, list[dict]] = {}
    for rec in records:
        by_skill.setdefault(rec["skill"], []).append(rec)
    payloads = []
    for skill, rows in by_skill.items():
        first = rows[0]
        root_names = list(dict.fromkeys((r.get("root_cause_name") or "")
                                       for r in rows if r.get("root_cause_name")))
        strategy = next((r.get("strategy") for r in rows if r.get("strategy")), "")
        pitfalls: list[str] = []
        stop: list[str] = []
        for r in rows:
            pitfalls += r.get("pitfalls") or []
            stop += r.get("stop_conditions") or []
        discriminators = []
        for r in rows:
            for d in r.get("discriminators") or []:
                if d not in discriminators:
                    discriminators.append(d)
        steps = [
            {"id": f"P{i}", "title": d.get("action"),
             "skills": [skill], "gate": "evidence",
             "objective": "按专家路径核对规则 " + "、".join(d.get("rule_ids") or [])}
            for i, d in enumerate(first.get("decision_order", [])[:8], 1)
        ]
        plan = {
            "summary": "专家排查剧本（仅约束排查路径，不预判根因）：" + (strategy or first.get("fault", "")),
            "hypothesis": root_names[:6],
            "steps": steps,
            "source": "curated_reasoning",
        }
        outcome = {
            "status": "done",
            "confidence": "reference",
            "chosen_skill": skill,
            "root_cause": "",
            "reason": "种子经验只提供竞争假设、鉴别点与停止条件；根因必须由本次任务证据重新判定。",
            "missing_info": [],
            "skills": [{"skill": skill, "status": "done", "root_cause": "", "score": 5.0, "evidence": []}],
            "source": "curated_reasoning",
            "pitfalls": list(dict.fromkeys(pitfalls))[:8],
            "discriminators": discriminators[:8],
            "stop_conditions": list(dict.fromkeys(stop))[:8],
        }
        payloads.append({
            "task_id": f"CURATED-{skill}-PLAYBOOK",
            "skill_name": skill,
            "description": ("[专家推理剧本] " + first.get("fault", "") + "：" + (strategy or "")
                            + " 竞争假设：" + "、".join(root_names) + "。")[:6000],
            "hints": [skill, first.get("fault")],
            "plan": plan,
            "outcome": outcome,
        })
    return payloads


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data/bbu.db"))
    ap.add_argument("--dataset", default=str(ROOT / "datasets/diagnostic_reasoning/corpus.jsonl"))
    ap.add_argument("--export", help="导出 JSON 供平台启动时自动加载")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    records = [json.loads(line) for line in Path(args.dataset).read_text(encoding="utf-8").splitlines() if line.strip()]
    payloads = build_payloads(records)
    if args.export:
        out = Path(args.export)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"exported": len(payloads), "path": str(out)}, ensure_ascii=False))
        return 0
    if args.dry_run:
        for item in payloads:
            print(json.dumps({"task_id": item["task_id"], "skill": item["skill_name"],
                              "description": item["description"][:220]}, ensure_ascii=False))
        return 0
    db = Database(Path(args.db))
    store = ExperienceStore(db)
    db.execute("DELETE FROM plan_experiences WHERE task_id LIKE 'CURATED-%'")
    inserted = 0
    for item in payloads:
        db.execute(
            "INSERT INTO plan_experiences(task_id,skill_name,root_cause_name,status,confidence,"
            "description,hints_json,plan_json,outcome_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item["task_id"], item["skill_name"], "", "done", "reference", item["description"],
             json.dumps(item["hints"], ensure_ascii=False),
             json.dumps(item["plan"], ensure_ascii=False, default=str),
             json.dumps(item["outcome"], ensure_ascii=False, default=str), iso_now()))
        inserted += 1
    print(json.dumps({"inserted": inserted, "stats": store.stats()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
