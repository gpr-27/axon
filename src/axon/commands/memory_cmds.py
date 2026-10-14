"""Memory & project commands: /memory /learn #quick-memory /init /prompt /optimize."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from axon.storage import axon_home as _axon_home
from axon.commands.common import (
    ask_text, confirm, err, estimate_tokens, fmt_tokens, header, hint, info, is_tty, ok, one_shot,
    open_in_editor, section, usage, ws_of,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, RST, SLATE, TEAL, WHITE

CAT = "Memory & Project"


def _store(agent: Any) -> Any:
    from axon.agent.memory import MemoryStore
    return MemoryStore(ws_of(agent))


def _item_path(store: Any, item: Any) -> Path:
    base = store.global_memory_dir if item.scope == "global" else store.project_memory_dir
    return Path(base) / f"{item.id}.md"


def _find_item(items: list[Any], key: str) -> Any:
    key = key.strip()
    if key.isdigit():
        i = int(key) - 1
        return items[i] if 0 <= i < len(items) else None
    k = key.lower().removesuffix(".md")
    for it in items:
        if it.id.lower() == k or it.title.lower() == k:
            return it
    matches = [it for it in items if it.id.lower().startswith(k)]
    return matches[0] if len(matches) == 1 else None


def _instruction_files(ws: Path) -> list[tuple[str, Path]]:
    Path.home()
    out = [("user", _axon_home() / "AXON.md"), ("project", ws / "AXON.md"), ("local", ws / "AXON.local.md")]
    for name in ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", ".axon/AXON.md"):
        p = ws / name
        if p.exists():
            out.append(("project", p))
    rules = ws / ".axon" / "rules"
    if rules.is_dir():
        out += [("rule", p) for p in sorted(rules.glob("*.md"))]
    return out


def add_memory(agent: Any, text: str, scope: str, distill: bool) -> None:
    store = _store(agent)
    item = None
    if distill and getattr(agent, "provider", None) is not None:
        try:
            from axon.agent.memory import distill_and_learn
            print(f"\n  {TEAL}🧠 Distilling memory…{RST}")
            item = distill_and_learn(agent.provider, text, ws_of(agent), scope=scope,
                                     ledger=getattr(agent, "ledger", None), model=agent.settings.model)
        except TypeError:
            from axon.agent.memory import distill_and_learn
            item = distill_and_learn(agent.provider, text, ws_of(agent), scope=scope, ledger=getattr(agent, "ledger", None))
        except Exception:
            item = None
    if item is None:
        item = store.add(text, scope=scope) if hasattr(store, "add") else store.learn(text, scope=scope)
    where = "~/.axon/memory" if item.scope == "global" else ".axon/memory"
    badge = f"{GOLD}[user]{RST}" if item.scope == "global" else f"{TEAL}[project]{RST}"
    ok(f"Remembered {badge} {BOLD}{item.title}{RST} {SLATE}→ {where}/{item.id}.md{RST}")


def quick_memory(agent: Any, text: str) -> Any:
    """``#text`` shortcut: save a note to project memory (``#global text`` for user memory)."""
    from axon.commands.registry import CommandResult
    t = text.strip()
    scope = "project"
    for pfx in ("global ", "user ", "--global "):
        if t.lower().startswith(pfx):
            scope, t = "global", t[len(pfx):].strip()
            break
    if not t:
        usage("#<note to remember>   (#global <note> for all projects)")
        return CommandResult(handled=True)
    add_memory(agent, t, scope, distill=False)
    return CommandResult(handled=True)


MEMORY_USAGE = """/memory                       list instruction files and saved memories
/memory add [--global] <text>  save a memory (LLM-distilled)
/memory edit <user|project|local|n|id> [new text]
/memory view <n|id>            /memory remove <n|id>
/memory search <text>          /memory clear
#<text>                        quick note (no LLM)"""


@command("memory", category=CAT, description="Instruction files (AXON.md …) and saved memories",
         aliases=("mem",), usage=MEMORY_USAGE)
