"""Context inspector: every part of what the model receives, with its full text, for the dashboard Context panel.

``inspect_section(agent, section)`` returns one plain dict (see ``SECTIONS`` for the keys):

    {"section", "title", "intro", "tokens", "pct", "window", "estimate",
     "sections": [{"key", "label", "tokens", "pct", "count"}, ...],      # always every section, in order
     "items": [{"id", "title", "subtitle", "group", "tokens", "kind", "lang", "body", "body_truncated",
                "size_chars", "meta": [[label, value]], "badges": [...], "links": [...], "explain", "actions"?}],
     "note": "..."}

Read-only and lazy: only the requested section builds its items. Every text that leaves here goes through the
dashboard's secret redactor, and a body is capped at ``BODY_CAP`` characters (``body_truncated``). A session with
no live agent gets ``empty_result``, not an error.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

BODY_CAP = 200_000
SECTIONS: tuple[tuple[str, str], ...] = (
    ("messages", "Messages"), ("system_prompt", "System prompt"), ("system_tools", "System tools"), ("mcp_tools", "MCP tools"),
    ("mcp_instructions", "MCP server instructions"), ("skills", "Skills"), ("custom_agents", "Custom agents"),
    ("memory_files", "Memory files"), ("autocompact", "Autocompact buffer"), ("free", "Free space"),
    ("permissions", "Permissions"), ("hooks", "Hooks"),
)
KEYS = ("overview",) + tuple(k for k, _ in SECTIONS)
NO_TOKENS = ("permissions", "hooks")
SECRET_NOTE = "Secrets (API keys, tokens, passwords) are masked in everything shown here."

_INTROS = {
    "messages": "Every message and tool result in this conversation, exactly as it is sent back to the model on each turn. "
                "It grows with every step and is the part you control most: pin what must stay, drop big results, or /compact.",
    "system_prompt": "Axon's built-in instructions to the model, sent first on every turn. They cost tokens every turn, "
                     "and are fixed by Axon plus your mode, output style and settings.",
    "system_tools": "The built-in tools the model may call (Read, Edit, Bash and so on) with the exact JSON schema it sees. "
                    "Each schema is sent every turn; the tools list and permissions decide which exist.",
    "mcp_tools": "Tools from MCP servers. Loaded tools send their full schema every turn; deferred tools cost almost nothing "
                 "until the model fetches them with ToolSearch. Configure servers with /mcp.",
    "mcp_instructions": "Usage notes that connected MCP servers ask the model to follow, sent in the system prompt. "
                        "They come from the server, not from you; disconnect the server to remove them.",
    "skills": "Installed skills: only the name and one-line description are sent each turn. The full instructions load "
              "when the model (or you) invokes the skill. Manage them with /skills.",
    "custom_agents": "Subagents the model can start with the Task tool. Their name and description are listed in the prompt; "
                     "their own system prompt only runs inside the subagent. Edit them under .axon/agents.",
    "memory_files": "Instruction and memory files loaded at startup (AXON.md, CLAUDE.md, rules, saved memory). Their full text "
                    "is part of the system prompt on every turn. You own these files; edit them in Memory.",
    "autocompact": "Room kept empty so that Axon can summarise the conversation before the window is full. "
                   "It is not sent to the model; it only reduces the usable window.",
    "free": "Window left for new messages, tool results and the model's reply. Nothing is sent for it.",
    "permissions": "What the model may do without asking, what always asks, and what is blocked: mode, rules and where each "
                   "came from, extra directories, protected paths and the sandbox. Not sent to the model; Axon enforces it.",
    "hooks": "Commands, URLs and prompts that run on events (before a tool, after a tool, on prompt submit and so on). "
             "They are not sent to the model but can change what it sees or block a tool.",
    "overview": "Everything that fills the context window right now, section by section. Open a row to see its exact contents.",
}
_MODE_HELP = {
    "default": "Asks before edits and commands.", "acceptEdits": "Edits inside the workspace run without asking; commands still ask.",
    "plan": "Read-only: the model plans and cannot change files or run commands.", "dontAsk": "Anything not explicitly allowed is refused instead of asking.",
    "auto": "A classifier model approves or refuses each call no rule decided; uncertain or dangerous calls ask.",
    "bypass": "Nothing asks. Only deny rules and hard safety checks apply.",
}
_KIND_HELP = {
    "allow": "Runs without asking.", "ask": "Always asks first, even where a mode would auto-approve.",
    "deny": "Blocked. The tool call is refused before it runs.",
}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _redact(text: Any) -> str:
    from axon.features.dashboard import redact_text
    return redact_text(text)


def _scrub(obj: Any) -> Any:
    """Redact every string inside a JSON-like value (never the serialized form: patterns would eat structure)."""
    from axon.features.dashboard import redact_text
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def _tok(obj: Any) -> int:
    from axon.agent.state import estimate_content_tokens
    try:
        return int(estimate_content_tokens(obj))
    except Exception:
        return max(1, len(str(obj)) // 4)


def _json(obj: Any) -> str:
    return json.dumps(_scrub(obj), indent=2, ensure_ascii=False, default=str)


def _home(p: Any) -> str:
    h = str(Path.home())
    s = str(p)
    return s.replace(h, "~", 1) if len(h) > 3 and s.startswith(h) else s


def _item(id_: str, title: str, *, subtitle: str = "", group: str = "", tokens: int | None = None, kind: str = "text",
          lang: str = "text", body: Any = "", meta: list[list[str]] | None = None, badges: list[str] | None = None,
          links: list[dict[str, str]] | None = None, explain: str = "", redact: bool = True, **extra: Any) -> dict[str, Any]:
    text = str(body if body is not None else "")
    text = _redact(text) if redact else text
    size = len(text)
    cut = size > BODY_CAP
    out = {"id": id_, "title": title, "subtitle": subtitle, "group": group, "tokens": tokens, "kind": kind, "lang": lang,
           "body": text[:BODY_CAP], "body_truncated": cut, "size_chars": size,
           "meta": [[str(a), _redact(b)] for a, b in (meta or [])], "badges": badges or [], "links": links or [], "explain": explain}
    out.update(extra)
    return out


def _schema_parts(s: dict[str, Any]) -> tuple[str, str, Any]:
    fn = s.get("function") if isinstance(s.get("function"), dict) else s
    return (str(fn.get("name") or "?"), str(fn.get("description") or ""),
            fn.get("input_schema") if "input_schema" in fn else fn.get("parameters", {}))


def _server_of(tool: str) -> str:
    parts = tool.split("__")
    return parts[1] if len(parts) > 2 else "mcp"


class _View:
    """Lazy, shared reads of the agent: one prompt build and one schema list per request."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self._blocks: list[dict[str, Any]] | None = None
        self._schemas: list[dict[str, Any]] | None = None
        self._breakdown: dict[str, Any] | None = None

    @property
    def blocks(self) -> list[dict[str, Any]]:
        if self._blocks is None:
            from axon.commands.context_cmds import _system_blocks
            self._blocks = _system_blocks(self.agent)
        return self._blocks

    @property
    def schemas(self) -> list[dict[str, Any]]:
        if self._schemas is None:
            from axon.commands.context_cmds import _tool_schemas
            self._schemas = _tool_schemas(self.agent)
        return self._schemas

    @property
    def breakdown(self) -> dict[str, Any]:
        if self._breakdown is None:
            from axon.features.workbench_data import context_breakdown
            self._breakdown = context_breakdown(self.agent)
        return self._breakdown

    def deferred(self) -> list[Any]:
        try:
            ts = self.agent.registry.tools.get("ToolSearch")
            return list(getattr(ts, "deferred", {}).values()) if ts is not None else []
        except Exception:
            return []


