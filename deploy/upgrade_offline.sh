#!/usr/bin/env bash
# 离线升级：备份运行数据与旧代码，保留 config.json，执行幂等迁移并校验版本。
set -Eeuo pipefail

APP_NAME="bbu-fault-platform"
PREFIX="/opt/${APP_NAME}"
STATE_DIR=""
BACKUP_ROOT=""
BACKUP_DIR=""
SERVICE_NAME="${APP_NAME}"
SERVICE_USER="bbu"
PYTHON_BIN="${PYTHON_BIN:-}"
MANAGE_SERVICE=1
DO_BACKUP=1
HEALTH_URL=""
HEALTH_TIMEOUT=45

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OLD_DIR=""
STAGE_DIR=""
CODE_SWAPPED=0
WAS_ACTIVE=0
UPGRADE_DONE=0

log() { echo "[升级] $*"; }
die() { echo "[错误] $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法:
  sudo bash bbu-fault-platform/deploy/upgrade_offline.sh [选项]

从“新离线包解压目录”执行，升级已安装的平台。默认保留配置、数据库、密钥、技能与案例。

选项:
  --prefix DIR        已安装程序目录（默认 /opt/bbu-fault-platform）
  --state-dir DIR     运行数据目录（默认从现有 config.json 的 data_dir 推导）
  --backup-dir DIR    升级快照父目录（默认 STATE_DIR/backups）
  --service NAME      systemd 服务名（默认 bbu-fault-platform）
  --user NAME         运行账号（默认 bbu）
  --health-url URL    健康检查地址（默认从 config.json 推导）
  --health-timeout N  健康检查秒数（默认 45）
  --no-service        不操作系统d；需先手工停止进程，升级后自行重启
  --skip-backup       跳过备份（不推荐；跳过后将无法可靠回滚）
  -h, --help          显示帮助
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --backup-dir) BACKUP_ROOT="${2:-}"; shift 2 ;;
    --service) SERVICE_NAME="${2:-}"; shift 2 ;;
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --health-url) HEALTH_URL="${2:-}"; shift 2 ;;
    --health-timeout) HEALTH_TIMEOUT="${2:-}"; shift 2 ;;
    --no-service) MANAGE_SERVICE=0; shift ;;
    --skip-backup) DO_BACKUP=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

on_exit() {
  rc=$?
  if [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ]; then rm -rf "$STAGE_DIR"; fi
  # 只有在尚未替换代码时，出错才自动恢复旧服务；换码后失败必须由回滚脚本处理。
  if [ "$rc" -ne 0 ] && [ "$WAS_ACTIVE" -eq 1 ] && [ "$CODE_SWAPPED" -eq 0 ] && [ "$UPGRADE_DONE" -eq 0 ]; then
    systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  if [ "$rc" -ne 0 ] && [ "$CODE_SWAPPED" -eq 1 ] && [ -n "$BACKUP_DIR" ]; then
    echo "[错误] 代码已切换但升级未完成；服务保持停止/异常状态，请先看日志。" >&2
    echo "       升级快照: $BACKUP_DIR" >&2
    echo "       完整回滚: bash $PREFIX/deploy/rollback_offline.sh --backup $BACKUP_DIR --with-state" >&2
  fi
  exit "$rc"
}
trap on_exit EXIT

if [ -z "$PYTHON_BIN" ]; then
  for candidate in /opt/bbu-python313/bin/python3.13 /opt/bbu-python313/bin/python3 \
                   /opt/python3.13/bin/python3.13 /opt/python3.13/bin/python3 \
                   "$PREFIX/../bbu-python313/bin/python3.13"; do
    if [ -x "$candidate" ]; then PYTHON_BIN="$candidate"; break; fi
  done
