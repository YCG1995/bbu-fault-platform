#!/usr/bin/env bash
# 使用 upgrade_offline.sh 生成的快照回滚。默认只回滚代码；--with-state 同时恢复升级前数据。
set -Eeuo pipefail

BACKUP_DIR=""
PREFIX=""
STATE_DIR=""
SERVICE_NAME=""
SERVICE_USER=""
HEALTH_URL=""
MANAGE_SERVICE=1
WITH_STATE=0
PYTHON_BIN="${PYTHON_BIN:-}"

log() { echo "[回滚] $*"; }
die() { echo "[错误] $*" >&2; exit 1; }
usage() {
  cat <<'EOF'
用法:
  sudo bash /opt/bbu-fault-platform/deploy/rollback_offline.sh --backup BACKUP_DIR [选项]

选项:
  --backup DIR       upgrade_offline.sh 输出的升级快照目录（必填）
  --with-state       同时恢复数据库、密钥、技能与案例到升级前；当前运行数据会移到 rollback-safety
  --prefix DIR       覆盖快照中的程序目录
  --state-dir DIR    覆盖快照中的数据目录
  --service NAME     覆盖 systemd 服务名
  --user NAME        覆盖运行账号
  --health-url URL   覆盖健康检查地址
  --no-service       手工部署模式，不停止/启动 systemd 服务
  -h, --help         显示帮助

不带 --with-state 时只恢复旧代码和 config.json，数据库保持当前版本；跨 schema 回滚必须带 --with-state。
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --backup) BACKUP_DIR="${2:-}"; shift 2 ;;
    --with-state) WITH_STATE=1; shift ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --service) SERVICE_NAME="${2:-}"; shift 2 ;;
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --health-url) HEALTH_URL="${2:-}"; shift 2 ;;
    --no-service) MANAGE_SERVICE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

if [ -z "$PYTHON_BIN" ]; then
  for candidate in /opt/bbu-python313/bin/python3.13 /opt/bbu-python313/bin/python3 \
                   "$PREFIX/../bbu-python313/bin/python3.13"; do
    if [ -x "$candidate" ]; then PYTHON_BIN="$candidate"; break; fi
  done