def _classified(view: _View, cls: str) -> list[tuple[int, str]]:
    from axon.features.workbench_data import _block_class
    return [(i, str(b.get("text", ""))) for i, b in enumerate(view.blocks, 1) if _block_class(str(b.get("text", ""))) == cls]


# ─── sections ────────────────────────────────────────────────────────────────

def _messages(view: _View) -> tuple[list[dict[str, Any]], str]:
    from axon.agent.context import result_id, result_text
    from axon.features.ctxmap import ctxmap_data
    agent = view.agent
    cm = {i["id"]: i for i in ctxmap_data(agent)["items"]}
    msgs = list(agent.conversation.messages)
    items: list[dict[str, Any]] = []

    def result_item(idx: int, rid: str, body: Any, label: str) -> dict[str, Any]:
        info = cm.get(rid or f"r{idx}", {})
        pinned, dropped = bool(info.get("pinned")), bool(info.get("dropped"))
        text = result_text(body)
        badges = (["pinned"] if pinned else []) + (["dropped"] if dropped else [])
        actions = []
        if rid and not dropped:
            actions.append({"label": "Unpin" if pinned else "Pin", "api": "/api/insights/unpin" if pinned else "/api/insights/pin",
                            "payload": {"id": rid, "target": rid}})
        if info.get("droppable"):
            actions.append({"label": "Drop", "api": "/api/insights/ctxmap_drop", "payload": {"id": rid}})
        return _item(rid or f"r{idx}", label or "tool result", subtitle=f"tool result · message {idx + 1}", tokens=_tok(body),
                     kind="tool_result", lang="text", body=text, badges=badges, actions=actions, meta=[["Call id", rid or "-"]],
                     explain=info.get("why") or "What the tool returned. Dropping it keeps a short stub in context and the full text on disk.")

    from axon.features.ctxmap import _call_labels
    labels = _call_labels(msgs)
    for idx, m in enumerate(msgs):
        role = str(m.get("role", "?"))
        content = m.get("content")
        if role == "tool":
            rid = result_id(m)
            items.append(result_item(idx, rid, content, labels.get(rid, "")))
            continue
        blocks = content if isinstance(content, list) else []
        parts: list[str] = []
        thoughts: list[str] = []
        calls: list[dict[str, Any]] = []
        results: list[tuple[str, Any]] = []
        for b in blocks:
            if not isinstance(b, dict):
                parts.append(str(b))
            elif b.get("type") == "tool_result":
                results.append((result_id(m, b), b.get("content")))
            elif b.get("type") == "tool_use":
                calls.append({"id": str(b.get("id", "")), "name": str(b.get("name", "")), "input": b.get("input") or {}})
            elif b.get("type") in ("thinking", "redacted_thinking"):
                if b.get("thinking"):
                    thoughts.append(str(b.get("thinking")))
            elif b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            else:
                parts.append(f"[{b.get('type', 'block')}]")
        if not blocks and content is not None:
            parts.append(content if isinstance(content, str) else json.dumps(content, default=str))
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"arguments": fn.get("arguments")}
            calls.append({"id": str(tc.get("id", "")), "name": str(fn.get("name", "")), "input": args})
        text = "\n\n".join(p for p in parts if p)
        thought = "\n\n".join(thoughts)
        if text or thought or calls or not results:
            who = {"user": "You", "assistant": "Axon"}.get(role, role)
            sections: list[str] = []
            if thought:
                sections.append("### Thinking\n\n" + thought)
            if text:
                sections.append(("### Answer\n\n" if role == "assistant" and (thought or calls) else "") + text)
            for c in calls:
                sections.append(f"### Tool call: {c['name']}\n\n`{c['id']}`\n\n```json\n{_json(c['input'])}\n```")
            body = "\n\n".join(sections)
            shown = text or thought
            snippet = " ".join(re.sub(r"[*`#>]+", "", shown).split())
            snippet = snippet[:90] + ("..." if len(snippet) > 90 else "")
            if text:
                title = f"{who}: {snippet}"
            elif calls:
                title = f"{who} calls " + ", ".join(c["name"] for c in calls)
            elif thought:
                title = f"{who} thinking: {snippet}"
            else:
                title = f"{who} message {idx}"
            badges = (["thinking"] if thought else []) + ([f"{len(calls)} tool call{'s' if len(calls) != 1 else ''}"] if calls else [])
            meta = [["Role", role], ["Message", str(idx + 1) + " of " + str(len(msgs))]]
            if calls:
                meta.append(["Tool calls", ", ".join(c["name"] for c in calls)])
            items.append(_item(f"m{idx}", title, subtitle=f"{role} · message {idx + 1}", tokens=_tok(content), kind="message", lang="markdown",
                               body=body, badges=badges, meta=meta,
                               tool_calls=[{"id": c["id"], "name": c["name"], "input": _scrub(c["input"])} for c in calls],
                               explain="A turn of the conversation, sent back to the model on every call. /compact summarises older turns."))
        for rid, body in results:
            items.append(result_item(idx, rid, body, labels.get(rid, "")))
    return items, "Pin keeps a result in full; Drop replaces it with a short stub (the full text stays on disk). " + SECRET_NOTE


