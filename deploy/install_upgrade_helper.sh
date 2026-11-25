#!/usr/bin/env bash
# 安装 root/systemd 一次性升级助手（必须 root 执行）。
set -euo pipefail
[[ "$(id -u)" == "0" ]] || { echo "请用 root 执行" >&2; exit 1; }
PREFIX="/opt/bbu-fault-platform"
STATE="/var/lib/bbu-fault-platform"
SERVICE="bbu-fault-platform"
SERVICE_USER="bbu"
PORT="8080"
PYTHON_BIN="${BBU_PYTHON_BIN:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;;
    --state-dir) STATE="$2"; shift 2;;
    --service) SERVICE="$2"; shift 2;;
    --user) SERVICE_USER="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --python-bin) PYTHON_BIN="$2"; shift 2;;
    -h|--help) echo "用法: sudo bash deploy/install_upgrade_helper.sh [--prefix DIR] [--state-dir DIR] [--service NAME] [--user NAME] [--port PORT] [--python-bin PATH]"; exit 0;;
    *) echo "未知参数 $1" >&2; exit 2;;
  esac
done
PREFIX="$(cd "$PREFIX" && pwd)"
[[ -f "$PREFIX/deploy/upgrade_offline.sh" ]] || { echo "未找到 $PREFIX/deploy/upgrade_offline.sh" >&2; exit 1; }
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x /opt/bbu-python313/bin/python3.13 ]]; then
    PYTHON_BIN="/opt/bbu-python313/bin/python3.13"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi
fi
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || { echo "未找到可执行的 Python: $PYTHON_BIN" >&2; exit 1; }
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 版本需 3.11+: $PYTHON_BIN" >&2; exit 1; }
STATE="$(mkdir -p "$STATE" && cd "$STATE" && pwd)"
mkdir -p "$STATE/updates" "$STATE/backups"
install -m 0755 "$PREFIX/deploy/bbu-upgrade-helper-trigger" /usr/local/bin/bbu-upgrade-helper-trigger
sed -e "s|/opt/bbu-fault-platform|$PREFIX|g" -e "s|@PYTHON_BIN@|$PYTHON_BIN|g" \
  "$PREFIX/deploy/bbu-upgrade-helper@.service" > /etc/systemd/system/bbu-upgrade-helper@.service
chmod 0644 /etc/systemd/system/bbu-upgrade-helper@.service
install -m 0440 "$PREFIX/deploy/bbu-upgrade.sudoers" "/etc/sudoers.d/bbu-upgrade"
cat > /etc/bbu-upgrade.json <<JSON
{
  "prefix": "$PREFIX",
  "state_dir": "$STATE",
  "service_name": "$SERVICE",
  "service_user": "$SERVICE_USER",
  "python_bin": "$PYTHON_BIN",
  "health_url": "http://127.0.0.1:$PORT/api/health",
  "allowed_work_roots": ["/var/lib", "/srv", "/data", "/mnt"]
}
JSON
chmod 0644 /etc/bbu-upgrade.json
visudo -cf /etc/sudoers.d/bbu-upgrade >/dev/null
systemctl daemon-reload
echo "[完成] 已安装 bbu-upgrade-helper@.service 与 sudo 触发器"
echo "  程序目录 : $PREFIX"
echo "  Python   : $PYTHON_BIN"
echo "  数据目录 : $STATE"
echo "  服务名   : $SERVICE"
echo "  验证     : sudo -u $SERVICE_USER sudo -n /usr/local/bin/bbu-upgrade-helper-trigger Uxxxxxxxx"
