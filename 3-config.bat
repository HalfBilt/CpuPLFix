@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

if not exist "%~dp0runtime\python.exe" (
  echo [X] runtime\python.exe not found. Reinstall CpuPLFix first.
  pause
  exit /b 1
)

"%~dp0runtime\python.exe" "%~dp0CpuPLFixCfg.py" %*
set RC=%ERRORLEVEL%

rem ????????????, ?????????; ????????????
echo %cmdcmdline% | find /i "%~nx0" >nul
if not errorlevel 1 pause
exit /b %RC%
