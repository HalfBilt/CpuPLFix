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
)
set LV=1
set /p LV=Level (1-7 quiet..loud, 64 = full speed): 
"%~dp0runtime\python.exe" "%~dp0CpuPLFix.py" fan level %LV%
echo.
pause
