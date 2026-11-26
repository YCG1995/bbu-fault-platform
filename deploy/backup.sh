#!/usr/bin/env bash
# SQLite 一致性备份：数据库走 Backup API，其余运行数据直接打包。
# 默认排除原始上传文件、案例输入和解压目录；报告、证据、技能、案例定义仍保留。
set -euo pipefail

STATE_DIR="/var/lib/bbu-fault-platform"
OUT=""
PYTHON_BIN="${PYTHON_BIN:-}"

# 优先使用一键包内置解释器；不要退回 Ubuntu 16.04 的系统 Python 3.5。
if [ -z "$PYTHON_BIN" ]; then
  for candidate in /opt/bbu-python313/bin/python3.13 \
                   /opt/bbu-python313/bin/python3 \
                   /opt/python3.13/bin/python3.13 \
                   /opt/python3.13/bin/python3 \
                   /opt/python3.12/bin/python3; do
    if [ -x "$candidate" ]; then PYTHON_BIN="$candidate"; break; fi
  done
fi
[ -n "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3 || true)"

usage() {
  echo "用法: bash backup.sh [--state-dir DIR] --out /path/backup.tar.gz"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

[ -n "$OUT" ] || { usage; exit 1; }
[ -n "$PYTHON_BIN" ] || { echo "未找到 python3" >&2; exit 1; }
[ -d "$STATE_DIR" ] || { echo "数据目录不存在: $STATE_DIR" >&2; exit 1; }
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)'; then
  echo "备份脚本需要 Python 3.7+（SQLite Backup API），当前解释器: $PYTHON_BIN" >&2
  "$PYTHON_BIN" --version >&2 || true
  echo "请指定平台实际使用的解释器，例如 /opt/bbu-python313/bin/python3.13" >&2
  exit 1
fi
mkdir -p "$(dirname "$OUT")"

"$PYTHON_BIN" - "$STATE_DIR" "$OUT" <<'PY'
import os, shutil, sqlite3, sys, tarfile, tempfile
from pathlib import Path

state = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2]).resolve()
db = state / "data" / "bbu.db"
tmp = Path(tempfile.mkdtemp(prefix="bbu-backup-"))
snapshot = tmp / "bbu.db"
if db.exists():
    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(snapshot))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

try:
    def excluded(rel):
        parts = rel.parts
        if not parts:
            return False
        # 原始上传、工作暂存、工坊解压、任务解压与运行临时目录。
        blocked = {"uploads", "draft_uploads", "job_uploads", "tmp", "unpacked", "extracted"}
        if any(part in blocked for part in parts):
            return True
        if any(part.endswith("_unpacked") or part.endswith(".extracted") for part in parts):
            return True
        # 历史现场日志与案例输入不进入备份。
        if parts[0] == "data" and len(parts) > 1 and parts[1] == "real_logs":
            return True
        if parts[0] in ("data", "cases") and any(
                part in ("real_cases", "real_cases_seed") for part in parts) and "input" in parts:
            return True
        if parts[0] == "cases" and "input" in parts[1:]:
            return True
        # data/workshop/<draft>/logs 是工坊原始日志。
        if len(parts) >= 4 and parts[:2] == ("data", "workshop") and parts[3] == "logs":
            return True
        return False

    with tarfile.open(out, "w:gz") as tf:
        for root_name in ("data", "skills", "cases"):
            root = state / root_name
            if not root.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                current = Path(dirpath)
                dirnames[:] = sorted(
                    name for name in dirnames
                    if not excluded((current / name).relative_to(state))
                )
                for name in dirnames:
                    rel = (current / name).relative_to(state)
                    tf.add(current / name, arcname=str(rel), recursive=False)
                for name in sorted(filenames):
                    path = current / name
                    rel = path.relative_to(state)
                    if name in ("bbu.db", "bbu.db-wal", "bbu.db-shm"):
                        continue
                    if excluded(rel):
                        continue
                    tf.add(path, arcname=str(rel), recursive=False)
        if snapshot.exists():
            tf.add(snapshot, arcname="data/bbu.db", recursive=False)
    print("[完成] {} ({} bytes)".format(out, out.stat().st_size))
    print("[排除] 原始上传、案例输入、解压目录与运行临时目录；报告/证据/案例定义已保留")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
