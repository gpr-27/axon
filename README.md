# Axon (vGPR_27)

<div align="center">

```
  ▲█▲   A X O N
  █⚡█   Terminal-Native Agentic Coding Assistant
```

[![Tests](https://img.shields.io/badge/tests-519%20passed-brightgreen.svg)](tests/)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](pyproject.toml)
[![Architecture](https://img.shields.io/badge/architecture-ReAct%20Loop%20%2B%20Subagents-orange.svg)](docs/01-ARCHITECTURE.md)
[![License](https://img.shields.io/badge/license-MIT-purple.svg)](LICENSE)

**Axon** is a production-grade, terminal-native AI coding assistant built from first principles. It reads entire codebases, architects multi-step plans, performs surgical code edits, executes shell workflows, validates test results, and coordinates concurrent subagents — with full prompt cache cost accounting, rollback checkpoints, and zero external agent frameworks.

</div>

---

## 🚀 Quick Setup & Getting Started

Follow these step-by-step instructions to get Axon running on your machine in under 1 minute.

### Step 1: Clone the Repository
```bash
git clone https://github.com/gpr-27/axon.git
cd axon
```

### Step 2: Install Required Dependencies
You can install dependencies using either the automatic installer script or standard pip:

#### Option A: Automated One-Step Setup (Recommended)
```bash
./install.sh
```

#### Option B: Manual Setup via Virtual Environment
```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install production dependencies
pip install -r requirements.txt

# 3. Install Axon in editable mode to register the global 'axon' command
pip install -e .
```

---

### Step 3: Configure Your API Key in `.env`
Axon comes with a pre-configured `.env.example` with optimal defaults for model selection, token limits, reasoning effort, and tool permissions. **You only need to supply your API key**:

```bash
# Copy example configuration to .env
cp .env.example .env
```

Open `.env` in any text editor and **replace only the API key**:
```ini
# ─── 1. Provider & Authentication ──────────────────────────────────────────
# Replace with your actual key (AgentRouter, Anthropic, or OpenAI-compatible)
AXON_API_KEY="your_api_key_here"

# All other configurations below are pre-configured and ready out of the box!
AXON_BASE_URL="https://agentrouter.org"
AXON_MODEL="deepseek-v4-flash"
AXON_EFFORT="quantum"
AXON_THINKING=true
AXON_MODE="bypass"
```

> **Note**: No other variables need to be changed. Axon will automatically pick up your `.env` file from the current directory, parent folders, or `~/.axon/.env`.

---

### Step 4: Start Axon

Launch the assistant in your terminal with a single command:

```bash
# Open interactive terminal assistant in the current directory:
axon
```

```
  ▲█▲  A X O N   vGPR_27
  █⚡█  Model: deepseek-v4-flash · Effort: quantum · Mode: bypass
  
  > Ask anything, use / for commands, @ for files, ! for shell...
```

#### Additional Ways to Launch:
| Command | What it does |
| :--- | :--- |
| **`axon`** | Opens the interactive full-featured terminal REPL in your workspace. |
| **`axon -p "Your prompt"`** | Runs a one-shot query or task directly from the CLI and exits. |
| **`axon --continue`** | Resumes the most recent conversation session right where you left off. |
| **`axon --model claude-opus-5`** | Starts Axon with a specific LLM model override. |

---

## 🧠 How Axon Works (System Overview)

Axon operates as an autonomous, multi-turn **ReAct (Reasoning + Action)** agent loop with built-in subagent fan-out, persistent session memory, and atomic tool safety invariants:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             USER INPUT / PROMPT                             │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SYSTEM PROMPT BUILDER                             │
│  • Workspace context · File state table · AGENTS.md conventions · Memory   │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          NEURAL REASONING & LLM                             │
│  • Models: DeepSeek-V4, Claude Opus 4.8/5, GPT-5.6 Sol, GLM-5.3            │
│  • Thinking stream (quantum/synapse/balanced/reflex) + Ephemeral Caching    │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           PERMISSION ENGINE                                 │
│  • Checks mode (default / acceptEdits / plan / bypass) & Hard Invariants    │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TOOL EXECUTION LAYER                              │
│  • Read · Write · Edit · MultiEdit · Patch · Diff · CodeSymbols · Bash      │
│  • Subagent Task Launching · TableSearch · DeepResearch · Process · Git     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     BATCH TOOL RESULTS & LEDGER COST                        │
│  • Formats tool results into a single turn message (Law 2 Invariant)        │
│  • Records exact tokens, cache read hits, and USD billing per turn          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 📂 Understanding the `.axon` Directories

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. GLOBAL PROFILE: ~/.axon/  (Across Your Entire Computer)                  │
├─────────────────────────────────────────────────────────────────────────────┤
│ • Path: ~/.axon/                                                            │
│ • Purpose: Universal configuration and credentials applied everywhere.      │
│ • Contents:                                                                 │
│     ├── config.toml    -> Global default model, reasoning tier, token caps   │
│     ├── .env           -> Universal API keys and provider endpoints         │
│     ├── skills/        -> User-wide custom skills & slash commands          │
│     └── memory/        -> Global knowledge & developer preferences          │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. WORKSPACE DIRECTORY: <project-root>/.axon/  (This Specific Repository)  │
├─────────────────────────────────────────────────────────────────────────────┤
│ • Path: ./my-project/.axon/                                                 │
│ • Purpose: Local, project-scoped state isolated to the current repo.        │
│ • Contents:                                                                 │
│     ├── sessions/      -> Append-only JSONL transcripts & subagent charts   │
│     ├── memory/        -> Learned project rules and AGENTS.md conventions   │
│     ├── outputs/       -> Full un-truncated command logs (latest_output.log)│
│     └── research/      -> Cached deep-research trees and scraped tables     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🛠️ 24 Built-In Native Tools

Axon comes pre-equipped with 24 native tools designed for terminal-first development:

| Category | Tools | Description |
| :--- | :--- | :--- |
| **File I/O** | `Read`, `Write`, `Edit`, `MultiEdit`, `Patch`, `Diff` | Surgical edits with `(mtime, sha256)` staleness detection and read-before-edit enforcement. |
| **Navigation** | `Ls`, `FileTree`, `Glob`, `Grep`, `CodeSymbols` | AST-aware code symbol extraction and fast regex workspace searching. |
| **Execution** | `Bash`, `Process`, `Env`, `Git`, `Doctor` | Direct shell execution, process monitoring, proxy diagnostics, and git status analysis. |
| **Research & Web** | `DeepResearch`, `TableSearch`, `WebSearch`, `WebFetch`, `Http` | Multi-round technical research, table querying, and web exploration. |
| **Planning & Tasks** | `Task`, `TodoWrite`, `ExitPlanMode` | Concurrent subagent workers, multi-step checklists, and plan-mode control. |

---

## ⌨️ Useful Commands & Shortcuts

Inside the Axon prompt, you can use these shortcuts:

- **`Tab`**: Cycle permission modes (`manual` → `auto-accept edits` → `plan mode` → `bypass permissions`).
- **`←` (Left Arrow)**: Open the interactive **Previous Chats / Session Switcher** dashboard.
- **`!` (Exclamation)**: Execute direct shell commands (e.g. `!pytest`, `!git status`, `!npm test`).
- **`@` (At symbol)**: Fuzzy search and link project files directly into your prompt context.
- **`/cost`**: View detailed token breakdown, prompt cache hits, subagent costs, and workspace lifetime billing.
- **`/subagents`**: Open the subagent monitor to inspect isolated subagent transcripts and costs.
- **`/main`**: Return to the main chat session from any subagent view.
- **`/model`**: Switch active LLM on the fly (`deepseek-v4-flash`, `claude-opus-5`, `gpt-5.6-sol`, `glm-5.3`).
- **`/effort`**: Adjust reasoning effort level (`reflex`, `balanced`, `synapse`, `quantum`).
- **`/clear`**: Reset the conversation and start a clean session.
- **`?` or `/help`**: Show the categorized interactive commands cheat sheet.

---

## 🧪 Running Unit Tests (519 Tests)

To run the automated test suite covering all tools, invariants, sessions, and edge cases:

```bash
# Run the complete test suite
pytest tests -v
```

```
============================= 519 passed in 4.5s ==============================
```

---

## 📄 License

MIT License. Designed and built for seamless terminal-native AI engineering.
