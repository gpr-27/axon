<#
  Axon installer for Windows PowerShell 5.1 and PowerShell 7+.

  One line, no clone needed. In PowerShell:
      irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1 | iex
  In Command Prompt:
      powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1 | iex"

  Options with the one-liner: set environment variables first, for example
      $env:AXON_INSTALL_REF = 'v0.27.2'; irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1 | iex
  or pass parameters through a script block:
      & ([scriptblock]::Create((irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1))) -Uninstall

  From a clone (editable install into .\.venv, for development):
      powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1

  Outside a clone the installer uses the first available method:
      uv     uv tool install   (when uv is on PATH; uv can also fetch Python for you)
      pipx   pipx install      (when pipx is on PATH)
      venv   a private virtual environment in %USERPROFILE%\.axon\venv plus the launcher
             %USERPROFILE%\.axon\bin\axon.cmd
  The launcher folder is added to your user PATH (no admin rights needed). Running the installer
  again upgrades Axon in place.

  Parameters (environment variable in brackets):
      -Method M        auto (default), uv, pipx or venv               [AXON_INSTALL_METHOD]
      -Ref REF         branch, tag or commit to install (main)        [AXON_INSTALL_REF]
      -Source SPEC     a local folder, git+ URL, archive URL or PyPI requirement such as
                       axon-terminal, instead of GitHub               [AXON_INSTALL_SOURCE]
      -InstallDir DIR  virtual environment for the venv method        [AXON_INSTALL_DIR]
      -PythonExe EXE   Python 3.10+ interpreter to use                [AXON_PYTHON]
      -Standalone      install from GitHub even when run from inside a clone
      -Uninstall       remove Axon; settings in %USERPROFILE%\.axon are kept   [AXON_UNINSTALL=1]
      -NoPath          do not change the user PATH                    [AXON_NO_MODIFY_PATH=1]
      -NoShim          (clone mode) do not create the axon launcher
      -DryRun          show what would happen without changing anything
      -Help            show this help

  Why '-ExecutionPolicy Bypass' for the file form: Windows blocks unsigned .ps1 files by default
  ("running scripts is disabled on this system"). The flag allows this one script for this one
  process and changes no machine setting. 'irm | iex' is not affected by the policy. install.bat
  does the same as this script without PowerShell policies.

  This file is intentionally pure ASCII so Windows PowerShell 5.1 parses it correctly.
#>
param(
    [string]$Method = '',
    [string]$Ref = '',
    [string]$Source = '',
    [string]$InstallDir = '',
    [string]$PythonExe = '',
    [switch]$Standalone,
    [switch]$Uninstall,
    [switch]$NoPath,
    [switch]$NoShim,
    [switch]$DryRun,
    [switch]$Help
)

# Everything lives in functions so that 'irm | iex' (which runs in your own session) does not
# change your session's preferences, and so a download cut off half way does nothing.

$AxonInstallerScriptRoot = $PSScriptRoot

function Write-AxonLine([string]$Text, [string]$Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }
function Write-AxonOk([string]$Text) { Write-AxonLine "[OK] $Text" 'Green' }
function Write-AxonStep([string]$Text) { Write-AxonLine "[..] $Text" 'Cyan' }
function Write-AxonWarn([string]$Text) { Write-AxonLine "[WARN] $Text" 'Yellow' }
function Write-AxonErr([string]$Text) { Write-AxonLine "[ERROR] $Text" 'Red' }

function Test-AxonOnWindows { return ($env:OS -eq 'Windows_NT') }

function Invoke-AxonCommand([string]$Exe, [string[]]$Arguments, [bool]$Dry) {
    # Prints the command, runs it with its output going to the console, returns the exit code.
    $shown = @($Exe) + $Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    Write-AxonLine ('     $ ' + ($shown -join ' ')) 'DarkGray'
    if ($Dry) { return 0 }
    & $Exe @Arguments | Out-Host
    return $LASTEXITCODE
}

