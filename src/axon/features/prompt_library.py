"""The prompt library: saved prompts with fill-in fields, as plain functions.

No terminal and no web imports (``/prompt`` in the terminal and ``/api/library/*`` in the app both call
these), so both show the same prompts.

Storage is the folder the older ``/prompt`` already used, so nothing saved before is lost:

* global: ``<AXON_HOME>/prompts/<name>.md``
* project: ``<workspace>/.axon/prompts/<name>.md``

A file is markdown with an optional ``description:`` line of frontmatter; the body is the prompt.
Fields are written ``{{name}}``. ``{{name|default}}`` gives a default and ``{{name:file}}`` makes the
app offer the project's files (names such as ``file``, ``path`` and ``config_file`` do that on their
own). The older single-brace ``{name}`` still works in a prompt that has no double-brace fields.

The starter prompts are built in. Removing one hides it (``<AXON_HOME>/prompts/.hidden.json``);
``restore_starters`` brings them back. Editing a starter saves your version as a normal prompt.

Prompts are text for the message box, not commands: they are never run by name as ``/<name>``, and a
name that is a built-in command, custom command or skill is refused so the two can never be mixed up.
Custom commands (``.axon/commands/*.md``, run as ``/name`` with ``$ARGUMENTS``) and skills (which the
model can pick up by itself) stay where they are; this library is for the text you would otherwise
retype, and it can be shared as one file (``export_bundle`` / ``import_bundle``).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_TEXT = 20_000
MAX_DESC = 200
MAX_BUNDLE_BYTES = 2_000_000
MAX_BUNDLE_PROMPTS = 500
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_FIELD = re.compile(r"\{\{\s*([A-Za-z_][\w-]{0,39})\s*(?::\s*(file|text)\s*)?(?:\|([^{}]*))?\}\}")
_LEGACY = re.compile(r"\{([A-Za-z_][A-Za-z0-9_ -]{0,39})\}")
_FILE_NAME = re.compile(r"^(?:.*[_-])?(?:file|path|filepath|filename)s?$", re.I)

# The names /prompt had before the library. They still work when typed, but are not listed.
OLD_NAMES: dict[str, tuple[str, str]] = {
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

STARTERS: dict[str, tuple[str, str]] = {
    "explain-codebase": ("Tour of the project",
                         "Give me an overview of this project: what it is for, how it is laid out, the main modules and how data moves through them, and how to run and test it. Name the key files."),
    "explain-file": ("Explain one file",
                     "Explain {{file}}: what it is for, how it works step by step, what calls it, and anything surprising or fragile. Quote line numbers."),
    "write-tests": ("Write and run tests",
                    "Write tests for {{file}} using the test framework this project already uses. Cover normal use, edge cases and error paths. Run them and fix any that fail. Do not change the code under test unless a test finds a real bug, and tell me if it does."),
    "fix-bug": ("Fix a bug properly",
                "Fix this bug: {{problem}}\nReproduce it first, explain the root cause, make the smallest fix that solves it, and add a test that fails without the fix. Run the tests."),
    "safe-refactor": ("Refactor without changing behaviour",
                      "Refactor {{file}} to {{goal}}. Behaviour must not change: run the tests before and after, keep changes small, and match the project's style. List what you changed."),
    "port-to-language": ("Port a file to another language",
                         "Port {{file}} to {{language}}. Keep the same behaviour and public interface, use the idioms of {{language}}, and put the result next to the original. Note anything that does not translate directly."),
    "review-changes": ("Review my uncommitted changes",
                       "Review my uncommitted changes (git diff) for bugs, security problems, missing tests and unclear code. Group findings by how serious they are, with file and line, and say what you would change."),
    "commit-message": ("Write a commit message",
                       "Read the staged changes (git diff --cached) and write a commit message: a short summary line, then a few lines on why. Do not commit."),
    "document-code": ("Document a file",
                      "Add clear documentation to {{file}}: what each public function or class is for, its parameters and results, and one short example where it helps. Do not change behaviour."),
}


@dataclass
class Field:
    name: str
    kind: str = "text"       # "text" or "file"
    default: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "default": self.default}


@dataclass
class Prompt:
    name: str
    text: str
    description: str = ""
    scope: str = "global"    # "global" | "project" | "starter"
    path: str = ""
    fields: list[Field] = field(default_factory=list)
    reserved: bool = False   # an older saved name that is also a command (shown, cannot be created again)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "text": self.text, "description": self.description, "scope": self.scope,
                "fields": [f.to_dict() for f in self.fields], "reserved": self.reserved}


class PromptError(ValueError):
    """A save, edit or fill that cannot be done; the message is for the user."""


# ─── fields ─────────────────────────────────────────────────────────────────

def parse_fields(text: str) -> list[Field]:
    """Fields in order of first use, without repeats."""
    out: dict[str, Field] = {}
    matches = list(_FIELD.finditer(text))
    for m in matches:
        name, kind, default = m.group(1), m.group(2), (m.group(3) or "").strip()
        f = out.get(name)
        if f is None:
            out[name] = Field(name, kind or ("file" if _FILE_NAME.match(name) else "text"), default)
        elif default and not f.default:
            f.default = default
    if not matches:
        for m in _LEGACY.finditer(text):
            name = m.group(1).strip()
            out.setdefault(name, Field(name, "file" if _FILE_NAME.match(name) else "text"))
    return list(out.values())


def fill(text: str, values: dict[str, str]) -> str:
    """Replace the fields; a field with no value falls back to its default. Raises PromptError listing
    the fields that still have nothing. Values are inserted once, never re-read for fields."""
    fields = parse_fields(text)
    missing = [f.name for f in fields if not str(values.get(f.name, "")).strip() and not f.default]
    if missing:
        raise PromptError("Fill in: " + ", ".join(missing))
    have = {f.name: (str(values.get(f.name, "")).strip() or f.default) for f in fields}
    if _FIELD.search(text):
        return _FIELD.sub(lambda m: have.get(m.group(1), m.group(0)), text)
    return _LEGACY.sub(lambda m: have.get(m.group(1).strip(), m.group(0)), text)


# ─── names ──────────────────────────────────────────────────────────────────

def clean_name(raw: str) -> str:
    n = re.sub(r"[^a-z0-9_-]+", "-", str(raw).strip().lstrip("#/").lower()).strip("-_")
    return n[:40].strip("-_")


def is_reserved(ws: Path | str | None, name: str) -> bool:
    """True when ``name`` is a built-in command or alias, a custom command or a skill."""
    n = name.lower()
    try:
        from axon.commands.registry import resolve_builtin
        if resolve_builtin(n) is not None:
            return True
    except Exception:
        pass
    if ws is not None:
        try:
            from axon.commands.custom import load_custom_commands
            if any(k.lower() == n or k.lower().rsplit(":", 1)[-1] == n for k in load_custom_commands(Path(ws))):
                return True
        except Exception:
            pass
        try:
            from axon.skills.manager import _manager_for
            if n in {k.lower() for k in _manager_for(Path(ws)).skills}:
                return True
        except Exception:
            pass
    return False


def check_name(ws: Path | str | None, raw: str) -> str:
    name = clean_name(raw)
    if not NAME_RE.match(name):
        raise PromptError("Use a short name with letters, digits, - or _ (for example review-api).")
    if is_reserved(ws, name):
        raise PromptError(f"'{name}' is already a command or skill name. Pick another so the two are never mixed up.")
    return name


# ─── storage ────────────────────────────────────────────────────────────────

def global_dir() -> Path:
    from axon.storage import axon_home
    return axon_home() / "prompts"


def project_dir(ws: Path | str) -> Path:
    return Path(ws) / ".axon" / "prompts"


def _dir(ws: Path | str | None, scope: str) -> Path:
    if scope == "project":
        return project_dir(ws or ".")
    if scope == "global":
        return global_dir()
    raise PromptError("Scope must be global or project.")


def _hidden_file() -> Path:
    return global_dir() / ".hidden.json"


def _hidden() -> set[str]:
    try:
        data = json.loads(_hidden_file().read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _write(path: Path, text: str) -> None:
    from axon.session.store import atomic_write_text
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


def _render_file(description: str, text: str) -> str:
    desc = " ".join(str(description).split()).strip("\"'")[:MAX_DESC]
    head = f"---\ndescription: {desc}\n---\n\n" if desc else ""
    return head + text.strip("\n") + "\n"


def _read_dir(d: Path, scope: str) -> list[Prompt]:
    out: list[Prompt] = []
    if not d.is_dir():
        return out
    try:
        files = sorted(d.glob("*.md"))
    except OSError:
        return out
    from axon.skills.manager import parse_frontmatter
    for f in files:
        try:
            if f.stat().st_size > 400_000:
                continue
            fm, body = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue            # a corrupt or unreadable file is skipped, not fatal
        body = body.strip()
        if not body or f.name.startswith("."):
            continue
        desc = fm.get("description") if isinstance(fm.get("description"), str) else ""
        out.append(Prompt(name=f.stem, text=body, description=str(desc or body.splitlines()[0][:60]), scope=scope,
                          path=str(f), fields=parse_fields(body)))
    return out


def list_prompts(ws: Path | str | None, scope: str = "") -> list[Prompt]:
    """What a user sees: project prompts hide global ones of the same name, which hide starters."""
    seen: dict[str, Prompt] = {}
    hidden = _hidden()
    for n, (d, t) in STARTERS.items():
        if n not in hidden:
            seen[n] = Prompt(n, t, d, "starter", "", parse_fields(t))
    for p in _read_dir(global_dir(), "global"):
        seen[p.name] = p
    if ws is not None:
        for p in _read_dir(project_dir(ws), "project"):
            seen[p.name] = p
    rows = list(seen.values())
    for p in rows:
        if p.scope != "starter" and is_reserved(ws, p.name):
            p.reserved = True
    if scope:
        rows = [p for p in rows if p.scope == scope]
    return sorted(rows, key=lambda p: ({"project": 0, "global": 1, "starter": 2}[p.scope], p.name))


def get(ws: Path | str | None, name: str) -> Prompt | None:
    n = clean_name(name)
    found = next((p for p in list_prompts(ws) if p.name == n), None)
    if found is None and n in OLD_NAMES:
        d, t = OLD_NAMES[n]
        return Prompt(n, t, d, "starter", "", parse_fields(t))
    return found


def search(ws: Path | str | None, query: str, scope: str = "") -> list[Prompt]:
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
    rows = list_prompts(ws, scope)
    if not words:
        return rows
    scored: list[tuple[int, Prompt]] = []
    for p in rows:
        score = 0
        for w in words:
            if p.name.startswith(w):
                score += 6
            elif w in p.name:
                score += 4
            elif w in p.description.lower():
                score += 3
            elif w in p.text.lower():
                score += 1
            else:
                score = -1
                break
        if score > 0:
            scored.append((score, p))
    return [p for _, p in sorted(scored, key=lambda t: (-t[0], t[1].name))]


def _validate_body(text: str, description: str) -> str:
    text = str(text or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        raise PromptError("The prompt is empty.")
    if len(text) > MAX_TEXT:
        raise PromptError(f"The prompt is too long (over {MAX_TEXT:,} characters).")
    try:
        from axon.features.memory_files import scan_secrets
        labels = scan_secrets(f"{description}\n{text}")
    except Exception:
        labels = []
    if labels:
        raise PromptError("This looks like it holds a secret (" + ", ".join(labels[:3]) + "). Prompts are saved as plain files, so keep keys and passwords out of them.")
    return text


def save(ws: Path | str | None, name: str, text: str, description: str = "", scope: str = "global",
         replace: str = "", replace_scope: str = "") -> Prompt:
    """Create a prompt, or with ``replace`` (the name being edited, and ``replace_scope`` where it lives)
    change it, renaming or moving it if needed. An existing name in the target scope is refused unless it
    is the prompt being edited."""
    if scope not in ("global", "project"):
        raise PromptError("Scope must be global or project.")
    if scope == "project" and ws is None:
        raise PromptError("There is no project folder.")
    new = check_name(ws, name)
    body = _validate_body(text, description)
    old = clean_name(replace) if replace else ""
    old_scope = replace_scope or scope
    same = bool(old) and old == new and old_scope == scope
    target = _dir(ws, scope) / f"{new}.md"
    if target.exists() and not same:
        raise PromptError(f"A {scope} prompt named '{new}' already exists.")
    _write(target, _render_file(description, body))
    if old and not same:
        if old_scope == "starter":
            if old != new:
                _hide_starter(old)               # a renamed starter is replaced, not duplicated
        elif old_scope in ("global", "project") and not (old_scope == "project" and ws is None):
            try:
                (_dir(ws, old_scope) / f"{old}.md").unlink()
            except OSError:
                pass
    _record(target, "updated" if old else "created", ws)
    return Prompt(new, body, " ".join(description.split())[:MAX_DESC] or body.splitlines()[0][:60], scope, str(target), parse_fields(body))


def _hide_starter(name: str) -> None:
    hidden = _hidden() | {name}
    _write(_hidden_file(), json.dumps(sorted(hidden)))


def _record(path: Path, action: str, ws: Path | str | None) -> None:
    try:
        from axon.session.library import record_saved
        record_saved(path, action=action, source="prompt library", workspace=Path(ws) if ws else None)
    except Exception:
        pass


def delete(ws: Path | str | None, name: str, scope: str = "") -> str:
    """Delete a saved prompt (``scope`` picks one when a name exists in both) or hide a starter.
    Returns what was removed, for the message."""
    n = clean_name(name)
    scopes = [scope] if scope else ["project", "global"]
    for sc in scopes:
        if sc == "starter":
            continue
        if sc == "project" and ws is None:
            continue
        f = _dir(ws, sc) / f"{n}.md"
        if f.is_file():
            try:
                f.unlink()
            except OSError as e:
                raise PromptError(f"Could not delete {n}: {e.strerror or e}") from None
            return f"Deleted {sc} prompt {n}"
    if n in STARTERS and n not in _hidden() and scope in ("", "starter"):
        _hide_starter(n)
        return f"Removed starter prompt {n} (restore the starters to get it back)"
    raise PromptError(f"No prompt named '{n}'.")


def restore_starters() -> int:
    """Bring back every removed starter; returns how many came back."""
    n = len(_hidden() & set(STARTERS))
    try:
        _hidden_file().unlink()
    except OSError:
        pass
    return n


# ─── import and export ──────────────────────────────────────────────────────

def export_bundle(ws: Path | str | None, names: list[str] | None = None, scope: str = "") -> dict[str, Any]:
    """The saved prompts (starters included only when asked for by name) as one JSON-able bundle."""
    want = {clean_name(n) for n in names} if names else None
    rows = [p for p in list_prompts(ws, scope) if (p.name in want if want else p.scope != "starter")]
    return {"axon_prompts": 1, "exported": time.strftime("%Y-%m-%d"),
            "prompts": [{"name": p.name, "description": p.description, "text": p.text} for p in rows]}


def import_bundle(ws: Path | str | None, data: Any, scope: str = "global", on_conflict: str = "skip") -> dict[str, Any]:
    """Add the prompts of a bundle. ``on_conflict``: skip | replace | rename. Nothing is half-written:
    each prompt is checked on its own and the result lists what was added, skipped and why."""
    if isinstance(data, (str, bytes)):
        if len(data) > MAX_BUNDLE_BYTES:
            raise PromptError("That file is too large to be a prompt bundle.")
        try:
            data = json.loads(data)
        except ValueError:
            raise PromptError("That file is not a prompt bundle (it is not valid JSON).") from None
    if isinstance(data, list):
        data = {"prompts": data}
    items = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise PromptError("That file is not a prompt bundle (no list of prompts).")
    if len(items) > MAX_BUNDLE_PROMPTS:
        raise PromptError(f"A bundle can hold up to {MAX_BUNDLE_PROMPTS} prompts.")
    if on_conflict not in ("skip", "replace", "rename"):
        raise PromptError("on_conflict must be skip, replace or rename.")
    added: list[str] = []
    skipped: list[dict[str, str]] = []
    d = _dir(ws, scope)
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("text"), str):
            skipped.append({"name": str(it.get("name", "?")) if isinstance(it, dict) else "?", "why": "not a prompt"})
            continue
        raw = str(it.get("name") or "")
        try:
            name = check_name(ws, raw)
            desc = str(it.get("description") or "") if isinstance(it.get("description"), str) else ""
            body = _validate_body(it["text"], desc)
        except PromptError as e:
            skipped.append({"name": raw or "?", "why": str(e)})
            continue
        target = d / f"{name}.md"
        if target.exists():
            if on_conflict == "skip":
                skipped.append({"name": name, "why": "you already have one with this name"})
                continue
            if on_conflict == "rename":
                k = 2
                while (d / f"{name[:36]}-{k}.md").exists():
                    k += 1
                name = f"{name[:36]}-{k}"
                target = d / f"{name}.md"
                if is_reserved(ws, name):
                    skipped.append({"name": name, "why": "name taken by a command"})
                    continue
        _write(target, _render_file(desc, body))
        _record(target, "created", ws)
        added.append(name)
    return {"added": added, "skipped": skipped}
