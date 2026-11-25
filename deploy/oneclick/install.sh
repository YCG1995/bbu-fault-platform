#!/usr/bin/env bash
# BBU FaultScope 一键部署安装器（内置 CPython，目标机无需预装 Python）。
# 用法：sudo bash install.sh [选项]
set -Eeuo pipefail

APP_NAME="bbu-fault-platform"
PREFIX="/opt/bbu-fault-platform"
PYTHON_PREFIX="/opt/bbu-python313"
STATE_DIR="/var/lib/bbu-fault-platform"
HOST="0.0.0.0"
PORT="8080"
SERVICE_NAME="bbu-fault-platform"
SERVICE_USER="bbu"
MANAGE_SERVICE=1
INSTALL_HELPER=1
FORCE=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="${SCRIPT_DIR}/app"
RUNTIME_SRC="${SCRIPT_DIR}/runtime/python"
PY_BIN="${PYTHON_PREFIX}/bin/python3.13"

die() { echo "[错误] $*" >&2; exit 1; }
log() { echo "[安装] $*"; }
usage() {
  cat <<'EOF'
用法:
  sudo bash install.sh [选项]

选项:
  --prefix DIR          程序目录（默认 /opt/bbu-fault-platform）
  --python-prefix DIR   内置 Python 目录（默认 /opt/bbu-python313）
  --state-dir DIR       数据/技能/案例目录（默认 /var/lib/bbu-fault-platform）
  --host ADDR           监听地址（默认 0.0.0.0；反代场景建议 127.0.0.1）
  --port PORT           监听端口（默认 8080）
  --user NAME           运行账号（默认 bbu，不存在则创建）
  --no-service          不安装 systemd，仅落盘并初始化（非 root 也可用）
  --no-helper           不安装网页更新所需的 root/systemd 升级助手
  --force               允许覆盖已有安装（数据目录保留；仅用于修复/重装）
  -h, --help            显示帮助
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --python-prefix) PYTHON_PREFIX="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --host) HOST="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --no-service) MANAGE_SERVICE=0; shift ;;
    --no-helper) INSTALL_HELPER=0; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

[ -d "$APP_SRC" ] || die "安装包缺少 app/ 目录"
[ -f "$APP_SRC/run.py" ] || die "安装包缺少 app/run.py"
[ -d "$APP_SRC/vendor/yaml" ] || die "安装包缺少 app/vendor/yaml"
[ -d "$APP_SRC/web" ] || die "安装包缺少 app/web"
[ -x "$RUNTIME_SRC/bin/python3.13" ] || die "安装包缺少 runtime/python/bin/python3.13"
[ -f "$RUNTIME_SRC/bin/python3.13" ] || die "内置 Python 不存在"

case "$PORT" in
  ''|*[!0-9]*) die "端口必须是数字" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口超出范围: $PORT"

if [ "$MANAGE_SERVICE" -eq 1 ]; then
  [ "$(id -u)" -eq 0 ] || die "安装 systemd 服务需要 root；非 root 请加 --no-service"
fi

case "$(uname -m)" in
  x86_64|amd64) ;;
  *) die "当前一键包只支持 x86_64；其他架构请使用源码离线包" ;;
esac

GLIBC_VER="$(ldd --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+' | head -n1 || true)"
[ -n "$GLIBC_VER" ] || die "无法检测 glibc 版本"
awk -v v="$GLIBC_VER" 'BEGIN{exit !(v+0 >= 2.17)}' || die "glibc 过低（$GLIBC_VER），需要 2.17+"

PREFIX="$(mkdir -p "$PREFIX" && cd "$PREFIX" && pwd)"
PYTHON_PREFIX="$(mkdir -p "$PYTHON_PREFIX" && cd "$PYTHON_PREFIX" && pwd)"
STATE_DIR="$(mkdir -p "$STATE_DIR" && cd "$STATE_DIR" && pwd)"
[ "$PREFIX" != "$PYTHON_PREFIX" ] || die "--prefix 不能与 --python-prefix 相同"
[ "$STATE_DIR" != "$PREFIX" ] || die "--state-dir 不能与 --prefix 相同"
[ "$STATE_DIR" != "$PYTHON_PREFIX" ] || die "--state-dir 不能与 --python-prefix 相同"
PY_BIN="${PYTHON_PREFIX}/bin/python3.13"

