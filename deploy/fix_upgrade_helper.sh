#!/usr/bin/env bash
# 修复网页离线升级的 root/systemd 助手。
#
# 典型问题：
#   sudo: effective uid is not 0, is /usr/bin/sudo on a file system with the
#   'nosuid' option set or an NFS file system without root privileges?
#
# 该错误说明 sudo 自身不可用，不是 bbu-upgrade-helper 的 Python 代码问题。
# 本脚本只做两件事：诊断环境；在 root 下修复 sudo setuid 并重装固定助手。
#
# 用法：
#   sudo bash deploy/fix_upgrade_helper.sh                  # 诊断 + 重装助手
#   sudo bash deploy/fix_upgrade_helper.sh --fix-sudo       # 额外修复 sudo setuid
#   bash deploy/fix_upgrade_helper.sh --diagnose            # 只诊断，不需要 root
#   bash deploy/fix_upgrade_helper.sh --manual              # 打印手工升级步骤
#
# 注意：如果 sudo 所在文件系统挂了 nosuid/noexec，或当前没有 root 控制台，
# 脚本无法凭空提权，只能改走 root 手工升级。
set -Eeuo pipefail

APP_NAME="bbu-fault-platform"
PREFIX="/opt/${APP_NAME}"
STATE_DIR="/var/lib/${APP_NAME}"
SERVICE_NAME="${APP_NAME}"
SERVICE_USER="bbu"
PORT=""
PYTHON_BIN=""
SUDO_BIN="${SUDO_BIN:-/usr/bin/sudo}"
TRIGGER="/usr/local/bin/bbu-upgrade-helper-trigger"
HELPER_UNIT="/etc/systemd/system/bbu-upgrade-helper@.service"
SUDOERS_FILE="/etc/sudoers.d/bbu-upgrade"
MODE="repair"
FIX_SUDO=0
FIX_NNP=0

log()  { echo "[修复] $*"; }
warn() { echo "[警告] $*" >&2; }
die()  { echo "[错误] $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法:
  sudo bash deploy/fix_upgrade_helper.sh [选项]

选项:
  --prefix DIR       程序目录（默认 /opt/bbu-fault-platform）
  --state-dir DIR    数据目录（默认 /var/lib/bbu-fault-platform）
  --service NAME     systemd 服务名（默认 bbu-fault-platform）
  --user NAME        运行账号（默认 bbu）
  --port PORT        平台端口（默认从程序目录 config.json 读取，否则 8080）
  --python-bin PATH  助手使用的 Python（默认 /opt/bbu-python313 或 python3）
  --fix-sudo         尝试恢复 /usr/bin/sudo 的 root:root + 4755 权限
  --fix-nnp           移除主服务单元中的 NoNewPrivileges=true 并重启服务
  --diagnose         只诊断，不修改任何文件，不需要 root
  --manual           只打印 root 手工升级步骤
  -h, --help         显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --service) SERVICE_NAME="${2:-}"; shift 2 ;;
    --user) SERVICE_USER="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --python-bin) PYTHON_BIN="${2:-}"; shift 2 ;;
    --fix-sudo) FIX_SUDO=1; shift ;;
    --fix-nnp) FIX_NNP=1; shift ;;
    --diagnose) MODE="diagnose"; shift ;;
    --manual) MODE="manual"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

if [[ -z "$PORT" && -f "$PREFIX/config.json" ]]; then
  PORT="$(python3 - "$PREFIX/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(int(json.load(open(sys.argv[1], encoding="utf-8")).get("port") or 8080))
except Exception:
    pass
PY
)"
fi
PORT="${PORT:-8080}"

mount_line() {
  local target="$1"
  if command -v findmnt >/dev/null 2>&1; then
    findmnt -T "$target" -no TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null || true
  fi
}

sudo_state() {
  local real perm
  if [[ ! -e "$SUDO_BIN" ]]; then
    echo "missing"
    return
  fi
  real="$(readlink -f "$SUDO_BIN" 2>/dev/null || echo "$SUDO_BIN")"
  perm="$(stat -c '%a' "$real" 2>/dev/null || echo "?")"
  if [[ "$perm" == "?" ]]; then
    echo "unknown"
    return
  fi
  local mnt
  mnt="$(mount_line "$real")"
  if echo "$mnt" | grep -qwE 'nosuid|noexec'; then
    echo "blocked-mount"
  elif [[ -u "$real" ]]; then
    echo "ok"
  else
    echo "no-setuid"
  fi
}

