"""
Model-facing tools that reach outside the tool sandbox:

* ``Skill``           load a skill's full instructions on demand (skills are only *listed* in the
                      system prompt; the body is fetched when the model decides it is relevant).
* ``AskUserQuestion`` ask the user one or more multiple-choice questions in the terminal.
* ``ToolSearch``      return the full JSON schemas of deferred (MCP) tools by name or keyword,
                      so large MCP catalogs do not have to sit in every request.
"""
from __future__ import annotations

import json
import sys
import threading
from typing import Any, Callable, ClassVar

from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

_ASK_LOCK = threading.Lock()
OTHER_LABEL = "Other (type your own answer)"


# ─── Skill ──────────────────────────────────────────────────────────────────

class SkillTool(Tool):
    name: ClassVar[str] = "Skill"
    description: ClassVar[str] = (
        "Load the full instructions of a skill listed under 'Available Skills' in the system prompt. "
        "Call it as soon as a skill is relevant to the task, before acting, then follow the returned instructions. "
        "Pass optional arguments in 'args' (they fill $ARGUMENTS / $1..$9 in the skill)."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "Skill name, e.g. 'pdf' or 'code-review'"},
            "args": {"type": "string", "description": "Optional arguments for the skill"},
        },
        "required": ["skill"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        name = str(args.get("skill") or args.get("name") or "").strip().lstrip("/")
        if name.startswith("skill:"):
            name = name[len("skill:"):]
        if not name:
            raise ToolError("Skill requires 'skill' (the skill name).")
        agent = getattr(ctx, "agent", None)
        mgr = getattr(agent, "skills", None)
        skill = None
        if mgr is not None:
            try:
                skill = mgr.get(name)
            except Exception:
                skill = None
        if skill is not None and getattr(skill, "disable_model_invocation", False):
            raise ToolError(f"Skill '{name}' can only be invoked by the user (/{name}); it disables model invocation.")
        try:
            from axon.skills.manager import skill_content
        except (ImportError, AttributeError) as e:
            raise ToolError(f"Skills are unavailable: {e}") from e
        text = skill_content(name, ctx.workspace, str(args.get("args") or ""))
        if text.startswith("Unknown skill"):
            raise ToolError(text)
        hooks = getattr(agent, "hooks", None)
        if skill is not None and getattr(skill, "hooks", None) and hasattr(hooks, "add_scoped"):
            try:
                hooks.add_scoped(skill.hooks, f"skill:{skill.name}")   # the skill's own hooks run while it is active
            except Exception:
                pass
        try:
            from axon.session.library import record_run
            scope = str(getattr(skill, "scope", "") or "")
            record_run("skill_run", {"name": name, "args": str(args.get("args") or ""), "scope": scope,
                                     "path": "" if scope in ("", "bundled") else str(getattr(skill, "path", "")),
                                     "source": "model", "instructions_chars": len(text)},
                       agent=agent, workspace=ctx.workspace)
        except Exception:
            pass
        return f"<skill name=\"{name}\">\n{text.strip()}\n</skill>\n\nFollow these skill instructions for the current task."

    def render_call(self, args: dict[str, Any]) -> str:
        extra = f" {args.get('args')}" if args.get("args") else ""
        return f"Skill /{args.get('skill', '?')}{extra}"


# ─── AskUserQuestion ────────────────────────────────────────────────────────

def _is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _parse_questions(args: dict[str, Any]) -> list[dict[str, Any]]:
    raw = args.get("questions")
    if raw is None and args.get("question"):
        raw = [{"question": args.get("question"), "options": args.get("options") or [],
                "multiSelect": args.get("multiSelect", False), "header": args.get("header", "")}]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise ToolError("'questions' must be a list of question objects.") from e
    if not isinstance(raw, list) or not raw:
        raise ToolError("AskUserQuestion requires 'questions': a non-empty list.")
    out: list[dict[str, Any]] = []
    for q in raw[:4]:
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            raise ToolError("Each question needs a 'question' text.")
        opts = []
        for o in q.get("options") or []:
            if isinstance(o, str):
                opts.append({"label": o, "description": ""})
            elif isinstance(o, dict) and str(o.get("label") or "").strip():
                opts.append({"label": str(o["label"]).strip(), "description": str(o.get("description") or "").strip()})
        if not opts:
            raise ToolError(f"Question '{q.get('question')}' needs at least one option.")
        out.append({"question": str(q["question"]).strip(), "header": str(q.get("header") or "").strip()[:24],
                    "options": opts[:8], "multiSelect": bool(q.get("multiSelect") or q.get("multi_select"))})
    return out


def _ask_one(q: dict[str, Any], index: int, total: int) -> list[str] | None:
    """Show one question with the Selector. Returns the chosen labels, or None if cancelled."""
    from axon.ui import decision_help, live_status
    from axon.ui.picker import SelectItem, Selector

    multi = q["multiSelect"]
    chosen: set[int] = set()
    items = [SelectItem(label=o["label"], value=i, hint=o["description"]) for i, o in enumerate(q["options"])]
    items.append(SelectItem(label=OTHER_LABEL, value="__other__"))

    def badge_for(i: int) -> str:
        return "[x]" if i in chosen else "[ ]"

    if multi:
        for it in items[:-1]:
            it.badge = badge_for(it.value)

    def on_key(sel: Any, key: str) -> bool:
        cur = sel.current()
        if cur is None:
            return False
        if cur.value == "__other__" and key == "enter":
            text = sel.prompt_line("Your answer: ")
            if text:
                sel.finish(("other", text))
            return True
        if multi and key == " " and isinstance(cur.value, int):
            chosen.symmetric_difference_update({cur.value})
            cur.badge = badge_for(cur.value)
            return True
        if multi and key == "enter":
            picked = sorted(chosen) or ([cur.value] if isinstance(cur.value, int) else [])
            sel.finish(("multi", picked))
            return True
        return False

    title = q["header"] or "Question"
    sub = f"{index}/{total}" if total > 1 else ""
    footer = "↑↓ move · space toggle · enter confirm · esc cancel" if multi else None
    sel = Selector(items, title=f"{title}: {q['question']}", subtitle=sub, footer=footer, on_key=on_key)
    with live_status.waiting("user", what="your answer"), decision_help.shown(decision_help.question_lines(q, index, total)):
        res = sel.run()
    if res is None:
        return None
    if isinstance(res, tuple):
        kind, val = res
        if kind == "other":
            return [str(val)]
        return [q["options"][i]["label"] for i in val]
    if getattr(res, "value", None) == "__other__":
        return None
    return [q["options"][int(res.value)]["label"]]


class AskUserQuestionTool(Tool):
    name: ClassVar[str] = "AskUserQuestion"
    description: ClassVar[str] = (
        "Ask the user 1-4 multiple-choice questions in the terminal when a decision genuinely needs their input "
        "(ambiguous requirements, choosing between approaches). Each question has 2-8 options; the user can also type "
        "a custom answer. Returns the selected answers. Do not use it for questions you can answer by reading the code."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The full question"},
                        "header": {"type": "string", "description": "Very short label (max 12 chars)"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
                                "required": ["label"],
                            },
                        },
                        "multiSelect": {"type": "boolean", "description": "Allow several answers"},
                    },
                    "required": ["question", "options"],
                },
            },
        },
        "required": ["questions"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True       # asking changes nothing, so plan mode allows it
    default_permission: ClassVar[str] = "allow"

    def __init__(self, ask: Callable[[dict[str, Any], int, int], list[str] | None] | None = None,
                 interactive: Callable[[], bool] | None = None) -> None:
        self._ask = ask or _ask_one
        self._interactive = interactive or _is_tty

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        questions = _parse_questions(args)
        if not self._interactive():
            raise ToolError("AskUserQuestion needs an interactive terminal; no user is available to answer. "
                            "Make a reasonable assumption, state it, and continue.")
        answers: list[tuple[str, list[str]]] = []
        try:
            from axon.agent.loop import _pause_input
        except Exception:  # pragma: no cover
            import contextlib
            _pause_input = contextlib.nullcontext  # type: ignore[assignment]
        try:
            from axon.ui.notify import notify_event
            notify_event("question", "Axon is asking you a question")      # never the question itself: it can quote files
        except Exception:
            pass
        with _ASK_LOCK, _pause_input():
            for i, q in enumerate(questions, 1):
                picked = self._ask(q, i, len(questions))
                if picked is None:
                    raise ToolError("The user dismissed the question without answering. Ask in plain text or proceed "
                                    "with a stated assumption.")
                answers.append((q["question"], picked))
        lines = ["The user answered:"]
        for question, picked in answers:
            lines.append(f'- "{question}" -> {", ".join(picked)}')
        return "\n".join(lines)

    def render_call(self, args: dict[str, Any]) -> str:
        try:
            qs = _parse_questions(args)
            return f"AskUserQuestion {qs[0]['question'][:60]}" + (f" (+{len(qs) - 1})" if len(qs) > 1 else "")
        except ToolError:
            return "AskUserQuestion"


# ─── ToolSearch (deferred MCP schemas) ──────────────────────────────────────

DEFER_FRACTION = 0.10


def schema_tokens(tools: list[Tool]) -> int:
    """Rough token cost of advertising these tools' schemas (chars / 4)."""
    total = 0
    for t in tools:
        try:
            total += len(json.dumps({"name": t.name, "description": t.description, "schema": t.schema},
                                    default=str, separators=(",", ":")))
        except Exception:
            continue
    return total // 4


def deferred_tool_summary(deferred: dict[str, Any]) -> str:
    """Deferred tool names grouped by MCP server, without repeating the long ``mcp__<server>__`` prefix.

    Example: "mcp__vercel__ (2): list_projects, get_deployment; OtherTool"."""
    if not deferred:
        return "(none)"
    by_server: dict[str, list[str]] = {}
    others: list[str] = []
    for name in sorted(deferred):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp":
            by_server.setdefault(parts[1], []).append(parts[2])
        else:
            others.append(name)
    groups = [f"mcp__{server}__ ({len(names)}): {', '.join(names)}" for server, names in by_server.items()]
    if others:
        groups.append(", ".join(others))
    return "; ".join(groups) + ". A full name is the prefix plus the tool name."


class ToolSearchTool(Tool):
    """Lists deferred tools by name only; the model fetches full schemas on demand. Fetched tools
    become callable through the registry (they are registered hidden, so the first request's tool
    list stays fixed and the prompt cache keeps hitting)."""

    name: ClassVar[str] = "ToolSearch"
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "'select:name1,name2' for exact tools, or keywords to search names and descriptions"},
            "max_results": {"type": "integer", "description": "Maximum matches for a keyword search (default 5)"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def __init__(self, deferred: list[Tool] | None = None) -> None:
        self.deferred: dict[str, Tool] = {t.name: t for t in (deferred or [])}

    @property
    def description(self) -> str:  # type: ignore[override]
        return ("Fetch the full input schemas of deferred tools before calling them. Their names are known but "
                "their parameters are not. Query 'select:A,B' for exact names or keywords to search. "
                f"Deferred tools: {deferred_tool_summary(self.deferred)}")

    def add(self, tools: list[Tool]) -> None:
        for t in tools:
            self.deferred[t.name] = t

    def search(self, query: str, max_results: int = 5) -> list[Tool]:
        q = (query or "").strip()
        if q.lower().startswith("select:"):
            wanted = [n.strip() for n in q.split(":", 1)[1].split(",") if n.strip()]
            low = {n.lower(): t for n, t in self.deferred.items()}
            return [low[w.lower()] for w in wanted if w.lower() in low]
        terms = [t for t in q.lower().replace("_", " ").split() if t]
        if not terms:
            return []
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self.deferred.items():
            hay_name = name.lower().replace("_", " ")
            hay = f"{hay_name} {str(tool.description or '').lower()}"
            score = sum(3 if term in hay_name else 1 for term in terms if term in hay)
            if score:
                scored.append((-score, name, tool))
        scored.sort()
        return [t for _, _, t in scored[:max(1, max_results)]]

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("ToolSearch requires 'query'.")
        try:
            limit = int(args.get("max_results") or 5)
        except (TypeError, ValueError):
            limit = 5
        pending, failed = _mcp_server_state()
        if pending:  # a server that is still connecting may be the one that has the tool
            _wait_for_mcp(pending)
            pending, failed = _mcp_server_state()
        found = self.search(query, limit)
        if not found:
            msg = f"No deferred tool matches '{query}'. Deferred tools: {', '.join(sorted(self.deferred)) or 'none'}"
            if pending:
                msg += f"\nStill connecting: {', '.join(pending)}."
            if failed:
                msg += "\nMCP servers that failed to connect: " + "; ".join(
                    f"{n} ({status}: {err[:160]})" if err else f"{n} ({status})" for n, status, err in failed)
            return msg
        agent = getattr(ctx, "agent", None)
        registry = getattr(agent, "registry", None)
        lines = [f"{len(found)} tool(s); they can now be called directly:", "<functions>"]
        for t in found:
            if registry is not None and not registry.has(t.name):
                registry.register(_Revealed(t))
            desc = getattr(t, "full_description", None) or t.description
            lines.append(json.dumps({"name": t.name, "description": str(desc or ""),
                                     "parameters": t.schema}, default=str, ensure_ascii=False))
        lines.append("</functions>")
        return "\n".join(lines)


class _Revealed(Tool):
    """A deferred tool made callable after ToolSearch. Hidden: it never enters the schema list."""
    hidden = True

    def __init__(self, target: Tool) -> None:
        self.target = target

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.target.name

    @property
    def description(self) -> str:  # type: ignore[override]
        return self.target.description

    @property
    def schema(self) -> dict[str, Any]:  # type: ignore[override]
        return self.target.schema

    @property
    def readonly(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.target, "readonly", False))

    @property
    def default_permission(self) -> str:  # type: ignore[override]
        return str(getattr(self.target, "default_permission", "ask"))

    @property
    def requires_user_interaction(self) -> bool:
        return getattr(self.target, "requires_user_interaction", False) is True

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return self.target.run(args, ctx)

    def render_call(self, args: dict[str, Any]) -> str:
        return self.target.render_call(args)

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        return self.target.summarize(args, output, is_error)


