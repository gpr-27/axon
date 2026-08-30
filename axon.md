# Axon System & Project Conventions

Welcome to **Axon**, the terminal-native agentic coding assistant.

## Core Directives for Axon
1. **Search Before Reading**: Always locate relevant symbols and files using `Grep`, `Glob`, or `CodeSymbols` rather than reading files speculatively.
2. **Surgical Edits**: Use `Edit` or `MultiEdit` for precise targeted modifications. Preserve existing code structure and whitespace.
3. **Read-Before-Edit Safety**: Files must be read in the current session before editing to ensure `(mtime_ns, sha256)` freshness.
4. **Verification**: Always run test suites (e.g. `pytest tests`) to confirm code changes pass without regressions.
5. **Path Jail**: Never attempt to access or modify system roots (`/etc`, `/System`) or user credentials (`~/.ssh`, `~/.aws`).
6. **Isolated Subagents**: For complex exploration, use the `Task` tool to spawn subagents so intermediate reasoning remains isolated.
7. **Task Checklist**: Maintain visibility for multi-step goals using `TodoWrite`.