def handle_memory(agent: Any, arg: str) -> CommandResult:
    ws = ws_of(agent)
    store = _store(agent)
    items = store.list_all()
    a = arg.strip()
    sub, _, rest = a.partition(" ")
    sub = sub.lower()
    rest = rest.strip()

    if sub in ("add", "learn", "+"):
        scope = "project"
        for pfx in ("--global ", "global ", "--user ", "user "):
            if rest.startswith(pfx):
                scope, rest = "global", rest[len(pfx):].strip()
        if not rest:
            usage("/memory add [--global] <text>")
            return CommandResult(handled=True)
        add_memory(agent, rest, scope, distill=True)
        return CommandResult(handled=True)

    if sub in ("remove", "delete", "rm", "del", "forget"):
        it = _find_item(items, rest) if rest else None
        if it is None:
            err(f"No memory matching '{rest}'. /memory lists them with numbers.")
            return CommandResult(handled=True)
        if store.delete(it.id):
            ok(f"Deleted memory {BOLD}{it.title}{RST} {SLATE}({it.id}){RST}")
        else:
            err(f"Could not delete {it.id}")
        return CommandResult(handled=True)

    if sub in ("view", "show", "read", "cat"):
        it = _find_item(items, rest) if rest else None
        if it is None:
            err(f"No memory matching '{rest}'.")
            return CommandResult(handled=True)
        header(it.title, f"{it.scope} · {it.category} · {_item_path(store, it)}")
        for ln in it.content.splitlines():
            print(f"    {WHITE}{ln}{RST}")
        print()
        return CommandResult(handled=True)

    if sub in ("edit", "open"):
        target, _, new_text = rest.partition(" ")
        files = {label: p for label, p in _instruction_files(ws) if label in ("user", "project", "local")}
        if target in files or not target:
            p = files.get(target or "project")
            if p is None:
                return CommandResult(handled=True)
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"# {'User' if target == 'user' else ws.name} instructions\n\n", encoding="utf-8")
                if target == "local":
                    _gitignore_add(ws, "AXON.local.md")
            if is_tty():
                open_in_editor(p)
            else:
                info(f"Edit {p} in your editor.")
            return CommandResult(handled=True)
        it = _find_item(items, target)
        if it is None:
            err(f"No memory or file '{target}'. Use user, project, local, a number or an id.")
            return CommandResult(handled=True)
        path = _item_path(store, it)
        if new_text.strip():
            _replace_body(path, new_text.strip())
            ok(f"Updated memory {BOLD}{it.title}{RST}")
        elif is_tty():
            open_in_editor(path)
        else:
            usage("/memory edit <n|id> <new text>")
        return CommandResult(handled=True)

    if sub in ("search", "find", "grep"):
        hits = store.search(rest) if rest else []
        header("Memory search", f"'{rest}' · {len(hits)} hit(s)")
        for it in hits:
            print(f"    {CYAN}{it.id}{RST} {WHITE}{it.title}{RST} {SLATE}({it.scope}){RST}")
        print()
        return CommandResult(handled=True)

    if sub == "clear":
        n = sum(1 for it in items if it.scope == "project")
        if n and confirm(f"Delete all {n} project memories?", default=not is_tty()):
            store.clear()
            ok(f"Cleared {n} project memories")
        else:
            info("Nothing cleared.")
        return CommandResult(handled=True)

    if sub:
        usage(MEMORY_USAGE)
        return CommandResult(handled=True)

    if is_tty() and memory_picker(agent, items):
        return CommandResult(handled=True)

    header("Memory", "loaded into every prompt")
    section("Instruction files")
    for label, p in _instruction_files(ws):
        if p.exists():
            try:
                size = estimate_tokens(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                size = 0
            print(f"    {MINT}●{RST} {SLATE}{label:<8}{RST} {WHITE}{p}{RST} {DARK_SLATE}~{fmt_tokens(size)} tok{RST}")
        else:
            print(f"    {DARK_SLATE}○ {label:<8} {p}  (not created · /memory edit {label}){RST}")
    for title, scope, color in (("Project memories (.axon/memory)", "project", TEAL), ("User memories (~/.axon/memory)", "global", GOLD)):
        rows = [(i, it) for i, it in enumerate(items, 1) if it.scope == scope]
        if rows:
            section(f"{title} · {len(rows)}", color)
            for i, it in rows:
                body = " ".join(it.content.split())
                print(f"    {CYAN}{i:>2}{RST} {WHITE}{it.title}{RST} {SLATE}{body[:60]}{'…' if len(body) > 60 else ''}{RST}")
    if not items:
        print(f"\n    {SLATE}No saved memories. Type {WHITE}#<note>{SLATE} to remember something.{RST}")
    print()
    hint("    /memory add · edit · view · remove · search · clear   (/help memory)")
    print()
    return CommandResult(handled=True)


def _ensure_instruction_file(ws: Path, label: str, p: Path) -> None:
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {'User' if label == 'user' else ws.name} instructions\n\n", encoding="utf-8")
    if label == "local":
        _gitignore_add(ws, "AXON.local.md")


def memory_picker(agent: Any, items: list[Any]) -> bool:
    """Interactive memory editor: choose an instruction file or saved memory and open it in $EDITOR
    (axon.platform.editor_command). Also toggles auto memory and opens its folder. False when cancelled."""
    from axon.ui.picker import SelectItem, Selector
    ws = ws_of(agent)
    store = _store(agent)
    entries: list[SelectItem] = []
    for label, p in _instruction_files(ws):
        entries.append(SelectItem(label=f"{label:<8} {p}", value=("file", label, p), group="Instruction files",
                                  hint="" if p.exists() else "create", badge=f"{MINT}●{RST}" if p.exists() else "○"))
    for it in items:
        entries.append(SelectItem(label=it.title[:60], value=("mem", it.scope, _item_path(store, it)),
                                  group="Saved memories", hint=it.scope, search=it.content[:200]))
    auto = bool(getattr(agent.settings, "auto_memory", True))
    entries.append(SelectItem(label=f"Auto memory: {'on' if auto else 'off'}", value=("toggle", "", None),
                              group="Auto memory", hint="Enter toggles"))
    try:
        from axon import storage
        mdir = storage.memory_dir(ws)
        entries.append(SelectItem(label=f"Open auto-memory folder {mdir}", value=("dir", "", mdir), group="Auto memory"))
    except Exception:
        pass
    chosen = Selector(entries, title="Memory", subtitle="Enter opens in your editor", filterable=True, max_visible=14).run()
    if chosen is None:
        return False
    kind, label, path = chosen.value
    if kind == "toggle":
        from axon.commands.common import update_settings, update_settings_file
        update_settings(agent, auto_memory=not auto)
        saved = update_settings_file({"autoMemoryEnabled": not auto})
        ok(f"Auto memory {'on' if not auto else 'off'}" + (f" {SLATE}(saved to {saved}){RST}" if saved else ""))
        return True
    if kind == "file":
        _ensure_instruction_file(ws, label, path)
    if kind == "dir":
        _open_folder(Path(path))
        return True
    open_in_editor(Path(path))
    if hasattr(agent, "reload_memory"):
        try:
            agent.reload_memory()
        except Exception:
            pass
    ok(f"Saved {path}")
    return True


def _open_folder(path: Path) -> None:
    import subprocess
    import sys as _sys
    path.mkdir(parents=True, exist_ok=True)
    try:
        if _sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif _sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info(f"Opened {path}")
    except (OSError, AttributeError):
        info(f"Auto-memory folder: {path}")


def _replace_body(path: Path, text: str) -> None:
    from axon.session.store import atomic_write_text
    raw = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    head = ""
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            head = raw[: end + 4] + "\n\n"
    atomic_write_text(path, f"{head}{text}\n")


def _gitignore_add(ws: Path, entry: str) -> None:
    gi = ws / ".gitignore"
    try:
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        if entry not in (ln.strip() for ln in lines):
            with open(gi, "a", encoding="utf-8") as f:
                if lines and lines[-1].strip():
                    f.write("\n")
                f.write(f"{entry}\n")
    except OSError:
        pass


@command("learn", category=CAT, description="Save a memory (same as /memory add)", aliases=("remember",),
         usage="/learn [--global] <rule or fact>")
def handle_learn(agent: Any, arg: str) -> CommandResult:
    if not arg.strip():
        usage("/learn [--global] <rule or fact>")
        return CommandResult(handled=True)
    return handle_memory(agent, f"add {arg.strip()}")


# ─── /init ──────────────────────────────────────────────────────────────────

_IGNORE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".next",
                ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode", ".axon", ".claude", "coverage"}
_LANG = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
         ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".cs": "C#",
         ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C/C++", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell",
         ".ps1": "PowerShell", ".dart": "Dart", ".ex": "Elixir", ".lua": "Lua", ".vue": "Vue", ".svelte": "Svelte"}
