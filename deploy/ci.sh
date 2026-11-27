#!/usr/bin/env bash
# 离线可运行的 CI：编译 → 单元 → REST → UI → eval_smoke → 打包前置检查。
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD:$PWD/vendor${PYTHONPATH:+:$PYTHONPATH}"
python3 -m compileall -q bbu meta tools tests
python3 -m pytest -q
python3 tests/api_full_smoke.py
if command -v python3 >/dev/null && python3 -c 'import playwright' >/dev/null 2>&1; then
  python3 tests/ui_r2_smoke.py
else
  echo "SKIP UI：未安装 Playwright"
fi
python3 tools/eval_gate.py --smoke
python3 tools/gen_manifests.py >/dev/null
python3 -m pytest -q tests/test_manifest_gate.py
echo "CI OK"
