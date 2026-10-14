@echo off
REM Axon installer for Command Prompt (or double-click). Same steps as install.ps1.
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo   Axon installer (Windows)
echo.

set "PY_CMD="
call :try_python py -3
if not defined PY_CMD call :try_python python
if not defined PY_CMD call :try_python python3
if not defined PY_CMD goto :no_python

echo [OK] Using Python: %PY_CMD%
%PY_CMD% scripts\setup_env.py %*
if errorlevel 1 goto :failed

echo.
echo Axon is installed. Open a NEW terminal window and run:  axon
echo.
if "%AXON_NO_PAUSE%"=="" pause
exit /b 0

:try_python
REM Accept only a real interpreter that is Python 3.10+ (rejects the Microsoft Store stub).
where %1 >nul 2>nul
if errorlevel 1 exit /b 0
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 3)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PY_CMD=%*"
exit /b 0

:no_python
echo [ERROR] Python 3.10 or newer was not found.
echo         Install it from https://www.python.org/downloads/windows/
echo         and tick "Add python.exe to PATH" in the installer.
echo         Note: the "python" command that opens the Microsoft Store is only a stub.
if "%AXON_NO_PAUSE%"=="" pause
exit /b 1

:failed
echo [ERROR] Setup failed. See the messages above.
if "%AXON_NO_PAUSE%"=="" pause
exit /b 1
