"""Regression tests for tool defects: lost updates, bad input coercion, symlink escapes, private-address fetches."""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import httpx
import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.permissions.engine import PermissionEngine
from axon.session.checkpoint import CheckpointManager
from axon.tools import _http, create_default_registry
from axon.tools.base import ToolContext


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(),
                       settings=Settings(workspace=workspace), checkpoints=CheckpointManager(workspace))


@pytest.fixture
def reg():
    return create_default_registry()


# 1. lost update ---------------------------------------------------------------------------------
def test_concurrent_edits_only_one_wins(ctx, reg, workspace):
    f = workspace / "a.txt"
    f.write_text("value = 0\n", encoding="utf-8")
    edit = reg.get("Edit")
    reg.get("Read").run({"path": "a.txt"}, ctx)
    results: list[str] = []
    barrier = threading.Barrier(6)

    def go(i: int) -> None:
        barrier.wait()
        try:
            edit.run({"path": "a.txt", "old_string": "value = 0", "new_string": f"value = {i + 1}"}, ctx)
            results.append("ok")
        except ToolError:
            results.append("err")

    for _ in range(5):  # repeat: the race is timing dependent
        f.write_text("value = 0\n", encoding="utf-8")
        reg.get("Read").run({"path": "a.txt"}, ctx)
        results.clear()
        barrier.reset()
        ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert results.count("ok") == 1, results


# 2. NUL / surrogates --------------------------------------------------------------------------------
def test_write_nul_path_is_tool_error(ctx, reg):
    with pytest.raises(ToolError):
        reg.get("Write").run({"path": "a\x00b", "content": "x"}, ctx)


def test_write_surrogate_content_is_tool_error(ctx, reg, workspace):
    with pytest.raises(ToolError):
        reg.get("Write").run({"path": "s.txt", "content": "\ud800"}, ctx)
    assert not list(workspace.glob("*axon-tmp*"))


def test_read_nul_path_is_tool_error(ctx, reg):
    with pytest.raises(ToolError):
        reg.get("Read").run({"path": "a\x00b"}, ctx)


# 3. symlinks ----------------------------------------------------------------------------------------
@pytest.fixture
def outside(tmp_path: Path) -> Path:
    d = tmp_path / "elsewhere"
    d.mkdir()
    (d / "f.txt").write_text("secret", encoding="utf-8")
    return d


@pytest.mark.parametrize("tool,args", [
    ("Write", {"path": "link/new.txt", "content": "x"}),
    ("Read", {"path": "link/f.txt"}),
    ("Write", {"path": "flink", "content": "x"}),
    ("Read", {"path": "flink"}),
])
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan"])
def test_symlink_escape_asks(workspace, outside, reg, tool, args, mode):
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    (workspace / "flink").symlink_to(outside / "f.txt")
    eng = PermissionEngine(Settings(workspace=workspace, mode=mode))
    d = eng.check(reg.get(tool), args)
    assert d.outcome == ("deny" if mode == "plan" and tool == "Write" else "ask"), d


# 4. private addresses -------------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/", "http://192.168.1.1/x",
    "http://172.16.0.1/", "http://[fd00::1]/", "http://[fe80::1]/", "http://0.0.0.0/",
    "http://metadata.google.internal.localdomain/",
])
def test_webfetch_refuses_private_literal(ctx, reg, monkeypatch, url):
    monkeypatch.setattr(_http, "_TRANSPORT", None)
    if "internal" in url:
        monkeypatch.setattr(_http, "_resolve_ips", lambda host: ["169.254.169.254"])
    with pytest.raises(ToolError, match="private|link-local|internal"):
        reg.get("WebFetch").run({"url": url}, ctx)


def test_webfetch_refuses_host_resolving_private(ctx, reg, monkeypatch):
    monkeypatch.setattr(_http, "_TRANSPORT", None)
    monkeypatch.setattr(_http, "_resolve_ips", lambda host: ["10.1.2.3"])
    with pytest.raises(ToolError, match="private"):
        reg.get("WebFetch").run({"url": "http://rebind.example.com/"}, ctx)


def test_webfetch_refuses_redirect_to_private(ctx, reg, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        return httpx.Response(200, text="SECRET")
    monkeypatch.setattr(_http, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(_http, "_resolve_ips", lambda host: ["93.184.216.34"] if host == "public.example.com" else ["169.254.169.254"])
    with pytest.raises(ToolError, match="private|link-local"):
        reg.get("WebFetch").run({"url": "http://public.example.com/"}, ctx)


def test_webfetch_loopback_still_allowed(ctx, reg, monkeypatch):
    monkeypatch.setattr(_http, "_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, text="hi", headers={"content-type": "text/plain"})))
    out = reg.get("WebFetch").run({"url": "http://127.0.0.1:8000/"}, ctx)
    assert "hi" in out


# 5. coercion ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("val", ["abc", -1, 0, 1.5, True])
def test_bash_bad_timeout_is_error(ctx, reg, val):
    with pytest.raises(ToolError, match="timeout_s"):
        reg.get("Bash").run({"command": "echo hi", "timeout_s": val}, ctx)


@pytest.mark.parametrize("key,val", [("limit", -5), ("limit", "abc"), ("offset", 0), ("offset", -2)])
def test_read_bad_range_is_error(ctx, reg, workspace, key, val):
    (workspace / "r.txt").write_text("a\nb\n", encoding="utf-8")
    with pytest.raises(ToolError, match=key):
        reg.get("Read").run({"path": "r.txt", key: val}, ctx)


@pytest.mark.parametrize("val", [5, ["x"], {"a": 1}, True])
def test_write_non_string_content_is_error(ctx, reg, val):
    with pytest.raises(ToolError, match="content"):
        reg.get("Write").run({"path": "w.txt", "content": val}, ctx)
