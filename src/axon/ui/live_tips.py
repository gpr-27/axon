"""
State-based tips for the live spinner (no LLM, no extra requests).

The tip is chosen from what Axon is doing right now (waiting, a tool running, retrying, high context ...)
and from facts it already has (context use, session cost, permission mode, how many MCP tools are loaded).
Rules:

* a tip shows for at least ``HOLD_S`` seconds before another one replaces it (urgent ones may cut in after
  ``URGENT_AFTER_S``);
* a tip, or another tip about the same feature, is never shown twice in one turn;
* a tip about a feature the user already used is skipped;
* a tip that names a /command is skipped when that command does not exist;
* nothing shows during the first ``MIN_TURN_AGE_S`` seconds of a turn (short turns stay quiet).

Everything here is pure: the picker gets the clock and the facts from its caller, so tests can drive it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

HOLD_S = 8.0
URGENT_AFTER_S = 3.0
MIN_TURN_AGE_S = 3.0
WAIT_LONG_S = 6.0
CONTEXT_HIGH_PCT = 70.0
COST_HIGH_USD = 0.5
MANY_MCP_TOOLS = 10


@dataclass(frozen=True)
class TipFacts:
    """What the tip rules may look at. Defaults mean "unknown", which never triggers a rule."""
    context_pct: float = 0.0
    session_cost: float = 0.0
    mode: str = ""
    mcp_tools: int = 0
    used: frozenset[str] = frozenset()              # features the user already used (command names, "queue")
    commands: frozenset[str] | None = None          # names that dispatch as /name; None = unknown


@dataclass(frozen=True)
class TipRule:
    id: str
    group: str                       # one tip per group per turn; also the feature checked against ``used``
    when: Callable[[Any, TipFacts, float], bool]     # (view, facts, now) -> applies
    text: Callable[[TipFacts], str]
    command: str = ""                # a /command the tip names ("" = none)
    urgent: bool = False


def _waiting_long(v: Any, f: TipFacts, now: float) -> bool:
    return v.phase in ("waiting", "between") and now - v.phase_t0 >= WAIT_LONG_S


def _model_busy(v: Any, f: TipFacts, now: float) -> bool:
    return v.phase in ("thinking", "writing", "writing_call")


def _tool_running(v: Any, f: TipFacts, now: float) -> bool:
    return bool(v.tools)


def _context(v: Any, f: TipFacts) -> float:
    return v.context_pct or f.context_pct


def _mode_label(f: TipFacts) -> str:
    return f" (now: {f.mode})" if f.mode else ""


def _rules() -> list[TipRule]:
    return [
        TipRule("retry-model", "model", lambda v, f, n: v.retry is not None,
                lambda f: "Switch model with /model if this keeps failing", command="model", urgent=True),
        TipRule("compact-context", "context", lambda v, f, n: v.compacting,
                lambda f: "/context shows what fills the window", command="context", urgent=True),
        TipRule("context-high", "compact", lambda v, f, n: _context(v, f) >= CONTEXT_HIGH_PCT,
                lambda f: f"ctx {f.context_pct:.0f}%: /compact frees space" if f.context_pct else "Context is filling up: /compact frees space",
                command="compact"),
        TipRule("wait-long", "queue", _waiting_long,
                lambda f: "Esc stops the turn; type to queue a message that is sent between steps"),
        TipRule("tool-trace", "transcript", _tool_running,
                lambda f: "ctrl+o opens the full transcript; alt+l changes trace detail", command="transcript"),
        TipRule("many-mcp", "tools", lambda v, f, n: f.mcp_tools >= MANY_MCP_TOOLS,
                lambda f: "MCP tools load on demand via ToolSearch; /tools lists them", command="tools"),
        TipRule("cost-rising", "cost", lambda v, f, n: f.session_cost >= COST_HIGH_USD,
                lambda f: "/cost shows where the money went", command="cost"),
        TipRule("subagents", "subagents", lambda v, f, n: bool(v.subagents),
                lambda f: "/subagents shows what each background agent is doing", command="subagents"),
        TipRule("queue-early", "queue", _model_busy,
                lambda f: "Type a message to queue it (sent between steps); /btw asks a side question"),
        TipRule("mode-cycle", "mode", lambda v, f, n: True,
                lambda f: f"shift+tab cycles permission modes{_mode_label(f)}"),
    ]


@dataclass
class TipPicker:
    """Chooses one tip for the current state, honouring hold time, repeats and used features."""
    rules: list[TipRule] = field(default_factory=_rules)
    fallback: Callable[[], str] | None = None        # generic tip source (lowest priority), may be None
    current: str = ""
    current_group: str = ""
    current_urgent: bool = False
    shown_at: float = 0.0
    seen_groups: set[str] = field(default_factory=set)
    seen_texts: set[str] = field(default_factory=set)

    def new_turn(self) -> None:
        self.current, self.current_group, self.current_urgent = "", "", False
        self.shown_at = 0.0
        self.seen_groups.clear()
        self.seen_texts.clear()

    def pick(self, view: Any, facts: TipFacts, now: float) -> str:
        """The tip to show now ("" for none)."""
        if now - view.turn_t0 < MIN_TURN_AGE_S:
            return ""
        held = now - self.shown_at
        if self.current and held < HOLD_S and not self._may_interrupt(view, facts, now, held):
            return self.current
        choice = self._first_candidate(view, facts, now)
        if choice is not None:
            self._show(choice, now)
            return self.current
        if self.current and not self._still_fits(view, facts, now):
            self.current, self.current_group = "", ""
        return self.current

    def _may_interrupt(self, view: Any, facts: TipFacts, now: float, held: float) -> bool:
        if self.current_urgent or held < URGENT_AFTER_S:
            return False
        return any(r.urgent for r in self._candidates(view, facts, now))

    def _candidates(self, view: Any, facts: TipFacts, now: float) -> list[TipRule]:
        out = []
        for rule in self.rules:
            if rule.group in self.seen_groups or rule.group in facts.used:
                continue
            if rule.command and facts.commands is not None and rule.command not in facts.commands:
                continue
            if rule.when(view, facts, now):
                out.append(rule)
        return out

    def _first_candidate(self, view: Any, facts: TipFacts, now: float) -> tuple[str, str, bool] | None:
        rules = self._candidates(view, facts, now)
        urgent = [r for r in rules if r.urgent]
        for rule in (urgent or rules)[:1]:
            return rule.text(facts), rule.group, rule.urgent
        return self._generic()

    def _generic(self) -> tuple[str, str, bool] | None:
        if self.fallback is None:
            return None
        text = (self.fallback() or "").strip()
        if not text or text in self.seen_texts:
            return None
        return text, "generic:" + text, False

    def _show(self, choice: tuple[str, str, bool], now: float) -> None:
        text, group, urgent = choice
        self.current, self.current_group, self.current_urgent, self.shown_at = text, group, urgent, now
        self.seen_groups.add(group)
        self.seen_texts.add(text)

    def _still_fits(self, view: Any, facts: TipFacts, now: float) -> bool:
        """Does the tip on screen still describe the situation? (generic tips always do)"""
        for rule in self.rules:
            if rule.group == self.current_group:
                return bool(rule.when(view, facts, now))
        return True


def facts_from_agent(agent: Any) -> TipFacts:
    """Gather the tip facts once (at the start of a turn). Never raises."""
    try:
        return _facts(agent)
    except Exception:
        return TipFacts()


def _facts(agent: Any) -> TipFacts:
    from axon.features import tips as tips_mod

    settings = getattr(agent, "settings", None)
    act = tips_mod.collect_activity(agent)
    used = {c for c in act.commands_used}
    if act.steer_used:
        used.add("queue")
    if {"btw", "q"} & used:
        used.add("queue")
    commands = frozenset(tips_mod.known_commands(agent))
    return TipFacts(context_pct=float(act.context_pct or 0.0), session_cost=float(act.cost or 0.0),
                    mode=str(getattr(settings, "mode", "") or ""), mcp_tools=_mcp_tool_count(agent),
                    used=frozenset(used), commands=commands)


def _mcp_tool_count(agent: Any) -> int:
    """MCP tools whose schemas wait behind ToolSearch (the tip about on-demand loading is only true then)."""
    try:
        search = agent.registry.tools.get("ToolSearch")
        return len(getattr(search, "deferred", None) or {})
    except Exception:
        return 0
