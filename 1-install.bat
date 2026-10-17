@echo off
cd /d "%~dp0"
if not exist "%~dp0runtime\python.exe" (
  if exist "%~dp0runtime.zip" (
    echo Expanding bundled Python runtime, please wait...
    tar -xf "%~dp0runtime.zip" -C "%~dp0"
  )
)
if not exist "%~dp0runtime\python.exe" (
  echo [X] runtime not found. Make sure runtime.zip is in this folder.
  pause
  exit /b 1
)
"%~dp0runtime\python.exe" "%~dp0CpuPLFix.py" install
echo.
pause
