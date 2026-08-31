# Axon (axon-gpr)

<div align="center">

```
  ▲█▲   A X O N
  █⚡█   Terminal-Native Agentic Coding Assistant
```

[![PyPI Version](https://img.shields.io/pypi/v/axon-gpr.svg)](https://pypi.org/project/axon-gpr/)
[![Tests](https://img.shields.io/badge/tests-522%20passed-brightgreen.svg)](tests/)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](pyproject.toml)
[![Architecture](https://img.shields.io/badge/architecture-ReAct%20Loop%20%2B%20Subagents-orange.svg)](docs/01-ARCHITECTURE.md)
[![License](https://img.shields.io/badge/license-MIT-purple.svg)](LICENSE)

**Axon** is a production-grade, terminal-native AI coding assistant built from first principles in pure Python. It autonomously analyzes codebases, plans multi-stage architectures, performs surgical code edits, executes shell workflows, validates test suites, and orchestrates concurrent subagents — complete with exact prompt cache cost accounting, multi-tier reasoning, rollback checkpoints, and zero workspace pollution.

</div>

---

## 📦 Installation

Install Axon directly from PyPI:

```bash
pip install axon-gpr
```

*(To upgrade an existing installation at any time: `pip install --upgrade axon-gpr`)*

---

## 🔑 Environment Configuration (`.env`)

Axon supports **AgentRouter**, **Anthropic**, and **OpenAI-compatible** endpoints. To start using Axon, set your API key using either method below:

### Method 1: Export Directly in Your Shell (Quickest)

Add your key to your terminal or shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
export AXON_API_KEY="your_api_key_here"
```

### Method 2: Global Configuration File (Recommended)

To use Axon seamlessly across any directory without repeating setup, create a global `.env` file at `~/.axon/.env`:

```bash
mkdir -p ~/.axon
cat << 'EOF' > ~/.axon/.env
AXON_API_KEY="your_api_key_here"
AXON_BASE_URL="https://agentrouter.org"
EOF
```

> **Note**: Axon searches for configuration in the following order:  
> `System Environment Variables` → `Current Workspace .env` → `~/.axon/.env` → `~/.axon/config.toml`

### Configuration Parameters Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| **`AXON_API_KEY`** | *Required* | API key for AgentRouter, Anthropic, or OpenAI-compatible provider. |
| **`AXON_BASE_URL`** | `https://agentrouter.org` | API endpoint base URL. |
| **`AXON_MODEL`** | `deepseek-v4-flash` | Active model (`deepseek-v4-flash`, `gpt-5.6-sol`, `glm-5.3`, `claude-opus-5`, `claude-opus-4-8`). |
| **`AXON_EFFORT`** | `quantum` | Reasoning intensity: `reflex` (fast), `balanced` (standard), `synapse` (deep), `quantum` (max). |
| **`AXON_THINKING`** | `true` | Stream live step-by-step assistant reasoning traces. |
| **`AXON_MODE`** | `default` | Permission mode: `default` (ask before edits/bash), `acceptEdits` (auto-write), `plan` (read-only), `bypass` (auto-run all). |
| **`AXON_MAX_TOKENS`** | `64000` | Maximum reasoning + output tokens per turn. |
| **`AXON_SESSION_COST_CEILING`** | `10.00` | Safety budget limit ($USD) per session before halting execution. |

---

## 🚀 Quick Start

Launch Axon anywhere on your system:

```bash
# 1. Start interactive terminal assistant
axon

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
| **`←` (Left Arrow)** | Open interactive **Previous Chats / Session Switcher** |
| **`!`** | Run raw shell commands immediately (e.g. `!pytest`, `!git status`) |
| **`@`** | Fuzzy search and link workspace files into prompt context |
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

Axon is backed by a comprehensive suite of **522 automated unit and integration tests** covering all security jails, permission matrices, session ledgers, tools, and UI rendering:

```bash
uv run pytest
```

```
============================= 522 passed in 4.8s ==============================
```

---

## 📄 License

MIT License. Designed and built for seamless terminal-native AI engineering.
