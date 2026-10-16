# Axon on Windows

Axon runs natively in **PowerShell 7**, **Windows PowerShell 5.1** and **Command Prompt**, inside
Windows Terminal, the classic console host, or the VS Code terminal. You do not need WSL, although
Axon also works there and in Git Bash.

## Install

One line in PowerShell (5.1 or 7). No clone and no execution-policy change is needed:

```powershell
irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1 | iex
```

From Command Prompt:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1 | iex"
```

The installer uses `uv tool install` when [uv](https://docs.astral.sh/uv/) is on PATH, then
`pipx install` when pipx is, and otherwise creates a private virtual environment in
`%USERPROFILE%\.axon\venv` with the launcher `%USERPROFILE%\.axon\bin\axon.cmd`. It adds the
launcher folder to your **user** PATH (no admin rights needed) and to the current window, so
`axon setup` works straight away. Without uv, it needs **Python 3.10+** from
[python.org](https://www.python.org/downloads/windows/) with **"Add python.exe to PATH"** ticked. The
`python` command that opens the Microsoft Store is only a stub, and the installer skips it.

Options are environment variables set before the one-liner, or parameters passed through a script
block:

```powershell
$env:AXON_INSTALL_REF = "v0.28.0"            # a branch, tag or commit instead of main
$env:AXON_INSTALL_METHOD = "venv"            # auto (default), uv, pipx or venv
irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1 | iex

# Upgrade: run the one-liner again.
# Uninstall (settings and sessions in %USERPROFILE%\.axon are kept):
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/gpr-27/axon/main/install.ps1))) -Uninstall
```

### From a clone (for development)

```powershell
# PowerShell (5.1 or 7)
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

```bat
:: Command Prompt, or double-click the file in Explorer
install.bat
```

Then open a **new** terminal window and run `axon setup`. In a clone the installer creates `.venv\`
in the repository, installs Axon into it in editable mode, writes the launcher
`%USERPROFILE%\.axon\bin\axon.cmd`, and adds that folder to your user PATH. Pass `-NoPath` or
`-NoShim` to `install.ps1`, or `--no-path` or `--no-shim` to `install.bat`, to skip those steps.
`-Standalone` installs from GitHub instead, as the one-liner does.

**Why `-ExecutionPolicy Bypass`?** Windows blocks unsigned `.ps1` files by default, with the error
"running scripts is disabled on this system". The flag allows this one script for this one process
and changes no machine setting. `irm ... | iex` is not affected by the policy. If you prefer not to
use the flag, run `install.bat`, which does the same thing.

## API keys

`axon setup` stores keys in `%USERPROFILE%\.axon\.env`. To use environment variables instead:

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."     # persistent; open a new terminal afterwards
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # current PowerShell session only
```

```bat
set ANTHROPIC_API_KEY=sk-ant-...
```

## Which shell runs commands

The `Bash` tool runs commands in the shell Axon detects. On Windows the order is: `pwsh`
(PowerShell 7), then `powershell` (Windows PowerShell 5.1), then `cmd`. The tool description tells
the model which shell is active, so it writes PowerShell or cmd syntax accordingly. To override the
choice:

```powershell
$env:AXON_SHELL = "bash"          # Git for Windows' bash.exe (never the WSL launcher)
$env:AXON_SHELL = "cmd"
$env:AXON_SHELL = "C:\Program Files\PowerShell\7\pwsh.exe"
```

`/env` shows the active shell, and `/doctor` checks the whole setup. Timeouts kill the entire
process tree, so a hung `npm` or `python` child process does not outlive the tool call.

## Terminal behaviour

- **Colours and Unicode.** Axon enables VT processing and UTF-8 output at startup. Windows Terminal
  gets 24-bit colour. On legacy consoles that cannot encode a glyph, Axon falls back to ASCII
  symbols. Set `NO_COLOR=1` to turn colour off, or use `/theme mono`.
- **Exit.** Press `Ctrl+D` on an empty prompt, type `/exit`, or press `Ctrl+Z` then Enter (the
  console EOF convention).
- **Newlines in the prompt.** `Alt+Enter` and `Ctrl+J` always insert a newline. To make
  `Shift+Enter` work in Windows Terminal or VS Code, run `/terminal-setup windows-terminal` or
  `/terminal-setup vscode`.
- **Clipboard.** `Ctrl+V` pastes text or an image. `/copy` copies an answer.
- **Paths.** Both `C:\work\app` and `C:/work/app` work, and paths are compared case-insensitively.
  Project state goes under `%USERPROFILE%\.axon\projects\` with a sanitised key.
- **Managed policy.** IT-managed instructions can be placed in `C:\Program Files\Axon\AXON.md`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `axon` is not recognised | Open a new terminal after installing. Check that `%USERPROFILE%\.axon\bin` (venv method) or `%USERPROFILE%\.local\bin` (uv, pipx) is on your user PATH. |
| `irm` fails with a TLS or SSL error | Old Windows PowerShell 5.1 builds default to TLS 1.0. Run `[Net.ServicePointManager]::SecurityProtocol = 'Tls12'` first. |
| "Python 3.10 or newer was not found" | Install Python from python.org with "Add to PATH" ticked, or disable the Store aliases in *Settings → Apps → Advanced app settings → App execution aliases*. |
| "running scripts is disabled on this system" | Use `-ExecutionPolicy Bypass` as shown above, or run `install.bat`. |
| Garbled box characters | Use Windows Terminal, or set a TrueType font such as Cascadia Mono in the console. Axon's ASCII fallback covers the rest. |
| Commands use the wrong syntax | Set `AXON_SHELL` to the shell you want and start a new session. |
| Hook scripts fail with "python not found" | Hook commands run through the shell. Use `py` or the full interpreter path in the hook's `command`. |
| A config file shows odd characters | Axon reads UTF-8, UTF-8 with BOM and UTF-16 config files, so files saved by Notepad or `Out-File` all work. A malformed file is reported with its path, and `axon doctor` lists every config file it found. |

Run `axon doctor` for a full report covering the installation, keys, shell and terminal
capabilities.