def _system_prompt(view: _View) -> tuple[list[dict[str, Any]], str]:
    items = []
    for i, text in _classified(view, "system_prompt"):
        first = (text.strip().splitlines() or [""])[0].lstrip("# ").strip()[:70] or f"System block {i}"
        items.append(_item(f"s{i}", first, subtitle=f"system block {i}", tokens=_tok(text), kind="block", lang="markdown", body=text,
                           meta=[["Block", str(i)], ["Sent", "every turn"]],
                           explain="Part of Axon's built-in instructions. It is rebuilt each turn from your mode, settings and tools."))
    return items, "Shown exactly as sent. " + SECRET_NOTE


def _system_tools(view: _View) -> tuple[list[dict[str, Any]], str]:
    items = []
    for s in view.schemas:
        name, desc, schema = _schema_parts(s)
        if name.startswith("mcp__"):
            continue
        items.append(_item(f"tool:{name}", name, subtitle="built-in tool", tokens=_tok(json.dumps(s, default=str)), kind="tool", lang="json",
                           body=_json(schema), meta=[["Description", desc], ["Parameters", ", ".join(((schema or {}).get("properties") or {}).keys()) or "none"],
                                                     ["Required", ", ".join((schema or {}).get("required") or []) or "none"]],
                           badges=["loaded"], description=_redact(desc),
                           explain="The model can call this tool. Removing it (a deny rule on the whole tool) means the model never sees this schema."))
    return items, "Body is the JSON input schema the model receives. " + SECRET_NOTE


