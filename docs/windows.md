# Axon on Windows

Axon runs natively in **PowerShell 7**, **Windows PowerShell 5.1** and **Command Prompt**, inside
Windows Terminal, the classic console host, or the VS Code terminal. You do not need WSL, although
Axon also works there and in Git Bash.

## Install

1. Install **Python 3.10+** from [python.org](https://www.python.org/downloads/windows/) and tick
   **"Add python.exe to PATH"**. The `python` command that opens the Microsoft Store is only a stub,
   and the installers detect and skip it.
2. Clone the repository and run one of the installers from its folder:

   ```powershell
   # PowerShell (5.1 or 7)
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
   ```

   ```bat
   :: Command Prompt, or double-click the file in Explorer
   install.bat
   ```

3. Open a **new** terminal window so it picks up the updated PATH, then run `axon setup`.

The installer creates `.venv\` in the repository, installs Axon into it in editable mode, writes the
launcher `%USERPROFILE%\.axon\bin\axon.cmd`, and adds that folder to your **user** PATH (no admin
rights needed). Pass `-NoPath` or `-NoShim` to `install.ps1`, or `--no-path` or `--no-shim` to
`install.bat`, to skip those steps.

**Why `-ExecutionPolicy Bypass`?** Windows blocks unsigned `.ps1` scripts by default, with the error
"running scripts is disabled on this system". The flag allows this one script for this one process
and changes no machine setting. If you prefer not to use it, run `install.bat`, which does the same
thing without PowerShell.

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
| `axon` is not recognised | Open a new terminal after installing. Check that `%USERPROFILE%\.axon\bin` is on your user PATH, or run `.venv\Scripts\python.exe -m axon`. |
| "Python 3.10 or newer was not found" | Install Python from python.org with "Add to PATH" ticked, or disable the Store aliases in *Settings → Apps → Advanced app settings → App execution aliases*. |
| "running scripts is disabled on this system" | Use `-ExecutionPolicy Bypass` as shown above, or run `install.bat`. |
| Garbled box characters | Use Windows Terminal, or set a TrueType font such as Cascadia Mono in the console. Axon's ASCII fallback covers the rest. |
| Commands use the wrong syntax | Set `AXON_SHELL` to the shell you want and start a new session. |
| Hook scripts fail with "python not found" | Hook commands run through the shell. Use `py` or the full interpreter path in the hook's `command`. |
| A config file shows odd characters | Axon reads UTF-8, UTF-8 with BOM and UTF-16 config files, so files saved by Notepad or `Out-File` all work. A malformed file is reported with its path, and `axon doctor` lists every config file it found. |

Run `axon doctor` for a full report covering the installation, keys, shell and terminal
capabilities.
