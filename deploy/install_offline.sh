#!/usr/bin/env bash
# 离线安装 BBU FaultScope。仅依赖 bash / tar / python3，不执行 pip/npm。
set -euo pipefail

APP_NAME="bbu-fault-platform"
SERVICE_NAME="bbu-fault-platform"
PREFIX="/opt/bbu-fault-platform"
STATE_DIR="/var/lib/bbu-fault-platform"
HOST="0.0.0.0"
PORT="8080"
SERVICE_USER="bbu"
INSTALL_SERVICE=1
FORCE_CONFIG=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

die() { echo "[错误] $*" >&2; exit 1; }
log() { echo "[安装] $*"; }

usage() {
  cat <<'EOF'
用法:
  sudo bash deploy/install_offline.sh [选项]

选项:
  --prefix DIR       程序目录（默认 /opt/bbu-fault-platform）
  --state-dir DIR    数据/技能/案例目录（默认 /var/lib/bbu-fault-platform）
  --host ADDR        监听地址（默认 0.0.0.0；反代场景建议 127.0.0.1）
  --port PORT        监听端口（默认 8080）
  --user NAME        运行账号（默认 bbu，不存在则创建）
  --no-service       不安装 systemd，仅落盘并初始化（非 root 也可用）
  --force            覆盖已有 config.json（不会删除数据）
  -h, --help         显示帮助
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --no-service) INSTALL_SERVICE=0; shift ;;
    --force) FORCE_CONFIG=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

[ -n "$PYTHON_BIN" ] || die "未找到 python3，请先安装 Python 3.11+"
[ -f "${ROOT_DIR}/run.py" ] || die "未找到 run.py，请在解压后的 bbu-fault-platform 目录内执行"
[ -d "${ROOT_DIR}/vendor/yaml" ] || die "缺少 vendor/yaml，离线包不完整"
[ -d "${ROOT_DIR}/meta/bbu-fault-skill-builder" ] || die "缺少元 Skill 目录，离线包不完整"
[ -d "${ROOT_DIR}/web" ] || die "缺少 web 目录，离线包不完整"

"$PYTHON_BIN" - <<'PY' || die "Python 版本过低，需要 3.11+"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
"$PYTHON_BIN" - <<'PY' || die "vendor PyYAML 无法导入，离线包损坏"
import sys
sys.path.insert(0, "vendor")
import yaml
print("PyYAML", yaml.__version__)
PY

case "$PORT" in
  ''|*[!0-9]*) die "端口必须是数字" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口超出范围: $PORT"

if [ "$INSTALL_SERVICE" -eq 1 ]; then
  [ "$(id -u)" -eq 0 ] || die "安装 systemd 服务需要 root；非 root 请加 --no-service"
fi

PREFIX="$(mkdir -p "$PREFIX" && cd "$PREFIX" && pwd)"
STATE_DIR="$(mkdir -p "$STATE_DIR" && cd "$STATE_DIR" && pwd)"
[ "$PREFIX" != "$ROOT_DIR" ] || die "--prefix 不能与源码目录相同，请换一个安装目录"
[ "$STATE_DIR" != "$PREFIX" ] || die "--state-dir 不能与 --prefix 相同"
if [ -f "$PREFIX/bbu/__init__.py" ] && [ -f "$PREFIX/config.json" ]; then
  die "检测到已有安装。版本升级请从新包执行 deploy/upgrade_offline.sh；不要使用 install_offline.sh 或 --force 覆盖升级"
fi

if [ "$INSTALL_SERVICE" -eq 1 ] && ! getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
  log "创建系统用户 $SERVICE_USER"
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" \
    || useradd --system --home-dir "$STATE_DIR" --shell /bin/false "$SERVICE_USER"
fi

if [ "$INSTALL_SERVICE" -eq 1 ] && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  log "停止已有服务 $SERVICE_NAME"
  systemctl stop "$SERVICE_NAME"
fi

log "复制程序到 $PREFIX"
tar -C "$ROOT_DIR" \
  --exclude='./data' \
  --exclude='./evaluations' \
  --exclude='./testlab' \
  --exclude='./skills' \
  --exclude='./cases' \
  --exclude='./dist' \
  --exclude='./config.json*' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='./.git' \
  -cf - . | tar -C "$PREFIX" -xf -

