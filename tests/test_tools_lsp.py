"""LSP tool against a fake language server placed on PATH as pyright-langserver."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.tools import create_default_registry, lsp
from axon.tools.base import ToolContext

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses a sh wrapper")
FAKE = Path(__file__).parent / "tools_fake_lsp.py"


@pytest.fixture
def fake_path(tmp_path: Path, monkeypatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    w = bindir / "pyright-langserver"
    w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    w.chmod(w.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + "/usr/bin:/bin")
    log = tmp_path / "lsp.log"
    monkeypatch.setenv("FAKE_LSP_LOG", str(log))
    monkeypatch.setenv("FAKE_LSP_CONFIG_REQUEST", "1")
    yield log
    lsp.POOL.shutdown_all()


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=workspace))


@pytest.fixture
def tool():
    return create_default_registry().get("LSP")


@pytest.fixture
def src(workspace: Path) -> Path:
    f = workspace / "m.py"
    f.write_text("def helper():\n    return 1\n\nx = helper()\ny = helper() + 1\n", encoding="utf-8")
    return f


def events(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def test_metadata(tool):
    assert tool.readonly and tool.default_permission == "allow"
    assert tool.schema["properties"]["action"]["enum"] == ["diagnostics", "definition", "references", "hover"]


def test_hidden_without_servers_and_visible_with(tool, monkeypatch, fake_path):
    assert tool.hidden is False
    monkeypatch.setenv("PATH", "/nonexistent")
    assert tool.hidden is True


def test_diagnostics_clean_and_dirty(tool, ctx, workspace, src, fake_path):
    assert "No diagnostics" in tool.run({"action": "diagnostics", "path": "m.py"}, ctx)
    src.write_text("def helper():\n    return BADTOKEN\n", encoding="utf-8")
    out = tool.run({"action": "diagnostics", "path": "m.py"}, ctx)
    assert "1 diagnostic(s), 1 error(s)" in out and "m.py:2:12 error [E1] (fake): bad token found" in out
    ms = [e.get("m") for e in events(fake_path)]
    assert ms.count("textDocument/didOpen") == 1 and ms.count("textDocument/didChange") == 1
    assert ms.count("initialize") == 1  # one lazily started server, reused
    # unchanged file: cached, no re-sync
    tool.run({"action": "diagnostics", "path": "m.py"}, ctx)
    assert [e.get("m") for e in events(fake_path)].count("textDocument/didChange") == 1


def test_definition_references_hover(tool, ctx, src, fake_path):
    d = tool.run({"action": "definition", "path": "m.py", "line": 4, "column": 6}, ctx)
    assert "1 definition(s)" in d and "m.py:1:5" in d and "def helper():" in d
    r = tool.run({"action": "references", "path": "m.py", "line": 1, "column": 6}, ctx)
    assert "3 reference(s)" in r and "m.py:4:5" in r and "m.py:5:5" in r
    assert tool.run({"action": "hover", "path": "m.py", "line": 5, "column": 6}, ctx) == "hover:helper"
    assert "No hover" in tool.run({"action": "hover", "path": "m.py", "line": 3, "column": 1}, ctx)
    assert "No definition found" in tool.run({"action": "definition", "path": "m.py", "line": 2, "column": 8}, ctx)


def test_server_requests_are_answered(tool, ctx, src, fake_path):
    tool.run({"action": "hover", "path": "m.py", "line": 4, "column": 6}, ctx)
    assert any(e.get("response_to_server") == 9001 for e in events(fake_path))


def test_unicode_column_is_utf16(tool, ctx, workspace, fake_path):
    f = workspace / "u.py"
    f.write_text('s = "\U0001F600"; helper = 1\n', encoding="utf-8")
    # column of "helper": chars before it = 11 (emoji is 1 char, 2 UTF-16 units)
    out = tool.run({"action": "hover", "path": "u.py", "line": 1, "column": 12}, ctx)
    assert out == "hover:helper"


def test_argument_errors(tool, ctx, src, fake_path):
    with pytest.raises(ToolError, match="requires 'line' and 'column'"):
        tool.run({"action": "hover", "path": "m.py"}, ctx)
    with pytest.raises(ToolError, match="line"):
        tool.run({"action": "hover", "path": "m.py", "line": 0, "column": 1}, ctx)
    with pytest.raises(ToolError, match="only 5 lines"):
        tool.run({"action": "hover", "path": "m.py", "line": 99, "column": 1}, ctx)
    with pytest.raises(ToolError, match="action"):
        tool.run({"action": "rename", "path": "m.py"}, ctx)
    with pytest.raises(ToolError, match="not found"):
        tool.run({"action": "diagnostics", "path": "nope.py"}, ctx)


def test_missing_server_names_what_to_install(tool, ctx, workspace, monkeypatch):
    (workspace / "a.py").write_text("x=1\n")
    (workspace / "b.go").write_text("package main\n")
    (workspace / "c.txt").write_text("x")
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ToolError, match="pyright-langserver.*npm i -g pyright"):
        tool.run({"action": "diagnostics", "path": "a.py"}, ctx)
    with pytest.raises(ToolError, match="gopls"):
        tool.run({"action": "diagnostics", "path": "b.go"}, ctx)
    with pytest.raises(ToolError, match="No language server mapping"):
        tool.run({"action": "diagnostics", "path": "c.txt"}, ctx)


def test_idle_shutdown_and_restart(tool, ctx, src, fake_path, monkeypatch):
    monkeypatch.setattr(lsp, "IDLE_SECONDS", 0.3)
    seen = []
    real_get = lsp.POOL.get
    monkeypatch.setattr(lsp.POOL, "get", lambda *a, **k: seen.append(real_get(*a, **k)) or seen[-1])
    tool.run({"action": "diagnostics", "path": "m.py"}, ctx)
    client = seen[0]
    deadline = time.time() + 10
    while client.proc.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    assert client.proc.poll() is not None, "idle server was not shut down"
    assert "exit" in [e.get("m") for e in events(fake_path)]
    assert "No diagnostics" in tool.run({"action": "diagnostics", "path": "m.py"}, ctx)
    assert [e.get("m") for e in events(fake_path)].count("initialize") == 2
