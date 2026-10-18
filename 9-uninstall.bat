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
if not exist "%~dp0runtime\python.exe" (
  echo [X] runtime not found. Please run 1-install.bat first.
  pause
  exit /b 1
)"%~dp0runtime\python.exe" "%~dp0CpuPLFix.py" uninstall
echo.
pause