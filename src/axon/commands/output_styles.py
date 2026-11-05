"""
Output styles: named system-prompt additions that change how Axon writes its answers.

Built-ins: ``default`` (no addition), ``proactive``, ``concise``, ``explanatory`` and ``learning``.
Custom styles are Markdown files in the Claude Code format::

    ---
    name: Explanatory
    description: Explains the reasoning behind each change
    keep-coding-instructions: true
    ---
    <style instructions>

``keep-coding-instructions`` defaults to false for a custom style (the standard software-engineering
rules are left out unless it is ``true``); the built-ins keep them. A plugin style may set
``force-for-plugin: true`` to apply whenever the plugin is enabled (it overrides the user's choice; the
first such style loaded wins).

Search order (later wins on a name clash): built-in, plugins (``output-styles/`` or the manifest
``outputStyles`` paths), ``~/.claude/output-styles``, ``~/.axon/output-styles``, then project
``.claude/output-styles`` / ``.axon/output-styles`` of every directory from the repository root down to
the workspace (the one nearest the workspace wins). The active style is ``settings.output_style``
(``outputStyle`` in settings.json, matched ignoring case); the system prompt gets
:func:`active_style_prompt`. Files are re-read on every call, so edits apply from the next message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Style:
    name: str
    description: str
    prompt: str | None
    source: str = "built-in"
    keep_coding_instructions: bool = True
    path: Path | None = None
    force_for_plugin: bool = False


_BUILTINS: tuple[Style, ...] = (
    Style("default", "Axon's standard behaviour: concise, task-focused software engineering answers", None),
    Style(
        "explanatory", "Completes the task and explains the reasoning, trade-offs and patterns behind it",
        "# Output style: Explanatory\n"
        "You still complete the user's task fully, but you also teach while you work:\n"
        "- Before a non-trivial change, say in one or two sentences what you will do and why this approach "
        "beats the obvious alternatives.\n"
        "- After meaningful changes add a short \"Insight\" section (2-4 bullets) about the pattern, API or "
        "trade-off involved, tied to the code just written. Skip it for trivial edits.\n"
        "- Name the relevant concepts so the user can look them up. Keep explanations proportional to the work.",
    ),
    Style(
        "learning", "Collaborative learn-by-doing: asks you to write small, meaningful pieces yourself",
        "# Output style: Learning\n"
        "The user wants to build skill, not only get results. Work collaboratively:\n"
        "- Do the scaffolding and boilerplate yourself, but for 1-2 small, meaningful pieces of logic per task "
        "(a condition, a function body of 2-10 lines, a design choice) stop and ask the user to write them. "
        "Mark the spot in the code with a `TODO(human)` comment and explain exactly what is needed and why.\n"
        "- Share short \"Insight\" notes about the concepts involved.\n"
        "- When the user's contribution arrives, review it kindly and precisely, then continue.",
    ),
    Style(
        "proactive", "Starts work right away and makes reasonable assumptions instead of asking about routine decisions",
        "# Output style: Proactive\n"
        "Start implementing as soon as the user sends a task. Make reasonable assumptions about routine decisions "
        "instead of stopping to ask, and do not switch to plan mode unless the user asks for a plan. The user can "
        "redirect you at any point.\n"
        "Before an action that deletes data or changes a shared or production system, check with the user in the "
        "conversation first. That check is an instruction you follow and is separate from permission prompts.",
    ),
    Style(
        "concise", "Minimal prose: short answers, no preamble or recap, code and facts only",
        "# Output style: Concise\n"
        "The first sentence of a response states what happened or what the answer is. Leave out the lead-in, the "
        "step-by-step narration and the closing recap; answer a simple question in one to three sentences. Do the "
        "engineering work as thoroughly as usual. Still write at full length when the user asks for an explanation "
        "or more detail, and keep error reports, failing test output, security warnings and confirmations for "
        "destructive actions complete.",
    ),
)

_TRUE = {"true", "yes", "1", "on"}


def _git_root(start: Path) -> Path | None:
    cur = start
    for _ in range(64):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _style_dirs(workspace: Path | None) -> list[tuple[Path, str]]:
    """User then project directories, lowest precedence first."""
    dirs: list[tuple[Path, str]] = [(Path.home() / ".claude" / "output-styles", "user")]
    try:
        from axon.storage import user_ext_dir
        dirs.append((user_ext_dir("output-styles"), "user"))
    except Exception:
        pass
    if workspace is not None:
        try:
            ws = Path(workspace).resolve()
        except OSError:
            ws = Path(workspace)
        root = _git_root(ws)
        chain = [ws]
        if root is not None:
            cur = ws
            while cur != root and cur.parent != cur:
                cur = cur.parent
                chain.append(cur)
        for d in reversed(chain):  # repository root first, the workspace last (nearest wins)
            dirs.append((d / ".claude" / "output-styles", "project"))
            dirs.append((d / ".axon" / "output-styles", "project"))
    return dirs


def _plugin_style_files(workspace: Path | None) -> list[tuple[Path, str]]:
    try:
        if workspace is None:
            return []
        from axon.commands.plugins import enabled_plugins, plugin_output_style_files
        return [(f, f"plugin:{p.name}") for p in enabled_plugins(Path(workspace)) for f in plugin_output_style_files(p)]
    except Exception:
        return []


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-")


def parse_style_file(path: Path, source: str) -> Style | None:
    """Parse one style file (frontmatter optional). Returns None when unreadable or empty."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        from axon.skills.manager import parse_frontmatter
        meta, body = parse_frontmatter(text)
    except Exception:
        meta, body = {}, text
    body = body.strip()
    if not body:
        return None
    raw_name = str(meta.get("name") or path.stem)
    desc = str(meta.get("description") or "").strip()
    if not desc:
        desc = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")[:100]
    keep = str(meta.get("keep-coding-instructions", meta.get("keep_coding_instructions", "false"))).strip().lower() in _TRUE
    force = source.startswith("plugin:") and str(meta.get("force-for-plugin", meta.get("force_for_plugin", "false"))).strip().lower() in _TRUE
    return Style(name=_slug(raw_name) or path.stem.lower(), description=desc, prompt=body, source=source,
                 keep_coding_instructions=keep, path=Path(path), force_for_plugin=force)


