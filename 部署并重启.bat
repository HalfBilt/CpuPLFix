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
title CpuPLFix deploy + restart

set "SRC=%~dp0"
set "DST=%APPDATA%\CpuPLFix"
set "LOG=%DST%\logs\fix.log"

echo [1/4] checking paths ...
if not exist "%SRC%CpuPLFix.py" (
  echo   [X] CpuPLFix.py not found next to this bat - run it from the _repo folder.
  echo.
  pause
  exit /b 1
)
if not exist "%DST%\CpuPLFix.py" (
  echo   [X] deploy folder "%DST%" has no CpuPLFix.py - install first ^(1-install.bat^).
  echo.
  pause
  exit /b 1
)
echo   repo   = %SRC%
echo   deploy = %DST%
fc /b "%SRC%CpuPLFix.py" "%DST%\CpuPLFix.py" >nul 2>&1
if not errorlevel 1 (
  echo   [i] the two files are already identical
) else (
  echo   [i] they differ - the new code will be copied
)

echo [2/4] copying CpuPLFix.py to the deploy folder ...
copy /y "%SRC%CpuPLFix.py" "%DST%\CpuPLFix.py" >nul
if errorlevel 1 (
  echo   [X] copy failed
  echo.
  pause
  exit /b 1
)
echo   [ok]

echo [3/4] removing __pycache__ so the new code is really loaded ...
if exist "%DST%\__pycache__" rd /s /q "%DST%\__pycache__"
echo   [ok]

echo [4/4] restarting service CpuPLFixSvc ...
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
echo Done. The next EC report line needs about 5-6 minutes.
echo How to confirm THIS build is live (the wait value no longer tells you):
echo   - 2-check prints the fan level label with 3880 RPM (not the old 3490)
echo   - fan write problems now say either "write failed" or "readback not confirmed"
echo   - the health line now reads "EC bus: ... N transactions ... collisions ..."
echo     (new wording; it counts transactions, not read decisions)
echo   - a NON-admin process can now OPEN the two named mutexes. Check it with:
echo       %APPDATA%\CpuPLFix\runtime\python.exe %APPDATA%\_repo\CpuPLFix.py lock
echo     (it must print "can open" for both; NOTE: 2-check itself always needs admin,
echo      because it opens the TSCtl driver - that is unrelated to the mutex)
echo   - a new "===== ... svc =====" block appears in fix.log after the restart
echo.
pause