def _mcp_tools(view: _View) -> tuple[list[dict[str, Any]], str]:
    items = []
    for s in view.schemas:
        name, desc, schema = _schema_parts(s)
        if not name.startswith("mcp__"):
            continue
        srv = _server_of(name)
        items.append(_item(f"mcp:{name}", name.split("__", 2)[-1], subtitle=f"{srv} · loaded", group=srv,
                           tokens=_tok(json.dumps(s, default=str)), kind="tool", lang="json", body=_json(schema), badges=["loaded"],
                           meta=[["Server", srv], ["Full name", name], ["Description", desc]], description=_redact(desc),
                           links=[{"label": "Open MCP servers", "href": "#/mcp"}],
                           explain="Sent to the model on every turn because it is loaded. Defer it (or disconnect the server) to save tokens."))
    from axon.tools.interactive import schema_tokens
    for t in view.deferred():
        name = str(t.name)
        srv = _server_of(name)
        desc = str(getattr(t, "full_description", "") or getattr(t, "description", ""))
        items.append(_item(f"mcp:{name}", name.split("__", 2)[-1], subtitle=f"{srv} · deferred", group=srv, tokens=0, kind="tool", lang="json",
                           body=_json(getattr(t, "schema", {})), badges=["deferred"], links=[{"label": "Open MCP servers", "href": "#/mcp"}],
                           meta=[["Server", srv], ["Full name", name], ["Description", desc], ["Schema cost when fetched", f"~{schema_tokens([t])} tokens"]],
                           description=_redact(desc),
                           explain="Only its name is listed to the model. The schema below is sent only if the model fetches it with ToolSearch."))
    return items, "Body is the JSON input schema. " + SECRET_NOTE


def _mcp_instructions(view: _View) -> tuple[list[dict[str, Any]], str]:
    items = []
    for i, text in _classified(view, "mcp_instructions"):
        chunks = re.split(r"(?m)^### (.+)$", text)
        head = chunks[0].strip()
        for k in range(1, len(chunks), 2):
            name, body = chunks[k].strip(), chunks[k + 1].strip()
            items.append(_item(f"mcpi:{name}", name, subtitle="server instructions", group=name, tokens=_tok(body), kind="text", lang="markdown",
                               body=body, links=[{"label": "Open MCP servers", "href": "#/mcp"}], meta=[["Server", name]],
                               explain="Written by the MCP server and sent in the system prompt every turn. Disconnect the server to stop sending it."))
        if len(chunks) == 1 and head:
            items.append(_item(f"mcpi:block{i}", "MCP server instructions", tokens=_tok(head), kind="block", lang="markdown", body=head,
                               explain="Instructions from connected MCP servers."))
    return items, "Shown as sent. " + SECRET_NOTE


