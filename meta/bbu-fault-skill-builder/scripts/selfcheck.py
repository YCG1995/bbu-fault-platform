#!/usr/bin/env python3
"""selfcheck.py — 子技能运行环境自检，输出机器可读错误码。"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ASSETS = SKILL / "assets"
MIN_PYTHON = (3, 11)
REQUIRED = [
    "SKILL.md",
    "assets/skill-manifest.json",
    "assets/fault-profile.yaml",
    "assets/evidence-rules.yaml",
    "scripts/run_analysis.py",
    "scripts/search_evidence.py",
    "scripts/render_report.py",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="子技能环境自检")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    errors, warnings = [], []
    if sys.version_info < MIN_PYTHON:
        errors.append({"code": "PYTHON_TOO_OLD",
                       "message": f"需要 Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}，"
                                  f"当前 {sys.version.split()[0]}"})
    for rel in REQUIRED:
        if not (SKILL / rel).exists():
            errors.append({"code": "MISSING_FILE", "path": rel,
                           "message": f"缺少 {rel}"})
    manifest = {}
    try:
        manifest = json.loads((ASSETS / "skill-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append({"code": "MANIFEST_INVALID", "message": str(exc)})
    if manifest and manifest.get("schema") != "skill-manifest/2":
        errors.append({"code": "MANIFEST_SCHEMA", "message": str(manifest.get("schema"))})
    try:
        import yaml  # noqa: F401
    except ImportError:
        warnings.append({"code": "PYYAML_MISSING",
                         "message": "当前运行期读取 .yaml 资产需要 PyYAML；"
                                    "建议构建期导出 JSON 资产（P2）"})
    data = {
        "ok": not errors,
        "skill": manifest.get("name") or SKILL.name,
        "version": manifest.get("version"),
        "python": sys.version.split()[0],
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
