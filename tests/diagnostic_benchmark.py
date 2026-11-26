#!/usr/bin/env python3
"""诊断数据基准：在 75 条合成案例上评估技能检索，而不是只看单测数量。

输出：
- 技能 Top-1 / Top-3 命中率；
- 每个技能的案例数、根因覆盖、检索失败样本；
- benchmark_report.json，供后续改 catalog/关键词时对比。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.db import Database  # noqa: E402
from bbu.skills import Matcher, SkillRegistry  # noqa: E402


def case_files(case_dir: Path) -> list[str]:
    input_dir = case_dir / "input"
    if not input_dir.exists():
        return []
    names = []
    for path in sorted(input_dir.rglob("*")):
        if path.is_file():
            names.append(path.name)
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skills-dir", default=str(ROOT / "dist/workshop-reasoning/skills"))
    ap.add_argument("--cases-dir", default=str(ROOT / "cases"))
    ap.add_argument("--json", default=str(ROOT / "datasets/diagnostic_reasoning/benchmark_report.json"))
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="bbu-diagnostic-bench-"))
    try:
        db = Database(work / "bench.db")
        registry = SkillRegistry(db, Path(args.skills_dir))
        skills = registry.scan(persist=False)
        matcher = Matcher(registry)
        records = []
        for case_path in sorted(Path(args.cases_dir).glob("*/*/case.yaml")):
            import yaml

            data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
            expected = data.get("skill")
            matches = matcher.match(str(data.get("description") or ""), case_files(case_path.parent))
            records.append({
                "id": data.get("id"), "expected_skill": expected,
                "top1": matches[0].skill.name if matches else None,
                "top1_score": round(matches[0].score, 2) if matches else 0,
                "top3": [m.skill.name for m in matches[:3]],
                "status": "PASS" if matches and matches[0].skill.name == expected else "FAIL",
            })
        total = len(records)
        top1 = sum(1 for x in records if x["status"] == "PASS")
        top3 = sum(1 for x in records if x["expected_skill"] in x["top3"])
        report = {
            "skills": [s.name for s in skills],
            "total_cases": total,
            "top1": top1,
            "top1_rate": round(top1 / total, 4) if total else 0,
            "top3": top3,
            "top3_rate": round(top3 / total, 4) if total else 0,
            "by_skill": {
                s.name: {
                    "cases": sum(1 for x in records if x["expected_skill"] == s.name),
                    "top1": sum(1 for x in records
                                if x["expected_skill"] == s.name and x["status"] == "PASS"),
                    "failures": [x["id"] for x in records
                                 if x["expected_skill"] == s.name and x["status"] == "FAIL"],
                } for s in skills
            },
            "failures": [x for x in records if x["status"] == "FAIL"],
        }
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in ("skills", "total_cases", "top1", "top1_rate",
                                                 "top3", "top3_rate")}, ensure_ascii=False, indent=2))
        for name, stat in report["by_skill"].items():
            print(f"{name}: {stat['top1']}/{stat['cases']} top1")
        if report["failures"]:
            print("失败样本:", json.dumps(report["failures"][:5], ensure_ascii=False))
        return 0 if top1 == total else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