def _live_bridge() -> Any:
    """The MCP bridge if MCP is in use (never creates one)."""
    import sys
    mod = sys.modules.get("axon.mcp.bridge")
    return getattr(mod, "_bridge", None) if mod is not None else None


def _mcp_server_state() -> tuple[list[str], list[tuple[str, str, str]]]:
    bridge = _live_bridge()
    if bridge is None:
        return [], []
    try:
        return bridge.pending_servers(), bridge.failed_servers()
    except Exception:
        return [], []


def _wait_for_mcp(pending: list[str], timeout: float = 10.0) -> None:
    bridge = _live_bridge()
    if bridge is not None:
        try:
            bridge.wait_for_servers(timeout)
        except Exception:
            pass


def parse_tool_search_mode(value: str) -> tuple[str, float]:
    """ENABLE_TOOL_SEARCH -> (mode, fraction): 'off' | 'always' | 'auto' with the threshold fraction.
    Unset behaves like ``auto`` (Axon defers only when schemas would crowd the context window)."""
    v = (value or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return "off", DEFER_FRACTION
    if v in ("1", "true", "yes", "on"):
        return "always", DEFER_FRACTION
    if v.startswith("auto:"):
        try:
            return "auto", max(0.0, min(100.0, float(v.split(":", 1)[1]))) / 100.0
        except ValueError:
            return "auto", DEFER_FRACTION
    return "auto", DEFER_FRACTION


# MCP schemas above this many tokens are deferred behind ToolSearch, however big the context window is: with a 1M
# window, "10% of the window" would let 100k tokens of tool definitions ride along on every request. A single
# server with a few tools (under ~2k tokens) stays loaded, since one extra ToolSearch step would cost more than it saves.
DEFER_MAX_TOKENS = 2_000


def defer_threshold(context_window: int, fraction: float) -> int:
    """Token size of the MCP tool schemas above which they are moved behind ToolSearch."""
    return max(1, min(int(context_window * fraction), DEFER_MAX_TOKENS))


def tool_search_enabled() -> bool:
    """Is MCP tool search on? (``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`` does not apply: Axon's ToolSearch is an
    ordinary tool, so it needs no pre-release API feature.)"""
    import os
    return parse_tool_search_mode(os.environ.get("ENABLE_TOOL_SEARCH", ""))[0] != "off"


def forget_deferred(registry: Any, names: list[str]) -> None:
    """Drop tools of a server that disconnected (or is reconnecting) from the ToolSearch list."""
    search = registry.tools.get("ToolSearch") if hasattr(registry, "tools") else None
    if isinstance(search, ToolSearchTool):
        for name in names:
            search.deferred.pop(name, None)


def defer_mcp_tools(registry: Any, context_window: int, *, fraction: float | None = None,
                    force: bool | None = None) -> ToolSearchTool | None:
    """Move MCP tool schemas out of the advertised tool list behind a ToolSearch tool.

    ``ENABLE_TOOL_SEARCH`` (env): ``true`` defers every MCP tool, ``false`` none, ``auto`` (also the
    default) defers when the schemas would take more than 10% of the context window and ``auto:N`` when
    more than N%. A server with ``"alwaysLoad": true`` (or a tool marked ``anthropic/alwaysLoad``) is never
    deferred; ``"alwaysLoad": false`` always defers that server's tools. Returns the ToolSearch tool
    when deferral happened."""
    import os
    mode, env_fraction = parse_tool_search_mode(os.environ.get("ENABLE_TOOL_SEARCH", ""))
    if fraction is None:
        fraction = env_fraction
    if force is None and mode == "off":
        return None
    mcp = [t for t in registry.all_tools() if str(t.name).startswith("mcp__") and not getattr(t, "hidden", False)]
    if not mcp:
        return None

    def _server_always(t: Any) -> bool | None:
        conn = getattr(t, "_connection", None)
        return getattr(getattr(conn, "spec", None), "always_load", None)

    exempt = [t for t in mcp if callable(getattr(t, "loads_upfront", None)) and t.loads_upfront()]
    pinned = [t for t in mcp if _server_always(t) is False]
    movable = [t for t in mcp if t not in exempt]
    existing = registry.tools.get("ToolSearch")
    already_deferring = isinstance(existing, ToolSearchTool) and bool(existing.deferred)
    if force is not None:
        want = force
    elif mode == "always" or already_deferring:
        want = True   # once some MCP tools are deferred, servers that connect later are deferred too
    else:
        want = bool(movable) and schema_tokens(movable) > defer_threshold(context_window, fraction)
    chosen = movable if want else [t for t in pinned if t in movable]
    if not chosen:
        return None
    search = existing if isinstance(existing, ToolSearchTool) else ToolSearchTool()
    for t in chosen:
        registry.unregister(t.name)
    search.add(chosen)
    registry.register(search)
    return search


__all__ = ["SkillTool", "AskUserQuestionTool", "ToolSearchTool", "defer_mcp_tools", "defer_threshold",
           "forget_deferred", "schema_tokens", "tool_search_enabled"]
