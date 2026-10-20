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
if not exist "%~dp0..\runtime\python.exe" (
  if exist "%~dp0..\runtime.zip" (
    echo Expanding bundled Python runtime, please wait...
    tar -xf "%~dp0..\runtime.zip" -C "%~dp0..\"
  )
)
if not exist "%~dp0..\runtime\python.exe" (
  echo [X] runtime not found.
echo     Expected: ..untime\python.exe  (shared runtime for both services)
echo     Fix: copy a Python 3.13 x64 embeddable runtime to ..untime\ ,
echo          or restore runtime.zip from git history and extract it there.
  pause
  exit /b 1
)
"%~dp0..\runtime\python.exe" "%~dp0CpuPLFix.py" install
echo.
pause