def list_styles(workspace: Path | None) -> list[Style]:
    """Every available style: built-ins first, then plugin, user and project files (later wins)."""
    by_name: dict[str, Style] = {s.name: s for s in _BUILTINS}
    for f, source in _plugin_style_files(workspace):
        st = parse_style_file(f, source)
        if st is not None:
            by_name[st.name] = st
    for d, source in _style_dirs(workspace):
        if not d.is_dir():
            continue
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            continue
        for f in files:
            st = parse_style_file(f, source)
            if st is not None:
                by_name[st.name] = st
    return list(by_name.values())


def forced_plugin_style(workspace: Path | None) -> Style | None:
    """The first enabled-plugin style with ``force-for-plugin: true`` (overrides the user's choice)."""
    for f, source in _plugin_style_files(workspace):
        st = parse_style_file(f, source)
        if st is not None and st.force_for_plugin:
            return st
    return None


def get_style(name: str, workspace: Path | None) -> Style | None:
    key = _slug(name or "")
    for st in list_styles(workspace):
        if st.name == key:
            return st
    return None


def active_style_name(settings: Any) -> str:
    """The style in force: a plugin's ``force-for-plugin`` style, else ``settings.output_style``."""
    ws = getattr(settings, "workspace", None)
    forced = forced_plugin_style(Path(ws)) if ws is not None else None
    if forced is not None:
        return forced.name
    return str(getattr(settings, "output_style", None) or "default").strip() or "default"


def active_style_prompt(settings: Any, workspace: Path | None = None) -> str | None:
    """System-prompt text for the active style (None for ``default`` or an unknown style)."""
    ws = workspace if workspace is not None else getattr(settings, "workspace", None)
    st = get_style(active_style_name(settings), Path(ws) if ws is not None else None)
    return st.prompt if st is not None else None


def create_style(name: str, description: str, prompt: str, workspace: Path | None, scope: str = "user") -> Path:
    """Write a new style file (user scope by default). Raises FileExistsError when it exists."""
    slug = _slug(name)
    if not slug:
        raise ValueError("style name must contain letters or digits")
    if scope == "project" and workspace is not None:
        root = Path(workspace) / ".axon" / "output-styles"
    else:
        from axon.storage import user_ext_dir
        root = user_ext_dir("output-styles")
    path = root / f"{slug}.md"
    if path.exists():
        raise FileExistsError(str(path))
    body = prompt.strip() or f"# Output style: {name}\nDescribe how responses should be written."
    text = (f"---\nname: {slug}\ndescription: {' '.join(description.split()) or slug}\n"
            f"keep-coding-instructions: true\n---\n\n{body}\n")
    from axon.session.store import atomic_write_text
    atomic_write_text(path, text)
    return path
