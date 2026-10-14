# Project memory for Axon

<!-- Copy to <project>/AXON.md (shared, commit it) or <project>/AXON.local.md (just you, add to .gitignore).
     Axon loads it at the start of every session. Keep it short and factual: every line costs tokens.
     Run /init to generate a first version from your codebase, and /memory to edit it later.
     Block comments like this one are stripped before the file is sent to the model. -->

## Overview
One or two sentences: what this project is and who uses it.

## Commands
- Install: `pip install -e ".[dev]"`
- Test (all): `pytest -q`
- Test (one): `pytest tests/test_api.py -q -k name`
- Lint / format: `ruff check . && ruff format .`
- Run locally: `python -m myapp --help`

## Architecture
- `src/myapp/cli.py` - entry point and argument parsing
- `src/myapp/core/` - business logic, no I/O
- `src/myapp/adapters/` - database and HTTP clients
- `tests/` - mirrors the package layout

## Conventions
- Type hints everywhere; `from __future__ import annotations` at the top of modules.
- Errors: raise domain exceptions from `myapp.errors`; never `print` from library code.
- Every bug fix gets a regression test.
- Do not edit generated files under `src/myapp/_generated/`.

## Imports
Other files can be pulled in with `@path` (relative to this file, ~ for your home directory):

@docs/architecture.md
@~/.axon/rules/personal-style.md