fi
[ -n "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3 || true)"
[ -n "$PYTHON_BIN" ] || die "未找到 Python，请使用一键部署包或指定 PYTHON_BIN"
[ "$MANAGE_SERVICE" -eq 0 ] || [ "$(id -u)" -eq 0 ] || die "systemd 升级需要 root；手工部署请加 --no-service"
[ -f "$ROOT_DIR/run.py" ] || die "请从新离线包解压后的 bbu-fault-platform 目录执行"
[ -f "$ROOT_DIR/bbu/__init__.py" ] || die "新包缺少 bbu/__init__.py"
[ -d "$ROOT_DIR/vendor/yaml" ] || die "新包缺少 vendor/yaml"
[ -d "$ROOT_DIR/web" ] || die "新包缺少 web"
[ -f "$PREFIX/bbu/__init__.py" ] || die "未找到旧安装: $PREFIX；首次部署请使用 install_offline.sh"
[ -f "$PREFIX/config.json" ] || die "旧安装缺少 config.json，请先修复配置"

PREFIX="$(cd "$PREFIX" && pwd)"
ROOT_DIR="$(cd "$ROOT_DIR" && pwd)"
[ "$PREFIX" != "$ROOT_DIR" ] || die "不能从正在运行的安装目录原地升级，请先解压新包到另一个目录"

read_version() {
  "$PYTHON_BIN" - "$1" <<'PY'
import re, sys
from pathlib import Path
text = (Path(sys.argv[1]) / "bbu" / "__init__.py").read_text(encoding="utf-8")
m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
if not m:
    raise SystemExit("无法读取版本号")
print(m.group(1))
PY
}
patch_helper_trigger() {
  [ "$MANAGE_SERVICE" -eq 1 ] || return 0
  local installed="/usr/local/bin/bbu-upgrade-helper-trigger"
  [ -f "$installed" ] || return 0
  [ -f "$ROOT_DIR/deploy/bbu-upgrade-helper-trigger" ] || return 0
  install -m 0755 "$ROOT_DIR/deploy/bbu-upgrade-helper-trigger" "$installed"
  log "已更新升级助手触发器（兼容 /usr/bin 与 /bin 下的 systemctl）"
}

patch_service_unit_nnp() {
  [ "$MANAGE_SERVICE" -eq 1 ] || return 0
  local unit="/etc/systemd/system/${SERVICE_NAME}.service"
  [ -f "$unit" ] || return 0
  if grep -qE '^[[:space:]]*NoNewPrivileges[[:space:]]*=[[:space:]]*(true|yes|1)[[:space:]]*$' "$unit"; then
    cp -p "$unit" "$BACKUP_DIR/${SERVICE_NAME}.service.before-${NEW_VERSION}" 2>/dev/null || true
    sed -i -E '/^[[:space:]]*NoNewPrivileges[[:space:]]*=[[:space:]]*(true|yes|1)[[:space:]]*$/d' "$unit"
    systemctl daemon-reload
    log "已移除服务单元的 NoNewPrivileges=true（会阻止受限 sudo 升级助手）"
  fi
}

OLD_VERSION="$(read_version "$PREFIX")"
NEW_VERSION="$(read_version "$ROOT_DIR")"
[ "$OLD_VERSION" != "$NEW_VERSION" ] || log "版本号同为 $NEW_VERSION；将按同版修复/重新部署执行"

if [ -z "$STATE_DIR" ]; then
  STATE_DIR="$("$PYTHON_BIN" - "$PREFIX/config.json" <<'PY'
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
data_dir = Path(cfg.get("data_dir") or "/var/lib/bbu-fault-platform/data").resolve()
print(data_dir.parent)
PY
)"
fi
[ -d "$STATE_DIR" ] || die "数据目录不存在: $STATE_DIR"
STATE_DIR="$(cd "$STATE_DIR" && pwd)"
[ "$STATE_DIR" != "$PREFIX" ] || die "state-dir 不能与 prefix 相同"
if [ -z "$BACKUP_ROOT" ]; then BACKUP_ROOT="$STATE_DIR/backups"; fi
mkdir -p "$BACKUP_ROOT"
BACKUP_ROOT="$(cd "$BACKUP_ROOT" && pwd)"

if [ -z "$HEALTH_URL" ]; then
  HEALTH_URL="$("$PYTHON_BIN" - "$PREFIX/config.json" <<'PY'
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
host = str(cfg.get("host") or "127.0.0.1")
if host in ("0.0.0.0", "::"):
    host = "127.0.0.1"
print(f"http://{host}:{int(cfg.get('port') or 8080)}/api/health")
PY
)"
fi

"$PYTHON_BIN" - <<'PY' || die "Python 版本过低，需要 3.11+"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

log "旧版本 $OLD_VERSION → 新版本 $NEW_VERSION"
log "程序目录 $PREFIX；数据目录 $STATE_DIR"
if [ "$MANAGE_SERVICE" -eq 1 ]; then
  getent passwd "$SERVICE_USER" >/dev/null 2>&1 || die "运行账号不存在: $SERVICE_USER"
  if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then WAS_ACTIVE=1; fi
  log "停止服务 $SERVICE_NAME"
  systemctl stop "$SERVICE_NAME"
else
  log "手工部署模式：请确认原进程已经停止；本脚本不会自动停止/启动它"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_ROOT/${STAMP}-${OLD_VERSION}-to-${NEW_VERSION}-$$"
mkdir -p "$BACKUP_DIR"

if [ "$DO_BACKUP" -eq 1 ]; then
  log "备份数据库、密钥、技能与案例到 $BACKUP_DIR/state.tar.gz"
  PYTHON_BIN="$PYTHON_BIN" bash "$ROOT_DIR/deploy/backup.sh" --state-dir "$STATE_DIR" --out "$BACKUP_DIR/state.tar.gz"
  log "备份旧代码与 config.json 到 $BACKUP_DIR/code.tar.gz"
  tar -C "$(dirname "$PREFIX")" -czf "$BACKUP_DIR/code.tar.gz" "$(basename "$PREFIX")"
