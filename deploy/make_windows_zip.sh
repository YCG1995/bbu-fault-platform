#!/usr/bin/env bash
# 生成 Windows 手动运行包（只含代码，不含 Python 运行时，不是一键安装器）。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${ROOT_DIR}/bbu/__init__.py")"
NAME="bbu-fault-platform-${VERSION}-windows"
OUT="${DIST_DIR}/${NAME}.zip"
STAGE="${DIST_DIR}/.windows-${NAME}"

rm -rf "$STAGE"
mkdir -p "$STAGE/${NAME}"
tar -C "$ROOT_DIR" \
  --exclude='./data' --exclude='./skills' --exclude='./cases' \
  --exclude='./evaluations' --exclude='./testlab' \
  --exclude='./updates' --exclude='./backups' \
  --exclude='./dist' --exclude='./build' --exclude='./config.json*' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='./.git' --exclude='./.pytest_cache' \
  -cf - . | tar -C "$STAGE/${NAME}" -xf -
mkdir -p "$DIST_DIR"
( cd "$STAGE" && zip -qr "$OUT" "$NAME" )
( cd "$DIST_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
rm -rf "$STAGE"
echo "[完成] $OUT"
echo "[完成] ${OUT}.sha256"
echo "大小: $(du -h "$OUT" | cut -f1)"
echo "运行: 解压后执行 deploy\\windows\\start_platform.bat"