_IMPORTS = [".cursorrules", ".cursor/rules", ".github/copilot-instructions.md", ".windsurfrules", ".windsurf/rules", ".clinerules", "AGENTS.md", "CLAUDE.md"]


def analyze_repo(ws: Path) -> dict[str, Any]:
    """Deterministic repository survey used by /init."""
    langs: dict[str, int] = {}
    n_files = 0
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
        for f in files:
            n_files += 1
            lang = _LANG.get(Path(f).suffix.lower())
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
        if n_files > 20000:
            break
    cmds: list[tuple[str, str]] = []
    info_: dict[str, Any] = {"languages": sorted(langs, key=lambda k: -langs[k])[:5], "files": n_files, "commands": cmds}
    pj = ws / "package.json"
    if pj.is_file():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            mgr = "pnpm" if (ws / "pnpm-lock.yaml").exists() else "yarn" if (ws / "yarn.lock").exists() else "npm"
            for k in ("dev", "build", "test", "lint", "typecheck", "format", "start"):
                if k in (data.get("scripts") or {}):
                    cmds.append((k, f"{mgr} run {k}" if k != "test" else f"{mgr} test"))
        except (OSError, ValueError):
            pass
    py = ws / "pyproject.toml"
    if py.is_file():
        try:
            from axon.commands.common import _toml_load
            data = _toml_load(py)
            tools = data.get("tool", {}) if isinstance(data, dict) else {}
            if "pytest" in tools or (ws / "tests").is_dir():
                cmds.append(("test", "python -m pytest -q"))
            if "ruff" in tools:
                cmds.append(("lint", "ruff check ."))
            if "mypy" in tools:
                cmds.append(("typecheck", "mypy ."))
            if "black" in tools:
                cmds.append(("format", "black ."))
            info_["python_project"] = (data.get("project") or {}).get("name", "")
        except Exception:
            pass
    elif (ws / "requirements.txt").is_file() and (ws / "tests").is_dir():
        cmds.append(("test", "python -m pytest -q"))
    if (ws / "Cargo.toml").is_file():
        cmds += [("build", "cargo build"), ("test", "cargo test"), ("lint", "cargo clippy")]
    if (ws / "go.mod").is_file():
        cmds += [("build", "go build ./..."), ("test", "go test ./..."), ("lint", "go vet ./...")]
    mk = ws / "Makefile"
    if mk.is_file():
        try:
            targets = re.findall(r"^([A-Za-z][\w-]*):(?!=)", mk.read_text(encoding="utf-8", errors="replace"), re.M)
            for t in targets[:8]:
                cmds.append((t, f"make {t}"))
        except OSError:
            pass
    info_["top_dirs"] = sorted(p.name for p in ws.iterdir() if p.is_dir() and p.name not in _IGNORE_DIRS and not p.name.startswith("."))[:20] if ws.is_dir() else []
    wf = ws / ".github" / "workflows"
    info_["ci"] = sorted(p.name for p in wf.glob("*.y*ml")) if wf.is_dir() else []
    imports: list[tuple[str, str]] = []
    for rel in _IMPORTS:
        p = ws / rel
        if p.is_file():
            imports.append((rel, p.read_text(encoding="utf-8", errors="replace")[:3000]))
        elif p.is_dir():
            for f in sorted(p.glob("*.md*"))[:10]:
                imports.append((f"{rel}/{f.name}", f.read_text(encoding="utf-8", errors="replace")[:2000]))
    info_["imports"] = imports
    return info_


