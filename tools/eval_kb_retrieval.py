#!/usr/bin/env python3
"""知识库检索评测：Recall@1/3/5、MRR，可选对比旧版实现。

用法：
    python3 tools/eval_kb_retrieval.py
    python3 tools/eval_kb_retrieval.py --baseline /path/to/old/assistant_kb.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bbu.assistant_kb import KnowledgeBase  # noqa: E402


def _load_module(path: str):
    loader = SourceFileLoader("kb_eval_baseline", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise SystemExit(f"无法加载模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def evaluate(kb, cases: list[dict]) -> dict[str, float]:
    def matched(result: dict, case: dict) -> bool:
        return (result["slug"] == case["slug"]
                and case["heading"] in result["heading"])

    ranks: list[int | None] = []
    for case in cases:
        found = next((i + 1 for i, item in enumerate(kb.search(case["question"], 5))
                      if matched(item, case)), None)
        ranks.append(found)
    n = len(cases)
    return {
        "cases": n,
        "recall@1": round(sum(1 for rank in ranks if rank == 1) / n, 4),
        "recall@3": round(sum(1 for rank in ranks if rank and rank <= 3) / n, 4),
        "recall@5": round(sum(1 for rank in ranks if rank and rank <= 5) / n, 4),
        "mrr": round(sum((1 / rank if rank else 0) for rank in ranks) / n, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(ROOT / "tests" / "kb_retrieval_cases.json"))
    parser.add_argument("--baseline", help="旧版 assistant_kb.py，用于同集对比")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    current = evaluate(KnowledgeBase(ROOT), cases)
    result = {"current": current}
    if args.baseline:
        old = _load_module(args.baseline)
        result["baseline"] = evaluate(old.KnowledgeBase(ROOT), cases)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for label, metrics in result.items():
            print(f"{label}: " + " ".join(f"{k}={v}" for k, v in metrics.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
