"""Tests for axon.features.tips (model-written, registry-grounded usage tips)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.agent.trace import Step, TurnTrace
from axon.commands.builtin import dispatch_command
from axon.config import Settings, map_json_settings
from axon.features import tips as T
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from fakes import FakeProvider


def _turn(text: str, usage: Usage | None = None) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn", usage=usage or Usage(input=1200, output=300))


def _make(workspace: Path, **kw) -> Agent:
    s = Settings(workspace=workspace, mode="default", **kw)
    return Agent(provider=FakeProvider([_turn("main answer")]), tools=create_default_registry(),
                 permissions=PermissionEngine(s), context=ContextManager(s), session=SessionStore(workspace),
                 ledger=Ledger(), settings=s)


class RecordingProvider(FakeProvider):
    """FakeProvider that also records the side-call parameters."""

    def __init__(self, turns, gate: threading.Event | None = None) -> None:
        super().__init__(turns)
        self.calls: list[dict] = []
        self.gate = gate

    def stream(self, **kw):  # type: ignore[override]
        self.calls.append({k: kw.get(k) for k in ("model", "max_tokens", "effort", "thinking", "tools")})
        if self.gate is not None:
            self.gate.wait(5)
        yield from super().stream(**kw)


GOOD_REPLY = json.dumps({"tips": [
    {"text": "Type while Axon works and press Enter to steer it between steps", "command": "", "reason": "long turns"},
    {"text": "Use /btw for a quick side question without polluting context", "command": "/btw", "reason": "r"},
    {"text": "Press Ctrl+O to cycle the trace level", "command": "ctrl+o", "reason": "debugging"},
    {"text": "Run /compact when the context gets full", "command": "/compact", "reason": "context 80%"},
]})


@pytest.fixture(autouse=True)
def _fresh_engine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AXON_NO_TIPS", raising=False)
    T.reset_tip_engine()
    yield
    T.reset_tip_engine()


@pytest.fixture
def agent(workspace: Path) -> Agent:
    return _make(workspace)


def _engine(agent: Agent, provider: FakeProvider | None = None, model: str = "small-model") -> T.TipEngine:
    eng = T.TipEngine()
    eng.bind(agent)
    eng.interactive = True
    if provider is not None:
        eng.provider_factory = lambda _a: (provider, model)
    return eng


# ─── validation ────────────────────────────────────────────────────────────

def test_validation_drops_unknown_commands_keys_long_and_duplicates() -> None:
    cmds = T.known_commands()
    keys = T.known_keys()
    items = [
        {"text": "Run /compact when the context window fills up", "command": "/compact"},
        {"text": "Use /teleport to jump between repos instantly", "command": "/teleport"},
        {"text": "Run /compact when the context window fills up!"},                    # duplicate (normalized)
        {"text": "x" * (T.MAX_TIP_CHARS + 1)},                                          # too long
        {"text": "short"},                                                              # too short
        {"text": "Press Cmd+K to clear everything in the terminal"},                    # key Axon does not bind
        {"text": "Press Ctrl+O to cycle the trace level", "command": "ctrl+o"},
        {"text": "\x1b[31mTip: Use /btw for side questions\x1b[0m\x07", "command": "/btw extra words"},
        {"text": "Paths like src/app.py and ~/.axon/keybindings.json are not commands"},
        {"text": "A fine tip text here", "command": "/nonexistent"},
        "Plain string tip mentioning /model to switch models",
        42,
    ]
    out = T.validate_tips(items, cmds, keys)
    texts = [t.text for t in out]
    assert texts == [
        "Run /compact when the context window fills up",
        "Press Ctrl+O to cycle the trace level",
        "Use /btw for side questions",
        "Paths like src/app.py and ~/.axon/keybindings.json are not commands",
        "Plain string tip mentioning /model to switch models",
    ]
    btw = out[2]
    assert btw.command == "/btw" and "\x1b" not in btw.text
    assert all(len(t) <= T.MAX_TIP_CHARS for t in texts)


def test_known_commands_and_keys_come_from_live_sources() -> None:
    cmds = T.known_commands()
    for name in ("btw", "compact", "model", "output", "tips", "kb", "queue"):
        assert name in cmds, name
    keys = T.known_keys()
    assert {"ctrl-o", "shift-tab", "esc", "ctrl-b"} <= keys  # bindings table + in-flight listener


def test_product_facts_ground_the_prompt() -> None:
    facts = T.product_facts()
    assert "/btw" in facts and "/compact" in facts and "/tips" in facts
    assert "ctrl+o" in facts                         # keybinding table
    assert "steer" in facts.lower()                  # in_flight docstring (type-ahead)
    assert "@path" in facts and "!cmd" in facts


# ─── JSON parsing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    GOOD_REPLY,
    f"```json\n{GOOD_REPLY}\n```",
    f"Here are your tips:\n```\n{GOOD_REPLY}\n```\nHope that helps!",
    f"Sure! {GOOD_REPLY} -- let me know if you want more.",
    json.dumps(json.loads(GOOD_REPLY)["tips"]) + "\ntrailing words",
])
def test_parse_tips_json_is_robust(reply: str) -> None:
    items = T.parse_tips_json(reply)
    assert len(items) == 4 and items[1]["command"] == "/btw"


def test_parse_tips_json_garbage() -> None:
    assert T.parse_tips_json("") == []
    assert T.parse_tips_json("no json at all {broken") == []
    assert T.parse_tips_json('{"other": 1}') == []


# ─── registry fallback ─────────────────────────────────────────────────────

def test_fallback_is_derived_from_the_registry_and_activity() -> None:
    from axon.commands.registry import visible_commands
    by_name = {f"/{c.name}": c for c in visible_commands()}
    act = T.Activity(recent_prompts=["the context window is nearly full, compact the tokens"],
                     commands_used={"cost": 3})
    tips = T.registry_tips(None, act)
    assert 0 < len(tips) <= T.FALLBACK_TIPS
    cmd_tips = [t for t in tips if t.command.startswith("/")]
    assert cmd_tips and all(t.command in by_name for t in cmd_tips)
    for t in cmd_tips:  # text is "/name — <registry description>"
        assert t.text.startswith(t.command) and by_name[t.command].description[:20] in t.text
    assert "/cost" not in [t.command for t in tips]                    # already used: skipped
    first = cmd_tips[0].text.lower()
    assert any(w in first for w in ("compact", "context", "window", "tokens"))
    assert any(not t.command.startswith("/") for t in tips)            # keybinding rows too
    assert T.validate_tips(tips, limit=len(tips)) == tips              # passes its own validation


def test_engine_falls_back_without_llm_tips(agent: Agent) -> None:
    eng = _engine(agent)
    eng.interactive = False
    assert eng.source == "registry"
    pool = eng.tips()
    assert pool and all(len(t.text) <= T.MAX_TIP_CHARS for t in pool)
    assert eng.spinner_tip() in {t.text for t in pool}


# ─── cache ─────────────────────────────────────────────────────────────────

def test_cache_round_trip_and_startup_load(agent: Agent, workspace: Path) -> None:
    tips = [T.Tip("Use /btw for a quick side question", "/btw", "why"),
            T.Tip("Press Ctrl+O to cycle the trace level", "ctrl+o", "")]
    assert T.save_cache(workspace, tips, {"origin": "llm", "model": "m", "generated_at": 1.0, "fingerprint": "x"})
    path = T.cache_path(workspace)
    assert path is not None and path.is_file() and path.parent.parent.name == "projects"
    items, meta = T.load_cache(workspace)
    assert [T.Tip(**i) for i in items] == tips and meta["model"] == "m"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["tips"].append({"text": "Try /warpdrive for instant answers", "command": "/warpdrive", "reason": ""})
    path.write_text(json.dumps(data), encoding="utf-8")
    eng = T.tip_engine(agent)                       # a fresh start: tips available before any LLM call
    assert eng.source == "llm" and [t.text for t in eng.tips()] == [t.text for t in tips]

    path.write_text("{not json", encoding="utf-8")
    assert T.load_cache(workspace) == ([], {})


# ─── LLM gating ────────────────────────────────────────────────────────────

def _factory_spy(eng: T.TipEngine) -> list:
    calls: list = []

    def factory(a):
        calls.append(a)
        return RecordingProvider([_turn(GOOD_REPLY)]), "m"
    eng.provider_factory = factory
    return calls


def test_no_llm_call_when_non_interactive_or_disabled(agent: Agent, workspace: Path, monkeypatch) -> None:
    eng = T.TipEngine()
    calls = _factory_spy(eng)
    eng.observe_turn(agent)                         # interactive=None under pytest -> never interactive
    assert eng.refresh(agent, background=False) is False

    eng.interactive = False                         # -p / piped stdin
    eng.observe_turn(agent)

    eng.interactive = True
    agent.settings = agent.settings.model_copy(update={"tips_llm": False})
    eng.observe_turn(agent)
    assert eng.refresh(agent, background=False) is False
    assert eng.tips() and eng.source == "registry"  # fallback still served

    agent.settings = agent.settings.model_copy(update={"tips_llm": True, "tips": False})
    eng.observe_turn(agent)
    assert eng.spinner_tip() == "" and eng.footer_tip() == ""

    agent.settings = agent.settings.model_copy(update={"tips": True})
    monkeypatch.setenv("AXON_NO_TIPS", "1")
    eng.observe_turn(agent)
    assert eng.spinner_tip() == "" and eng.footer_tip() == ""
    assert calls == []

    sub = _make(workspace)
    sub.is_subagent = True
    monkeypatch.delenv("AXON_NO_TIPS")
    eng.observe_turn(sub)
    assert calls == []


def test_refresh_records_cost_in_ledger_and_caches(agent: Agent, workspace: Path) -> None:
    prov = RecordingProvider([_turn(GOOD_REPLY, Usage(input=2000, output=250))])
    eng = _engine(agent, prov, model="claude-haiku-4-5")
    assert eng.refresh(agent, background=False) is True
    assert eng.source == "llm" and len(eng.tips()) == 4
    call = prov.calls[0]
    assert call["model"] == "claude-haiku-4-5" and call["effort"] == "low" and call["thinking"] is False
    # room for reasoning models that think before answering (a 1000-token cap left the JSON cut off)
    assert call["tools"] == [] and 2000 <= call["max_tokens"] <= 8000
    req = prov.requests[0]
    assert "PRODUCT FACTS" in req["messages"][0]["content"] and "ACTIVITY" in req["messages"][0]["content"]
    entries = [e for e in agent.ledger.entries if e.tag == "tips"]
    assert len(entries) == 1 and entries[0].usage.input == 2000
    assert "tips" in agent.ledger.tag_breakdown()
    items, meta = T.load_cache(workspace)
    assert len(items) == 4 and meta["model"] == "claude-haiku-4-5" and meta["origin"] == "llm"


def test_refresh_failure_is_silent_and_keeps_fallback(agent: Agent) -> None:
    class Boom(FakeProvider):
        def stream(self, **kw):  # type: ignore[override]
            raise ConnectionError("offline")
    eng = _engine(agent, Boom())
    assert eng.refresh(agent, background=False) is False
    assert "offline" in eng.last_error and eng.source == "registry" and eng.tips()
    eng2 = _engine(agent, RecordingProvider([_turn("I cannot help with that.")]))
    assert eng2.refresh(agent, background=False) is False and eng2.source == "registry"


def test_observe_turn_refreshes_in_background_once(agent: Agent) -> None:
    gate = threading.Event()
    prov = RecordingProvider([_turn(GOOD_REPLY), _turn(GOOD_REPLY)], gate=gate)
    eng = _engine(agent, prov)
    done = threading.Event()
    eng.on_refresh = lambda tips: done.set()
    eng._turns = T.FIRST_REFRESH_TURNS - 1
    t0 = time.monotonic()
    eng.observe_turn(agent)
    assert time.monotonic() - t0 < 1.0                 # never blocks the REPL
    eng.observe_turn(agent)                            # one in flight at a time
    eng.observe_turn(agent)
    gate.set()
    assert done.wait(5)
    assert len(prov.calls) == 1 and eng.source == "llm"
    eng.observe_turn(agent)                            # rate limited (turns / minutes)
    time.sleep(0.05)
    assert len(prov.calls) == 1


def test_should_refresh_policy(agent: Agent) -> None:
    eng = _engine(agent, RecordingProvider([]))
    agent.settings = agent.settings.model_copy(update={"tips_refresh_turns": 2, "tips_refresh_minutes": 1.0})
    assert eng.should_refresh(agent) is False          # no turn yet
    eng._turns = T.FIRST_REFRESH_TURNS                # the first pool comes right after the first turn
    assert T.FIRST_REFRESH_TURNS == 1
    assert eng.should_refresh(agent) is True
    now = time.time()
    eng.set_tips([T.Tip("Use /btw for a quick side question")],
                 {"generated_at": now, "fingerprint": "same"}, persist=False)
    eng._fingerprint = "same"
    eng._turns, eng._turns_at_refresh = 5, 0
    assert eng.should_refresh(agent, now + 120) is False   # activity unchanged
    eng._fingerprint = "changed"
    assert eng.should_refresh(agent, now + 10) is False    # too soon
    assert eng.should_refresh(agent, now + 120) is True
    eng._turns_at_refresh = 4
    assert eng.should_refresh(agent, now + 120) is False   # not enough turns


# ─── spinner / footer ──────────────────────────────────────────────────────

def test_spinner_tip_rotates_and_is_thread_safe(agent: Agent) -> None:
    eng = _engine(agent)
    pool = [T.Tip(f"Use /btw for side question number {i}") for i in range(3)]
    eng.set_tips(pool, persist=False)
    eng._offset = 0
    eng._t0 = time.monotonic()
    first = eng.spinner_tip()
    assert first == pool[0].text and eng.spinner_tip() == first      # stable within the window
    eng._t0 -= T.ROTATE_S
    assert eng.spinner_tip() == pool[1].text
    eng._t0 -= T.ROTATE_S * 2
    assert eng.spinner_tip() == pool[0].text

    texts = {t.text for t in pool} | {"Press Ctrl+O to cycle the trace level"}
    errors: list[BaseException] = []
    bad: list[str] = []

    def reader():
        try:
            for _ in range(400):
                s = eng.spinner_tip()
                if s and s not in texts:
                    bad.append(s)
                eng.footer_tip()
        except BaseException as e:  # pragma: no cover - failure path
            errors.append(e)

    def writer():
        for i in range(200):
            eng.set_tips(pool if i % 2 else [T.Tip("Press Ctrl+O to cycle the trace level")], persist=False)

    threads = [threading.Thread(target=reader) for _ in range(6)] + [threading.Thread(target=writer)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(10)
    assert not errors and not bad


def test_footer_tip_does_not_repeat(agent: Agent) -> None:
    eng = _engine(agent)
    pool = [T.Tip("Use /btw for a quick side question"), T.Tip("Press Ctrl+O to cycle the trace level")]
    eng.set_tips(pool, persist=False)
    a = eng.footer_tip()
    assert a in {t.text for t in pool}
    assert eng.footer_tip() == ""                     # same pool, no new turns: nothing new
    eng._turns += T.FOOTER_EVERY_TURNS
    b = eng.footer_tip()
    assert b and b != a and {a, b} == {t.text for t in pool}
    eng._turns += T.FOOTER_EVERY_TURNS
    assert eng.footer_tip() == ""                     # every tip already shown
    eng.set_tips(pool + [T.Tip("Run /compact when the context gets full")], persist=False)
    assert eng.footer_tip() == "Run /compact when the context gets full"   # fresh pool: shown at once


# ─── activity, settings, /tips ─────────────────────────────────────────────

def test_activity_signals(agent: Agent) -> None:
    t = TurnTrace(turn=1, prompt="fix the failing test", model="m", mode="default")
    t.steps.append(Step(index=1, kind="tool", started_at=0.0, data={"name": "Bash", "input": {"command": "pytest"},
                                                                       "result_preview": "1 failed", "is_error": True}))
    t.steps.append(Step(index=2, kind="tool", started_at=0.0, data={"name": "Edit", "input": {"path": "a.py"}}))
    agent.traces = [t]
    agent.conversation.messages.append({"role": "user", "content": "[User added while you were working]: also x"})
    act = T.collect_activity(agent, history=["/cost", "/stats now", "!ls", "#remember tabs", "look at @src/a.py"])
    assert act.tools == {"Bash": 1, "Edit": 1} and act.tool_errors == {"Bash": 1}
    assert act.tests == "failed" and act.edits == 1 and act.recent_prompts == ["fix the failing test"]
    assert set(act.commands_used) == {"cost", "stats"} and set(act.prefixes_used) == {"!", "#", "@"}
    assert act.steer_used == 1
    text = act.as_text()
    assert "/cost" in text and "Bash" in text and "yes" in text
    assert act.fingerprint() == T.collect_activity(agent, history=["/cost", "/stats", "!x", "#y", "@z"]).fingerprint()


def test_settings_fields_and_json_mapping(workspace: Path) -> None:
    s = Settings(workspace=workspace)
    assert s.tips is True and s.tips_llm is True and s.tips_refresh_turns >= 1 and s.tips_refresh_minutes > 0
    m = map_json_settings({"spinnerTipsEnabled": False, "tipsLlm": False, "tipsRefreshTurns": 9,
                           "tipsRefreshMinutes": 30})
    assert m == {"tips": False, "tips_llm": False, "tips_refresh_turns": 9, "tips_refresh_minutes": 30}


def test_tips_command(agent: Agent, isolate_user_home: Path, capsys) -> None:
    from axon.commands.registry import resolve_builtin
    assert resolve_builtin("tips") is not None
    dispatch_command("/tips", agent)
    out = capsys.readouterr().out
    assert "Tips" in out and "/tips refresh" in out
    dispatch_command("/tips off", agent)
    assert agent.settings.tips is False
    saved = json.loads((isolate_user_home / ".axon" / "settings.json").read_text(encoding="utf-8"))
    assert saved["tips"] is False
    dispatch_command("/tips on", agent)
    assert agent.settings.tips is True
    dispatch_command("/tips llm off", agent)
    assert agent.settings.tips_llm is False
    capsys.readouterr()
    dispatch_command("/tips refresh", agent)
    assert "off" in capsys.readouterr().out
    agent.settings = agent.settings.model_copy(update={"tips_llm": True})
    dispatch_command("/tips refresh", agent)          # under pytest: never interactive -> no call
    assert "interactive" in capsys.readouterr().out
    dispatch_command("/config tips=off", agent)
    assert agent.settings.tips is False


def test_no_refresh_while_a_turn_is_running(agent: Agent, monkeypatch) -> None:
    prov = RecordingProvider([_turn(GOOD_REPLY)])
    eng = _engine(agent, prov)
    eng._turns = T.FIRST_REFRESH_TURNS
    monkeypatch.setattr(type(agent), "busy", property(lambda self: True))
    eng.observe_turn(agent)
    assert eng.refresh(agent, background=False) is False and prov.calls == []
    assert "turn is running" in eng.last_error and eng._last_attempt == 0.0   # retried after a later turn


def test_spinner_and_footer_never_wait_on_the_lock(agent: Agent) -> None:
    eng = _engine(agent)
    eng.set_tips([T.Tip("Use /btw for a quick side question")], persist=False)
    assert eng.spinner_tip() == "Use /btw for a quick side question"
    got: list[str] = []
    with eng._lock:                                    # e.g. a refresh is swapping the pool
        th = threading.Thread(target=lambda: got.append(eng.spinner_tip()))
        th.start()
        th.join(2)
        assert not th.is_alive()
    assert got == ["Use /btw for a quick side question"]   # last tip, without blocking


def test_tip_output_is_ascii_safe(agent: Agent, monkeypatch, capsys) -> None:
    for t in T.registry_tips():
        assert t.text.isascii(), t.text
    monkeypatch.setenv("AXON_ASCII", "1")
    assert T.display_text("Use /btw \u2014 side question \u2026 \u201cnow\u201d \u2713") == 'Use /btw - side question ... "now"'
    eng = T.tip_engine(agent)
    eng.set_tips([T.Tip("Use /btw \u2014 a side question \u2026", reason="fits \u2014 now")], persist=False)
    assert eng.spinner_tip().isascii() and eng.footer_tip().isascii()
    dispatch_command("/tips", agent)
    dispatch_command("/tips off", agent)
    assert capsys.readouterr().out.isascii()


def test_reply_cut_off_by_the_token_limit_keeps_every_complete_tip() -> None:
    cut = ('{"tips": [{"text": "Use /compact when the context fills up", "command": "/compact"}, '
           '{"text": "Press Ctrl+F to open the full transcript", "command": "ctrl+f"}, {"text": "Type wh')
    tips = T.parse_tips_json(cut)
    assert [t["command"] for t in tips] == ["/compact", "ctrl+f"]
    assert T.parse_tips_json('{"text": "a single tip object"}') == [{"text": "a single tip object"}]