def _skills(view: _View) -> tuple[list[dict[str, Any]], str]:
    mgr = getattr(view.agent, "skills", None)
    skills = list(getattr(mgr, "skills", {}).values()) if isinstance(getattr(mgr, "skills", None), dict) else []
    items = []
    for sk in sorted(skills, key=lambda s: s.name):
        listed = not getattr(sk, "disable_model_invocation", False)
        line = f"- **{sk.name}**: {sk.description}" if not getattr(sk, "name_only", False) else f"- **{sk.name}**"
        meta = [["Source", str(getattr(sk, "scope", ""))], ["File", _home(getattr(sk, "path", ""))]]
        if getattr(sk, "allowed_tools", None):
            meta.append(["Allowed tools", ", ".join(sk.allowed_tools)])
        if getattr(sk, "model", None):
            meta.append(["Model", str(sk.model)])
        if getattr(sk, "when_to_use", ""):
            meta.append(["When to use", sk.when_to_use])
        badges = ["enabled" if listed else "disabled", str(getattr(sk, "scope", ""))]
        items.append(_item(f"skill:{sk.name}", sk.name, subtitle=f"{getattr(sk, 'scope', '')} skill", tokens=_tok(line) if listed else 0, kind="skill",
                           lang="markdown", body=line if listed else "(not listed to the model: model invocation is disabled)", meta=meta,
                           badges=[b for b in badges if b], links=[{"label": "Open Skills", "href": "#/skills"}],
                           extra=[{"label": "Full instructions (loaded only when the skill runs)", "lang": "markdown", "body": _redact(getattr(sk, "instructions", ""))[:BODY_CAP]}],
                           explain="Only this line is sent each turn. The full instructions load when the skill is invoked."))
    return items, "Body is the line the model sees; the full SKILL.md instructions are in the extra block. " + SECRET_NOTE


def _custom_agents(view: _View) -> tuple[list[dict[str, Any]], str]:
    from axon.agent.subagent import load_agent_definitions
    try:
        defs = load_agent_definitions(Path(view.agent.settings.workspace))
    except Exception:
        defs = []
    items = []
    for a in sorted(defs, key=lambda x: x.name):
        tools = ", ".join(a.tools) if a.tools else ("read-only tools" if a.readonly else "all tools")
        meta = [["Description", a.description], ["Tools", tools], ["Model", a.model or "inherits the session model"], ["Source", _home(a.path or a.source)]]
        if a.max_turns:
            meta.append(["Max turns", str(a.max_turns)])
        if a.permission_mode:
            meta.append(["Permission mode", a.permission_mode])
        if a.skills:
            meta.append(["Skills", ", ".join(a.skills)])
        line = f"- **{a.name}**: {a.description}"
        items.append(_item(f"agent:{a.name}", a.name, subtitle=("built-in agent" if a.source == "builtin" else f"{a.source} agent"), tokens=_tok(line), kind="agent",
                           lang="markdown", body=a.system_prompt or "(no system prompt of its own: it uses the default one)", meta=meta,
                           badges=["enabled"] + ([] if a.source == "builtin" else ["project" if "project" in str(a.source) else "user"]),
                           explain="The model sees the name and description (the token count) and can start it with Task. Body is the prompt it runs with."))
    return items, "Body is the agent's own system prompt (sent only inside that subagent). " + SECRET_NOTE


def _memory_files(view: _View) -> tuple[list[dict[str, Any]], str]:
    from axon.agent.memory import MemoryLoader
    try:
        files = MemoryLoader(Path(view.agent.settings.workspace)).load_startup()
    except Exception:
        files = []
    items: list[dict[str, Any]] = []

    def walk(f: Any, parent: str = "") -> None:
        scope = str(f.scope)
        path = Path(f.path)
        body = f.content or ""
        items.append(_item(f"mem:{path}", path.name, subtitle=(f"imported by {parent}" if parent else f"{scope} · {_home(path.parent)}"),
                           group=scope, tokens=int(len(body) / 3.7), kind="file", lang="markdown", body=body,
                           meta=[["Path", _home(path)], ["Scope", scope], ["Lines", str(body.count("\n") + 1 if body else 0)]] + ([["Imported by", parent]] if parent else []),
                           badges=[b for b in (scope if scope in ("project", "user") else "",) if b], links=[{"label": "Open in Memory", "href": "#/memory"}],
                           explain="Loaded at startup and sent in the system prompt on every turn. Edit the file to change what the model is told."))
        for imp in getattr(f, "imports", []) or []:
            walk(imp, path.name)
    for f in files:
        walk(f)
    return items, "Full file contents as loaded. " + SECRET_NOTE


