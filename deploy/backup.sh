#!/usr/bin/env bash
# SQLite 一致性备份：数据库走 Backup API，其余运行数据直接打包。
set -euo pipefail

STATE_DIR="/var/lib/bbu-fault-platform"
OUT=""
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

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
    with tarfile.open(out, "w:gz") as tf:
        for root_name in ("data", "skills", "cases"):
            root = state / root_name
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(state)
                if path.name in ("bbu.db", "bbu.db-wal", "bbu.db-shm"):
                    continue
                tf.add(path, arcname=str(rel), recursive=False)
        if snapshot.exists():
            tf.add(snapshot, arcname="data/bbu.db", recursive=False)
    print(f"[完成] {out} ({out.stat().st_size} bytes)")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