def render_axon_md(ws: Path, info_: dict[str, Any]) -> str:
    seen: set[str] = set()
    cmd_lines = []
    for k, c in info_["commands"]:
        if c not in seen:
            seen.add(c)
            cmd_lines.append(f"- {k}: `{c}`")
    parts = [
        f"# {ws.name}",
        "",
        "Project instructions for AI coding agents (Axon reads this file every session).",
        "",
        "## Overview",
        f"- Languages: {', '.join(info_['languages']) or 'unknown'}",
    ]
    if info_["top_dirs"]:
        parts.append(f"- Top-level directories: {', '.join(f'`{d}/`' for d in info_['top_dirs'])}")
    if info_["ci"]:
        parts.append(f"- CI workflows: {', '.join(info_['ci'])}")
    parts += ["", "## Commands"] + (cmd_lines or ["- (add build / test / lint commands here)"])
    parts += [
        "",
        "## Conventions",
        "- Follow the existing code style and structure of neighbouring files.",
        "- Run the test command after changes and keep it passing.",
        "- Keep changes minimal and focused on the task.",
    ]
    for rel, text in info_["imports"]:
        if rel in ("AGENTS.md", "CLAUDE.md"):
            parts += ["", f"## Imported from {rel}", "", f"@{rel}"]
        else:
            parts += ["", f"## Imported from {rel}", "", text.strip()]
    return "\n".join(parts).rstrip() + "\n"


