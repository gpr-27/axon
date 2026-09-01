# Axon (axon-gpr)

<div align="center">

```
   ___   _  __ ___   _  __
  / _ | | |/ // _ \ / |/ /
 / __ |  / // // //    / 
/_/ |_| /_/  \___//_/|_/  
Terminal-Native Agentic Coding Assistant
```

[![PyPI Version](https://img.shields.io/pypi/v/axon-gpr.svg)](https://pypi.org/project/axon-gpr/)
[![Tests](https://img.shields.io/badge/tests-528%20passed-brightgreen.svg)](tests/)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](pyproject.toml)
[![Architecture](https://img.shields.io/badge/architecture-ReAct%20Loop%20%2B%20Subagents-orange.svg)](docs/01-ARCHITECTURE.md)
[![License](https://img.shields.io/badge/license-MIT-purple.svg)](LICENSE)

**Axon** is a production-grade, terminal-native AI coding assistant built from first principles in pure Python. It autonomously analyzes codebases, plans multi-stage architectures, performs surgical code edits, executes shell workflows, validates test suites, and orchestrates concurrent subagents — complete with exact prompt cache cost accounting, multi-tier reasoning, rollback checkpoints, and zero workspace pollution.

</div>

---

## ⚡ Quick Start & Installation

Axon requires **Python >= 3.10**. Follow the 3-step setup below:

### 1️⃣ Try the Standard Install Command First

If you already have Python and `pip` installed, run the standard installation command directly:

```bash
# macOS / Linux / Configured Windows
pip install axon-gpr

# Windows Zero-Configuration Alternative (Works immediately via built-in py launcher):
py -m pip install axon-gpr
```

Once installed, launch Axon:
```bash
axon
# (Or on Windows if PATH is not configured: py -m axon)
```

---

### 2️⃣ If Python or Pip is Missing (1-Command Full Automated Setup)

If `pip` or `python` is missing entirely on your computer, use the 1-liner for your operating system:

* **🪟 Windows (PowerShell - 1-Click All-in-One)**:
  Run this single command in PowerShell. It automatically downloads Python, accepts agreements silently, bootstraps & upgrades Pip, refreshes the active session, and configures the system `PATH`:
  ```powershell
  winget install --id Python.Python.3.12 --exact --source winget --accept-source-agreements --accept-package-agreements; $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User'); python -m ensurepip --upgrade; python -m pip install --upgrade pip; $scripts = python -c "import sysconfig; print(sysconfig.get_path('scripts'))"; $userPath = [Environment]::GetEnvironmentVariable('Path','User'); if (($userPath -split ';') -notcontains $scripts) { [Environment]::SetEnvironmentVariable('Path', "$userPath;$scripts", 'User') }
  ```

* **🍎 macOS (Terminal - 1-Liner via Homebrew)**:
  ```bash
  brew install python && python3 -m pip install --upgrade pip
  ```
  *(Or download the official installer directly from [python.org/downloads](https://www.python.org/downloads/)).*

* **🐧 Linux (Ubuntu / Debian - 1-Liner)**:
  ```bash
  sudo apt update && sudo apt install -y python3 python3-pip python3-venv && python3 -m pip install --upgrade pip
  ```

---

### 3️⃣ If `pip` Still Says "Not Recognized" on Windows (Quick PATH Fix)

If Python is already installed but typing `pip` or `axon` returns `'not recognized'`:

* **Option A: 1-Line Automated PATH Fix (PowerShell)**:
  ```powershell
  $pyScripts = (python -c "import sysconfig; print(sysconfig.get_path('scripts'))"); [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', 'User') + ";$pyScripts", 'User'); Write-Host "✓ Added $pyScripts to PATH. Restart terminal to apply." -ForegroundColor Green
  ```

* **Option B: Manual GUI Setup**:
  1. Find your Scripts directory:
     ```cmd
     py -m pip -V
     ```
  2. Press <kbd>Win</kbd> + <kbd>R</kbd>, type **`sysdm.cpl`**, and press **Enter**.
  3. Go to **Advanced** $\rightarrow$ **Environment Variables...** $\rightarrow$ Under **User variables**, select **`Path`** $\rightarrow$ **Edit...** $\rightarrow$ **New**.
  4. Paste the path to your `...\Scripts` folder and click **OK**.
  5. Restart your terminal/IDE.

* **Option C: 1-Click Repo Auto-Setup**:
  Clone the repo and double-click `install.bat` (or run `.\install.ps1`).

---

### 🐧 Linux (Ubuntu / Debian / Fedora / Arch)

* **Ubuntu / Debian**:
  ```bash
  sudo apt update && sudo apt install -y python3 python3-pip python3-venv
  ```
* **Fedora / RHEL**:
  ```bash
  sudo dnf install -y python3 python3-pip python3-virtualenv
  ```
* **Arch Linux**:
  ```bash
  sudo pacman -S python python-pip python-virtualenv
  ```
* **Universal (Without Root)**:
  ```bash
  curl -sSL https://bootstrap.pypa.io/get-pip.py | python3
  ```

---

## 💻 Axon Installation Methods

### Method 1: Using `pipx` (Recommended for isolated CLI tools)
`pipx` installs Axon into its own isolated environment so it never conflicts with system packages:
```bash
# Install pipx (if not already installed)
# Windows: winget install pipx  OR  python -m pip install --user pipx
# macOS:   brew install pipx    OR  python3 -m pip install --user pipx
# Linux:   sudo apt install pipx

pipx install axon-gpr
axon
```

### Method 2: Standard Pip Install
* **Windows**:
  ```cmd
  pip install --upgrade axon-gpr
  axon
  ```
  *(If `pip` is not in PATH, use: `python -m pip install --upgrade axon-gpr` and launch with `python -m axon`)*

* **macOS & Linux**:
  ```bash
  pip3 install --user --upgrade axon-gpr
  axon
  ```
  *(If `axon` is not in PATH, launch with: `python3 -m axon`)*

### Method 3: 1-Click Bootstrap from Source (Cloned Repository)
If you have cloned or downloaded the Axon repository, run the self-contained bootstrapper:
* **Windows**: Double-click `install.bat` or run:
  ```powershell
  python setup_env.py
  ```
* **macOS & Linux**: Run:
  ```bash
  chmod +x install.sh && ./install.sh
  ```
  *(Or directly: `python3 setup_env.py`)*

---

## 🛠️ Troubleshooting Common Setup Errors

| Error | Why It Happens | How to Fix |
|---|---|---|
| **`pip: command not found`** | `pip` is not linked in your `$PATH`. | Run `python3 -m pip install axon-gpr` (macOS/Linux) or `python -m pip install axon-gpr` (Windows). |
| **`'pip' is not recognized as the name of a cmdlet`** (Windows) | Python's Scripts folder was not added to your Windows environment PATH. | Run `python -m ensurepip --default-pip` or `py -m pip install axon-gpr`. |
| **`error: externally-managed-environment`** (macOS/Linux) | PEP 668 prevents modifying system Python directly. | Use `pipx install axon-gpr` or run `pip3 install --user axon-gpr --break-system-packages`. |
| **`axon: command not found` after install** | Python's global script directory is not in your shell `$PATH`. | Run `python3 -m axon` (or on Windows `python -m axon`), or run `pipx ensurepath`. |

> [!TIP]
> **First-Time Setup**: When you launch `axon` for the first time, it will automatically prompt you for your `AXON_API_KEY`, test it live against the API, and save it permanently to `~/.axon/.env` (or `%USERPROFILE%\.axon\.env` on Windows). You only have to enter your key once!

---

## 🛠️ Troubleshooting Common Setup Errors

| Error | Why It Happens | How to Fix |
|---|---|---|
| **`pip: command not found`** | The `pip` alias is not directly linked to your Python executable in your terminal PATH. | Run `python3 -m pip install axon-gpr` (macOS/Linux) or `python -m pip install axon-gpr` (Windows). |
| **`'pip' is not recognized as the name of a cmdlet`** (Windows) | Python's Scripts directory was not added to your Windows environment PATH during installation. | Run `py -m pip install axon-gpr` or reinstall Python with the **"Add Python to PATH"** checkbox selected. |
| **`error: externally-managed-environment`** (macOS/Linux) | Modern Linux & macOS (PEP 668) prevent modifying system Python directly. | Use `pipx install axon-gpr` or add the `--break-system-packages` flag: `python3 -m pip install --user axon-gpr --break-system-packages`. |
| **`axon: command not found` after install** | Python's global bin/scripts folder is not in your shell `$PATH`. | Run `python3 -m axon` (or on Windows `python -m axon`), or run `pipx ensurepath`. |

---

## 🔑 Environment & API Key Configuration

Axon requires only a single environment variable (`AXON_API_KEY`) to authenticate. All other settings (default model, base URL, effort tier, and token budgets) work automatically out of the box.

### Option 1: Interactive First-Run (Easiest)
Simply run `axon` in your terminal. If no key is found, Axon will prompt you to enter it and will save it permanently in your user profile.

### Option 2: Permanent Global Config
Save your key directly into the global Axon configuration directory:

* **Windows (PowerShell)**:
  ```powershell
  New-Item -ItemType Directory -Force -Path "$HOME\.axon"
  Set-Content -Path "$HOME\.axon\.env" -Value 'AXON_API_KEY="your_api_key_here"'
  ```
* **Windows (Command Prompt)**:
  ```cmd
  if not exist "%USERPROFILE%\.axon" mkdir "%USERPROFILE%\.axon"
  echo AXON_API_KEY="your_api_key_here" > "%USERPROFILE%\.axon\.env"
  ```
* **macOS & Linux**:
  ```bash
  mkdir -p ~/.axon
  echo 'AXON_API_KEY="your_api_key_here"' > ~/.axon/.env
  ```

### Option 3: Local Project `.env`
Copy `.env.example` to `.env` in the root of any repository:
```bash
cp .env.example .env    # On Windows CMD: copy .env.example .env
```

---

## 🚀 How to Use Axon

Once installed, navigate to any codebase or repository on your computer and launch Axon:

```bash
# 1. Start interactive coding session
axon

# 2. Run a one-shot instruction or query
axon -p "Review this repository and write unit tests for edge cases"

# 3. Resume your latest conversation
axon --continue

# 4. Launch with a specific model override
axon --model claude-opus-5
```

---

## 🧠 What is Axon? (Complete Architectural Overview)

Axon is engineered from first principles in pure Python to provide a full-featured, developer-first coding agent inside your terminal without third-party framework bloat:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              USER PROMPT                                │
└────────────────────────────────────┬────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        AXON REACT ENGINE LOOP                           │
│                                                                         │
│   ┌───────────────────┐    Prompt + History    ┌────────────────────┐   │
│   │                   │ ─────────────────────> │                    │   │
│   │   LLM REASONING   │                        │   6-LAW SECURITY   │   │
│   │  & THINKING TRACE │ <───────────────────── │  PERMISSION MATRIX │   │
│   │                   │      Tool Decisions    │                    │   │
│   └─────────┬─────────┘                        └─────────┬──────────┘   │
│             │                                            │              │
│             │ Executes Tool Call                         │              │
│             ▼                                            ▼              │
│   ┌─────────────────────────────────────────────────────────────────┐   │
│   │               24 NATIVE RUNTIME AGENT TOOLS                     │   │
│   │   File I/O  ·  Ripgrep/AST  ·  Shell  ·  Subagents  ·  Research  │   │
│   └─────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────┬────────────────────────────────────┘
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│             GLOBAL ~/.axon/ LEDGER & AUTO CHECKPOINT ROLLBACK           │
└─────────────────────────────────────────────────────────────────────────┘
```

### Key Architectural Pillars

1. **⚡ Native ReAct Loop with Real-Time Thinking Traces**:
   Built with zero dependencies on LangChain or CrewAI. Streams reasoning tokens live, self-corrects on tool execution errors, and manages structured multi-turn conversation context.

2. **🔒 6-Law Security & Permission Matrix**:
   Enforces strict boundaries. Tools are partitioned into read-only, workspace mutation, external shell, and privileged execution tiers. Instantly toggle between `default` (ask), `acceptEdits`, `plan` (read-only), and `bypass` modes with `Tab`.

3. **⏪ Atomic File Checkpoints & Undo (`/rewind`)**:
   Every file edit takes an in-memory SHA256 snapshot before touching disk. If a patch fails or tests break, roll back your workspace modifications instantly.

4. **👥 Concurrent Subagents (`Task` Tool & Subagent Monitor)**:
   Axon can spawn isolated subagent workers to research documentation, run background tasks, or explore repositories concurrently without polluting the main conversation context.

5. **💰 Prompt Cache & Exact Token Cost Ledger**:
   Full visibility into cache read/write tokens and real-time dollar costs per session, logged append-only into `~/.axon/sessions/`.

---

## 📂 Zero-Pollution Global Storage (`~/.axon/`)

To keep your project workspaces 100% clean, Axon isolates all state and history in your user home directory:

```
~/.axon/ (or %USERPROFILE%\.axon\ on Windows)
├── config.toml       # Global defaults (default model, effort tier, permissions)
├── .env              # Global API credentials
├── sessions/         # Append-only JSONL transcripts, cost ledgers, and switcher data
├── memory/           # Universal long-term learned conventions (from /learn --global)
├── skills/           # Custom reusable workflows (from /skill create or /skill install)
├── research/         # Full deep-research markdown briefs
├── images/           # Multimodal image ingestion cache & vision attachments
└── bin/              # Precompiled native helpers
```

---

## 🛠️ 24 Built-In Native Tools

| Category | Tools | Purpose |
| :--- | :--- | :--- |
| **File I/O** | `Read`, `Write`, `Edit`, `MultiEdit`, `Patch`, `Diff` | Surgical source code edits with `(mtime, sha256)` staleness detection and read-before-write safety. |
| **Navigation** | `Ls`, `FileTree`, `Glob`, `Grep`, `CodeSymbols` | AST-aware code symbol extraction and high-speed ripgrep search. |
| **Execution** | `Bash`, `Process`, `Env`, `Git`, `Doctor` | Controlled shell execution, background task monitoring, git state inspection, and system health checks. |
| **Research & Web** | `DeepResearch`, `TableSearch`, `WebSearch`, `WebFetch`, `Http` | Multi-step deep technical research, web search, URL fetching, and API interaction. |
| **Planning & Tasks**| `Task`, `TodoWrite`, `ExitPlanMode` | Spawning specialized subagent workers, maintaining interactive task checklists, and plan approval. |

---

## ⌨️ Shortcuts & Slash Commands

| Key / Command | Action |
| :--- | :--- |
| **`Tab`** | Cycle permission modes: `default` ➔ `acceptEdits` ➔ `plan` ➔ `bypass` |
| **`←` (Left Arrow)** | Open interactive **Previous Chats / Session Switcher** dashboard |
| **`!`** | Run direct shell commands immediately (e.g. `!pytest`, `!git status`) |
| **`@` (At Symbol)** | Fuzzy search and insert workspace files into prompt context |
| **`/cost`** | Display token usage, prompt cache breakdown, and session dollar cost |
| **`/model`** | Switch active model on the fly (`deepseek-v4-flash`, `claude-opus-5`, etc.) |
| **`/effort`** | Adjust reasoning tier (`reflex`, `balanced`, `synapse`, `quantum`) |
| **`/learn`** | Save long-term facts, conventions, or debugging tips into memory |
| **`/subagents`** | View live subagent status, spawned tasks, and token usage |
| **`/rewind`** | Roll back file edits made during previous turns |
| **`/diff`** | View uncommitted git diff in the current workspace |
| **`/clear`** | Clear conversation context and start fresh |
| **`?` / `/help`** | Open interactive commands cheat sheet |

---

## 🩺 Multi-Model Diagnostic Suite (`check_models.py`)

Test and benchmark live connectivity and latency across all supported model endpoints:

```bash
python check_models.py
```

```text
⚡ Testing connectivity for 5 models (2 rounds · Base: https://agentrouter.org)...

--- [Round #1 of 2] 16:53:01 ---
deepseek-v4-flash    | ● WORKING |   1279 ms | OK
gpt-5.6-sol          | ● WORKING |   5251 ms | OK. I'm ChatGPT.
glm-5.3              | ● WORKING |   1803 ms | OK
claude-opus-5        | ● WORKING |   2026 ms | OK
claude-opus-4-8      | ● WORKING |   1772 ms | OK. I'm Claude, made by Anthropic.

✓ Model verification complete (2 rounds finished).
```

---

## 🧪 Test Suite

Axon is backed by a comprehensive suite of **528 automated unit and integration tests** covering all security jails, permission matrices, session ledgers, cross-platform tools, and UI rendering:

```bash
pytest
```

```text
============================= 528 passed in 5.1s ==============================
```

---

## 📄 License

MIT License. Designed and built for seamless terminal-native AI engineering.
