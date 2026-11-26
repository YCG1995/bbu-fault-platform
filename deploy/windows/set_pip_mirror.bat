@echo off
chcp 65001 >nul
setlocal
set "TARGET=%APPDATA%\pip"
if not exist "%TARGET%" mkdir "%TARGET%"
if exist "%TARGET%\pip.ini" copy /Y "%TARGET%\pip.ini" "%TARGET%\pip.ini.bak" >nul
copy /Y "%~dp0..\pip.ini" "%TARGET%\pip.ini" >nul
if errorlevel 1 (
  echo [错误] 无法写入 %TARGET%\pip.ini
  exit /b 1
)
echo [完成] 已配置内网 pip 源：%TARGET%\pip.ini
python -m pip config list 2>nul