@command("init", category=CAT, description="Create AXON.md from a repo analysis (no overwrite)",
         usage="/init            create AXON.md (or suggest improvements if it exists)\n/init local      create a personal, git-ignored AXON.local.md\n/init --force    regenerate AXON.md after confirmation")
def handle_init(agent: Any, arg: str) -> CommandResult:
    ws = ws_of(agent)
    a = arg.strip().lower()
    if a in ("local", "--local", "personal"):
        p = ws / "AXON.local.md"
        if p.exists():
            info(f"{p.name} already exists. /memory edit local opens it.")
            return CommandResult(handled=True)
        p.write_text(f"# Personal notes for {ws.name}\n\nPreferences that apply only to you (not committed).\n", encoding="utf-8")
        _gitignore_add(ws, "AXON.local.md")
        ok(f"Created {p} and added it to .gitignore")
        return CommandResult(handled=True)

    info_ = analyze_repo(ws)
    target = ws / "AXON.md"
    existing = next((ws / n for n in ("AXON.md", "axon.md") if (ws / n).is_file()), None)
    if existing is not None and a != "--force":
        text = existing.read_text(encoding="utf-8", errors="replace")
        header(f"{existing.name} already exists", "suggestions (nothing was changed)")
        missing = [c for _, c in info_["commands"] if c not in text]
        tips = []
        if missing:
            tips.append(f"Document these commands: {', '.join(f'`{c}`' for c in dict.fromkeys(missing))}")
        for rel, _ in info_["imports"]:
            if rel not in text:
                tips.append(f"Import or summarise {rel} (e.g. add `@{rel}`)")
        tok = estimate_tokens(text)
        if tok > 4000:
            tips.append(f"It is ~{fmt_tokens(tok)} tokens; trim rarely-needed detail to save context every turn")
        if "## " not in text:
            tips.append("Add sections (## Commands, ## Conventions) so the model can scan it")
        for t in tips or ["Looks complete. /init --force regenerates it after confirmation."]:
            print(f"    {GOLD}•{RST} {WHITE}{t}{RST}")
        print()
        return CommandResult(handled=True)
    content = render_axon_md(ws, info_)
    if existing is not None:
        if not confirm(f"Overwrite {existing.name} with a regenerated version?", default=False):
            info("Kept the existing file.")
            return CommandResult(handled=True)
        target = existing
    from axon.session.store import atomic_write_text
    atomic_write_text(target, content)
    ok(f"Created {BOLD}{target.name}{RST} {SLATE}({len(content.splitlines())} lines · {len(info_['commands'])} commands detected){RST}")
    hint("    Review it, then ask the agent to refine it: \"improve AXON.md with what you know about this repo\"")
    print()
    return CommandResult(handled=True)


