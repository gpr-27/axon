"""
Output styles: named system-prompt additions that change how Axon writes its answers.

Built-ins: ``default`` (no addition), ``explanatory``, ``learning`` and ``concise``.
Custom styles are Markdown files in the Claude Code format::

    ---
    name: Explanatory
    description: Explains the reasoning behind each change
    keep-coding-instructions: true
    ---
    <style instructions>

Search order (later wins on a name clash): built-in, ``~/.axon/output-styles``,
``<ws>/.claude/output-styles``, ``<ws>/.axon/output-styles``. Plugins may ship an
``output-styles/`` folder too. The active style is ``settings.output_style``;
P-CORE injects :func:`active_style_prompt` into the system prompt.
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
        "concise", "Minimal prose: short answers, no preamble or recap, code and facts only",
        "# Output style: Concise\n"
        "Answer with the minimum words needed. No preamble, no restating the question, no closing summary. "
        "Prefer bullet points and code over paragraphs. Report results in one or two lines unless the user "
        "asks for detail.",
    ),
)

_TRUE = {"true", "yes", "1", "on"}


def _style_dirs(workspace: Path | None) -> list[tuple[Path, str]]:
    dirs: list[tuple[Path, str]] = []
    try:
        from axon.storage import user_ext_dir
        dirs.append((user_ext_dir("output-styles"), "user"))
    except Exception:
        pass
    if workspace is not None:
        ws = Path(workspace)
        dirs.append((ws / ".claude" / "output-styles", "project"))
        dirs.append((ws / ".axon" / "output-styles", "project"))
    return dirs


def _plugin_style_dirs(workspace: Path | None) -> list[tuple[Path, str]]:
    try:
        if workspace is None:
            return []
        from axon.commands.plugins import load_plugins
        return [(Path(p.path) / "output-styles", f"plugin:{p.name}") for p in load_plugins(Path(workspace)) if p.enabled]
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
    keep = str(meta.get("keep-coding-instructions", meta.get("keep_coding_instructions", "true"))).strip().lower() in _TRUE
    return Style(name=_slug(raw_name) or path.stem.lower(), description=desc, prompt=body, source=source,
                 keep_coding_instructions=keep, path=Path(path))


def list_styles(workspace: Path | None) -> list[Style]:
    """Every available style: built-ins first, then user, plugin and project files (later wins)."""
    by_name: dict[str, Style] = {s.name: s for s in _BUILTINS}
    for d, source in _style_dirs(workspace)[:1] + _plugin_style_dirs(workspace) + _style_dirs(workspace)[1:]:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            st = parse_style_file(f, source)
            if st is not None:
                by_name[st.name] = st
    return list(by_name.values())


def get_style(name: str, workspace: Path | None) -> Style | None:
    key = _slug(name or "")
    for st in list_styles(workspace):
        if st.name == key:
            return st
    return None


def active_style_name(settings: Any) -> str:
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
