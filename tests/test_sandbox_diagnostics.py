"""Language-server errors appended after Edit/Write/MultiEdit: capped, new-only, fast, switchable."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.tools import diagnostics, lsp


class FakeClient:
    def __init__(self, diags, delay=0.0):
        self.lock = threading.RLock()
        self.diags, self.delay = diags, delay
        self.last_used = 0.0

    def sync(self, path, lang):
        return "file://" + str(path), True

    def diagnostics(self, uri, changed, wait_s):
        time.sleep(self.delay)
        return self.diags


def _diag(line, msg, sev=1):
    return {"range": {"start": {"line": line, "character": 0}}, "severity": sev, "message": msg}


@pytest.fixture
def ctx(workspace: Path):
    return SimpleNamespace(workspace=workspace, settings=SimpleNamespace(lsp_diagnostics_after_edit=True),
                           file_state=None)


@pytest.fixture
def py_file(workspace: Path) -> Path:
    f = workspace / "a.py"
    f.write_text("x = 1\n" * 30)
    diagnostics._seen.clear()
    return f


def _with_server(monkeypatch, client):
    monkeypatch.setattr(lsp.shutil, "which", lambda exe: "/usr/bin/" + exe)
    monkeypatch.setattr(lsp.POOL, "get", lambda spec, root: client)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda exe: "/usr/bin/" + exe)


def test_new_errors_are_appended_and_capped_at_ten(py_file, ctx, monkeypatch):
    _with_server(monkeypatch, FakeClient([_diag(i, f"bad {i}") for i in range(15)] + [_diag(20, "just a warning", 2)]))
    out = diagnostics.after_edit("Edit", {"path": str(py_file)}, "Edited a.py", ctx)
    assert out.startswith("Edited a.py") and "15 new error(s)" in out
    assert out.count("bad ") == 10
    assert "just a warning" not in out and "5 more" in out


def test_errors_already_reported_are_not_repeated(py_file, ctx, monkeypatch):
    _with_server(monkeypatch, FakeClient([_diag(1, "same")]))
    first = diagnostics.after_edit("Write", {"path": str(py_file)}, "ok", ctx)
    second = diagnostics.after_edit("Write", {"path": str(py_file)}, "ok", ctx)
    assert "same" in first and second == "ok"


def test_no_server_adds_no_delay_and_no_thread(py_file, ctx, monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, "which", lambda exe: None)
    before = threading.active_count()
    t0 = time.monotonic()
    assert diagnostics.after_edit("Edit", {"path": str(py_file)}, "ok", ctx) == "ok"
    assert time.monotonic() - t0 < 0.05 and threading.active_count() == before


def test_slow_server_times_out_at_about_two_seconds(py_file, ctx, monkeypatch):
    _with_server(monkeypatch, FakeClient([_diag(1, "late")], delay=6.0))
    t0 = time.monotonic()
    out = diagnostics.after_edit("MultiEdit", {"path": str(py_file)}, "ok", ctx)
    took = time.monotonic() - t0
    assert out == "ok" and 1.8 < took < 3.0


def test_setting_off_and_other_tools_are_untouched(py_file, ctx, monkeypatch):
    _with_server(monkeypatch, FakeClient([_diag(1, "boom")]))
    assert diagnostics.after_edit("Read", {"path": str(py_file)}, "ok", ctx) == "ok"
    ctx.settings.lsp_diagnostics_after_edit = False
    assert diagnostics.after_edit("Edit", {"path": str(py_file)}, "ok", ctx) == "ok"


def test_unsupported_extension_is_untouched(workspace, ctx, monkeypatch):
    f = workspace / "notes.txt"
    f.write_text("hi")
    _with_server(monkeypatch, FakeClient([_diag(0, "x")]))
    assert diagnostics.after_edit("Edit", {"path": str(f)}, "ok", ctx) == "ok"