mkdir -p "$STATE_DIR/data" "$STATE_DIR/skills" "$STATE_DIR/cases"

log "生成配置文件 $PREFIX/config.json"
if [ ! -f "$PREFIX/config.json" ] || [ "$FORCE_CONFIG" -eq 1 ]; then
  "$PYTHON_BIN" - "$PREFIX" "$STATE_DIR" "$HOST" "$PORT" <<'PY'
import json, sys
from pathlib import Path

prefix = Path(sys.argv[1])
state = Path(sys.argv[2])
host = sys.argv[3]
port = int(sys.argv[4])
example = prefix / "config.example.json"
base = json.loads(example.read_text(encoding="utf-8")) if example.exists() else {}
base.update({
    "host": host,
    "port": port,
    "data_dir": str(state / "data"),
    "skills_dir": str(state / "skills"),
    "cases_dir": str(state / "cases"),
    "examples_dir": str(prefix / "examples"),
    "meta_skill_dir": str(prefix / "meta" / "bbu-fault-skill-builder"),
})
(prefix / "config.json").write_text(
    json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi

chmod 640 "$PREFIX/config.json" 2>/dev/null || true

if [ "$INSTALL_SERVICE" -eq 1 ]; then
  chown -R root:root "$PREFIX"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
  chmod 750 "$STATE_DIR"
  chown root:"$SERVICE_USER" "$PREFIX/config.json"
fi

log "初始化数据库与管理员账号（首次启动会生成 data/初始管理员密码.txt）"
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  su -s /bin/sh -c "cd '$PREFIX' && '$PYTHON_BIN' run.py --init-only" "$SERVICE_USER"
else
  (cd "$PREFIX" && "$PYTHON_BIN" run.py --init-only)
fi

if [ "$INSTALL_SERVICE" -eq 1 ]; then
  log "安装 systemd 服务"
  UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
  sed \
    -e "s|@APP_DIR@|$PREFIX|g" \
    -e "s|@STATE_DIR@|$STATE_DIR|g" \
    -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
    -e "s|@PYTHON_BIN@|$PYTHON_BIN|g" \
    -e "s|@HOST@|$HOST|g" \
    -e "s|@PORT@|$PORT|g" \
    "${PREFIX}/deploy/bbu-fault-platform.service.in" > "$UNIT"
  chmod 644 "$UNIT"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
  systemctl restart "$SERVICE_NAME"
else
  log "已跳过 systemd；可用以下命令启动："
  echo "  cd $PREFIX && $PYTHON_BIN run.py --host $HOST --port $PORT"
fi

CHECK_HOST="$HOST"
case "$CHECK_HOST" in
  0.0.0.0|::) CHECK_HOST="127.0.0.1" ;;
esac
READY=0
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  i=0
  while [ "$i" -lt 30 ]; do
    if "$PYTHON_BIN" - "$CHECK_HOST" "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
host, port = sys.argv[1], int(sys.argv[2])
with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as r:
    raise SystemExit(0 if r.status == 200 else 1)
PY
    then READY=1; break; fi
    i=$((i + 1)); sleep 1
  done
fi

echo
if [ "$INSTALL_SERVICE" -eq 0 ]; then
  echo "[完成] 已安装并初始化；未安装 systemd，未启动服务。"
elif [ "$READY" -eq 1 ]; then
  echo "[完成] 服务已就绪：http://${CHECK_HOST}:${PORT}/"
else
  echo "[警告] 30 秒内未通过健康检查，请查看：journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
fi
echo "  程序目录 : $PREFIX"
echo "  数据目录 : $STATE_DIR"
echo "  初始密码 : $STATE_DIR/data/初始管理员密码.txt"
echo "  首次登录 : admin / 初始密码（可直接进入工作台，建议随后改密）"
if [ "$INSTALL_SERVICE" -eq 1 ]; then
  echo "  服务日志 : journalctl -u ${SERVICE_NAME} -f"
else
  echo "  启动命令 : cd $PREFIX && $PYTHON_BIN run.py --host $HOST --port $PORT"
fi
echo
echo "提醒：改密后可删除初始密码文件；备份时务必同时备份 data/master.key。"
