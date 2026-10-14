<#
  Axon installer for Windows PowerShell 5.1 and PowerShell 7+.

  Run from the repository folder:
      powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
  or, inside an open PowerShell window:
      Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
      .\install.ps1

  Why ExecutionPolicy: Windows blocks unsigned .ps1 scripts by default ("running scripts is
  disabled on this system"). '-ExecutionPolicy Bypass' (or the Process-scoped command above)
  allows this one script for this one session only; it does not change any machine setting.
  If you prefer not to, run install.bat instead - it does the same thing without PowerShell.

  Options:  -NoPath   do not add the axon launcher folder to your user PATH
            -NoShim   do not create the axon launcher

  This file is intentionally pure ASCII so Windows PowerShell 5.1 parses it correctly.
#>
[CmdletBinding()]
param(
    [switch]$NoPath,
    [switch]$NoShim
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Write-Step([string]$Text, [string]$Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }

function Test-PythonCandidate([string]$Exe, [string[]]$PreArgs) {
    # Returns $true when the interpreter runs and is Python >= 3.10. The Microsoft Store stub
    # (WindowsApps\python.exe) exits with an error instead of running, so it is rejected here.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & $Exe @PreArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 3)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $old
    }
}

Write-Host ''
Write-Step '  Axon installer (Windows)' 'Cyan'
Write-Host ''

$candidates = @(
    @{ Exe = 'py';      Args = @('-3') },
    @{ Exe = 'python';  Args = @() },
    @{ Exe = 'python3'; Args = @() }
)
$python = $null
foreach ($c in $candidates) {
    if (Get-Command $c.Exe -ErrorAction SilentlyContinue) {
        if (Test-PythonCandidate $c.Exe $c.Args) { $python = $c; break }
    }
}

if (-not $python) {
    Write-Step '[ERROR] Python 3.10 or newer was not found.' 'Red'
    Write-Step '        Install it from https://www.python.org/downloads/windows/' 'White'
    Write-Step '        and tick "Add python.exe to PATH" in the installer.' 'White'
    Write-Step '        Note: the "python" command that opens the Microsoft Store is only a stub.' 'Yellow'
    exit 1
}

$shown = ($python.Exe + ' ' + ($python.Args -join ' ')).Trim()
Write-Step "[OK] Using Python: $shown" 'Green'

$setupArgs = @((Join-Path $PSScriptRoot 'scripts\setup_env.py'))
if ($NoPath) { $setupArgs += '--no-path' }
if ($NoShim) { $setupArgs += '--no-shim' }

$ErrorActionPreference = 'Continue'
$allArgs = @($python.Args) + $setupArgs
& $python.Exe @allArgs
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Step "[ERROR] Setup failed (exit code $code). See the messages above." 'Red'
    exit $code
}

Write-Host ''
Write-Step 'Axon is installed. Open a NEW terminal window and run:  axon' 'Green'
Write-Host ''