if [ -f "$PREFIX/bbu/__init__.py" ] && [ -f "$PREFIX/config.json" ] && [ "$FORCE" -ne 1 ]; then
  die "检测到已有安装。后续升级请使用平台“系统信息 → 离线升级”上传更新包；如确需重装，请加 --force（数据目录会保留）"
fi

log "校验内置 Python 运行时"
"$RUNTIME_SRC/bin/python3.13" - <<'PY' || die "内置 Python 自检失败"
import sys, ssl, sqlite3, ctypes, lzma, bz2
assert sys.version_info >= (3, 11), sys.version
print("[安装] Python", sys.version.split()[0], "OpenSSL", ssl.OPENSSL_VERSION,
      "SQLite", sqlite3.sqlite_version)
PY

if [ "$MANAGE_SERVICE" -eq 1 ]; then
  if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
    log "使用已有系统账号 $SERVICE_USER"
  else
    log "创建系统账号 $SERVICE_USER"
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" \
      || useradd --system --home-dir "$STATE_DIR" --shell /bin/false "$SERVICE_USER"
  fi
  if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    log "停止已有服务 $SERVICE_NAME"
    systemctl stop "$SERVICE_NAME" || true
  fi
fi

log "安装内置 Python 到 $PYTHON_PREFIX"
rm -rf "$PYTHON_PREFIX"
mkdir -p "$PYTHON_PREFIX"
cp -a "$RUNTIME_SRC"/. "$PYTHON_PREFIX"/
if [ "$MANAGE_SERVICE" -eq 1 ]; then
  chown -R root:root "$PYTHON_PREFIX"
else
  chown -R "$(id -u):$(id -g)" "$PYTHON_PREFIX" 2>/dev/null || true
fi
chmod -R go-w "$PYTHON_PREFIX"

log "安装程序到 $PREFIX"
rm -rf "$PREFIX"
mkdir -p "$PREFIX"
tar -C "$APP_SRC" \
  --exclude='./config.json' \
  --exclude='./data' \
  --exclude='./skills' \
  --exclude='./cases' \
  --exclude='./dist' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='./.git' \
  -cf - . | tar -C "$PREFIX" -xf -
if [ "$MANAGE_SERVICE" -eq 1 ]; then
  chown -R root:root "$PREFIX"
else
  chown -R "$(id -u):$(id -g)" "$PREFIX" 2>/dev/null || true
fi
chmod -R go-w "$PREFIX"

mkdir -p "$STATE_DIR/data" "$STATE_DIR/skills" "$STATE_DIR/cases" \
         "$STATE_DIR/logs" "$STATE_DIR/uploads" "$STATE_DIR/tasks" \
         "$STATE_DIR/tmp" "$STATE_DIR/skill_versions"

log "生成配置文件"
"$PY_BIN" - "$PREFIX" "$STATE_DIR" "$HOST" "$PORT" <<'PY'
import json, sys
from pathlib import Path

