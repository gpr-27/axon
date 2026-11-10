"""The one-line summary on a tool card says what really happened, above all when output was cut short."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import httpx
import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.tools import _http, create_default_registry
from axon.tools import web_search as ws_mod
from axon.tools.base import ToolContext, default_summary
from axon.tools.textio import MAX_TEXT_BYTES


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    root = tmp_path / "ws"
    root.mkdir()
    return ToolContext(workspace=root, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=root))


@pytest.fixture
def reg():
    return create_default_registry()


def _run(reg, ctx, name: str, **args):
    tool = reg.get(name)
    out = tool.run(args, ctx)
    return out, tool.summarize(args, out)


# ─── Read ──────────────────────────────────────────────────────────────────

def test_read_summary_names_the_total_and_lines_that_were_cut(reg, ctx) -> None:
    (ctx.workspace / "big.txt").write_text("\n".join(f"line {i}" for i in range(1, 5001)), encoding="utf-8")
    _out, summary = _run(reg, ctx, "Read", path="big.txt")
    assert summary.endswith("(lines 1-2000 of 5,000)")
    _out, summary = _run(reg, ctx, "Read", path="big.txt", offset=4995)
    assert summary.endswith("(lines 4995-5000)")           # the last page: nothing more to count
    (ctx.workspace / "one.min.js").write_text("a" * 500_000, encoding="utf-8")
    out, summary = _run(reg, ctx, "Read", path="one.min.js")
    assert "line truncated" in out
    assert "1 long line cut" in summary                       # not just "1 line · 2.0 KB" for a 500 KB file


def test_a_huge_file_page_cut_by_the_output_cap_says_where_to_continue(reg, ctx) -> None:
    line = "log line number %d with some padding text to make every line long\n"
    with open(ctx.workspace / "huge.log", "w", encoding="utf-8") as f:
        i = 0
        while f.tell() <= MAX_TEXT_BYTES + 1000:
            f.write(line % i)
            i += 1
    out, summary = _run(reg, ctx, "Read", path="huge.log")
    shown = [int(n) for n in re.findall(r"^\s*(\d+)→", out, re.M)]
    assert shown[0] == 1 and shown[-1] < 2000                 # 2000 of these lines do not fit one result
    assert f"lines 1-{shown[-1]} " in out.split("\n", 1)[0]   # the header names what is really below it
    assert f"Continue with offset={shown[-1] + 1} " in out    # and the next page starts at the first line not shown
    assert "output truncated" not in out
    assert summary.endswith(f"(lines 1-{shown[-1]}, more follow)")


# ─── Glob ──────────────────────────────────────────────────────────────────

def test_glob_summary_counts_bracket_names_and_a_capped_listing(reg, ctx) -> None:
    (ctx.workspace / "[id].tsx").write_text("x", encoding="utf-8")
    _out, summary = _run(reg, ctx, "Glob", pattern="*.tsx")
    assert summary == "1 file"
    deep = ctx.workspace / ("d" * 100)
    deep.mkdir()
    for i in range(600):
        (deep / f"{'n' * 80}{i}.ts").write_text("x", encoding="utf-8")
    out, summary = _run(reg, ctx, "Glob", pattern="**/*.ts", head_limit=2000)
    shown = len([ln for ln in out.split("\n") if ln.endswith(".ts")])
    assert shown < 600
    assert summary == f"600 files (showing {shown})"
    assert f"{600 - shown} more files matched" in out


# ─── the default summary (Git, LSP, CodeSearch ...) ────────────────────────

def test_default_summary_says_when_output_was_truncated(reg, ctx) -> None:
    assert default_summary("a\nb\n\n[... output truncated: 9,000 more characters ...]").endswith("(truncated)")
    assert "(truncated)" not in default_summary("a\nb")
    root = ctx.workspace
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "c.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=root, check=True)
    (root / "c.txt").write_text("\n".join(f"changed {i} " + "z" * 60 for i in range(2000)), encoding="utf-8")
    out, summary = _run(reg, ctx, "Git", subcommand="diff")
    assert "output truncated" in out and summary.endswith("(truncated)")


# ─── Patch ─────────────────────────────────────────────────────────────────

def test_patch_summary_counts_files_not_commas(reg, ctx) -> None:
    out, summary = _run(reg, ctx, "Patch", patch="--- /dev/null\n+++ b/new, file.txt\n@@ -0,0 +1 @@\n+hi\n")
    assert "created new, file.txt" in out
    assert summary == "1 hunk · 1 file"


# ─── WebFetch / WebSearch (no network: a mock transport) ───────────────────

def _serve(monkeypatch: pytest.MonkeyPatch, body: str, ctype: str = "text/plain") -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": ctype}, content=body.encode())
    monkeypatch.setattr(_http, "_TRANSPORT", httpx.MockTransport(handler))


def test_webfetch_summary_says_how_much_of_the_page_was_returned(reg, ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, "word " * 20_000)                      # 100,000 chars
    _out, summary = _run(reg, ctx, "WebFetch", url="https://example.test/long")
    assert summary == "HTTP 200 · text/plain · chars 1-25,000 of 100,000"
    _out, summary = _run(reg, ctx, "WebFetch", url="https://example.test/long", start_char=90_000)
    assert summary == "HTTP 200 · text/plain · chars 90,001-100,000 of 100,000"
    _serve(monkeypatch, "short page")
    _out, summary = _run(reg, ctx, "WebFetch", url="https://example.test/short")
    assert summary == "HTTP 200 · text/plain · 10 chars"
    ctx.settings = Settings(workspace=ctx.workspace, tool_output_cap=5000)      # the session's own cap cuts it further
    _serve(monkeypatch, "word " * 20_000)
    out, summary = _run(reg, ctx, "WebFetch", url="https://example.test/long")
    assert "output truncated" in out and summary.endswith("(truncated)")


def test_websearch_shows_the_backend_it_fell_back_from(reg, ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.errors import ToolError

    def brave(*_a, **_k):
        raise ToolError("Brave Search rejected the API key")
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setattr(_http, "_TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(500)))
    monkeypatch.setattr(ws_mod, "search_brave", brave)
    monkeypatch.setattr(ws_mod, "search_ddg_html", lambda q: [ws_mod.SearchResult("T", "https://a.test/x", "s")])
    out, summary = _run(reg, ctx, "WebSearch", query="axon cli")
    assert "Brave: Brave Search rejected the API key" in out
    assert summary == "1 result via DuckDuckGo (Brave failed)"