# ─── Prompt library ─────────────────────────────────────────────────────────

BUILTIN_PROMPTS: dict[str, tuple[str, str]] = {
    "overview": ("Explain the codebase", "Give me an overview of this codebase: purpose, architecture, main modules, data flow and how to run it. Cite key files."),
    "trace": ("Trace a feature", "Trace how {feature} works end to end in this codebase. List the files and functions involved in order, with file:line references."),
    "impact": ("What would break", "What would break if I deleted or changed {target}? Find every usage, explain the impact and how to do it safely."),
    "tests": ("Write and run tests", "Write tests for {path}, run them, and fix any failures. Cover edge cases and error paths."),
    "bugfix": ("Fix a bug", "Investigate and fix this bug: {description}. Reproduce it first, explain the root cause, apply a minimal fix and verify it with a test."),
    "refactor": ("Safe refactor", "Refactor {target} to {goal} without changing behaviour. Keep tests passing and match the repository's conventions."),
    "review": ("Review changes", "Review my uncommitted changes for bugs, security issues and missing tests. Report by severity with file:line references."),
    "docs": ("Document code", "Write clear documentation for {target}: purpose, usage examples, parameters and pitfalls."),
    "commit": ("Commit message", "Look at the staged changes (git diff --cached) and write a conventional commit message. Do not commit."),
}
_PH = re.compile(r"\{([A-Za-z_][A-Za-z0-9_ -]*)\}")


def prompt_dirs(ws: Path) -> list[Path]:
    return [_axon_home() / "prompts", ws / ".axon" / "prompts"]


def load_prompts(ws: Path) -> dict[str, tuple[str, str, str]]:
    """name -> (description, template, source)."""
    out = {k: (d, t, "built-in") for k, (d, t) in BUILTIN_PROMPTS.items()}
    from axon.skills.manager import parse_frontmatter
    for d in prompt_dirs(ws):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                fm, body = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            desc = fm.get("description") if isinstance(fm.get("description"), str) else ""
            out[f.stem] = (str(desc or body.splitlines()[0][:60] if body else ""), body.strip(), str(f))
    return out


def fill_placeholders(template: str, given: dict[str, str], ask: bool) -> str | None:
    names = list(dict.fromkeys(m.group(1) for m in _PH.finditer(template)))
    values = dict(given)
    for n in names:
        if n not in values:
            if not ask:
                return None
            v = ask_text(f"{n}:")
            if not v:
                return None
            values[n] = v
    return _PH.sub(lambda m: values.get(m.group(1), m.group(0)), template)


@command("prompt", category=CAT, description="Prompt library: templates with {placeholders}",
         aliases=("prompts", "template", "templates"),
         usage="/prompt                         list templates\n/prompt <name> [key=value ...]  fill placeholders (asks for missing ones) and send\n/prompt show <name>\n/prompt save <name> [--global] <template text>")