fi
[ -n "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3 || true)"
[ -n "$PYTHON_BIN" ] || die "未找到 Python"
[ -n "$BACKUP_DIR" ] || { usage; exit 1; }
[ -f "$BACKUP_DIR/backup.json" ] || die "快照缺少 backup.json: $BACKUP_DIR"
[ -f "$BACKUP_DIR/code.tar.gz" ] || die "快照缺少 code.tar.gz；跳过备份的升级不能自动回滚"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"

read_meta() {
  "$PYTHON_BIN" - "$BACKUP_DIR/backup.json" "$1" <<'PY'
import json, sys
from pathlib import Path
obj = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(obj.get(sys.argv[2]) or "")
PY
}

[ -n "$PREFIX" ] || PREFIX="$(read_meta prefix)"
[ -n "$STATE_DIR" ] || STATE_DIR="$(read_meta state_dir)"
[ -n "$SERVICE_NAME" ] || SERVICE_NAME="$(read_meta service_name)"
[ -n "$SERVICE_USER" ] || SERVICE_USER="$(read_meta service_user)"
[ -n "$HEALTH_URL" ] || HEALTH_URL="$(read_meta health_url)"
OLD_VERSION="$(read_meta old_version)"
NEW_VERSION="$(read_meta new_version)"
[ -n "$PREFIX" ] && [ -n "$STATE_DIR" ] && [ -n "$OLD_VERSION" ] || die "快照元数据不完整"
[ -d "$PREFIX" ] || die "当前程序目录不存在: $PREFIX"
PREFIX="$(cd "$PREFIX" && pwd)"
[ -d "$STATE_DIR" ] || die "当前数据目录不存在: $STATE_DIR"
STATE_DIR="$(cd "$STATE_DIR" && pwd)"
if [ "$WITH_STATE" -eq 1 ]; then
  [ -f "$BACKUP_DIR/state.tar.gz" ] || die "快照没有 state.tar.gz，不能恢复运行数据"
  "$PYTHON_BIN" - "$BACKUP_DIR/state.tar.gz" <<'PY'
import sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as tf:
    for m in tf.getmembers():
        name = m.name
        if name.startswith("/") or ".." in name.split("/"):
            raise SystemExit(f"备份包含非法路径: {name}")
        if name.split("/", 1)[0] not in ("data", "skills", "cases"):
            raise SystemExit(f"备份包含非运行数据路径: {name}")
PY
fi

if [ "$MANAGE_SERVICE" -eq 1 ]; then
  [ "$(id -u)" -eq 0 ] || die "systemd 回滚需要 root；手工部署请加 --no-service"
  getent passwd "$SERVICE_USER" >/dev/null 2>&1 || die "运行账号不存在: $SERVICE_USER"
  log "停止服务 $SERVICE_NAME"
  systemctl stop "$SERVICE_NAME"
else
  log "手工部署模式：请确认原进程已经停止"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RESTORE_DIR="$(mktemp -d "$(dirname "$PREFIX")/.bbu-rollback.XXXXXX")"
trap 'rm -rf "$RESTORE_DIR"' EXIT
tar -xzf "$BACKUP_DIR/code.tar.gz" -C "$RESTORE_DIR" --strip-components=1
[ -f "$RESTORE_DIR/bbu/__init__.py" ] || die "旧代码快照不完整"
[ -f "$RESTORE_DIR/config.json" ] || die "旧代码快照缺少 config.json"

log "恢复旧代码 $OLD_VERSION；当前失败代码将保留"
FAILED_DIR="${PREFIX}.failed.${STAMP}.$$"
mv "$PREFIX" "$FAILED_DIR"
if ! mv "$RESTORE_DIR" "$PREFIX"; then
  mv "$FAILED_DIR" "$PREFIX" || true
  die "旧代码恢复失败"
fi

if [ "$WITH_STATE" -eq 1 ]; then
  log "恢复升级前运行数据；当前数据移入 rollback-safety"
  SAFETY_DIR="$STATE_DIR/rollback-safety/${STAMP}-from-${NEW_VERSION}"
  mkdir -p "$SAFETY_DIR"
  for name in data skills cases; do
    if [ -e "$STATE_DIR/$name" ]; then mv "$STATE_DIR/$name" "$SAFETY_DIR/$name"; fi
  done
  if ! tar -xzf "$BACKUP_DIR/state.tar.gz" -C "$STATE_DIR"; then
    die "运行数据解压失败；原当前数据保留在 $SAFETY_DIR"
  fi
  echo "[回滚] 当前数据已保留在 $SAFETY_DIR"
else
  log "仅回滚代码；数据库与运行数据保持当前状态"
fi

if [ "$MANAGE_SERVICE" -eq 1 ]; then
  chown -R root:root "$PREFIX"
  chown root:"$SERVICE_USER" "$PREFIX/config.json"
  chmod 640 "$PREFIX/config.json"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
  systemctl restart "$SERVICE_NAME"
  "$PYTHON_BIN" - "$HEALTH_URL" "$OLD_VERSION" <<'PY'
import json, sys, time, urllib.request
url, expected = sys.argv[1], sys.argv[2]
deadline = time.time() + 45
last = "尚未连接"
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            payload = json.load(r)
        if r.status == 200 and payload.get("ok") and payload.get("version") == expected:
            print(f"[回滚] 健康检查通过：version={expected}")
            break
        last = repr(payload)
    except Exception as exc:
        last = f"{type(exc).__name__}: {exc}"
    time.sleep(1)
else:
    raise SystemExit(f"回滚后健康检查失败：{last}")
PY
fi

echo
echo "[完成] 已回滚到 $OLD_VERSION"
echo "  程序目录 : $PREFIX"
echo "  数据目录 : $STATE_DIR"
echo "  回滚前失败代码 : $FAILED_DIR"
if [ "$WITH_STATE" -eq 1 ]; then
  echo "  数据模式 : 已恢复到升级前；回滚时点的数据保存在 rollback-safety"
else
  echo "  数据模式 : 未恢复数据库；若版本不兼容，请重新执行并加 --with-state"
fi
if [ "$MANAGE_SERVICE" -eq 0 ]; then echo "  下一步   : 请手工启动服务"; fi