sudo_detail() {
  local real perm owner group
  real="$(readlink -f "$SUDO_BIN" 2>/dev/null || echo "$SUDO_BIN")"
  if [[ ! -e "$real" ]]; then
    echo "  sudo       : 不存在 ($SUDO_BIN)"
    return
  fi
  perm="$(stat -c '%a' "$real" 2>/dev/null || echo '?')"
  owner="$(stat -c '%U' "$real" 2>/dev/null || echo '?')"
  group="$(stat -c '%G' "$real" 2>/dev/null || echo '?')"
  echo "  sudo       : $real"
  echo "  权限/属主  : $perm $owner:$group"
  if [[ -u "$real" ]]; then
    echo "  setuid     : 正常"
  else
    echo "  setuid     : 缺失（sudo 无法提权）"
  fi
  local mnt
  mnt="$(mount_line "$real")"
  if [[ -n "$mnt" ]]; then
    echo "  挂载       : $mnt"
    if echo "$mnt" | grep -qw nosuid; then
      echo "  致命项     : 挂载含 nosuid，setuid 无效"
    fi
    if echo "$mnt" | grep -qw noexec; then
      echo "  致命项     : 挂载含 noexec，sudo 无法执行"
    fi
  fi
}

systemd_state() {
  if [[ ! -d /run/systemd/system ]]; then
    echo "not-running"
  elif ! command -v systemctl >/dev/null 2>&1; then
    echo "no-systemctl"
  else
    echo "ok"
  fi
}

unit_file() { echo "/etc/systemd/system/${SERVICE_NAME}.service"; }

nnp_state() {
  local unit show
  unit="$(unit_file)"
  if [[ -f "$unit" ]] && grep -qE '^[[:space:]]*NoNewPrivileges[[:space:]]*=[[:space:]]*(true|yes|1)[[:space:]]*$' "$unit"; then
    echo "enabled"
    return
  fi
  if command -v systemctl >/dev/null 2>&1; then
    show="$(systemctl show "$SERVICE_NAME" -p NoNewPrivileges 2>/dev/null || true)"
    if echo "$show" | grep -qE '=yes'; then
      echo "enabled"
      return
    fi
  fi
  echo "disabled"
}

run_as_service_user() {
  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    return 127
  fi
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    su -s /bin/sh "$SERVICE_USER" -c "$(printf '%q ' "$@")"
  fi
}

diagnose() {
  echo "=================================================================="
  echo " 网页升级助手诊断"
  echo "=================================================================="
  echo "  当前用户   : $(id -un 2>/dev/null || echo '?') (uid=$(id -u))"
  echo "  程序目录   : $PREFIX"
  echo "  数据目录   : $STATE_DIR"
  echo "  平台服务   : $SERVICE_NAME / $SERVICE_USER"
  echo "  Python     : ${PYTHON_BIN:-自动选择}"
  echo "------------------------------------------------------------------"
  sudo_detail
  echo "  systemd    : $(systemd_state)"
  echo "  NoNewPrivileges : $(nnp_state) ($(unit_file))"
  echo "  触发器     : $([[ -x "$TRIGGER" ]] && echo 可执行 || echo '缺失/不可执行') ($TRIGGER)"
  echo "  助手 unit  : $([[ -f "$HELPER_UNIT" ]] && echo 存在 || echo '缺失') ($HELPER_UNIT)"
  echo "  sudoers    : $([[ -f "$SUDOERS_FILE" ]] && echo 存在 || echo '缺失') ($SUDOERS_FILE)"
  echo "  助手配置   : $([[ -f /etc/bbu-upgrade.json ]] && echo 存在 || echo '缺失') (/etc/bbu-upgrade.json)"
  echo "  升级脚本   : $([[ -x "$PREFIX/deploy/upgrade_offline.sh" ]] && echo 可执行 || echo '缺失')"
  echo "------------------------------------------------------------------"
  if [[ "$(nnp_state)" == "enabled" ]]; then
    echo "  结论       : 服务单元启用 NoNewPrivileges=true，setuid sudo 被内核禁用；需要 --fix-nnp。"
    return 1
  fi
  if [[ "$(sudo_state)" == "ok" && -x "$TRIGGER" && -f "$HELPER_UNIT" && -f "$SUDOERS_FILE" ]]; then
    local out rc
    set +e
    out="$(run_as_service_user "$SUDO_BIN" -n "$TRIGGER" Utest 2>&1)"
    rc=$?
    set -e
    echo "  触发器自检 : exit=$rc"
    [[ -n "$out" ]] && echo "  输出       : $(echo "$out" | tail -3)"
    # 触发器对非法任务 ID 固定返回 2；这里不 grep 中文输出，
    # 避免脚本被保存成 GBK/ANSI 后与触发器 UTF-8 输出编码不一致而误判失败。
    if [[ "$rc" -eq 2 ]]; then
      echo "  结论       : 助手可用，网页自动升级应可启动。"
      return 0
    fi
    if echo "$out" | grep -q "effective uid is not 0"; then
      echo "  结论       : sudo setuid 仍不可用，需要 root 修复或改手工升级。"
    elif echo "$out" | grep -qE "password is required|not allowed"; then
      echo "  结论       : sudoers 未生效或账号不匹配，需要重装助手。"
    else
      echo "  结论       : 触发器执行异常，请按上面输出检查。"
    fi
    return 1
  fi
  echo "  结论       : 助手链路不完整；若 sudo 正常且有 systemd，直接运行本脚本修复。"
  return 1
}

