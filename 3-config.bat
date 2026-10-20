@echo off
setlocal
cd /d "%~dp0"
rem ---- console codepage adaptive: keep Python output readable ----
for /f "tokens=2 delims=: " %%c in ('chcp') do set "CP=%%c"
if "%CP%"=="936" set "PYTHONIOENCODING=gbk"
if "%CP%"=="65001" set "PYTHONIOENCODING=utf-8"

rem runtime 在上两级目录（两个服务共用），找不到再退回 %APPDATA%\T14scontrol\runtime
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
