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
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](pyproject.toml)

[![Architecture](https://img.shields.io/badge/architecture-ReAct%20Loop%20%2B%20Subagents-orange.svg)](docs/01-ARCHITECTURE.md)
[![License](https://img.shields.io/badge/license-MIT-purple.svg)](LICENSE)

**Axon** is a production-grade, terminal-native AI coding assistant built from first principles in pure Python. It autonomously analyzes codebases, plans multi-stage architectures, performs surgical code edits, executes shell workflows, validates test suites, and orchestrates concurrent subagents — complete with exact prompt cache cost accounting, multi-tier reasoning, rollback checkpoints, and zero workspace pollution.

</div>

---

## 📦 Installation & Setup (Cross-Platform)

Axon runs seamlessly on **Windows (Command Prompt, PowerShell, Git Bash)**, **macOS**, and **Linux**.

### Option A: 1-Click Automated Setup (Recommended)

Choose your platform for an automated, zero-interruption setup that automatically configures your virtual environment, installs dependencies, and prepares configuration:

<table>
<tr>
<th>Platform</th>
<th>1-Click Setup Command</th>
<th>Description</th>
</tr>
<tr>
<td><b>Windows (CMD / Double-click)</b></td>
<td>

```cmd
install.bat
```
</td>
<td>Automatically verifies Python, creates <code>.venv</code>, installs requirements, and sets up <code>~/.axon</code>.</td>
</tr>
<tr>
<td><b>Windows (PowerShell)</b></td>
<td>

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```
</td>
<td>Native PowerShell script that sets up the environment without execution policy restrictions.</td>
</tr>
<tr>
<td><b>macOS / Linux / WSL</b></td>
<td>

```bash
chmod +x install.sh && ./install.sh
```
</td>
<td>Automated shell setup for Unix terminals, Zsh, and Git Bash.</td>
</tr>
<tr>
<td><b>Universal (Any OS)</b></td>
<td>

```bash
python setup_env.py
```
</td>
<td>Pure Python setup script that works identically across all operating systems.</td>
</tr>
</table>

> [!TIP]
> **Zero-Interruption Launcher**: You can also directly run `python axon_run.py` on any OS. It will automatically detect if dependencies are missing, install them on the fly, and boot Axon immediately!

---

### Option B: Manual Setup via Pip

If you prefer setting up manually or installing directly from PyPI:

#### 1. On Windows (PowerShell or Command Prompt):
```powershell
# 1. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\activate

# 2. Install dependencies & Axon package
pip install -r requirements.txt -e .
```

#### 2. On macOS / Linux:
```bash
# 1. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies & Axon package
pip install -r requirements.txt -e .
```

*(Or install directly from PyPI: `pip install axon-gpr`)*

---

## 🔑 Environment Configuration

Axon requires only a single environment variable (`AXON_API_KEY`) to authenticate. All other settings (default model, base URL, effort tier, and budgets) work automatically out of the box.

### Method 1: Permanent Global Setup (Recommended)

Save your key to the global Axon directory (`~/.axon/.env` on macOS/Linux or `%USERPROFILE%\.axon\.env` on Windows). This is set once and works permanently across all terminal windows, IDEs, and project folders.

**On Windows (PowerShell):**
```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.axon"
Set-Content -Path "$HOME\.axon\.env" -Value 'AXON_API_KEY="your_api_key_here"'
```

**On Windows (Command Prompt):**
```cmd
if not exist "%USERPROFILE%\.axon" mkdir "%USERPROFILE%\.axon"
echo AXON_API_KEY="your_api_key_here" > "%USERPROFILE%\.axon\.env"
```

**On macOS & Linux:**
```bash
mkdir -p ~/.axon
echo 'AXON_API_KEY="your_api_key_here"' > ~/.axon/.env
```

---

### Method 2: Project `.env` File

Copy `.env.example` to `.env` in your project root and add your API key:
```bash
# macOS / Linux / Git Bash
cp .env.example .env

# Windows Command Prompt
copy .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

---

### Method 3: Temporary Session Export (Quick Test)

Export your key for the active terminal session:

* **Windows PowerShell**: `$env:AXON_API_KEY="your_api_key_here"`
* **Windows CMD**: `set AXON_API_KEY="your_api_key_here"`
* **macOS / Linux**: `export AXON_API_KEY="your_api_key_here"`

---

## 🚀 Quick Start

Launch Axon anywhere in your workspace:

```bash
# 1. Start interactive terminal assistant
axon
# (or: python axon_run.py)

# 2. Run a one-shot query or direct instruction
axon -p "Analyze this codebase and write unit tests for edge cases"

# 3. Resume your latest conversation
axon --continue

# 4. Launch with a specific model override
axon --model claude-opus-5
```


---

## 🧠 What is Axon? (Complete Project Overview)

Axon is engineered to provide an autonomous, developer-first coding agent inside your terminal without third-party agent framework bloat:

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

1. **⚡ Native ReAct Loop with Thinking Traces**:
   Built from first principles with zero dependencies on LangChain or CrewAI. Streams real-time reasoning tokens, self-corrects on tool failures, and manages structured multi-turn context.

2. **🔒 6-Law Security & Permission Matrix**:
   Enforces strict safety boundaries. Tools are classified into read-only, workspace mutation, external shell, and privileged execution. Instantly toggle between `default`, `acceptEdits`, `plan`, and `bypass` modes with `Tab`.

3. **⏪ Atomic File Checkpoints & Undo (`/rewind`)**:
   Every file edit takes an in-memory SHA256 snapshot before touching disk. If a patch fails or tests break, rollback your workspace instantly.

4. **👥 Concurrent Subagents (`Task` Tool)**:
   Axon can spawn isolated subagent workers to research documentation, run background tasks, or explore repositories concurrently without polluting the main conversation context.

5. **💰 Prompt Cache & Exact Token Cost Ledger**:
   Full visibility into cache read/write tokens and real-time dollar costs per session, logged append-only into `~/.axon/sessions/`.

---

## 📂 Zero-Pollution Global Storage (`~/.axon/`)

To keep your repositories and workspaces 100% clean, Axon isolates all state and history in your home directory:

```
~/.axon/
├── config.toml       # Global defaults (default model, effort tier, permissions)
├── .env              # Global API credentials
├── sessions/         # Append-only JSONL transcripts, cost ledgers, and switcher data
├── memory/           # Universal long-term learned conventions (from /learn --global)
├── skills/           # Custom reusable workflows (from /skill create or /skill install)
├── research/         # Full deep-research markdown briefs
├── images/           # Multimodal image ingestion cache & vision attachments
└── bin/              # Precompiled native helpers (e.g., instant macOS clipboard paste)
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
python3 check_models.py
```

```
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
uv run pytest
```

```
============================= 528 passed in 5.1s ==============================
```


---

## 📄 License

MIT License. Designed and built for seamless terminal-native AI engineering.