def _budget_numbers(view: _View) -> dict[str, int]:
    bd = view.breakdown
    rows = {r["key"]: r["tokens"] for r in bd["rows"]}
    return {"window": bd["window"], "used": bd["used"], "threshold": bd["threshold"], "buffer": rows.get("autocompact", 0), "free": rows.get("free", 0)}


def _autocompact(view: _View) -> tuple[list[dict[str, Any]], str]:
    n = _budget_numbers(view)
    meta = [["Window", f"{n['window']:,} tokens"], ["Compacts at", f"{n['threshold']:,} tokens"], ["Buffer", f"{n['buffer']:,} tokens"],
            ["Used now", f"{n['used']:,} tokens"]]
    body = (f"Window        {n['window']:>10,}\nCompact at    {n['threshold']:>10,}\nBuffer        {n['buffer']:>10,}\nUsed          {n['used']:>10,}\n\n"
            "When used tokens reach the compact point, Axon summarises older turns and replaces them with the summary.\n"
            "Change the point with /autocompact (for example /autocompact 80), or compact now with /compact.")
    return [_item("autocompact", "Autocompact buffer", subtitle=f"{n['buffer']:,} tokens held back", tokens=n["buffer"], kind="text", lang="text", body=body, meta=meta,
                  explain="Not sent to the model. A bigger buffer compacts earlier and keeps more room for the reply; a smaller one keeps more history.")], ""


def _free(view: _View) -> tuple[list[dict[str, Any]], str]:
    n = _budget_numbers(view)
    body = f"Free space  {n['free']:,} tokens\n\nWindow {n['window']:,} minus used {n['used']:,} minus buffer {n['buffer']:,}.\nUse /compact or drop big tool results to get more."
    return [_item("free", "Free space", subtitle=f"{n['free']:,} tokens left", tokens=n["free"], kind="text", lang="text", body=body,
                  meta=[["Window", f"{n['window']:,}"], ["Used", f"{n['used']:,}"], ["Buffer", f"{n['buffer']:,}"]],
                  explain="Room for new messages, tool results and the reply.")], ""


def _permissions(view: _View) -> tuple[list[dict[str, Any]], str]:
    agent = view.agent
    perms = getattr(agent, "permissions", None)
    s = agent.settings
    items: list[dict[str, Any]] = []
    mode = str(getattr(s, "mode", ""))
    items.append(_item("perm:mode", f"Mode: {mode}", subtitle="permission mode", kind="rule", lang="text", body=_MODE_HELP.get(mode, ""),
                       meta=[["Mode", mode]], links=[{"label": "Open Permissions", "href": "#/permissions"}],
                       explain="Decides the default for anything no rule covers. Change it with /mode or Shift+Tab."))
    if perms is None:
        return items, SECRET_NOTE
    files = {}
    try:
        files = {label: str(path) for label, path, _fmt in perms.rule_sources()}
    except Exception:
        pass
    for n, e in enumerate(perms.list_rules()):
        rule = str(e.rule)
        tool = e.rule.tool
        what = f"{tool} calls matching `{e.rule.pattern}`" if e.rule.pattern else f"every {tool} call"
        src = str(e.source)
        file = files.get(src, "")
        items.append(_item(f"perm:{e.kind}:{n}", rule, subtitle=f"{e.kind} · {src}", group=f"{e.kind.capitalize()} rules", kind="rule", lang="text",
                           body=f"{e.kind}  {rule}\n\n{what}\n{_KIND_HELP.get(e.kind, '')}",
                           meta=[["Decision", e.kind], ["Source", src], ["File", _home(file) if file else "this session only"], ["Applies to", what]],
                           badges=[e.kind], explain=f"{_KIND_HELP.get(e.kind, '')} Remove it from {src} to stop applying it."))
    wd = [str(d) for d in (perms.working_dirs() if hasattr(perms, "working_dirs") else [])]
    items.append(_item("perm:dirs", "Working directories", subtitle=f"{len(wd) + 1} folders", group="Folders", kind="rule", lang="text",
                       body="\n".join([str(s.workspace) + "   (workspace)"] + [d + "   (extra)" for d in wd]),
                       explain="Tools may read and write here without the outside-the-project prompt. Add more with /add-dir."))
    extra = [str(d) for d in (getattr(s, "additional_dirs", None) or [])]
    items.append(_item("perm:adddirs", "Additional directories", subtitle=f"{len(extra)} configured", group="Folders", kind="rule", lang="text",
                       body="\n".join(extra) or "none configured", explain="Set with additionalDirectories in settings or --add-dir."))
    from axon.permissions import paths as pp
    items.append(_item("perm:protected", "Protected write locations", subtitle="never auto-approved", group="Protected", kind="rule", lang="text",
                       body="Directories:\n  " + "\n  ".join(sorted(pp.PROTECTED_WRITE_DIRS)) + "\n\nFiles:\n  " + "\n  ".join(sorted(pp.PROTECTED_WRITE_FILES)),
                       explain="Writes here always ask, except in bypass mode, because they change repository state or run on their own."))
    try:
        stores = [_home(p) for p in pp.credential_stores()]
    except Exception:
        stores = []
    items.append(_item("perm:creds", "Protected credential stores", subtitle=f"{len(stores)} found", group="Protected", kind="rule", lang="text",
                       body="\n".join(stores) or "none found on this machine", explain="Tools are refused access to these, in every mode."))
    try:
        from axon.permissions import sandbox
        on = sandbox.enabled(s)
        sb = sandbox.status_text(s)
    except Exception as ex:
        on, sb = False, f"unavailable ({ex})"
    items.append(_item("perm:sandbox", f"Sandbox: {'on' if on else 'off'}", subtitle="Bash sandbox", group="Sandbox", kind="rule", lang="text", body=sb,
                       badges=["enabled" if on else "disabled"], explain="When on, the OS limits what every Bash command can read, write and reach."))
    return items, SECRET_NOTE


