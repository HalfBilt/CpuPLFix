@echo off
cd /d "%~dp0"
if not exist "%~dp0runtime\python.exe" (
  echo [X] runtime not found. Please run 1-install.bat first.
  pause
  exit /b 1
)
"%~dp0runtime\python.exe" "%~dp0CpuPLFix.py" fix
echo.
pause
