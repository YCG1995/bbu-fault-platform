#!/usr/bin/env bash
# 生成“一键部署包”：程序 + 内置 CPython 运行时 + install.sh + 安装说明。
# 用法：bash deploy/make_oneclick.sh --runtime /path/to/python-build-standalone
set -euo pipefail

APP_NAME="bbu-fault-platform"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${ROOT_DIR}/bbu/__init__.py")"
RUNTIME_DIR="${BBU_RUNTIME_DIR:-}"
NAME="${APP_NAME}-${VERSION}-ubuntu1604-offline"
OUT="${DIST_DIR}/${NAME}.tar.gz"
STAGE="${DIST_DIR}/.oneclick-${NAME}"

usage() {
  cat <<'EOF'
用法: bash deploy/make_oneclick.sh --runtime /path/to/python
说明: --runtime 指向 python-build-standalone 的 install_only 解压目录（含 bin/python3.13）。
      也可用 BBU_RUNTIME_DIR 环境变量；默认尝试 /tmp/python 与 build/runtime/python。
EOF
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --runtime) RUNTIME_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done
if [ -z "$RUNTIME_DIR" ]; then
  for candidate in /tmp/python "${ROOT_DIR}/build/runtime/python"; do
    if [ -x "${candidate}/bin/python3.13" ]; then RUNTIME_DIR="$candidate"; break; fi
  done
fi
[ -x "${RUNTIME_DIR}/bin/python3.13" ] || {
  echo "[错误] 未找到内置 Python，请用 --runtime 指定（需含 bin/python3.13）" >&2
  exit 1
}
"${RUNTIME_DIR}/bin/python3.13" - <<'PY' || { echo "[错误] 内置 Python 自检失败" >&2; exit 1; }
import sys, ssl, sqlite3, ctypes, lzma, bz2
assert sys.version_info >= (3, 11), sys.version
print("[检查] Python", sys.version.split()[0], "OpenSSL", ssl.OPENSSL_VERSION,
      "SQLite", sqlite3.sqlite_version)
PY

rm -rf "$STAGE"
mkdir -p "$STAGE/${NAME}/app" "$STAGE/${NAME}/runtime"

tar -C "$ROOT_DIR" \
  --exclude='./data' \
  --exclude='./evaluations' \
  --exclude='./testlab' \
  --exclude='./skills' \
  --exclude='./cases' \
  --exclude='./updates' \
  --exclude='./backups' \
  --exclude='./dist' \
  --exclude='./build' \
  --exclude='./config.json*' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='./.git' \
  --exclude='./.pytest_cache' \
  -cf - . | tar -C "$STAGE/${NAME}/app" -xf -

cp -a "$RUNTIME_DIR" "$STAGE/${NAME}/runtime/python"
cp "$ROOT_DIR/deploy/oneclick/install.sh" "$STAGE/${NAME}/install.sh"
cp "$ROOT_DIR/deploy/oneclick/安装说明.txt" "$STAGE/${NAME}/安装说明.txt"
# 说明文件中的版本号跟随当前构建版本
sed -i "s/bbu-fault-platform-[0-9][0-9.]*-ubuntu1604-offline/bbu-fault-platform-${VERSION}-ubuntu1604-offline/g" \
  "$STAGE/${NAME}/安装说明.txt" 2>/dev/null || true
chmod +x "$STAGE/${NAME}/install.sh"
chmod +x "$STAGE/${NAME}/app/deploy/install_upgrade_helper.sh" 2>/dev/null || true

mkdir -p "$DIST_DIR"
tar -C "$STAGE" -czf "$OUT" "${NAME}"
( cd "$DIST_DIR" && (sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" 2>/dev/null || shasum -a 256 "$(basename "$OUT")" > "$(basename "$OUT").sha256") )
rm -rf "$STAGE"

echo "[完成] $OUT"
echo "[完成] ${OUT}.sha256"
echo "大小: $(du -h "$OUT" | cut -f1)"
echo "安装: tar -xzf $(basename "$OUT") && cd ${NAME} && sudo bash install.sh"