def _hooks(view: _View) -> tuple[list[dict[str, Any]], str]:
    agent = view.agent
    mgr = getattr(agent, "hooks", None)
    ws = Path(agent.settings.workspace)
    items: list[dict[str, Any]] = []
    try:
        from axon.hooks import trust
        state = trust.status(ws)
        allowed = trust.allowed(ws)
    except Exception:
        state, allowed = "unknown", True
    trust_text = {"none": "No project hooks or servers to trust.", "trusted": "This folder is trusted: project hooks run.",
                  "changed": "Project hooks changed since you trusted this folder.", "untrusted": "This folder is not trusted yet."}.get(state, state)
    items.append(_item("hook:trust", f"Folder trust: {state}", subtitle="project hooks " + ("run" if allowed else "are held back"), kind="hook", lang="text",
                       body=f"{trust_text}\nFolder: {ws}\n\nUse /trust to review what would run and trust it.",
                       badges=["enabled" if allowed else "disabled"], explain="Hooks from a project run programs, so Axon asks you to trust the folder first."))
    for n, h in enumerate(getattr(mgr, "_hooks", []) or []):
        target = h.url if h.type == "http" else h.prompt if h.type in ("prompt", "agent") else f"{h.server}:{h.tool}" if h.type == "mcp_tool" else h.command
        meta = [["Event", h.event], ["Matcher", h.matcher or "(every call)"], ["Type", h.type], ["Source", str(h.source)], ["Timeout", f"{h.timeout:g}s"]]
        if h.if_cond:
            meta.append(["Only if", h.if_cond])
        if h.run_async:
            meta.append(["Async", "yes (does not block)"])
        items.append(_item(f"hook:{n}", f"{h.event}" + (f" · {h.matcher}" if h.matcher else ""), subtitle=f"{h.type} · {h.source}", group=h.event, kind="hook",
                           lang="bash" if h.type == "command" else "text", body=target, meta=meta, badges=["enabled", "project" if "project" in str(h.source) else "user"],
                           explain="Runs on this event. A command that exits with code 2 blocks the action; its output can be added to what the model sees."))
    errs = list(getattr(mgr, "load_errors", []) or [])
    for k, e in enumerate(errs):
        items.append(_item(f"hook:err{k}", "Hook configuration error", kind="hook", lang="text", body=e, badges=["disabled"],
                           explain="This entry was skipped. Fix the settings file."))
    return items, SECRET_NOTE