manual_help() {
  cat <<EOF
手工升级步骤（需要 root 或能写程序目录、能停止平台的账号）：

  1. 校验并解压新包
     sha256sum -c bbu-fault-platform-1.20.2-offline.tar.gz.sha256
     tar -xzf bbu-fault-platform-1.20.2-offline.tar.gz -C /tmp

  2. 停止平台
     systemctl stop ${SERVICE_NAME}

  3. 执行无服务升级（保留数据库、密钥、配置）
     bash /tmp/bbu-fault-platform/deploy/upgrade_offline.sh \\
       --prefix ${PREFIX} \\
       --state-dir ${STATE_DIR} \\
       --no-service

  4. 启动并检查
     systemctl start ${SERVICE_NAME}
     curl -fsS http://127.0.0.1:${PORT}/api/health

  如果没有 systemd，第 2/4 步改为停止/启动原 run.py 进程。
  如果没有 root、sudo 也无法修复，网页自动升级在该机器上不可用；
  只能由具备程序目录写权限和维护权限的账号执行上面的手工升级。
EOF
}

fix_nnp() {
  [[ "$(id -u)" == "0" ]] || die "--fix-nnp 需要 root"
  local unit
  unit="$(unit_file)"
  [[ -f "$unit" ]] || die "找不到服务单元: $unit"
  if grep -qE '^[[:space:]]*NoNewPrivileges[[:space:]]*=[[:space:]]*(true|yes|1)[[:space:]]*$' "$unit"; then
    cp -p "$unit" "${unit}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    sed -i -E '/^[[:space:]]*NoNewPrivileges[[:space:]]*=[[:space:]]*(true|yes|1)[[:space:]]*$/d' "$unit"
    log "已从服务单元移除 NoNewPrivileges=true"
  fi
  if [[ "$(systemd_state)" == "ok" ]]; then
    systemctl daemon-reload
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      systemctl restart "$SERVICE_NAME"
    fi
  fi
  if [[ "$(nnp_state)" == "enabled" ]]; then
    die "服务仍启用 NoNewPrivileges，请检查 systemd drop-in（systemctl cat $SERVICE_NAME）"
  fi
  log "NoNewPrivileges 已关闭；受限 sudo 升级助手可重新生效"
}