else
  log "警告：已跳过备份，本版本升级不能可靠回滚"
fi

"$PYTHON_BIN" - "$BACKUP_DIR/backup.json" "$OLD_VERSION" "$NEW_VERSION" "$PREFIX" \
  "$STATE_DIR" "$SERVICE_NAME" "$SERVICE_USER" "$HEALTH_URL" "$DO_BACKUP" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
out = Path(sys.argv[1])
old, new, prefix, state, service, user, health, do_backup = sys.argv[2:]
out.write_text(json.dumps({
    "created_at": datetime.now(timezone.utc).isoformat(),
    "old_version": old, "new_version": new, "prefix": prefix, "state_dir": state,
    "service_name": service, "service_user": user, "health_url": health,
    "has_state_backup": do_backup == "1",
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

STAGE_DIR="$(mktemp -d "$(dirname "$PREFIX")/.${APP_NAME}.new.XXXXXX")"
log "复制新代码到临时目录"
tar -C "$ROOT_DIR" \
  --exclude='./data' --exclude='./skills' --exclude='./cases' \
  --exclude='./evaluations' --exclude='./testlab' \
  --exclude='./dist' --exclude='./config.json*' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='./.git' --exclude='./.pytest_cache' \
  -cf - . | tar -C "$STAGE_DIR" -xf -
cp -p "$PREFIX/config.json" "$STAGE_DIR/config.json"

(
  cd "$STAGE_DIR"
  "$PYTHON_BIN" - "$NEW_VERSION" <<'PY'
import sys
import bbu
import yaml
assert bbu.__version__ == sys.argv[1], (bbu.__version__, sys.argv[1])
print(f"[升级] 新包自检通过：Python 模块与 PyYAML 可导入，版本 {bbu.__version__}")
PY
)

OLD_DIR="${PREFIX}.old.${STAMP}.$$"
log "替换程序代码；运行数据不参与覆盖"
mv "$PREFIX" "$OLD_DIR"
if ! mv "$STAGE_DIR" "$PREFIX"; then
  mv "$OLD_DIR" "$PREFIX" || true
  die "新代码目录切换失败，已尝试恢复旧目录"
fi
STAGE_DIR=""
CODE_SWAPPED=1

patch_service_unit_nnp
patch_helper_trigger

if [ "$MANAGE_SERVICE" -eq 1 ]; then
  chown -R root:root "$PREFIX"
  chown root:"$SERVICE_USER" "$PREFIX/config.json"
  chmod 640 "$PREFIX/config.json"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
fi

log "执行幂等初始化与数据库迁移"
if [ "$MANAGE_SERVICE" -eq 1 ]; then
  su -s /bin/sh -c "cd '$PREFIX' && '$PYTHON_BIN' run.py --init-only" "$SERVICE_USER"
else
  (cd "$PREFIX" && "$PYTHON_BIN" run.py --init-only)
fi

if [ "$MANAGE_SERVICE" -eq 1 ]; then
  log "启动服务并检查 /api/health"
  systemctl restart "$SERVICE_NAME"
  "$PYTHON_BIN" - "$HEALTH_URL" "$NEW_VERSION" "$HEALTH_TIMEOUT" <<'PY'
import json, sys, time, urllib.request
url, expected, timeout = sys.argv[1], sys.argv[2], int(sys.argv[3])
deadline = time.time() + timeout
last = "尚未连接"
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            payload = json.load(r)
        if r.status == 200 and payload.get("ok") and payload.get("version") == expected:
            print(f"[升级] 健康检查通过：{url}，version={expected}")
            break
        last = repr(payload)
    except Exception as exc:
        last = f"{type(exc).__name__}: {exc}"
    time.sleep(1)
else:
    raise SystemExit(f"健康检查失败或版本不符：{last}")
PY
fi

UPGRADE_DONE=1
if [ -n "$OLD_DIR" ] && [ -d "$OLD_DIR" ]; then rm -rf "$OLD_DIR"; fi

echo
echo "[完成] 平台已从 $OLD_VERSION 升级到 $NEW_VERSION"
echo "  程序目录 : $PREFIX"
echo "  数据目录 : $STATE_DIR（数据库、密钥、技能、案例均保留）"
echo "  配置文件 : $PREFIX/config.json（已保留）"
echo "  升级快照 : $BACKUP_DIR"
if [ "$MANAGE_SERVICE" -eq 1 ]; then
  echo "  服务状态 : systemctl status $SERVICE_NAME"
  echo "  服务日志 : journalctl -u $SERVICE_NAME -n 100 --no-pager"
else
  echo "  下一步   : 请使用原启动命令手工重启服务"
fi
if [ "$DO_BACKUP" -eq 1 ]; then
  echo "  回滚入口 : bash $PREFIX/deploy/rollback_offline.sh --backup $BACKUP_DIR --with-state"
fi
