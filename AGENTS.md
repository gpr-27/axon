# Axon Project Conventions

## Core Rules
- Strictly adhere to standard Python 3.12+ features and strict typing.
- All errors that the model needs to recover from must inherit from `ToolError`.
- Never bypass the workspace path jail.
- Respect read-before-edit invariants `(mtime_ns, sha256)`.
- Replay assistant turns with native content verbatim.
- Batch all tool results from one turn into a single user message.
