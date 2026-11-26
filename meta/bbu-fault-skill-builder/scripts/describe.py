#!/usr/bin/env python3
"""describe.py — 子技能自描述入口，供任意宿主读取，不依赖平台类。"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ASSETS = SKILL / "assets"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def main() -> int:
    ap = argparse.ArgumentParser(description="输出子技能 manifest 与路由索引")
    ap.add_argument("--json", action="store_true", help="JSON 输出（默认）")
    args = ap.parse_args()
    manifest = read_json(ASSETS / "skill-manifest.json", {}) or {}
    if not manifest:
        print("SKILL_MANIFEST_MISSING: 请用 diagnostic-skill-builder 重新生成", file=sys.stderr)
        return 2
    data = {
        "schema": manifest.get("schema"),
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "title": manifest.get("title"),
        "description": manifest.get("description"),
        "domain": manifest.get("domain"),
        "entrypoints": manifest.get("entrypoints"),
        "routing": manifest.get("routing"),
        "capabilities": manifest.get("capabilities"),
        "requires": manifest.get("requires"),
        "permissions": manifest.get("permissions"),
        "limits": manifest.get("limits"),
        "engine": manifest.get("engine"),
        "contract": manifest.get("contract"),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
