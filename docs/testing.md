# Testing

Axon has two test layers. Neither needs network access or an API key.

| Layer | Location | What it exercises | Size |
|---|---|---|---|
| Unit and integration | `tests/*.py`, `tests/suite/` | Modules in-process, with fakes for providers, the terminal and the filesystem | about 1,900 tests |
| End-to-end | `tests/e2e/` (marker `e2e`) | The real `axon` CLI as a subprocess, against a local fake LLM server and a fake MCP server | about 1,200 tests |

## Running

```bash
python -m pip install -e ".[dev]"

python -m pytest -q -m "not e2e"                 # unit + integration
python -m pytest -q -m e2e tests/e2e             # end-to-end
python -m pytest -q tests/test_permissions.py    # one file
python -m pytest -q -k "rewind and not slow"     # by keyword
python -m pytest -q --cov=axon -m "not e2e"      # with coverage
```

On Windows, use the same commands with `.venv\Scripts\python.exe`. The suites run on Windows,
macOS and Linux. The few tests that drive a pseudo-terminal are POSIX-only and skip themselves
elsewhere.

`pyproject.toml` registers the `e2e` marker, and `tests/e2e/conftest.py` applies it automatically
to every test in that folder.

## How the end-to-end harness works

- `tests/e2e/fake_llm.py` is a local HTTP server that speaks both the OpenAI-compatible and the
  Anthropic Messages APIs, including streaming. Tests queue scripted responses, such as text,
  tool calls, errors and usage figures, and then assert on the requests Axon sent.
- `tests/e2e/harness.py` runs `python -m axon` in an isolated home directory, `AXON_HOME` and
  workspace. Only a small allow-list of environment variables passes through, so your real keys
  and `~/.axon` are never touched. `run_axon_pty` drives the interactive REPL through a
  pseudo-terminal.
- `tests/e2e/fake_mcp_server.py` is a stdio MCP server used to test `/mcp` and MCP tools.

```python
def test_print_mode_answers(axon_env):
    axon_env.llm.enqueue([{"text": "hi"}])
    r = axon_env.run(["-p", "hello"])
    assert r.code == 0 and "hi" in r.stdout
```

## Writing tests

- Put unit tests next to the closest existing file (`tests/test_<area>_*.py`). Use the helpers in
  `tests/fakes.py` and `tests/conftest.py` for fake providers and agents.
- Never call a real provider. Use `AXON_HOME=<tmp>` (fixtures do this) so tests cannot read or
  write your own state.
- Output assertions should strip ANSI codes (`axon.ui.theme.strip_ansi`) and check display width
  with `display_width` when layout matters.
- Every file in `examples/` is parsed by the real loaders in `tests/test_feature_scaffold.py`. If
  you add an example, also list it in `examples/README.md`.

## Docs and lint checks

```bash
ruff check src scripts tests
python scripts/gen_docs.py --check     # generated doc sections are up to date
```

`scripts/gen_docs.py` regenerates the command, tool, provider and settings tables, the CLI help,
and the terminal demo (`docs/assets/demo.svg`) from the code. Run it without `--check` after you
change any of those.