prefix = Path(sys.argv[1])
state = Path(sys.argv[2])
host = sys.argv[3]
port = int(sys.argv[4])
example = prefix / "config.example.json"
base = json.loads(example.read_text(encoding="utf-8")) if example.exists() else {}
base.update({
    "brand": "BBU 故障诊断平台",
    "host": host,
    "port": port,
    "data_dir": str(state / "data"),
    "skills_dir": str(state / "skills"),
    "cases_dir": str(state / "cases"),
    "examples_dir": str(prefix / "examples"),
    "meta_skill_dir": str(prefix / "meta" / "bbu-fault-skill-builder"),
    "install_demo_skills": True,
})
(prefix / "config.json").write_text(
    json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

if [ "$MANAGE_SERVICE" -eq 1 ]; then
  chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
  chmod 750 "$STATE_DIR"
  chown root:"$SERVICE_USER" "$PREFIX/config.json"
  chmod 640 "$PREFIX/config.json"
else
  chown -R "$(id -u):$(id -g)" "$STATE_DIR" 2>/dev/null || true
fi

log "初始化数据库与初始管理员（首次启动会生成 data/初始管理员密码.txt）"
if [ "$MANAGE_SERVICE" -eq 1 ]; then
  su -s /bin/sh -c "cd '$PREFIX' && BBU_DATA_DIR='$STATE_DIR/data' BBU_SKILLS_DIR='$STATE_DIR/skills' BBU_CASES_DIR='$STATE_DIR/cases' '$PY_BIN' run.py --init-only" "$SERVICE_USER"
else
  (cd "$PREFIX" && BBU_DATA_DIR="$STATE_DIR/data" BBU_SKILLS_DIR="$STATE_DIR/skills" \
     BBU_CASES_DIR="$STATE_DIR/cases" "$PY_BIN" run.py --init-only)
fi

if [ "$MANAGE_SERVICE" -eq 0 ]; then
  cat <<EOF

[完成] 已落盘并初始化（--no-service，未安装 systemd）。
  程序目录 : $PREFIX
  Python   : $PY_BIN
  数据目录 : $STATE_DIR
  启动命令 : cd $PREFIX && $PY_BIN run.py --host $HOST --port $PORT
  初始密码 : $STATE_DIR/data/初始管理员密码.txt
EOF
  exit 0
fi

log "写入 systemd 服务"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=BBU FaultScope 故障诊断平台
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PREFIX
Environment=BBU_DATA_DIR=$STATE_DIR/data
Environment=BBU_SKILLS_DIR=$STATE_DIR/skills
Environment=BBU_CASES_DIR=$STATE_DIR/cases
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$PY_BIN $PREFIX/run.py --host $HOST --port $PORT
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"

log "等待健康检查"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
deadline=$(( $(date +%s) + 45 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if "$PY_BIN" - "$HEALTH_URL" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        data = json.loads(r.read().decode("utf-8"))
    if r.status == 200 and data.get("ok"):
        print("ok")
        raise SystemExit(0)
except Exception:
    pass
raise SystemExit(1)
PY
  then
    break
  fi
  sleep 1
done
if ! "$PY_BIN" - "$HEALTH_URL" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=3) as r:
    data = json.loads(r.read().decode("utf-8"))
assert r.status == 200 and data.get("ok")
print("[安装] 健康检查通过：", data.get("version"))
PY
then
  die "健康检查失败，请执行：systemctl status $SERVICE_NAME；journalctl -u $SERVICE_NAME -n 100 --no-pager"
fi

if [ "$INSTALL_HELPER" -eq 1 ]; then
  log "安装网页离线升级助手（root/systemd 一次性助手）"
  if bash "$PREFIX/deploy/install_upgrade_helper.sh" --prefix "$PREFIX" --state-dir "$STATE_DIR" \
       --service "$SERVICE_NAME" --user "$SERVICE_USER" --port "$PORT" --python-bin "$PY_BIN"; then
    log "升级助手安装完成"
  else
    log "警告：升级助手安装失败；网页升级入口暂不可用，请联系维护人员检查 systemd/sudoers"
  fi
fi

cat <<EOF

[完成] BBU FaultScope 已一键部署。
  程序目录 : $PREFIX
  Python   : $PY_BIN
  数据目录 : $STATE_DIR
  服务名   : $SERVICE_NAME
  访问地址 : http://<本机IP>:$PORT/
  初始密码 : $STATE_DIR/data/初始管理员密码.txt
  服务状态 : systemctl status $SERVICE_NAME
  服务日志 : journalctl -u $SERVICE_NAME -n 100 --no-pager

首次登录请使用 admin 与初始密码，系统会强制改密。后续更新请在平台“系统信息 → 离线升级”上传更新包。
EOF
