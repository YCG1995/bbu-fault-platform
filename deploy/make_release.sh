#!/usr/bin/env bash
# 生成可拷贝到内网的离线发布包；不包含 data、skills、cases、config.json。
set -euo pipefail

APP_NAME="bbu-fault-platform"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${ROOT_DIR}/bbu/__init__.py")"
OUT="${DIST_DIR}/${APP_NAME}-${VERSION}-offline.tar.gz"
STAGE="${DIST_DIR}/.stage-${APP_NAME}-${VERSION}"

rm -rf "$STAGE"
mkdir -p "$STAGE/${APP_NAME}" "$DIST_DIR"

tar -C "$ROOT_DIR" \
  --exclude='./data' \
  --exclude='./skills' \
  --exclude='./cases' \
  --exclude='./updates' \
  --exclude='./backups' \
  --exclude='./dist' \
  --exclude='./config.json' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='./.git' \
  --exclude='./.pytest_cache' \
  -cf - . | tar -C "$STAGE/${APP_NAME}" -xf -

# 保留空目录，运行时由 config 指向 state dir 或自动创建
mkdir -p "$STAGE/${APP_NAME}/skills" "$STAGE/${APP_NAME}/cases"

tar -C "$STAGE" -czf "$OUT" "${APP_NAME}"
( cd "$DIST_DIR" && (sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" 2>/dev/null || shasum -a 256 "$(basename "$OUT")" > "$(basename "$OUT").sha256") )
rm -rf "$STAGE"

echo "[完成] $OUT"
echo "[完成] ${OUT}.sha256"
echo "安装：tar -xzf $(basename "$OUT") && sudo bash ${APP_NAME}/deploy/install_offline.sh --port 8080"
