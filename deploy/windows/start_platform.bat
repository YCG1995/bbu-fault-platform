@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0..\.."
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请安装 Python 3.11+ x64 并勾选 Add to PATH。
  exit /b 1
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [错误] Python 版本低于 3.11，请升级。
  exit /b 1
)
python run.py --host 0.0.0.0 --port 8080 %*
