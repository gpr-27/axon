"""
Menus for slash commands, and the catalogue of which command gets what.

Rule for every command:  no arguments in a terminal -> a menu;  arguments -> the typed behaviour;
no terminal -> the plain text output. Kinds of commands:

* ``OWN_MENU``: the command opens its own picker or panel (model picker, /mcp browser, ...). If it
  ends up showing nothing (an empty list), the "What next?" menu below is shown instead.
* ``USAGE_MENU``: the menu comes first and is made from the command's usage lines. ``/notify
  [important|all|off|after <seconds>]`` becomes the choices "important", "all", "off" and "after
  <seconds>" (which asks for the seconds). Choosing one runs the command's own handler with those words.
* ``ACTION_MENU``: commands that are an action (/clear, /compact, /commit, ...). The menu says in one
  line what the command does and offers "Run it", the choices its arguments offer, and "Cancel".
* ``REPORT_MENU``: commands that print a report (/status, /cost, ...). The report is printed, then one
  dim "related: ..." footer line names the related views. The old "What next?" menu (``NEXT_STEPS``) is
  still there behind ``AXON_NEXT_MENU=1`` / ``"nextMenu": true`` (``next_menu_enabled``).
* ``NO_MENU``: commands where a menu would be absurd, each with the reason.

Every choice runs the same handler as the typed command (``/cost calls`` and the menu's "Every API
call" are the same call). tests/test_keyboard_menus_sweep.py checks the catalogue and that nothing
prints a "type this next" hint in a terminal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from axon.ui.menus import ask_text, choose_value, confirm

OWN_MENU = {
    "agents": "subagent browser", "config": "settings panel", "effort": "effort picker",
    "endpoint": "endpoint manager", "export": "clipboard or file picker", "find": "fuzzy file finder",
    "help": "command navigator", "hooks": "hook browser", "keys": "key manager", "marketplace": "store browser",
    "mcp": "server browser", "memory": "memory picker", "menu": "command navigator", "mode": "mode picker",
    "model": "model picker", "output": "tool output picker", "output-style": "style picker",
    "payload": "section picker", "permissions": "rules panel", "plugin": "plugin browser",
    "provider": "provider picker", "release-notes": "version picker", "restore": "trash picker",
    "resume": "session picker", "rewind": "checkpoint menu", "sessions": "session switcher",
    "settings": "settings hub", "skills": "skill browser", "theme": "theme picker", "color": "colour picker",
    "trace": "level picker", "usage": "tabbed usage screen", "transcript": "full-screen viewer", "copy": "code block picker",
    "new": "template picker", "tips": "tip list", "tools": "tool browser",
}

# Full-screen viewers draw and read keys themselves; there is no "What next?" after them.
VIEWERS = {"transcript"}

USAGE_MENU = {
    "delete", "fast", "focus", "import", "init", "notify", "prompt", "statusbar", "statusline", "stop",
    "suggestions", "terminal-setup", "thinking", "vim", "search",
}

# name -> what the command does, in one line (shown under the menu title)
ACTION_MENU = {
    "sandbox": "Turns the operating-system sandbox for shell commands on or off, or shows what it enforces.",
    "trust": "Shows, grants or revokes trust for this folder's hooks, plugins and MCP servers.",
    "autofix": "Runs the project checks and fixes what fails, until they pass.",
    "branch": "Continues this conversation in a new branch session.",
    "btw": "Asks a quick side question that is not added to the conversation.",
    "bug": "Saves a bug report about what went wrong.",
    "cd": "Moves the whole session to another directory.",
    "clear": "Starts a new, empty conversation. This one stays in /resume.",
    "commit": "Writes a commit message for the staged changes and commits.",
    "compact": "Summarises the conversation so far to free up context.",
    "debug": "Debugs the last error.",
    "fix": "Fixes the last failing check or command.",
    "index": "Builds the code index for this project.",
    "learn": "Saves a rule or fact to memory.",
    "loop": "Re-runs a prompt on a schedule while Axon is idle.",
    "main": "Switches back to the main conversation.",
    "map": "Maps the symbols of the project.",
    "next": "Suggests what to do next.",
    "optimize": "Turns a rough request into a sharp prompt and sends it.",
    "paste": "Pastes multi-line text.",
    "plan": "Enters plan mode: read-only, the model proposes a plan first.",
    "plan-run": "Plans a task, then runs the plan step by step.",
    "pr": "Writes and opens a pull request for this branch.",
    "recap": "Summarises what happened in this session.",
    "reload-plugins": "Re-scans plugins, custom commands and skills.",
    "reload-skills": "Re-scans the skills folders.",
    "rename": "Gives this session a title.",
    "report": "Writes the HTML usage report.",
    "retitle": "Re-generates the session title from the conversation.",
    "retry": "Sends the last prompt again.",
    "review": "Reviews the current changes for bugs.",
    "security-review": "Reviews the changes on this branch for security problems.",
    "serve": "Starts the local HTTP server.",
    "open": "Opens the Axon app in the browser.",
    "setup": "Walks through provider, model and mode.",
    "simplify": "Simplifies the changed code and applies the fixes.",
    "skill-doctor": "Checks every skill file for problems.",
    "star": "Stars or unstars this session.",
    "subtask": "Runs a task in a subagent.",
    "tag": "Tags this session with a name.",
    "tell": "Sends a message to a running subagent.",
    "test": "Runs the project tests.",
    "verify": "Runs the project checks (tests, lint, types).",
    "inspect": "Shows the detailed timeline of a turn.",
    "explain": "Explains what happened in a turn.",
}

REPORT_MENU = {
    "activity", "add-dir", "autocompact", "budget", "changes", "context", "cost", "diff", "doctor", "env",
    "ctxdiff", "faq", "git", "goal", "history", "insights", "keybindings", "library", "list-agents", "plan-resume",
    "pin", "plans", "pricing", "project", "receipts", "queue", "stats", "status", "storage", "subagents", "tasks", "todos",
    "unpin", "whatif", "window", "files", "trail", "timeline", "ctxmap",
}

NO_MENU = {
    "exit": "leaves Axon; there is nothing to choose",
}

@dataclass
class Choice:
    label: str        # what the menu shows
    words: str        # the fixed words passed to the handler
    ask: str = ""     # a required placeholder to ask for ("" = nothing to ask)
    hint: str = ""


def ch(label: str, words: str = "", ask: str = "", hint: str = "") -> Choice:
    return Choice(label=label, words=words, ask=ask, hint=hint)


# Commands whose usage text does not make good choices. The list is the whole menu (the author
# decides whether there is a plain "Run it"); "…" in a label means a question follows.
OVERRIDES: dict[str, list[Choice]] = {
    "autofix": [ch("Run it", hint="uses the checks it detects"), ch("Run one command…", ask="Command to run and fix"),
                ch("Limit the rounds…", "--max", "Maximum rounds")],
    "branch": [ch("Run it", hint="picks a name"), ch("Name the branch…", ask="Branch name")],
    "btw": [ch("Ask a side question…", ask="Question")],
    "bug": [ch("Describe what went wrong…", ask="What went wrong?")],
    "cd": [ch("Choose a directory…", ask="Directory")],
    "clear": [ch("Run it"), ch("Name the session being left…", ask="Name for the session being left")],
    "commit": [ch("Run it", hint="message written from the diff"), ch("Give a hint for the message…", ask="Hint")],
    "compact": [ch("Run it"), ch("Focus the summary…", ask="What should the summary focus on?")],
    "debug": [ch("Run it", hint="the last error"), ch("Describe what went wrong…", ask="What went wrong?")],
    "explain": [ch("Run it", hint="the last turn"), ch("Explain one turn…", ask="Turn number")],
    "inspect": [ch("Run it", hint="the last turn"), ch("Inspect one turn…", ask="Turn number")],
    "fix": [ch("Run it", hint="the last failing check"), ch("Fix a command…", ask="Command to run and fix"),
            ch("Fix a pasted error…", ask="Error text")],
    "learn": [ch("Remember a rule or fact…", ask="Rule or fact"),
              ch("Remember it for every project…", "--global", "Rule or fact")],
    "map": [ch("Run it"), ch("Map one path…", ask="Path"), ch("Limit the size…", "--tokens", "Token limit")],
    "next": [ch("Run it"), ch("Show suggestion number…", ask="Number")],
    "optimize": [ch("Improve a request…", ask="Rough request")],
    "plan": [ch("Enter plan mode"), ch("Plan a task…", ask="Task"), ch("Leave plan mode", "off")],
    "plan-run": [ch("Plan and run a task…", ask="Task")],
    "pr": [ch("Run it", hint="against the default branch"), ch("Against another branch…", ask="Base branch")],
    "rename": [ch("Run it", hint="title generated from the conversation"), ch("Type a title…", ask="New title")],
    "retitle": [ch("This chat", hint="a title from the conversation"),
                ch("All chats still named after their first words", "all", hint="one short model call each")],
    "review": [ch("Run it"), ch("Review this session's edits hunk by hunk", "hunks"), ch("Low effort", "low"), ch("Medium effort", "medium"), ch("High effort", "high"),
               ch("Maximum effort", "max"), ch("Focus on something…", ask="What to focus on")],
    "security-review": [ch("Run it"), ch("Focus on something…", ask="What to focus on")],
    "simplify": [ch("Run it"), ch("Focus on something…", ask="What to focus on")],
    "serve": [ch("Run it", hint="default port"), ch("Choose a port…", ask="Port")],
    "open": [ch("Open it", hint="in your browser"), ch("Print the link only", "--no-open"), ch("Stop it", "stop")],
    "tag": [ch("Tag this session…", ask="Tag"), ch("Remove the tag", "-")],
    "tell": [ch("Message a subagent…", ask="Subagent number, then the message")],
    "test": [ch("Run it"), ch("With extra arguments…", ask="Extra arguments")],
    "verify": [ch("Run every check"), ch("Run named checks…", ask="Check names")],
    "subtask": [ch("Run a task in a subagent…", ask="Task")],
    "report": [ch("Write only", hint="saves the HTML file"), ch("Write and open in browser", "--open"),
               ch("Choose the period in days…", "--days", "Days"), ch("Every project, not just this one", "--all-projects"),
               ch("Every project, and open in browser", "--all-projects --open")],
    "index": [ch("Run it"), ch("Rebuild from scratch", "--rebuild")],
    "loop": [ch("List the loops"), ch("Repeat a prompt N times…", ask="N and the prompt, for example: 3x run the tests"),
             ch("Re-run on a schedule…", ask="Interval and prompt, for example: 5m check the build"),
             ch("Stop every loop", "stop")],
    "stop": [ch("Stop every subagent"), ch("Stop one…", ask="Task id")],
    "import": [ch("From Cursor", "cursor"), ch("From Gemini", "gemini"), ch("From Codex", "codex"),
               ch("From all of them", "all"), ch("Preview only (dry run)", "all --dry-run")],
    "statusbar": [ch("Show the current state"), ch("Turn the status bar on or off", "toggle")],
    "delete": [ch("Choose a session to delete…", hint="opens the session list"),
               ch("Delete every session", "--all")],
}

DESTRUCTIVE = {"clear", "delete", "drop", "kill", "remove", "rm", "reset", "purge", "--all"}


def menu_kind(name: str) -> str:
    """'own', 'usage', 'action', 'report', 'none' or '' (not catalogued)."""
    if name in OWN_MENU:
        return "own"
    if name in USAGE_MENU:
        return "usage"
    if name in ACTION_MENU:
        return "action"
    if name in REPORT_MENU:
        return "report"
    if name in NO_MENU:
        return "none"
    return ""


# ─── reading usage text ──────────────────────────────────────────────────────

def split_alternatives(text: str) -> list[str]:
    """'a|b <x>|c [y]' -> ['a', 'b <x>', 'c [y]'] (bars inside nested brackets are kept)."""
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch in "[<(":
            depth += 1
        elif ch in "]>)":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    parts.append(current.strip())
    return [p for p in parts if p]


PLACEHOLDER_WORDS = {"n", "N", "id", "name", "path", "file", "amount", "session-id", "turns", "window",
                     "command", "query", "version", "focus", "hint", "base", "port", "message"}


def _usage_commands(name: str, usage: str) -> list[tuple[str, str]]:
    """(argument text, hint) for each '/name ...' in the usage. Two or more spaces separate the
    arguments from the explanation: '/cost calls      every API call' -> ('calls', 'every API call')."""
    found = []
    for raw in usage.splitlines():
        for piece in raw.split(" · "):
            piece = piece.strip()
            head = piece.split(" ", 1)[0]
            if head != f"/{name}":
                continue
            rest = piece[len(head):]
            if not rest.strip() or rest.startswith("  "):
                found.append(("", rest.strip()))
                continue
            parts = re.split(r"\s{2,}", rest[1:], maxsplit=1)
            found.append((parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))
    return found


def _matching_bracket(text: str) -> int:
    depth = 0
    for position, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return position
    return len(text)


def _choice_from(name: str, body: str, hint: str) -> list[Choice]:
    """One or more choices from one usage line ('a|b' alternatives become separate choices)."""
    if body.startswith("["):
        end = _matching_bracket(body)
        inner, after = body[1:end], body[end + 1:].strip()
        if "|" in inner:
            return [_one(name, f"{alt} {after}".strip(), hint) for alt in split_alternatives(inner)]
    tokens = body.split()
    for position, token in enumerate(tokens):
        bracketed = token[:1] in "[<" and token[-1:] in "]>"
        if "|" not in token or (token[:1] in "[<" and not bracketed):
            continue
        alternatives = split_alternatives(token[1:-1] if bracketed else token)
        before, after = tokens[:position], tokens[position + 1:]
        return [_one(name, " ".join(before + [alt] + after), hint) for alt in alternatives]
    return [_one(name, body, hint)]


def _is_placeholder(token: str) -> bool:
    return "<" in token or token.strip("[]") in PLACEHOLDER_WORDS


def _one(name: str, body: str, hint: str) -> Choice:
    """Fixed words up to the first placeholder; the placeholder and what follows is asked for.
    Optional flags in brackets ([--save]) are left out."""
    fixed: list[str] = []
    ask = ""
    tokens = body.split()
    for position, token in enumerate(tokens):
        if _is_placeholder(token):
            ask = " ".join(tokens[position:])
            break
        if token.startswith("["):
            break
        fixed.append(token)
    words = " ".join(fixed)
    label = f"/{name} {words}".strip() + (f" {ask}" if ask else "")
    return Choice(label=label, words=words, ask=ask, hint=hint)


def usage_choices(name: str, usage: str) -> list[Choice]:
    """Every choice the usage text describes, the bare command first, without duplicates."""
    if name in OVERRIDES:
        return list(OVERRIDES[name])
    lines = _usage_commands(name, usage)
    bare_hint = next((hint for body, hint in lines if not body and hint), "what it shows with no arguments")
    choices = [Choice(label=f"/{name}", words="", hint=bare_hint)]
    seen = {"|"}
    for body, hint in lines:
        for choice in _choice_from(name, body, hint):
            key = f"{choice.words}|{choice.ask}"
            if key in seen:
                continue
            seen.add(key)
            choices.append(choice)
    return choices


# ─── running the menu ────────────────────────────────────────────────────────

def _needs_confirm(command_name: str, words: str) -> bool:
    first = words.split()[0].lower() if words else ""
    if command_name == "retitle":
        return words == "all"      # one model call per chat: ask first
    return first in DESTRUCTIVE or (not words and command_name == "clear")


def _ask_for(choice: Choice) -> str | None:
    """The text a choice needs ("" = nothing to ask). None when the user cancelled the question."""
    if not choice.ask:
        return ""
    optional = choice.ask.startswith("[")
    label = choice.ask.strip("[]") + (" (Enter to skip)" if optional else "")
    answer = ask_text(label)
    if answer is None or (not answer and not optional):
        return None
    return answer


def run_usage_menu(agent: Any, command: Any) -> Any:
    """Show the choices; run the handler with the chosen words. Esc closes; Esc at a question goes
    back to the list. Action commands say what they do and end with "Cancel"."""
    choices = usage_choices(command.name, command.usage or "")
    action_text = ACTION_MENU.get(command.name, "")
    for position, choice in enumerate(choices):
        if action_text and not choice.words and not choice.ask and choice.label == f"/{command.name}":
            choices[position] = Choice(label="Run it", words="", hint=action_text[:60])
    options = [(c.label, c, c.hint[:60]) for c in choices]
    if action_text:
        options.append(("Cancel", None, ""))
    subtitle = action_text or command.description[:60]
    while True:
        choice = choose_value(f"/{command.name}", options, subtitle=subtitle)
        if choice is None:
            return None
        answer = _ask_for(choice)
        if answer is None:
            continue  # cancelled: back to the list
        words = f"{choice.words} {answer}".strip()
        if _needs_confirm(command.name, words) and not confirm(f"Run /{command.name} {words}?".replace("  ", " ")):
            continue
        return command.handler(agent, words)


# ─── "What next?" after a report ─────────────────────────────────────────────

@dataclass
class Step:
    """One entry of a "What next?" menu: a typed line, run through the normal dispatcher."""
    label: str
    line: str         # "/cost calls"; "{}" is replaced by the text asked for
    hint: str = ""
    ask: str = ""     # the question for "{}" ("" = nothing to ask)


def step(label: str, line: str, hint: str = "", ask: str = "") -> Step:
    return Step(label, line, hint, ask)


NEXT_STEPS: dict[str, list[Step]] = {
    "status": [step("Cost breakdown", "/cost"), step("Context usage", "/context"), step("Run diagnostics", "/doctor"),
               step("Switch model", "/model"), step("Change permission mode", "/mode"), step("Sessions", "/sessions"),
               step("Storage", "/storage"), step("Settings", "/settings")],
    "cost": [step("Every API call", "/cost calls"), step("This project", "/cost --project"),
             step("Every project", "/cost --all"), step("Model prices", "/pricing"),
             step("Set a spending limit", "/budget"), step("Usage over time", "/stats")],
    "context": [step("Why each block is there", "/context --why"), step("Per-tool token sizes", "/context all"),
                step("Compact the conversation", "/compact"), step("Change the context limit", "/context limit"),
                step("The exact prompt", "/payload"), step("All tools", "/tools")],
    "doctor": [step("Check instructions for conflicts", "/doctor prompt-audit"), step("API keys", "/keys"),
               step("Endpoints", "/endpoint"), step("Settings", "/settings"), step("Environment", "/env")],
    "stats": [step("Today", "/stats today"), step("Last 30 days", "/stats 30d"), step("All time", "/stats all"),
              step("Every project", "/stats --all-projects"), step("Full HTML report", "/report"),
              step("Written insights", "/insights")],
    "insights": [step("Today", "/insights today"), step("Last 30 days", "/insights 30d"),
                 step("All time", "/insights all"), step("Every project", "/insights --all-projects"),
                 step("Usage numbers", "/stats")],
    "window": [step("Set the number of turns", "/window {}", "keep only the last N turns", "Turns to keep"),
               step("Unlimited", "/window 0"), step("Context usage", "/context")],
    "env": [step("Diagnostics", "/doctor"), step("Status", "/status"), step("Settings", "/settings"),
            step("Terminal setup", "/terminal-setup")],
    "storage": [step("Everything saved for this project", "/library"), step("Restore deleted sessions", "/restore"),
                step("Delete a session", "/delete"), step("Sessions", "/sessions")],
    "git": [step("Diff", "/diff"), step("Commit", "/commit"), step("Open a pull request", "/pr"),
            step("Files changed this session", "/changes"), step("Review the changes", "/review")],
    "changes": [step("Diff", "/diff"), step("Rewind to a checkpoint", "/rewind"), step("Commit", "/commit"),
                step("Review the changes", "/review")],
    "project": [step("Memory files", "/memory"), step("Sessions", "/sessions"), step("Storage", "/storage"),
                step("Everything saved", "/library")],
    "pricing": [step("Filter by model name", "/pricing {}", "", "Model name"), step("Switch model", "/model"),
                step("Session cost", "/cost")],
    "todos": [step("Background tasks", "/tasks"), step("Saved plans", "/plans"), step("Goal", "/goal")],
    "plans": [step("Resume the latest unfinished plan", "/plan-resume"),
              step("Plan and run a task", "/plan-run {}", "", "What should the plan do?"),
              step("Plan mode", "/plan")],
    "plan-resume": [step("Saved plans", "/plans"), step("Plan and run a task", "/plan-run {}", "", "What should the plan do?")],
    "list-agents": [step("Subagents", "/subagents"), step("Background tasks", "/tasks"),
                    step("Stop all subagents", "/stop"), step("Agent definitions", "/agents")],
    "keybindings": [step("Settings", "/settings"), step("Terminal setup", "/terminal-setup"), step("Vim mode", "/vim")],
    "add-dir": [step("Allow a directory", "/add-dir {}", "", "Directory"),
                step("Allow a directory and remember it", "/add-dir {} --save", "", "Directory"),
                step("Stop allowing a directory", "/add-dir remove {}", "", "Directory"),
                step("Permission rules", "/permissions")],
    "autocompact": [step("Set a percent or token count", "/autocompact {}", "e.g. 80 or 500k", "Percent (10-99) or tokens like 500k"),
                    step("Back to the default", "/autocompact auto"), step("Context usage", "/context")],
    "budget": [step("Set a limit in dollars", "/budget {}", "", "Amount in dollars"), step("Turn the limit off", "/budget off"),
               step("Cost breakdown", "/cost")],
    "diff": [step("Staged changes", "/diff staged"), step("Edits since a checkpoint", "/diff T{}", "", "Checkpoint number"),
             step("Rewind", "/rewind"), step("Commit", "/commit")],
    "faq": [step("402: out of credit", "/faq 402"), step("400: request rejected", "/faq 400"),
            step("401: key rejected", "/faq 401"), step("Sensitive content", "/faq sensitive"),
            step("Backups", "/faq backup"), step("Credit", "/faq credit"), step("Pricing", "/faq pricing")],
    "goal": [step("Set a goal", "/goal {}", "the model keeps working toward it", "Goal"),
             step("Clear the goal", "/goal clear"), step("Status", "/status")],
    "history": [step("Last N turns", "/history {}", "", "Number of turns"), step("Every record", "/history all"),
                step("Resume a session", "/resume"), step("Transcript viewer", "/transcript")],
    "library": [step(name.capitalize(), f"/library {name}") for name in
                ("sessions", "subagents", "skills", "agents", "commands", "memory", "hooks", "plans", "todos",
                 "files", "outputs", "usage")],
    "queue": [step("Add a prompt", "/queue {}", "", "Prompt"), step("Run the next one now", "/queue run"),
              step("Remove one", "/queue drop {}", "", "Item number"), step("Clear the queue", "/queue clear")],
    "subagents": [step("Open the main conversation", "/subagents main"), step("Queued messages", "/subagents queue"),
                  step("Open subagent number…", "/subagents {}", "", "Subagent number"),
                  step("Background tasks", "/tasks"), step("Stop all subagents", "/stop")],
    "tasks": [step("Output of a shell", "/tasks log {}", "", "Shell id (bash_1)"),
              step("Stop a shell", "/tasks kill {}", "", "Shell id (bash_1)"),
              step("Subagents", "/subagents"), step("Stop all subagents", "/stop")],
    "activity": [step("The last N events", "/activity {}", "", "Number of events"),
                 step("Only prompts and shell", "/activity --kind prompt,shell"), step("Since 2 hours ago", "/activity --since 2h"),
                 step("Since 3 days ago", "/activity --since 3d"), step("Every project", "/activity --all-projects"),
                 step("Write it to a file", "/activity --export {}", "", "File (.md or .jsonl)"),
                 step("Where the journal lives", "/activity --path")],
    "hooks": [step("Every hook event", "/hooks events"), step("Reload hooks", "/hooks reload"), step("Settings", "/settings")],
    "restore": [step("Sessions", "/sessions"), step("Resume a session", "/resume"), step("Delete a session", "/delete")],
    "sessions": [step("Resume a session", "/resume"), step("History of this session", "/history"),
                 step("Delete a session", "/delete"), step("Restore deleted sessions", "/restore")],
    "output": [step("Full transcript", "/transcript"), step("Tools", "/tools"), step("Last answer", "/copy")],
    "find": [step("Memory files", "/memory"), step("Map the project", "/map"), step("Index the project", "/index")],
    "copy": [step("Full transcript", "/transcript"), step("Export the conversation", "/export")],
    "release-notes": [step("Every version", "/release-notes all"), step("Status", "/status")],
    "trail": [step("Detailed timeline of the turn", "/inspect"), step("Timeline chart", "/timeline"), step("Permissions", "/permissions")],
    "timeline": [step("Detailed steps", "/inspect"), step("Decision trail of a tool call", "/trail"), step("Cost", "/cost")],
    "ctxmap": [step("Context usage", "/context"), step("Compact the conversation", "/compact"), step("Pin a result", "/pin")],
    "receipts": [step("Full transcript", "/transcript"), step("Changes made", "/changes"), step("Run the project checks", "/verify")],
    "files": [step("Full transcript", "/transcript"), step("Where everything is kept", "/storage")],
    "whatif": [step("Every priced model", "/whatif all"), step("Model prices", "/pricing"), step("Cost breakdown", "/cost")],
}


def next_steps(command: Any) -> list[Step]:
    """The "What next?" entries of a command: the curated list, or the choices its usage text offers."""
    if command.name in NEXT_STEPS:
        return NEXT_STEPS[command.name]
    steps = []
    for choice in usage_choices(command.name, command.usage or "")[1:]:
        line = f"/{command.name} {choice.words}".strip() + (" {}" if choice.ask else "")
        steps.append(Step(choice.label, line, choice.hint[:60], choice.ask.strip("[]")))
    return steps


def run_next_menu(agent: Any, command: Any) -> None:
    """"What next?": pick a related view or action; it runs like the typed command; back to the list."""
    from axon.commands.registry import dispatch_command
    steps = next_steps(command)
    if not steps:
        return
    options = [(s.label, s, s.hint[:60]) for s in steps]
    while True:
        picked = choose_value("What next?", options, subtitle=f"after /{command.name} · Esc to leave")
        if picked is None:
            return
        line = picked.line
        if picked.ask:
            answer = ask_text(picked.ask)
            if not answer:
                continue
            line = line.replace("{}", answer)
        if _needs_confirm(command.name, " ".join(line.split()[1:])) and not confirm(f"Run {line}?"):
            continue
        dispatch_command(line, agent)


def run_bare_in_terminal(agent: Any, command: Any) -> Any:
    """A bare own-menu or report command in a terminal: run the handler; when it showed no menu of
    its own (a report, an empty list), finish with the "What next?" menu."""
    from axon.ui.menus import menus_started
    before = menus_started()
    result = command.handler(agent, "")
    if command.name not in VIEWERS and menus_started() == before:
        if next_menu_enabled():
            run_next_menu(agent, command)
        else:
            print_footer_hint(command)
    return result


def next_menu_enabled() -> bool:
    """The "What next?" menu after a report is off: one footer hint replaces it. Turn it back on with
    ``AXON_NEXT_MENU=1`` or ``"nextMenu": true`` in ~/.axon/settings.json."""
    import os
    env = os.environ.get("AXON_NEXT_MENU", "").strip().lower()
    if env:
        return env in ("1", "true", "yes", "on")
    try:
        from axon.commands.common import read_settings_file, settings_file
        data = read_settings_file(settings_file("user")) or {}
        return bool(data.get("nextMenu", False))
    except Exception:
        return False


def footer_hint(command: Any) -> str:
    """One dim line naming the related views (no slashes: a hint is not a prompt to type)."""
    names: list[str] = []
    for st in next_steps(command):
        word = st.line.split()[0].lstrip("/") if st.line.split() else ""
        if word and word != command.name and word not in names:
            names.append(word)
    if "usage" not in names and command.name in {"cost", "stats", "status", "context", "budget", "window", "autocompact", "pricing"}:
        names.insert(0, "usage")
    return ("related: " + " · ".join(names[:5])) if names else ""


def print_footer_hint(command: Any) -> None:
    text = footer_hint(command)
    if text:
        from axon.ui.theme import DARK_SLATE, RST, term_width, truncate
        print(truncate(f"  {DARK_SLATE}{text}{RST}", term_width() - 1) + "\n")


def bare_menu_runner(command: Any) -> Any:
    """What a bare ``/command`` runs in a terminal: a function of the agent, or None for the plain
    handler (no terminal, commands with no menu)."""
    from axon.commands.common import is_tty
    if not is_tty():
        return None
    kind = menu_kind(command.name)
    if kind in ("usage", "action"):
        return lambda agent: run_usage_menu(agent, command)
    if kind in ("own", "report"):
        return lambda agent: run_bare_in_terminal(agent, command)
    return None
