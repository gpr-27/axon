@echo off
REM Axon installer for Command Prompt (or double-click).
REM   From a clone: editable install into .venv (same steps as install.ps1).
REM   Anywhere else (for example a downloaded install.bat): runs the PowerShell installer, which
REM   installs Axon from GitHub. Pass --uninstall to remove Axon.
setlocal EnableExtensions
cd /d "%~dp0"

if /i "%~1"=="--uninstall" set "AXON_UNINSTALL=1"
if defined AXON_UNINSTALL goto :standalone
if not exist "scripts\setup_env.py" goto :standalone

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
echo Axon is installed. Open a NEW terminal window and run:  axon setup
echo.
if "%AXON_NO_PAUSE%"=="" pause
exit /b 0

:standalone
REM Not inside a clone: hand over to install.ps1 (the local copy when present, else from GitHub).
if "%AXON_INSTALL_REF%"=="" set "AXON_INSTALL_REF=main"
if exist "%~dp0install.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Standalone
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/gpr-27/axon/%AXON_INSTALL_REF%/install.ps1 | iex; exit $AxonInstallExitCode"
)
if errorlevel 1 goto :failed
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
