@echo off
setlocal
set ROOT=%~dp0..
python "%ROOT%\tools\bbu_ops.py" %*
exit /b %ERRORLEVEL%
