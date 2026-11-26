#!/usr/bin/env python3
"""真实模型流式探针：不修改主库，输出可复现的延迟与 token 证据。

用法：
    python3 tests/live_model_probe.py
    python3 tests/live_model_probe.py --max-tokens 512
    python3 tests/live_model_probe.py --prompt-file /tmp/extract-prompt.txt

脚本会复制当前数据库到临时目录，API Key 仍从原数据目录的 master.key 解密；
主库的模型健康统计不会被探针污染。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIGateway, AIError  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.security import SecretBox  # noqa: E402

DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["ok", "note"],
    "additionalProperties": False,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="BBU 真实模型流式探针")
    parser.add_argument("--prompt", default="请只输出 JSON，字段 ok 为 true，note 为 \"pong\"。")
    parser.add_argument("--prompt-file", help="从文件读取 prompt；用于复现长上下文")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=int, default=0, help="覆盖模型 timeout_sec")
    args = parser.parse_args()

    cfg = Config(ROOT)
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not prompt.strip():
        print("prompt 不能为空", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="bbu-live-probe-"))
    db_copy = work / "probe.db"
    shutil.copy2(cfg.db_path, db_copy)
    db = Database(db_copy)
    if args.timeout:
        db.execute("UPDATE model_configs SET timeout_sec=? WHERE enabled=1", (args.timeout,))
    gateway = AIGateway(db, cfg, secret_box=SecretBox(cfg.data_dir))
    attempts: list[dict] = []
    started = time.monotonic()
    try:
        result = gateway.complete(
            "workshop",
            "你是 BBU 故障诊断平台的模型连通性探针，只输出严格 JSON。",
            prompt,
            json_schema=DEFAULT_SCHEMA,
            max_tokens=args.max_tokens,
            stream=True,
            purpose="live-probe",
            attempt_sink=attempts.append,
        )
    except AIError as exc:
        print(json.dumps({
            "ok": False,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "prompt_chars": len(prompt),
            "attempts": attempts,
            "error": str(exc)[:500],
        }, ensure_ascii=False, indent=2))
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(json.dumps({
        "ok": True,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "prompt_chars": len(prompt),
        "model": result.model,
        "latency_ms": result.latency_ms,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "total_tokens": result.usage.total_tokens,
        },
        "data": result.data,
        "text_preview": result.text[:160],
        "attempts": attempts,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
