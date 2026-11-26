#!/usr/bin/env bash
# root systemd 一次性助手入口；只接受一个任务 ID。
set -euo pipefail
JOB="${1:-}"
[[ "$JOB" =~ ^U[A-Za-z0-9_-]{6,40}$ ]] || { echo "任务 ID 非法" >&2; exit 2; }
PYTHON="${BBU_HELPER_PYTHON:-python3}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PYTHON" "$DIR/bbu_upgrade_helper.py" "$JOB"
