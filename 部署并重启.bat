@echo off
cd /d "%~dp0"
if "%CPF_ELEV%"=="1" goto run
fltmc >nul 2>&1
if %errorlevel%==0 goto run
set CPF_ELEV=1
echo Requesting administrator privileges...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b
:run
chcp 65001 >nul
title CpuPLFix restart

rem ============================================================
rem  2026-10-20 起: 本目录既是"安装目录"也是"源码仓库"。
rem  改完 CpuPLFix.py 不用再复制 —— 这个脚本只做"清缓存 + 重启服务"。
rem  (以前要跑 _repo 里的同名脚本, 现在 _repo 已合并进来)
rem ============================================================

set "DST=%~dp0"
if "%DST:~-1%"=="\" set "DST=%DST:~0,-1%"
set "LOG=%DST%\logs\fix.log"

echo [1/3] checking files ...
if not exist "%DST%\CpuPLFix.py" (
  echo   [X] CpuPLFix.py not found in "%DST%"
  echo.
  pause
  exit /b 1
)
if not exist "%DST%\runtime\python.exe" (
  echo   [X] runtime\python.exe missing - run 1-install.bat first.
  echo.
  pause
  exit /b 1
)
echo   install dir = %DST%
echo   (this folder is the repo too - no copying needed)

echo [2/3] clearing __pycache__ ...
if exist "%DST%\__pycache__" rd /s /q "%DST%\__pycache__"
echo   [ok]

echo [3/3] restarting service CpuPLFixSvc ...
net stop CpuPLFixSvc
timeout /t 3 /nobreak >nul
net start CpuPLFixSvc
echo.
sc query CpuPLFixSvc | findstr /i "SERVICE_NAME STATE"

echo.
echo ---------------- log tail (last 14 lines) ----------------
powershell -NoProfile -Command "Get-Content -Encoding UTF8 -Tail 14 '%LOG%'"
echo ----------------------------------------------------------
echo.
echo Done. 新代码要等日志里出现 "服务就绪" 才算生效。
echo.
pause
