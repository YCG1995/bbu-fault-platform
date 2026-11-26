"""评测门禁：运行或读取评测摘要，对基线、样本量和错误做硬判定。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.evaluation import EvaluationService  # noqa: E402


def compare_summary(summary: dict[str, Any], baseline: dict[str, Any] | None,
                    min_cases: int = 3) -> dict[str, Any]:
    failures: list[str] = []
    cases = int(summary.get("cases") or 0)
    errors = int(summary.get("errors") or 0)
    if cases < min_cases:
        failures.append(f"数据不足：需要至少 {min_cases} 条 golden，当前 {cases} 条")
    if errors:
        failures.append(f"评测执行错误 {errors} 条")
    if baseline:
        for key in ("exact_rate", "f1", "precision", "recall"):
            if key not in baseline:
                continue
            before = baseline.get(key)
            after = summary.get(key)
            if before is None or after is None:
                continue
            if float(after) < float(before) - 0.05:
                failures.append(f"{key} 下降超过 5pp：{before} -> {after}")
        if "false_positive_rate" in baseline and summary.get("false_positive_rate") is not None:
            before = float(baseline["false_positive_rate"])
            after = float(summary["false_positive_rate"])
            if after > before + 0.05:
                failures.append(f"false_positive_rate 上升超过 5pp：{before} -> {after}")
    return {"ok": not failures, "failures": failures,
            "cases": cases, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="BBU 评测门禁")
    parser.add_argument("--summary", help="已有评测 summary.json；不传则现场运行")
    parser.add_argument("--baseline", default=str(ROOT / "tests/fixtures/eval_baseline.json"))
    parser.add_argument("--split", default="test")
    parser.add_argument("--min-cases", type=int, default=3)
    parser.add_argument("--smoke", action="store_true", help="PR 冒烟：至少 3 条且 0 错误")
    parser.add_argument("--out", default="artifacts/eval")
    args = parser.parse_args()
    min_cases = 3 if args.smoke else max(3, args.min_cases)
    baseline = None
    if Path(args.baseline).exists():
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    if args.summary:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    else:
        cfg = Config(ROOT)
        cfg.ensure_dirs()
        service = EvaluationService(Database(cfg.db_path), cfg, data_dir=Path(cfg.data_dir))
        result = service.evaluate(split=args.split, name=f"gate-{args.split}")
        summary = result["summary"]
        out_dir = ROOT / args.out / str(result["id"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    verdict = compare_summary(summary, baseline, min_cases=min_cases)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