def handle_prompt(agent: Any, arg: str) -> CommandResult:
    ws = ws_of(agent)
    prompts = load_prompts(ws)
    a = arg.strip()
    sub, _, rest = a.partition(" ")
    if not a or sub.lower() in ("list", "ls"):
        header("Prompt library", f"{len(prompts)} templates · ~/.axon/prompts and .axon/prompts")
        for name, (desc, tpl, src) in sorted(prompts.items()):
            phs = ", ".join(dict.fromkeys(m.group(1) for m in _PH.finditer(tpl)))
            print(f"    {CYAN}{name:<12}{RST} {WHITE}{desc[:44]:<44}{RST} {DARK_SLATE}{('{' + phs + '}') if phs else ''}{RST}")
        hint("\n    /prompt <name> key=value · /prompt save <name> <text>")
        print()
        return CommandResult(handled=True)
    if sub.lower() == "show":
        p = prompts.get(rest.strip())
        if not p:
            err(f"No template '{rest.strip()}'.")
            return CommandResult(handled=True)
        header(rest.strip(), p[2])
        print(f"    {WHITE}{p[1]}{RST}\n")
        return CommandResult(handled=True)
    if sub.lower() == "save":
        words = rest.split()
        glob = "--global" in words
        words = [w for w in words if w != "--global"]
        if len(words) < 2:
            usage("/prompt save <name> [--global] <template text with {placeholders}>")
            return CommandResult(handled=True)
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", words[0]).strip("-").lower()
        d = prompt_dirs(ws)[0 if glob else 1]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text(" ".join(words[1:]) + "\n", encoding="utf-8")
        ok(f"Saved template {BOLD}{name}{RST} → {d / (name + '.md')}")
        return CommandResult(handled=True)
    p = prompts.get(sub)
    if p is None:
        err(f"No template '{sub}'. /prompt lists them.")
        return CommandResult(handled=True)
    given: dict[str, str] = {}
    free: list[str] = []
    for tok in rest.split():
        k, eq, v = tok.partition("=")
        if eq:
            given[k] = v
        else:
            free.append(tok)
    names = list(dict.fromkeys(m.group(1) for m in _PH.finditer(p[1])))
    if free and names:
        pending = [n for n in names if n not in given]
        if len(pending) == 1:
            given[pending[0]] = " ".join(free)
    text = fill_placeholders(p[1], given, ask=is_tty())
    if text is None:
        err(f"Missing values for: {', '.join(n for n in names if n not in given)} (use key=value)")
        return CommandResult(handled=True)
    print(f"\n  {TEAL}▸ /prompt {sub}{RST}  {SLATE}{text[:100]}{'…' if len(text) > 100 else ''}{RST}\n")
    agent.run_turn(text)
    return CommandResult(handled=True)


_OPT_SYSTEM = "You are a prompt engineer for an autonomous coding agent with file, search and shell tools. Return only the improved prompt."


@command("optimize", category=CAT, description="Rewrite a rough request into a precise agent prompt",
         aliases=("enhance",), usage="/optimize <rough request>")
def handle_optimize(agent: Any, arg: str) -> CommandResult:
    rough = arg.strip()
    if not rough:
        usage("/optimize <rough request>")
        return CommandResult(handled=True)
    ask = ("Rewrite this request into a clear prompt for a coding agent: objective and scope, files or areas to "
           "inspect first, constraints, and how to verify the result (tests/build). 2-5 sentences.\n\n"
           f"Request:\n{rough}")
    try:
        better = one_shot(agent, _OPT_SYSTEM, ask, max_tokens=600, source="prompt_enhancement").strip().strip('"')
    except Exception as e:
        err(f"Optimizer unavailable: {e}")
        return CommandResult(handled=True)
    if not better:
        err("The model returned nothing.")
        return CommandResult(handled=True)
    header("Optimized prompt")
    import textwrap
    for ln in textwrap.wrap(better, 92):
        print(f"    {WHITE}{ln}{RST}")
    print()
    if is_tty() and confirm("Send it now?", default=False):
        agent.run_turn(better)
    else:
        hint("    copy it, or /queue it")
        print()
    return CommandResult(handled=True)
