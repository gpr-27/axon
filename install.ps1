# ==============================================================================
# Axon Windows PowerShell 1-Click Installer
# ==============================================================================
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   or from within PowerShell: .\install.ps1
# ==============================================================================

Write-Host ""
Write-Host "  ▲█▲  Axon Windows PowerShell Installer" -ForegroundColor Cyan
Write-Host "  █⚡█  Terminal-Native Agentic Coding Assistant" -ForegroundColor Yellow
Write-Host ""

# 1. Find Python executable
$pythonExe = ""
if (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonExe = "py"
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $pythonExe = "python3"
}

if (-not $pythonExe) {
    Write-Host "❌ Error: Python is not installed or not in PATH." -ForegroundColor Red
    Write-Host "   Please install Python 3.11+ from https://www.python.org/downloads/" -ForegroundColor White
    Write-Host "   Ensure 'Add python.exe to PATH' is checked during setup." -ForegroundColor Yellow
    exit 1
}

Write-Host "✓ Found Python: $pythonExe" -ForegroundColor Green

# 2. Run universal environment setup
& $pythonExe setup_env.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Setup encountered an error." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "🎉 PowerShell setup completed!" -ForegroundColor Green
Write-Host "   To run Axon directly:" -ForegroundColor White
Write-Host "     python axon_run.py" -ForegroundColor Cyan
Write-Host "   Or activate virtual environment:" -ForegroundColor White
Write-Host "     .\.venv\Scripts\Activate.ps1; axon" -ForegroundColor Cyan
Write-Host ""
