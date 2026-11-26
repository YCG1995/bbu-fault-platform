#!/usr/bin/env python3
"""真实案例库与评测命令行。

用法：
  python3 tools/real_cases.py ingest --name site-a-20250519 --path data/real_logs/log.zip \
      --title "顺河社区东北现场日志" --site 顺河社区东北 --description "现场采集包，无文字描述"

  python3 tools/real_cases.py label site-a-20250519 \
      --expected "光模块接收光功率低于门限" --split test --status golden

  python3 tools/real_cases.py list --split all
  python3 tools/real_cases.py evaluate --split test --name baseline-1.3
  python3 tools/real_cases.py export --output datasets/real_cases/cases.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.evaluation import EvaluationService, RealCaseStore  # noqa: E402


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split("|") if x.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="BBU 真实案例库与评测")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="登记一个真实案例；可先不标注")
    p_ingest.add_argument("--name", required=True)
    p_ingest.add_argument("--path", action="append", required=True,
                          help="日志包或目录，可重复传入多个输入")
    p_ingest.add_argument("--title", default="")
    p_ingest.add_argument("--description", default="")
    p_ingest.add_argument("--site", default="")
    p_ingest.add_argument("--device-type", default="")
    p_ingest.add_argument("--software-version", default="")
    p_ingest.add_argument("--split", default="unlabeled")
    p_ingest.add_argument("--status", default="unlabeled")
    p_ingest.add_argument("--expected", help="期望根因，多个用 | 分隔")
    p_ingest.add_argument("--excluded", help="明确排除根因，多个用 | 分隔")
    p_ingest.add_argument("--notes", default="")
    p_ingest.add_argument("--origin", default="real",
                          choices=("real", "review", "synthetic", "imported"),
                          help="案例来源；synthetic 不计入真实成熟度门禁")
    p_ingest.add_argument("--no-copy", action="store_true",
                          help="只登记路径，不复制输入；案例会依赖原路径")

    p_label = sub.add_parser("label", help="补充/修改案例标签")
    p_label.add_argument("case")
    p_label.add_argument("--expected", help="多个用 | 分隔；空字符串表示阴性")
    p_label.add_argument("--excluded")
    p_label.add_argument("--split")
    p_label.add_argument("--status")
    p_label.add_argument("--notes")

    p_list = sub.add_parser("list", help="列出案例")
    p_list.add_argument("--split", default="all")
    p_list.add_argument("--status", default="all")
    p_list.add_argument("--json", action="store_true")

    p_eval = sub.add_parser("evaluate", help="运行评测；默认确定性，可显式让模型参与")
    p_eval.add_argument("--split", default="test")
    p_eval.add_argument("--limit", type=int, default=0)
    p_eval.add_argument("--case", action="append", default=[])
    p_eval.add_argument("--name", default="")
    p_eval.add_argument("--llm-mode", choices=("off", "planner", "report", "full"),
                        default="off", help="off=确定性；planner=规划；report=报告；full=规划+报告")
    p_eval.add_argument("--model-id", type=int, default=None,
                        help="固定使用 model_configs.id；不填则由角色选择")

    p_export = sub.add_parser("export", help="导出案例清单（不含日志本体）")
    p_export.add_argument("--output", default="datasets/real_cases/cases.jsonl")
    p_export.add_argument("--split", default="all")

    args = parser.parse_args()
    config = Config(ROOT)
    db = Database(config.db_path)
    store = RealCaseStore(db, config.data_dir)

    if args.command == "ingest":
        payload = store.ingest(
            [Path(p) for p in args.path], name=args.name, title=args.title,
            description=args.description, site=args.site,
            device_type=args.device_type, software_version=args.software_version,
            split=args.split, status=args.status,
            expected_roots=_csv(args.expected), excluded_roots=_csv(args.excluded),
            notes=args.notes, origin=args.origin, copy_inputs=not args.no_copy)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.command == "label":
        kwargs: dict = {}
        if args.expected is not None:
            kwargs["expected_roots"] = _csv(args.expected)
        if args.excluded is not None:
            kwargs["excluded_roots"] = _csv(args.excluded)
        if args.split:
            kwargs["split"] = args.split
        if args.status:
            kwargs["status"] = args.status
        if args.notes is not None:
            kwargs["notes"] = args.notes
        print(json.dumps(store.update_labels(args.case, **kwargs), ensure_ascii=False, indent=2))
    elif args.command == "list":
        rows = store.list(args.split, args.status)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print(f"共 {len(rows)} 个案例")
            for row in rows:
                print(f"{row['name']:36} {row['status']:10} {row['split']:9} "
                      f"{row['dataset_sha256'][:12]} {row['title'][:40]}")
    elif args.command == "evaluate":
        service = EvaluationService(db, config, case_store=store)
        try:
            result = service.evaluate(split=args.split, limit=args.limit,
                                      case_ids=args.case or None, name=args.name,
                                      llm_mode=args.llm_mode, model_config_id=args.model_id)
        except ValueError as exc:
            print(f"评测失败: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        print(f"\n评测报告: {result['report']}")
        return 0 if result["summary"].get("errors", 0) == 0 else 1
    elif args.command == "export":
        out = Path(args.output)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = store.list(args.split, "all")
        out.write_text("\n".join(json.dumps({k: v for k, v in row.items()
                                             if k != "input_paths_json"},
                                            ensure_ascii=False) for row in rows) + "\n",
                       encoding="utf-8")
        print(f"已导出 {len(rows)} 个案例到 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