fix_sudo() {
  [[ "$(id -u)" == "0" ]] || die "--fix-sudo 需要 root"
  local real perm mnt pkg
  real="$(readlink -f "$SUDO_BIN" 2>/dev/null || echo "$SUDO_BIN")"
  [[ -e "$real" ]] || die "找不到 $SUDO_BIN"
  if [[ -u "$real" ]]; then
    log "sudo setuid 已正常，无需修改"
    return 0
  fi
  mnt="$(mount_line "$real")"
  if echo "$mnt" | grep -qw nosuid; then
    die "sudo 所在文件系统含 nosuid，chmod 4755 无效；请先由系统管理员 remount 或改用 root 手工升级"
  fi
  if echo "$mnt" | grep -qw noexec; then
    die "sudo 所在文件系统含 noexec，无法执行；请先修复挂载或改用 root 手工升级"
  fi
  # 只修复由包管理器登记、且属于 sudo 包的文件，避免把任意二进制变成 setuid root。
  if command -v dpkg >/dev/null 2>&1; then
    pkg="$(dpkg -S "$real" 2>/dev/null | awk -F: 'NR==1{print $1}')"
    [[ "$pkg" == "sudo" ]] || die "$real 不是 Debian sudo 包登记的文件（识别为: ${pkg:-未知}），拒绝自动 chmod"
  elif command -v rpm >/dev/null 2>&1; then
    pkg="$(rpm -qf --qf '%{NAME}\n' "$real" 2>/dev/null | head -1)"
    [[ "$pkg" == "sudo" ]] || die "$real 不是 RPM sudo 包登记的文件（识别为: ${pkg:-未知}），拒绝自动 chmod"
  else
    die "未找到 dpkg/rpm，无法校验 sudo 文件来源，拒绝自动 chmod"
  fi
  chown root:root "$real"
  chmod 4755 "$real"
  [[ -u "$real" ]] || die "设置 setuid 后仍无效，请检查挂载与文件系统"
  log "已恢复 $real 为 root:root 4755"
}

reinstall_helper() {
  [[ "$(id -u)" == "0" ]] || die "重装助手需要 root"
  [[ "$(systemd_state)" == "ok" ]] || die "当前 systemd 未运行，无法安装 root/systemd 助手；请使用 --manual"
  id -u "$SERVICE_USER" >/dev/null 2>&1 || die "服务用户不存在: $SERVICE_USER"
  [[ -x "$PREFIX/deploy/install_upgrade_helper.sh" ]] || die "缺少 $PREFIX/deploy/install_upgrade_helper.sh"
  local args=(--prefix "$PREFIX" --state-dir "$STATE_DIR" --service "$SERVICE_NAME"
              --user "$SERVICE_USER" --port "$PORT")
  if [[ -n "$PYTHON_BIN" ]]; then
    args+=(--python-bin "$PYTHON_BIN")
  fi
  log "重装 bbu-upgrade-helper@.service / 触发器 / sudoers"
  bash "$PREFIX/deploy/install_upgrade_helper.sh" "${args[@]}"
  systemctl daemon-reload
  if systemctl list-unit-files 'bbu-upgrade-helper@.service' >/dev/null 2>&1; then
    log "助手 unit 已登记"
  fi
}

main() {
  if [[ "$MODE" == "manual" ]]; then
    manual_help
    return 0
  fi
  if [[ "$MODE" == "diagnose" ]]; then
    diagnose
    return
  fi
  [[ "$(id -u)" == "0" ]] || die "修复需要 root。请用 root 控制台执行，或先运行 --diagnose 查看原因；没有 root 时只能 --manual"

  diagnose || true
  if [[ "$(nnp_state)" == "enabled" ]]; then
    if [[ "$FIX_NNP" -eq 1 ]]; then
      fix_nnp
    else
      die "服务单元启用 NoNewPrivileges=true，sudo 无法提权；加 --fix-nnp 重试"
    fi
  fi
  if [[ "$(sudo_state)" != "ok" ]]; then
    if [[ "$FIX_SUDO" -eq 1 ]]; then
      fix_sudo
    else
      die "sudo setuid 缺失/不可用；确认是包管理器安装的 sudo 后，加 --fix-sudo 重试，或改 --manual"
    fi
  fi
  if [[ "$(systemd_state)" != "ok" ]]; then
    die "systemd 未运行，网页自动升级无法使用；请使用 --manual"
  fi
  reinstall_helper
  if diagnose; then
    log "修复完成：网页升级助手可启动。"
  else
    die "重装后自检仍未通过；请查看 systemctl status bbu-upgrade-helper@<job> 与 journalctl"
  fi
}

main "$@"