def _overview(view: _View) -> tuple[list[dict[str, Any]], str]:
    rows = _section_rows(view)
    items = []
    for r in rows:
        items.append(_item(f"sec:{r['key']}", r["label"], subtitle=(f"{r['count']} item" + ("" if r["count"] == 1 else "s") if r.get("count") is not None else ""), tokens=r["tokens"], kind="text",
                           body=_INTROS.get(r["key"], ""), meta=[["Share of window", f"{r['pct']}%" if r["tokens"] is not None else "-"]],
                           explain=_INTROS.get(r["key"], "")))
    return items, "Token counts are estimates (about 3.7 characters per token) unless the provider reported the total."


_BUILDERS = {
    "messages": _messages, "system_prompt": _system_prompt, "system_tools": _system_tools, "mcp_tools": _mcp_tools,
    "mcp_instructions": _mcp_instructions, "skills": _skills, "custom_agents": _custom_agents, "memory_files": _memory_files,
    "autocompact": _autocompact, "free": _free, "permissions": _permissions, "hooks": _hooks, "overview": _overview,
}


# ─── result ──────────────────────────────────────────────────────────────────

def _section_rows(view: _View) -> list[dict[str, Any]]:
    bd = view.breakdown
    tok = {r["key"]: r for r in bd["rows"]}
    lists = bd["lists"]
    window = bd["window"]
    schemas = view.schemas
    n_sys = sum(1 for s in schemas if not _schema_parts(s)[0].startswith("mcp__"))
    n_mcp = sum(g["count"] for g in lists["mcp_tools"])
    n_instr = sum(len(re.findall(r"(?m)^### ", t)) for _i, t in _classified(view, "mcp_instructions"))
    counts = {
        "messages": len(view.agent.conversation.messages), "system_prompt": len(_classified(view, "system_prompt")), "system_tools": n_sys,
        "mcp_tools": n_mcp, "mcp_instructions": n_instr, "skills": len(lists["skills"]), "custom_agents": len(lists["custom_agents"]),
        "memory_files": len(lists["memory_files"]), "autocompact": 1, "free": 1,
    }
    try:
        perms = view.agent.permissions
        counts["permissions"] = len(perms.list_rules())
    except Exception:
        counts["permissions"] = None
    try:
        counts["hooks"] = len(getattr(view.agent.hooks, "_hooks", []) or [])
    except Exception:
        counts["hooks"] = None
    rows = []
    for key, label in SECTIONS:
        t = None if key in NO_TOKENS else int(tok.get(key, {}).get("tokens", 0))
        rows.append({"key": key, "label": label, "tokens": t, "pct": (round(100 * t / window, 1) if (t is not None and window) else 0.0), "count": counts.get(key)})
    return rows


def inspect_section(agent: Any, section: str) -> dict[str, Any]:
    """The contract dict for one section (``overview`` or a key in ``SECTIONS``). Unknown keys raise ValueError."""
    if section not in KEYS:
        raise ValueError(f"unknown section {section!r}")
    view = _View(agent)
    rows = _section_rows(view)
    window = view.breakdown["window"]
    this = next((r for r in rows if r["key"] == section), None)
    title = "Overview" if section == "overview" else dict(SECTIONS)[section]
    items, note = _BUILDERS[section](view)
    if not items:
        note = f"Nothing in {title.lower()} right now. " + note
    if section == "overview":
        tokens, pct = view.breakdown["used"], float(view.breakdown["pct"])
    else:
        tokens, pct = (this["tokens"], this["pct"]) if this else (None, 0.0)
    return {"section": section, "title": title, "intro": _INTROS[section], "tokens": tokens, "pct": pct, "window": window, "estimate": not view.breakdown.get("exact"),
            "sections": rows, "items": items, "note": note}


def empty_result(section: str, why: str = "Open or start a chat first: this view reads the live session.") -> dict[str, Any]:
    """Same shape with nothing in it, for a session that has no live agent."""
    sec = section if section in KEYS else "overview"
    rows = [{"key": k, "label": label, "tokens": None if k in NO_TOKENS else 0, "pct": 0.0, "count": 0} for k, label in SECTIONS]
    return {"section": sec, "title": "Overview" if sec == "overview" else dict(SECTIONS)[sec], "intro": _INTROS[sec], "tokens": 0 if sec not in NO_TOKENS else None,
            "pct": 0.0, "window": 0, "estimate": True, "sections": rows, "items": [], "note": why}
