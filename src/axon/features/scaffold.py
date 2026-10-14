"""
Project scaffolding: ``/new <template> [name]`` and ``axon new <template> <dir>``.

Templates are plain data files under ``axon/features/templates/<template>/``:

  template.json            {"description", "language", "next": [shell steps shown after creation]}
  **/<file>.tmpl           copied with the ``.tmpl`` suffix removed

Placeholders (in file contents and paths): ``{{name}}`` (project slug, e.g. ``my-tool``),
``{{module}}`` (identifier, ``my_tool``) and ``{{title}}`` (``My Tool``). A path part starting
with ``dot-`` becomes a dotfile (``dot-gitignore`` -> ``.gitignore``) so packaging tools never
drop it. An existing non-empty target directory is never written without confirmation.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axon.features import FeatureCommand
from axon.ui.theme import BOLD, DIM, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE, glyph, safe_text

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SUFFIX = ".tmpl"
_PLACEHOLDER_RE = re.compile(r"\{\{(name|module|title)\}\}")


@dataclass
class TemplateInfo:
    name: str
    description: str
    language: str
    path: Path
    next_steps: list[str] = field(default_factory=list)

    def files(self) -> list[Path]:
        return sorted(p for p in self.path.rglob(f"*{SUFFIX}") if p.is_file())


def list_templates(root: Path | None = None) -> list[TemplateInfo]:
    base = Path(root) if root is not None else TEMPLATES_DIR
    out: list[TemplateInfo] = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        manifest = d / "template.json"
        if not manifest.is_file():
            continue
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(TemplateInfo(name=d.name, description=str(meta.get("description", "")),
                                language=str(meta.get("language", "")), path=d,
                                next_steps=[str(s) for s in meta.get("next", []) or []]))
    return out


def get_template(name: str, root: Path | None = None) -> TemplateInfo | None:
    key = (name or "").strip().lower()
    return next((t for t in list_templates(root) if t.name == key), None)


def slugify(raw: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", (raw or "").strip().lower()).strip("-._")
    return s or "my-project"


def module_name(slug: str) -> str:
    m = re.sub(r"[^a-z0-9_]+", "_", slug.lower()).strip("_") or "app"
    return f"_{m}" if m[0].isdigit() else m


def title_of(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_.\s]+", slug) if w) or slug


def placeholders(name: str) -> dict[str, str]:
    slug = slugify(name)
    return {"name": slug, "module": module_name(slug), "title": title_of(slug)}


def render_text(text: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)


def _target_rel(rel: Path, values: dict[str, str]) -> Path:
    parts = []
    for part in rel.parts:
        part = render_text(part, values)
        if part.endswith(SUFFIX):
            part = part[: -len(SUFFIX)]
        if part.startswith("dot-"):
            part = "." + part[4:]
        parts.append(part)
    return Path(*parts)


def is_nonempty_dir(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return True


def scaffold(template: TemplateInfo | str, dest: Path, name: str | None = None, *, overwrite: bool = False) -> list[Path]:
    """Create the project in `dest`. Raises FileExistsError when dest is a non-empty directory
    (unless overwrite=True; existing files with the same name are then replaced, others kept)."""
    info = get_template(template) if isinstance(template, str) else template
    if info is None:
        raise KeyError(f"unknown template '{template}'")
    dest = Path(dest)
    if dest.exists() and not dest.is_dir():
        raise FileExistsError(f"{dest} exists and is not a directory")
    if is_nonempty_dir(dest) and not overwrite:
        raise FileExistsError(f"{dest} is not empty")
    values = placeholders(name or dest.name)
    planned: list[tuple[Path, str]] = []
    for src in info.files():
        rel = _target_rel(src.relative_to(info.path), values)
        planned.append((dest / rel, render_text(src.read_text(encoding="utf-8"), values)))
    written: list[Path] = []
    for target, content in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        written.append(target)
    for script in ("bin/cli.js",):
        p = dest / script
        if p in written and os.name != "nt":
            try:
                p.chmod(0o755)
            except OSError:
                pass
    return written


# ─── Presentation ──────────────────────────────────────────────────────────

def _say(text: str = "") -> None:
    try:
        print(safe_text(text), flush=True)
    except Exception:
        pass


def templates_table() -> str:
    rows = list_templates()
    if not rows:
        return f"  {ROSE}No templates found in {TEMPLATES_DIR}{RST}"
    w = max(len(t.name) for t in rows)
    lw = max(len(t.language) for t in rows)
    out = [f"  {GOLD}{BOLD}Project templates{RST}  {SLATE}/new <template> [name]   axon new <template> <dir>{RST}"]
    out.append(f"  {DIM}{glyph('h') * (w + lw + 50)}{RST}")
    for t in rows:
        out.append(f"  {TEAL}{t.name:<{w}}{RST}  {SLATE}{t.language:<{lw}}{RST}  {WHITE}{t.description}{RST}")
    return "\n".join(out)


def _report(info: TemplateInfo, dest: Path, written: list[Path], cwd: Path, values: dict[str, str]) -> None:
    try:
        shown = dest.relative_to(cwd)
    except ValueError:
        shown = dest
    _say(f"\n  {MINT}{glyph('ok')}{RST} Created {BOLD}{info.name}{RST} project in {WHITE}{shown}{RST} {SLATE}({len(written)} files){RST}")
    for p in written[:20]:
        _say(f"    {DIM}{p.relative_to(dest).as_posix()}{RST}")
    if info.next_steps:
        _say(f"\n  {GOLD}{BOLD}Next{RST}")
        _say(f"    {WHITE}cd {shown}{RST}")
        for step in info.next_steps:
            _say(f"    {WHITE}{render_text(step, values)}{RST}")
    _say("")


def _confirm_overwrite(dest: Path, ask: Callable[[str], str]) -> bool:
    try:
        ans = ask(safe_text(f"  {GOLD}{dest} is not empty. Write the template into it anyway? (y/N): {RST}"))
    except (EOFError, KeyboardInterrupt):
        return False
    return ans.strip().lower() in ("y", "yes")


def create_project(template: str, dest: Path, name: str | None = None, *, force: bool = False,
                   ask: Callable[[str], str] | None = None, cwd: Path | None = None) -> int:
    """Shared by /new and `axon new`: validate, confirm when needed, write, report. Returns exit code."""
    info = get_template(template)
    if info is None:
        _say(f"  {ROSE}Unknown template '{template}'.{RST}")
        _say(templates_table())
        return 2
    dest = Path(dest)
    if is_nonempty_dir(dest) and not force:
        interactive = ask is not None or (sys.stdin is not None and sys.stdin.isatty())
        if not interactive or not _confirm_overwrite(dest, ask or input):
            _say(f"  {SLATE}Nothing written: {dest} is not empty (use --force to write into it).{RST}")
            return 1
    try:
        written = scaffold(info, dest, name, overwrite=True)
    except (OSError, KeyError) as e:
        _say(f"  {ROSE}Could not create the project: {e}{RST}")
        return 1
    _report(info, dest, written, Path(cwd or Path.cwd()), placeholders(name or dest.name))
    return 0


def handle_new(agent: Any, arg: str) -> None:
    parts = (arg or "").split()
    force = "--force" in parts
    parts = [p for p in parts if p != "--force"]
    if not parts or parts[0] in ("list", "--list", "-l"):
        _say(templates_table())
        return
    template = parts[0]
    name = parts[1] if len(parts) > 1 else f"my-{template}"
    ws = getattr(getattr(agent, "settings", None), "workspace", None)
    base = Path(ws) if ws else Path.cwd()
    dest = Path(name) if Path(name).is_absolute() else base / name
    create_project(template, dest, Path(name).name, force=force, cwd=base)


def new_main(argv: list[str]) -> int:
    """`axon new [--list] | axon new <template> <dir> [--name NAME] [--force]`."""
    import argparse
    parser = argparse.ArgumentParser(prog="axon new", description="Create a runnable starter project")
    parser.add_argument("template", nargs="?", help="template name (omit to list them)")
    parser.add_argument("dir", nargs="?", help="target directory (default: my-<template>)")
    parser.add_argument("--name", help="project name used inside the files (default: directory name)")
    parser.add_argument("--force", action="store_true", help="write into a non-empty directory")
    parser.add_argument("--list", action="store_true", help="list templates")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.list or not args.template:
        _say(templates_table())
        return 0
    dest = Path(args.dir or f"my-{args.template}").expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    return create_project(args.template, dest, args.name or dest.name, force=args.force)


COMMANDS = [
    FeatureCommand(name="/new", handler=handle_new, category="Getting started", aliases=("/scaffold",),
                   description="Create a runnable starter project from a template (list with /new)",
                   usage="/new [<template> [name]] [--force]"),
]
CLI_SUBCOMMANDS = {"new": new_main}

__all__ = ["TemplateInfo", "list_templates", "get_template", "scaffold", "create_project", "templates_table",
           "placeholders", "render_text", "slugify", "module_name", "handle_new", "new_main", "COMMANDS",
           "CLI_SUBCOMMANDS", "TEMPLATES_DIR"]
