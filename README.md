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

**Axon** is a production-grade, terminal-native AI coding assistant built from first principles. It reads entire codebases, architects multi-step plans, performs surgical code edits, executes shell workflows, validates test results, and coordinates concurrent subagents — with full prompt cache cost accounting, rollback checkpoints, zero project pollution, and zero external agent frameworks.

</div>

---

## 🚀 Quick Setup & Getting Started

### For New Users

#### Option 1: Install via PyPI (Recommended)
Install the published package on any computer:
```bash
pip install axon-gpr
```
*(Or with `uv`: `uv tool install axon-gpr`)*

#### Option 2: Install from Source (Developer Mode)
If you want to contribute or modify Axon's source code:
```bash
# 1. Clone the repository
git clone https://github.com/gpr-27/axon.git
cd axon

# 2. Install in editable mode
pip install -e .
```

---

### Step 2: Configure Your API Key

Axon supports **AgentRouter**, **Anthropic**, and **OpenAI-compatible** providers.

Copy `.env.example` to `.env` in your project or create `~/.axon/.env` for global access:
```bash
cp .env.example .env
```

Open `.env` and set your API key:
```ini
AXON_API_KEY="your_api_key_here"
AXON_BASE_URL="https://agentrouter.org"
AXON_MODEL="deepseek-v4-flash"
AXON_EFFORT="quantum"
AXON_THINKING=true
AXON_MODE="default"
```

*(You can also export `export AXON_API_KEY="sk-..."` directly in your shell).*

---

### Step 3: Start Axon

Launch the assistant in any directory:
```bash
# 1. Interactive terminal assistant
axon

# 2. One-shot query / command
axon -p "Review this repository and summarize architecture"

# 3. Resume your latest conversation
axon --continue

# 4. Use a specific model override
axon --model claude-opus-5
```

---

## 🔄 For Existing Users: How to Update

To update your existing Axon installation to the newest release:

### If installed via PyPI / Pip:
```bash
pip install --upgrade axon-gpr
```

### If installed via `uv`:
```bash
uv tool upgrade axon-gpr
```

### If installed from Source Git Repo:
```bash
git pull origin main
pip install -e .
```

---

## 🛠️ For Developers: Making Changes & Publishing Updates

If you edit Axon and want to publish new versions to PyPI or deploy to other computers:

### 1. Develop & Test Locally
Because Axon is installed in editable mode (`pip install -e .`), any edits in `src/axon/` are live immediately on your computer.

Always verify that tests pass before releasing:
```bash
uv run pytest
```

### 2. Bump the Version
When you're ready to publish, open [`pyproject.toml`](file:///Users/gpr/Documents/axon/pyproject.toml) and increment the version:
```toml
[project]
name = "axon-gpr"
version = "0.27.3"   # <-- Increment patch, minor, or major version
```

### 3. Build the Distribution
Clean previous builds and package the new wheels:
```bash
rm -rf dist/*
uv build
```

### 4. Upload to PyPI
Upload the new release:
```bash
twine upload dist/*
```

### 5. Commit and Push to Git
```bash
git add .
git commit -m "Release v0.27.3: Summary of changes"
git push origin main
```

---

## 📂 Global Storage Architecture (`~/.axon/`)

Axon saves all persistent states in your home directory (**`~/.axon/`**) rather than polluting your individual project workspaces:

```
~/.axon/
├── config.toml       # Global configuration (default model, effort, permissions)
├── sessions/         # Append-only JSONL chat transcripts and token cost ledgers
├── memory/           # Universal learned conventions & facts (from /learn --global)
├── skills/           # Custom & community skills (from /skill install)
├── research/         # Full markdown reports from DeepResearch
├── images/           # Image cache and screenshot attachments
└── bin/              # CLI executable binaries and launchers
```

* **Zero Workspace Clutter**: Your project folders stay 100% clean with zero unintended dotfiles.
* **Universal Resumption**: You can switch between and resume past sessions from any workspace with `/resume` or `axon --continue`.

---

## 🩺 Model Connectivity Check (`check_models.py`)

Verify connectivity and response latency across all 5 supported models (`deepseek-v4-flash`, `gpt-5.6-sol`, `glm-5.3`, `claude-opus-5`, `claude-opus-4-8`):

```bash
python3 check_models.py
```

**Output:**
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

## 🛠️ 24 Built-In Native Tools

| Category | Tools | Description |
| :--- | :--- | :--- |
| **File I/O** | `Read`, `Write`, `Edit`, `MultiEdit`, `Patch`, `Diff` | Surgical edits with `(mtime, sha256)` staleness detection and read-before-edit enforcement. |
| **Navigation** | `Ls`, `FileTree`, `Glob`, `Grep`, `CodeSymbols` | AST-aware code symbol extraction and fast workspace search with ripgrep. |
| **Execution** | `Bash`, `Process`, `Env`, `Git`, `Doctor` | Direct shell execution, process monitoring, diagnostics, and git status analysis. |
| **Research & Web** | `DeepResearch`, `TableSearch`, `WebSearch`, `WebFetch`, `Http` | Multi-round technical research, table querying, and web exploration. |
| **Planning & Tasks** | `Task`, `TodoWrite`, `ExitPlanMode` | Concurrent subagent workers, multi-step checklists, and plan-mode control. |

---

## ⌨️ Useful Commands & Shortcuts

* **`Tab`**: Cycle permission modes (`default` → `acceptEdits` → `plan` → `bypass`).
* **`←` (Left Arrow)**: Open the interactive **Previous Chats / Session Switcher** dashboard.
* **`!` (Exclamation)**: Run shell commands directly (e.g. `!pytest`, `!git status`, `!npm test`).
* **`@` (At symbol)**: Fuzzy search and link project files into your prompt context.
* **`/cost`**: View detailed token breakdown, prompt cache savings, and workspace lifetime billing.
* **`/subagents`**: Open the subagent monitor to inspect isolated subagent tasks and costs.
* **`/main`**: Return to the main chat session from any subagent view.
* **`/model`**: Switch active LLM on the fly (`deepseek-v4-flash`, `claude-opus-5`, `gpt-5.6-sol`, `glm-5.3`).
* **`/effort`**: Adjust reasoning effort tier (`reflex`, `balanced`, `synapse`, `quantum`).
* **`/clear`**: Reset the conversation and start a clean session.
* **`?` or `/help`**: Show the interactive commands cheat sheet.

---

## 🧪 Running Unit Tests (522 Tests)

```bash
uv run pytest
```

```
============================= 522 passed in 4.8s ==============================
```

---

## 📄 License

MIT License. Designed and built for seamless terminal-native AI engineering.
