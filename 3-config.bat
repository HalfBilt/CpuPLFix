@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

rem Prefer the runtime next to this script (that is the deployed copy). The git repo
rem only ships runtime.zip, so fall back to the installed copy under %APPDATA%\T14scontrol\cpu.
set PYBIN=%~dp0..\runtime\python.exe
set CFGPY=%~dp0CpuPLFixCfg.py
if not exist "%PYBIN%" set PYBIN=%APPDATA%\T14scontrol\runtime\python.exe
if not exist "%CFGPY%" set CFGPY=%APPDATA%\T14scontrol\cpu\CpuPLFixCfg.py

if not exist "%PYBIN%" (
  echo [X] runtime\python.exe not found - run 1-install.bat first.
  pause
  exit /b 1
)
if not exist "%CFGPY%" (
  echo [X] CpuPLFixCfg.py not found.
  pause
  exit /b 1
)

"%PYBIN%" "%CFGPY%" %*
set RC=%ERRORLEVEL%

rem Pause only when double-clicked; do not pause when called from a script.
echo %cmdcmdline% | find /i "%~nx0" >nul
if not errorlevel 1 pause
exit /b %RC%