function Test-AxonPython([string]$Exe, [string[]]$PreArgs) {
    # $true when the interpreter runs and is Python >= 3.10. The Microsoft Store stub
    # (WindowsApps\python.exe) exits with an error instead of running, so it is rejected here.
    try {
        $null = & $Exe @PreArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 3)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-AxonPython([string]$Preferred) {
    # Returns @{ Exe = ...; Args = @(...) } or $null.
    $candidates = @()
    if ($Preferred) { $candidates += @{ Exe = $Preferred; Args = @() } }
    $candidates += @(
        @{ Exe = 'py';      Args = @('-3') },
        @{ Exe = 'python';  Args = @() },
        @{ Exe = 'python3'; Args = @() },
        @{ Exe = 'python3.13'; Args = @() },
        @{ Exe = 'python3.12'; Args = @() },
        @{ Exe = 'python3.11'; Args = @() },
        @{ Exe = 'python3.10'; Args = @() }
    )
    foreach ($c in $candidates) {
        if (Get-Command $c.Exe -ErrorAction SilentlyContinue) {
            if (Test-AxonPython $c.Exe $c.Args) { return $c }
        }
        if ($Preferred -and $c.Exe -eq $Preferred) {
            Write-AxonWarn "$Preferred is not a working Python 3.10 or newer; looking for another one."
        }
    }
    return $null
}

function Write-AxonPythonHelp {
    Write-AxonLine '        Install Python 3.10+ from https://www.python.org/downloads/windows/' 'White'
    Write-AxonLine '        and tick "Add python.exe to PATH", or install uv (it fetches Python for you):' 'White'
    Write-AxonLine '          powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"' 'White'
    Write-AxonLine '        Note: the "python" command that opens the Microsoft Store is only a stub.' 'Yellow'
}

function Get-AxonNormalizedDir([string]$Dir) {
    return ([Environment]::ExpandEnvironmentVariables($Dir)).TrimEnd('\', '/').ToLowerInvariant()
}

function Test-AxonOnPath([string]$Dir) {
    $want = Get-AxonNormalizedDir $Dir
    foreach ($p in ($env:PATH -split [IO.Path]::PathSeparator)) {
        if ($p -and (Get-AxonNormalizedDir $p) -eq $want) { return $true }
    }
    return $false
}

function Send-AxonEnvironmentChanged {
    # Setting and clearing a user variable makes Windows broadcast WM_SETTINGCHANGE, so new
    # terminals opened from Explorer or the taskbar see the updated PATH.
    [Environment]::SetEnvironmentVariable('AXON_INSTALLER_REFRESH', '1', 'User')
    [Environment]::SetEnvironmentVariable('AXON_INSTALLER_REFRESH', $null, 'User')
}

function Get-AxonUserPathKey {
    return [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
}

function Add-AxonUserPath([string]$Dir, [bool]$Dry) {
    # Adds $Dir to the user PATH (HKCU\Environment), keeping %VARIABLES% unexpanded.
    if (-not (Test-AxonOnPath $Dir)) { $env:PATH = $Dir + [IO.Path]::PathSeparator + $env:PATH }
    if (-not (Test-AxonOnWindows)) { return $false }
    $key = Get-AxonUserPathKey
    try {
        $current = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $want = Get-AxonNormalizedDir $Dir
        $parts = @($current -split ';' | Where-Object { $_ })
        foreach ($p in $parts) { if ((Get-AxonNormalizedDir $p) -eq $want) { return $false } }
        if ($Dry) { return $true }
        $key.SetValue('Path', (($parts + $Dir) -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
    } finally {
        $key.Close()
    }
    Send-AxonEnvironmentChanged
    return $true
}

function Remove-AxonUserPath([string]$Dir, [bool]$Dry) {
    if (-not (Test-AxonOnWindows)) { return $false }
    $key = Get-AxonUserPathKey
    try {
        $current = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $want = Get-AxonNormalizedDir $Dir
        $parts = @($current -split ';' | Where-Object { $_ })
        $keep = @($parts | Where-Object { (Get-AxonNormalizedDir $_) -ne $want })
        if ($keep.Count -eq $parts.Count) { return $false }
        if ($Dry) { return $true }
        $key.SetValue('Path', ($keep -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
    } finally {
        $key.Close()
    }
    Send-AxonEnvironmentChanged
    return $true
}

function Get-AxonLauncher([string]$Dir) {
    foreach ($name in @('axon.exe', 'axon.cmd', 'axon')) {
        $p = Join-Path $Dir $name
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
    return $null
}

function Test-AxonShim([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $text = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    return ($text -and ($text -match 'Axon installer' -or $text -match '-m axon'))
}

function Get-AxonToolOutput([string]$Exe, [string[]]$Arguments) {
    try {
        $out = & $Exe @Arguments 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -First 1)).Trim() }
    } catch { }
    return $null
}

function Show-AxonHelp {
    $file = $null
    if ($AxonInstallerScriptRoot) { $file = Join-Path $AxonInstallerScriptRoot 'install.ps1' }
    if ($file -and (Test-Path -LiteralPath $file)) {
        $text = Get-Content -LiteralPath $file -Raw
        $start = $text.IndexOf('<#') + 2
        Write-Host $text.Substring($start, $text.IndexOf('#>') - $start)
    } else {
        Write-Host 'Axon installer. See https://github.com/gpr-27/axon#installation for all options.'
        Write-Host 'Parameters: -Method auto|uv|pipx|venv  -Ref REF  -Source SPEC  -InstallDir DIR'
        Write-Host '            -PythonExe EXE  -Standalone  -Uninstall  -NoPath  -NoShim  -DryRun  -Help'
    }
}

function Invoke-AxonUninstall([hashtable]$O) {
    Write-Host ''
    Write-AxonLine '  Uninstalling Axon' 'Cyan'
    Write-Host ''
    $removed = $false
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $list = (& uv tool list 2>$null) -join "`n"
        if ($list -match '(?m)^axon-terminal ') {
            if ((Invoke-AxonCommand 'uv' @('tool', 'uninstall', 'axon-terminal') $O.DryRun) -eq 0) { $removed = $true }
        }
    }
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        $list = (& pipx list --short 2>$null) -join "`n"
        if ($list -match '(?m)^axon-terminal ') {
            if ((Invoke-AxonCommand 'pipx' @('uninstall', 'axon-terminal') $O.DryRun) -eq 0) { $removed = $true }
        }
    }
    if (Test-Path -LiteralPath $O.VenvDir) {
        if (Test-Path -LiteralPath (Join-Path $O.VenvDir '.axon-installer')) {
            Write-AxonStep "Removing $($O.VenvDir)"
            if (-not $O.DryRun) { Remove-Item -LiteralPath $O.VenvDir -Recurse -Force }
            $removed = $true
        } else {
            Write-AxonWarn "Leaving $($O.VenvDir) alone: it was not created by this installer."
        }
    }
    foreach ($name in @('axon.cmd', 'axon')) {
        $shim = Join-Path $O.BinDir $name
        if (Test-AxonShim $shim) {
            Write-AxonStep "Removing $shim"
            if (-not $O.DryRun) { Remove-Item -LiteralPath $shim -Force }
            $removed = $true
        }
    }
    if ((Test-Path -LiteralPath $O.BinDir) -and -not (Get-ChildItem -LiteralPath $O.BinDir -Force | Select-Object -First 1)) {
        if (-not $O.DryRun) { Remove-Item -LiteralPath $O.BinDir -Force }
        if (Remove-AxonUserPath $O.BinDir $O.DryRun) { Write-AxonOk "Removed $($O.BinDir) from your user PATH" }
    }
    if ($removed) { Write-AxonOk 'Axon was removed.' } else { Write-AxonOk 'No installer-managed Axon installation was found.' }
    $still = Get-Command axon -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($still -and -not $O.DryRun -and -not (Test-Path -LiteralPath $still.Source)) { $still = $null }
    if ($still -and -not $O.DryRun) {
        Write-AxonWarn "An 'axon' command is still on PATH: $($still.Source)"
        Write-AxonLine '       If you installed it with pip, run:  py -m pip uninstall axon-terminal' 'White'
    }
    Write-Host ''
    Write-AxonLine '  Your settings, keys and sessions in %USERPROFILE%\.axon were kept. To delete them too:' 'Gray'
    Write-AxonLine '      Remove-Item -Recurse -Force "$env:USERPROFILE\.axon"' 'Gray'
    Write-AxonLine "  A source checkout's .venv is not touched; delete the clone to remove it." 'Gray'
    Write-Host ''
    return 0
}

function Invoke-AxonCloneSetup([hashtable]$O, [string]$Root) {
    $py = Find-AxonPython $O.Python
    if (-not $py) {
        Write-AxonErr 'Python 3.10 or newer was not found.'
        Write-AxonPythonHelp
        return 1
    }
    $shown = ($py.Exe + ' ' + ($py.Args -join ' ')).Trim()
    Write-AxonOk "Using Python: $shown"
    $setupArgs = @($py.Args) + @((Join-Path (Join-Path $Root 'scripts') 'setup_env.py'))
    if ($O.NoPath) { $setupArgs += '--no-path' }
    if ($O.NoShim) { $setupArgs += '--no-shim' }
    $code = Invoke-AxonCommand $py.Exe $setupArgs $O.DryRun
    if ($code -ne 0) {
        Write-AxonErr "Setup failed (exit code $code). See the messages above."
        return $code
    }
    if ($O.DryRun) { return 0 }
    Write-Host ''
    Write-AxonLine 'Axon is installed. Open a NEW terminal window and run:  axon setup' 'Green'
    Write-Host ''
    return 0
}

function Install-AxonWithVenv([hashtable]$O, [string]$Spec) {
    $py = Find-AxonPython $O.Python
    if (-not $py) {
        Write-AxonErr 'Python 3.10 or newer was not found.'
        Write-AxonPythonHelp
        return $null
    }
    $exe = Get-AxonToolOutput $py.Exe (@($py.Args) + @('-c', 'import sys; print(sys.executable)'))
    if (-not $exe) { $exe = $py.Exe }
    Write-AxonOk "Using Python: $exe"
    $onWin = Test-AxonOnWindows
    if ($onWin) { $vpy = Join-Path (Join-Path $O.VenvDir 'Scripts') 'python.exe' } else { $vpy = Join-Path (Join-Path $O.VenvDir 'bin') 'python' }
    $marker = Join-Path $O.VenvDir '.axon-installer'
    if ((Test-Path -LiteralPath $vpy) -and -not (Test-AxonPython $vpy @())) {
        if (-not (Test-Path -LiteralPath $marker)) {
            Write-AxonErr "$($O.VenvDir) exists but was not created by this installer. Remove it or pass -InstallDir."
            return $null
        }
        Write-AxonStep "Recreating the broken virtual environment in $($O.VenvDir)"
        if (-not $O.DryRun) { Remove-Item -LiteralPath $O.VenvDir -Recurse -Force }
    }
    if (-not (Test-Path -LiteralPath $vpy)) {
        if ((Test-Path -LiteralPath $O.VenvDir) -and (Get-ChildItem -LiteralPath $O.VenvDir -Force | Select-Object -First 1) -and -not (Test-Path -LiteralPath $marker)) {
            Write-AxonErr "$($O.VenvDir) exists and is not an Axon virtual environment. Remove it or pass -InstallDir."
            return $null
        }
        Write-AxonStep "Creating a virtual environment in $($O.VenvDir)"
        if ((Invoke-AxonCommand $py.Exe (@($py.Args) + @('-m', 'venv', $O.VenvDir)) $O.DryRun) -ne 0) {
            Write-AxonErr "Could not create $($O.VenvDir)."
            return $null
        }
    } else {
        Write-AxonOk "Existing virtual environment: $($O.VenvDir)"
    }
    if (-not $O.DryRun) {
        [IO.File]::WriteAllText($marker, "Created by the Axon installer. Remove with: install.ps1 -Uninstall`r`n", [Text.Encoding]::ASCII)
    }
    Write-AxonStep "Installing Axon from $Spec"
    $pipBase = @('-m', 'pip', 'install', '--disable-pip-version-check')
    $null = Invoke-AxonCommand $vpy ($pipBase + @('--quiet', '--upgrade', 'pip')) $O.DryRun
    $installed = $false
    if (-not $O.DryRun) {
        $null = & $vpy -m pip show --quiet axon-terminal 2>$null
        $installed = ($LASTEXITCODE -eq 0)
    }
    if ($installed) {
        # Upgrade: pip skips a git or folder source whose version did not change, so reinstall
        # Axon itself, then bring its dependencies up to date.
        $code = Invoke-AxonCommand $vpy ($pipBase + @('--force-reinstall', '--no-deps', $Spec)) $O.DryRun
        if ($code -eq 0) { $code = Invoke-AxonCommand $vpy ($pipBase + @('--quiet', '--upgrade', $Spec)) $O.DryRun }
    } else {
        $code = Invoke-AxonCommand $vpy ($pipBase + @($Spec)) $O.DryRun
    }
    if ($code -ne 0) {
        Write-AxonErr "pip install failed (exit code $code). See the messages above."
        return $null
    }

    if (-not $O.DryRun) { $null = New-Item -ItemType Directory -Force -Path $O.BinDir }
    if ($onWin) {
        $shim = Join-Path $O.BinDir 'axon.cmd'
        $target = Join-Path (Join-Path $O.VenvDir 'Scripts') 'axon.exe'
        $text = "@echo off`r`nREM axon launcher, written by the Axon installer (install.ps1 -Uninstall removes it)`r`n`"$target`" %*`r`n"
    } else {
        $shim = Join-Path $O.BinDir 'axon'
        $target = Join-Path (Join-Path $O.VenvDir 'bin') 'axon'
        $text = "#!/bin/sh`n# axon launcher, written by the Axon installer (install.ps1 -Uninstall removes it)`nexec `"$target`" `"`$@`"`n"
    }
    Write-AxonStep "Writing the launcher $shim"
    if (-not $O.DryRun) {
        [IO.File]::WriteAllText($shim, $text, [Text.Encoding]::ASCII)
        if (-not $onWin) { $null = & chmod 755 $shim }
    }
    return $O.BinDir
}

function Invoke-AxonInstaller {
    $ErrorActionPreference = 'Continue'

    $O = @{
        Method     = $Method
        Ref        = $Ref
        Source     = $Source
        Python     = $PythonExe
        DryRun     = [bool]$DryRun
        NoPath     = [bool]$NoPath -or ($env:AXON_NO_MODIFY_PATH -eq '1')
        NoShim     = [bool]$NoShim
        Uninstall  = [bool]$Uninstall -or ($env:AXON_UNINSTALL -eq '1')
        Standalone = [bool]$Standalone
        VenvDir    = $InstallDir
    }
    if (-not $O.Method) { $O.Method = $env:AXON_INSTALL_METHOD }
    if (-not $O.Ref) { $O.Ref = $env:AXON_INSTALL_REF }
    if (-not $O.Source) { $O.Source = $env:AXON_INSTALL_SOURCE }
    if (-not $O.Python) { $O.Python = $env:AXON_PYTHON }
    if (-not $O.VenvDir) { $O.VenvDir = $env:AXON_INSTALL_DIR }
    if ($O.Ref -or $O.Source -or ($O.Method -and $O.Method -ne 'auto')) { $O.Standalone = $true }
    if (-not $O.Method) { $O.Method = 'auto' }
    if (-not $O.Ref) { $O.Ref = 'main' }
    $homeDir = $env:USERPROFILE
    if (-not $homeDir) { $homeDir = $HOME }
    $axonDir = Join-Path $homeDir '.axon'
    if (-not $O.VenvDir) { $O.VenvDir = Join-Path $axonDir 'venv' }
    $O.VenvDir = [IO.Path]::GetFullPath($O.VenvDir)
    $O.BinDir = Join-Path $axonDir 'bin'
    if ($env:AXON_BIN_DIR) { $O.BinDir = [IO.Path]::GetFullPath($env:AXON_BIN_DIR) }

    if ($Help) { Show-AxonHelp; return 0 }
    if (@('auto', 'uv', 'pipx', 'venv') -notcontains $O.Method) {
        Write-AxonErr "-Method must be auto, uv, pipx or venv (got: $($O.Method))"
        return 2
    }
    if ($O.Uninstall) { return (Invoke-AxonUninstall $O) }

    Write-Host ''
    Write-AxonLine '  Axon installer (Windows)' 'Cyan'
    Write-Host ''

    $root = $AxonInstallerScriptRoot
    if (-not $O.Standalone -and $root -and (Test-Path -LiteralPath (Join-Path (Join-Path $root 'scripts') 'setup_env.py')) -and (Test-Path -LiteralPath (Join-Path $root 'pyproject.toml'))) {
        return (Invoke-AxonCloneSetup $O $root)
    }

    $method = $O.Method
    if ($method -eq 'auto') {
        if (Get-Command uv -ErrorAction SilentlyContinue) { $method = 'uv' }
        elseif (Get-Command pipx -ErrorAction SilentlyContinue) { $method = 'pipx' }
        else { $method = 'venv' }
    }
    if ($method -ne 'venv' -and -not (Get-Command $method -ErrorAction SilentlyContinue)) {
        Write-AxonErr "-Method $method was requested but $method is not on PATH."
        return 1
    }

    $repo = $env:AXON_REPO_URL
    if (-not $repo) { $repo = 'https://github.com/gpr-27/axon.git' }
    $hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
    if ($O.Source) {
        if (Test-Path -LiteralPath $O.Source -PathType Container) { $spec = (Resolve-Path -LiteralPath $O.Source).Path } else { $spec = $O.Source }
    } elseif ($hasGit -or $env:AXON_REPO_URL) {
        $spec = "git+$repo@$($O.Ref)"
    } else {
        # No git: GitHub serves any branch, tag or commit as a source archive.
        $spec = "https://github.com/gpr-27/axon/archive/$($O.Ref).zip"
    }
    if ($spec.StartsWith('git+') -and -not $hasGit) {
        Write-AxonErr "Installing from $spec needs git (https://git-scm.com/download/win)."
        return 1
    }
    Write-AxonOk "Method: $method"
    Write-AxonOk "Source: $spec"

    $launcherDir = $null
    if ($method -eq 'uv') {
        $code = Invoke-AxonCommand 'uv' @('tool', 'install', '--force', '--python', '>=3.10', '--reinstall-package', 'axon-terminal', '--refresh-package', 'axon-terminal', $spec) $O.DryRun
        if ($code -ne 0) { Write-AxonErr "uv tool install failed (exit code $code)."; return $code }
        $launcherDir = Get-AxonToolOutput 'uv' @('tool', 'dir', '--bin')
        if (-not $launcherDir) { $launcherDir = Join-Path (Join-Path $homeDir '.local') 'bin' }
    } elseif ($method -eq 'pipx') {
        $pipxArgs = @('install', '--force')
        $py = Find-AxonPython $O.Python
        if ($py) {
            $exe = Get-AxonToolOutput $py.Exe (@($py.Args) + @('-c', 'import sys; print(sys.executable)'))
            if ($exe) { $pipxArgs += @('--python', $exe) }
        }
        $code = Invoke-AxonCommand 'pipx' ($pipxArgs + @($spec)) $O.DryRun
        if ($code -ne 0) { Write-AxonErr "pipx install failed (exit code $code)."; return $code }
        $launcherDir = Get-AxonToolOutput 'pipx' @('environment', '--value', 'PIPX_BIN_DIR')
        if (-not $launcherDir) { $launcherDir = Join-Path (Join-Path $homeDir '.local') 'bin' }
    } else {
        $launcherDir = Install-AxonWithVenv $O $spec
        if (-not $launcherDir) { return 1 }
    }

    if ($O.DryRun) {
        Write-Host ''
        Write-AxonLine '  Dry run: nothing was changed.' 'Gray'
        return 0
    }

    $launcher = Get-AxonLauncher $launcherDir
    $version = $null
    if ($launcher) { $version = Get-AxonToolOutput $launcher @('--version') }
    if (-not $version) {
        Write-AxonErr "The axon launcher in $launcherDir did not run. See the messages above."
        return 1
    }
    Write-AxonOk "Installed: $version  ($launcher)"
    $null = New-Item -ItemType Directory -Force -Path $axonDir

    $reopen = $false
    if ($O.NoPath) {
        if (-not (Test-AxonOnPath $launcherDir)) { Write-AxonWarn "$launcherDir is not on your PATH; add it to run 'axon' by name." }
    } elseif (Add-AxonUserPath $launcherDir $false) {
        Write-AxonOk "Added $launcherDir to your user PATH"
        $reopen = $true
    }

    Write-Host ''
    Write-AxonLine '  Done. Next steps:' 'Green'
    if ($reopen) { Write-AxonLine '    (other terminal windows need to be reopened to see the new PATH)' 'Gray' }
    Write-AxonLine '    1. axon setup      add an API key and pick a model' 'White'
    Write-AxonLine '    2. axon            start Axon in any project folder' 'White'
    Write-Host ''
    Write-AxonLine '  Upgrade:    run this installer again' 'Gray'
    Write-AxonLine '  Uninstall:  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1))) -Uninstall' 'Gray'
    Write-Host ''
    return 0
}

$AxonInstallExitCode = Invoke-AxonInstaller
if ($AxonInstallExitCode -ne 0 -and $PSCommandPath) {
    # Run as a file: report failure through the process exit code. Under 'irm | iex' we never call
    # exit, because that would close your PowerShell window.
    exit $AxonInstallExitCode
}
