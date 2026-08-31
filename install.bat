@echo off
REM ==============================================================================
REM Axon Windows 1-Click Installer (Command Prompt & Double-Click)
REM ==============================================================================

echo.
echo   [Axon Windows Installer]
echo   Terminal-Native Agentic Coding Assistant
echo.

REM 1. Check for Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% neq 0 (
        echo [ERROR] Python is not installed or not in your system PATH.
        echo Please download and install Python 3.11+ from https://www.python.org/downloads/
        echo Make sure to check "Add Python to PATH" during installation.
        pause
        exit /b 1
    ) else (
        set "PY_CMD=py"
    )
) else (
    set "PY_CMD=python"
)

echo [OK] Using %PY_CMD%...

REM 2. Run Python Universal Auto-Setup
%PY_CMD% setup_env.py
if %errorlevel% neq 0 (
    echo [ERROR] Environment setup encountered an issue.
    pause
    exit /b %errorlevel%
)

echo.
echo ==============================================================================
echo Setup finished! You can now start Axon or run check_models.py
echo ==============================================================================
echo.
pause
