"""Memory scopes shared by the terminal /memory command and the web Memory page, plus automatic project memory."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon.agent import auto_memory as am  # noqa: E402
from axon.agent.context import ContextManager  # noqa: E402
from axon.agent.loop import Agent  # noqa: E402
from axon.agent.memory import MemoryLoader, render_memory_context  # noqa: E402
from axon.commands.builtin import dispatch_command  # noqa: E402
from axon.config import Settings  # noqa: E402
from axon.features import memory_files as mf  # noqa: E402
from axon.features import memory_hub as hub  # noqa: E402
from axon.permissions.engine import PermissionEngine  # noqa: E402
from axon.session.ledger import Ledger  # noqa: E402
from axon.session.store import SessionStore  # noqa: E402
from axon.tools import create_default_registry  # noqa: E402
from fakes import FakeProvider, scripted  # noqa: E402


@pytest.fixture(autouse=True)
def _home(isolate_user_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AXON_HOME", str(isolate_user_home / ".axon"))
    monkeypatch.delenv("AXON_AUTO_MEMORY_DIR", raising=False)
    monkeypatch.delenv("AXON_DISABLE_AUTO_MEMORY", raising=False)
    return isolate_user_home / ".axon"


@pytest.fixture
def agent(workspace: Path) -> Agent:
    s = Settings(workspace=workspace, mode="default")
    return Agent(provider=FakeProvider(scripted([], "ok")), tools=create_default_registry(), permissions=PermissionEngine(s),
                 context=ContextManager(s), session=SessionStore(workspace), ledger=Ledger(), settings=s)


def run(agent: Agent, line: str, capsys) -> str:
    from axon.ui.theme import strip_ansi
    dispatch_command(line, agent)
    return strip_ansi(capsys.readouterr().out)


# ─── extraction (pure) ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,topic", [
    ("We use pytest, not unittest.", "conventions"),
    ("Never push to main without asking me.", "conventions"),
    ("Remember that the staging API lives at staging.example.com", "facts"),
    ("The test command is `uv run pytest -q`.", "commands"),
    ("We decided to use Postgres for the database.", "facts"),
    ("I prefer small commits with clear messages.", "preferences"),
    ("This project uses FastAPI and SQLModel.", "facts"),
])
def test_extracts_durable_statements(text, topic):
    facts = am.extract_facts(text)
    assert facts and facts[0].topic == topic


@pytest.mark.parametrize("text", [
    "Can you fix the failing test in parser.py?",
    "What does this function do?",
    "Please add a retry to the request helper.",
    "/memory show global",
    "# remember x",
    "I want you to refactor this file.",
    "export API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789 and always use it",
    "```python\nalways = 1\n```",
    "hi",
])
def test_ignores_requests_questions_code_and_secrets(text):
    assert am.extract_facts(text) == []


def test_never_saves_a_secret_even_inside_a_rule(workspace):
    assert am.capture(workspace, "Remember that our password: hunter2hunter2 is for staging") == []
    assert not list(mf.scope_dir(workspace, "project").glob("*.md"))


# ─── saving: topic files, index, dedupe, caps ────────────────────────────────

def test_capture_writes_topic_file_and_index_and_dedupes(workspace):
    saved = am.capture(workspace, "We use pytest, not unittest.")
    assert [f.topic for f in saved] == ["conventions"]
    d = mf.scope_dir(workspace, "project")
    text = (d / "conventions.md").read_text()
    assert "pytest, not unittest" in text and "type: feedback" in text
    assert "conventions.md" in (d / "MEMORY.md").read_text()
    assert am.capture(workspace, "we use pytest, not unittest") == []          # same fact: nothing new
    assert (d / "conventions.md").read_text().count("pytest") == 1
    am.capture(workspace, "Never push to main without asking.")
    assert (d / "conventions.md").read_text().count("\n- ") == 2


def test_fuller_restatement_replaces_the_short_one(workspace):
    am.capture(workspace, "Tests run with pytest.\nWe always use pytest")
    am.capture(workspace, "We always use pytest with the -q flag and xdist")
    body = (mf.scope_dir(workspace, "project") / "conventions.md").read_text()
    assert body.count("pytest") == 1 and "xdist" in body


def test_bullets_are_capped(workspace):
    for i in range(am.MAX_BULLETS + 10):
        am.save_facts(workspace, [am.Fact(f"Always keep rule number {i} in mind here", "conventions")])
    mem = mf.parse_memory(mf.scope_dir(workspace, "project") / "conventions.md", "project")
    n = sum(1 for ln in mem.body.splitlines() if ln.startswith("- "))
    assert n == am.MAX_BULLETS and "rule number 49" in mem.body and "number 0 " not in mem.body


def test_off_switch_saves_nothing(workspace):
    s = Settings(workspace=workspace, auto_memory=False)
    assert am.capture(workspace, "We use pytest, not unittest.", s) == []


# ─── loading: a new session reads it back ────────────────────────────────────

def test_next_session_loads_saved_facts_into_the_prompt(workspace):
    am.capture(workspace, "We use pytest, not unittest.\nThe test command is `uv run pytest -q`.")
    loader = MemoryLoader(workspace)       # what a fresh start builds
    loader.load_startup()
    text = render_memory_context(workspace, loader=loader)
    assert "pytest, not unittest" in text and "uv run pytest -q" in text
    off = MemoryLoader(workspace, auto_memory=False)
    off.load_startup()
    assert "pytest, not unittest" not in render_memory_context(workspace, loader=off)


def test_agent_turn_saves_and_emits_notice(agent, workspace):
    events: list = []
    agent.on_event = events.append
    agent._emit = events.append            # type: ignore[method-assign]
    agent._auto_memory_capture("Remember that deploys go through make release")
    assert any(getattr(e, "reason", "") == "saved" and "make release" in e.message for e in events)
    agent._auto_memory_capture("Remember that deploys go through make release")
    assert len([e for e in events if getattr(e, "reason", "") == "saved"]) == 1      # shown once, not again


# ─── scopes ──────────────────────────────────────────────────────────────────

def test_overview_lists_every_scope(workspace, _home):
    hub.append_line(workspace, "project", "use tabs")
    rows = {r["key"]: r for r in hub.overview(workspace)}
    assert list(rows) == ["global", "project", "local", "repo", "auto"]
    assert rows["project"]["loaded"] and rows["project"]["size"] > 0 and not rows["global"]["exists"]
    assert rows["global"]["path"] == str(_home / "AXON.md")
    assert hub.summary(workspace) == "memory: 1 file"
    am.capture(workspace, "We use pytest, not unittest.")
    assert hub.summary(workspace) == "memory: 2 files + 1 note" or "1 note" in hub.summary(workspace)


def test_local_is_gitignored_and_global_is_private(workspace, _home):
    res = hub.append_line(workspace, "local", "my laptop is slow, keep builds short")
    assert res["gitignored"] and "AXON.local.md" in (workspace / ".gitignore").read_text()
    assert not hub.append_line(workspace, "local", "another")["gitignored"]
    assert (workspace / ".gitignore").read_text().count("AXON.local.md") == 1
    hub.append_line(workspace, "global", "answer briefly")
    if os.name != "nt":
        assert stat.S_IMODE((_home / "AXON.md").stat().st_mode) == 0o600


def test_write_redacts_secrets_and_keeps_old_version(workspace):
    hub.write_scope(workspace, "project", "# P\n- one\n")
    res = hub.write_scope(workspace, "project", "# P\n- two\nAPI_KEY=abcdef123456789xyz\n")
    assert res["redacted"] and "abcdef123456789xyz" not in (workspace / "AXON.md").read_text()
    assert mf.list_trash(workspace)


def test_notes_scopes_are_not_single_files(workspace):
    with pytest.raises(ValueError):
        hub.write_scope(workspace, "auto", "x")
    with pytest.raises(ValueError):
        hub.norm("elsewhere")


def test_forget_removes_matching_lines_across_scopes(workspace):
    hub.append_line(workspace, "project", "deploy with make")
    hub.append_line(workspace, "local", "deploy from my laptop")
    am.capture(workspace, "Remember that we deploy on Fridays")
    hits = hub.forget_matches(workspace, "deploy")
    assert {h["key"] for h in hits} == {"project", "local", "auto"}
    assert hub.forget(workspace, hits) == 3
    assert "deploy" not in (workspace / "AXON.md").read_text()
    assert not (mf.scope_dir(workspace, "project") / "project-facts.md").exists()      # a note with no lines left is removed


def test_delete_scope_keeps_a_copy(workspace):
    hub.append_line(workspace, "project", "x line")
    assert hub.delete_scope(workspace, "project")["removed"] == 1 and not (workspace / "AXON.md").exists()
    assert mf.list_trash(workspace)


# ─── the /memory command ─────────────────────────────────────────────────────

def test_memory_overview_command(agent, workspace, capsys):
    out = run(agent, "/memory", capsys)
    for word in ("Global", "Project", "Local", "Repo notes", "Auto memory", "not created"):
        assert word in out


def test_memory_add_show_edit_forget_flow(agent, workspace, _home, capsys):
    assert "Remembered in Project" in run(agent, "/memory add project we use ruff", capsys)
    assert "Remembered in Local" in run(agent, "/memory add local keep replies short", capsys)
    assert "added to .gitignore" in capsys.readouterr().out or "AXON.local.md" in (workspace / ".gitignore").read_text()
    assert "Remembered in Global" in run(agent, "/memory add global I like dark themes", capsys)
    assert "Remembered in Auto" in run(agent, "/memory add auto the api lives in src/api", capsys)
    assert "we use ruff" in run(agent, "/memory show project", capsys)
    assert "dark themes" in (_home / "AXON.md").read_text()
    assert "Replaced Project" in run(agent, "/memory edit project # fresh\n- only this", capsys)
    assert (workspace / "AXON.md").read_text().strip().endswith("only this")
    out = run(agent, "/memory forget keep replies short", capsys)
    assert "Forgot 1 line" in out and "replies" not in (workspace / "AXON.local.md").read_text()


def test_memory_auto_toggle_persists(agent, workspace, _home, capsys):
    assert "Auto memory off" in run(agent, "/memory auto off", capsys)
    assert agent.settings.auto_memory is False
    assert json.loads((_home / "settings.json").read_text())["autoMemoryEnabled"] is False
    assert "is off" in run(agent, "/memory auto", capsys)
    assert "Auto memory on" in run(agent, "/memory auto on", capsys)
    assert agent.settings.auto_memory is True


def test_hash_local_shortcut(agent, workspace, capsys):
    from axon.cli import _quick_memory
    _quick_memory(agent, "#local my editor is vim")
    assert "my editor is vim" in (workspace / "AXON.local.md").read_text()
    assert "AXON.local.md" in (workspace / ".gitignore").read_text()


# ─── web endpoints ───────────────────────────────────────────────────────────

@pytest.fixture
def web(tmp_path, monkeypatch):
    import threading
    from axon.features import dashboard as dash
    from test_dashboard import Client
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AXON_API_KEY", "sk-test-key-for-dashboard")
    ws = tmp_path / "proj"
    ws.mkdir()
    srv = dash.make_server(0, ws)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    cl = Client(srv)
    cl.login()
    yield cl, ws, tmp_path / "home"
    srv.shutdown_runs()
    srv.shutdown()
    srv.server_close()


def _post(cl, route, body):
    st, _, b = cl.post(route, body)
    return st, json.loads(b)


def test_web_scope_endpoints(web):
    cl, ws, home = web
    d = cl.json("/api/manage/memory/scopes")
    assert [s["key"] for s in d["scopes"]] == ["global", "project", "local", "repo", "auto"] and d["auto"] is True
    st, r = _post(cl, "/api/manage/memory/scope", {"key": "project", "text": "# P\n- use tabs\n"})
    assert st == 200 and (ws / "AXON.md").read_text().startswith("# P")
    st, r = _post(cl, "/api/manage/memory/scope/add", {"key": "local", "text": "private thing"})
    assert st == 200 and r["gitignored"] and "private thing" in (ws / "AXON.local.md").read_text()
    st, r = _post(cl, "/api/manage/memory/scope/add", {"key": "auto", "text": "the build uses make"})
    assert st == 200
    d = cl.json("/api/manage/memory/scopes")
    byk = {s["key"]: s for s in d["scopes"]}
    assert byk["project"]["text"].startswith("# P") and byk["auto"]["count"] == 1 and byk["local"]["loaded"]
    st, r = _post(cl, "/api/manage/memory/auto", {"enabled": False})
    assert st == 200 and cl.json("/api/manage/memory/scopes")["auto"] is False
    assert json.loads((home / "settings.json").read_text())["autoMemoryEnabled"] is False
    st, r = _post(cl, "/api/manage/memory/scope/delete", {"key": "project"})
    assert st == 200 and not (ws / "AXON.md").exists()


def test_web_scope_endpoints_refuse_bad_input(web):
    cl, ws, _ = web
    assert _post(cl, "/api/manage/memory/scope", {"key": "../../etc", "text": "x"})[0] == 400
    assert _post(cl, "/api/manage/memory/scope", {"key": "auto", "text": "x"})[0] == 400
    assert _post(cl, "/api/manage/memory/auto", {"enabled": "yes"})[0] == 400
    big = "x" * 300_000
    assert _post(cl, "/api/manage/memory/scope", {"key": "project", "text": big})[0] == 413
    st, _, _ = cl.req("GET", "/api/manage/memory/scopes", cookie=False)
    assert st in (401, 403)


def test_web_masks_a_file_that_holds_a_secret(web):
    cl, ws, _ = web
    (ws / "AXON.md").write_text("token\nAPI_KEY=abcdef123456789xyz\n")
    row = next(s for s in cl.json("/api/manage/memory/scopes")["scopes"] if s["key"] == "project")
    assert row["locked"] and "abcdef123456789xyz" not in row["text"]
    assert _post(cl, "/api/manage/memory/scope", {"key": "project", "text": "clean"})[0] == 409
