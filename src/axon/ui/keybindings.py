"""
Prompt-editor key bindings with user overrides from ``~/.axon/keybindings.json``.

Action names (``category:name``) are documented in :data:`ACTIONS`. Key names are the normalized
names produced by :func:`axon.platform.read_key` ("ctrl-e", "alt-enter", "shift-tab", "up", "f",
...). A chord is a space-separated sequence ("ctrl-x ctrl-e"). In the JSON file keys may be
written Claude-Code style too ("ctrl+e", "Ctrl+Shift+Tab" -> not supported combos are ignored,
"escape", "return", "option+enter", "meta+b").

Accepted file formats (all may be mixed)::

    {"bindings": {"chat:externalEditor": ["ctrl+e"], "edit:undo": null}}      # action -> keys
    {"bindings": {"ctrl+e": "chat:externalEditor", "ctrl+t": null}}           # key -> action
    {"bindings": [{"context": "Chat", "bindings": {"ctrl+e": "chat:externalEditor"}}]}

``action -> keys`` replaces the default keys of that action (``null`` / ``[]`` unbinds it);
``key -> action`` adds the key to that action (removing it from any other action); ``key -> null``
unbinds the key. Unknown actions and malformed entries are reported by :func:`keybinding_errors`
and otherwise ignored, so a broken file never breaks the prompt.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

# action -> (description, default keys)
ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chat:submit": ("Submit the prompt (or accept the highlighted command)", ("enter",)),
    "chat:newline": ("Insert a newline", ("shift-enter", "alt-enter", "ctrl-j")),
    "chat:complete": ("Accept completion · complete from shell history in ! mode", ("tab",)),
    "chat:cycleMode": ("Cycle permission mode", ("shift-tab", "alt-m")),
    "chat:cancel": ("Dismiss popup · Esc Esc clears draft / opens rewind", ("esc",)),
    "chat:interrupt": ("Clear the draft · on empty prompt exit (twice)", ("ctrl-c",)),
    "chat:exit": ("Delete forward · on empty prompt exit (twice)", ("ctrl-d",)),
    "chat:stash": ("Stash / restore the draft", ("ctrl-s",)),
    "chat:externalEditor": ("Edit the prompt in $EDITOR", ("ctrl-g", "ctrl-x ctrl-e")),
    "chat:paste": ("Paste image or text from the clipboard", ("ctrl-v", "alt-v")),
    "history:search": ("Reverse-search prompt history", ("ctrl-r",)),
    "history:prev": ("Previous prompt from history", ()),
    "history:next": ("Next prompt from history", ("ctrl-n",)),
    "cursor:up": ("Move up a line · popup up · older history at the top", ("up",)),
    "cursor:down": ("Move down a line · popup down · newer history at the bottom", ("down",)),
    "cursor:left": ("Move left · on empty prompt open the session switcher", ("left",)),
    "cursor:right": ("Move right · accept @file completion at end of line", ("right",)),
    "cursor:wordLeft": ("Move one word left", ("ctrl-left", "alt-b")),
    "cursor:wordRight": ("Move one word right", ("ctrl-right", "alt-f")),
    "cursor:lineStart": ("Start of line", ("home", "ctrl-a")),
    "cursor:lineEnd": ("End of line", ("end", "ctrl-e")),
    "edit:backspace": ("Delete the character before the cursor", ("backspace",)),
    "edit:delete": ("Delete the character under the cursor", ("delete",)),
    "edit:killToEnd": ("Cut to end of line", ("ctrl-k",)),
    "edit:killToStart": ("Cut to start of line", ("ctrl-u",)),
    "edit:killWord": ("Cut the previous word", ("ctrl-w", "alt-backspace")),
    "edit:killWordForward": ("Cut the next word", ("alt-d",)),
    "edit:yank": ("Paste the last cut", ("ctrl-y",)),
    "edit:yankPop": ("Replace the paste with an older cut", ("alt-y",)),
    "edit:undo": ("Undo", ("ctrl-_",)),
    "app:redraw": ("Clear the screen and redraw", ("ctrl-l",)),
    "app:fileFinder": ("Fuzzy file finder (inserts @path)", ("ctrl-p",)),
    "app:todos": ("Show the task list", ("ctrl-t",)),
    "app:traceCycle": ("Cycle trace level compact → normal → detailed → debug", ("ctrl-o",)),
    "app:modelPicker": ("Open the model picker", ("alt-p",)),
    "app:thinkingToggle": ("Toggle reasoning display", ("alt-t",)),
    "app:suspend": ("Suspend Axon (POSIX job control)", ("ctrl-z",)),
    "app:fastToggle": ("Toggle fast mode (/fast)", ("alt-o",)),
    "app:vimToggle": ("Toggle vim editing mode", ()),
}

_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "path": None, "map": None, "errors": []}

_KEY_ALIASES = {
    "escape": "esc", "return": "enter", "ret": "enter", "cr": "enter", "bs": "backspace",
    "del": "delete", "pgup": "pageup", "pgdn": "pagedown", "page_up": "pageup", "page_down": "pagedown",
    "space": " ", "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}
_MOD_ALIASES = {"ctrl": "ctrl", "control": "ctrl", "c": "ctrl", "alt": "alt", "meta": "alt", "option": "alt",
                "opt": "alt", "m": "alt", "shift": "shift", "s": "shift"}


def keybindings_path() -> Path:
    from axon.storage import user_file
    return user_file("keybindings.json")


def normalize_key(spec: str) -> str | None:
    """'Ctrl+E' -> 'ctrl-e'; 'ctrl+x ctrl+e' -> 'ctrl-x ctrl-e'; returns None when unparseable."""
    if not isinstance(spec, str) or not spec.strip():
        return None
    out: list[str] = []
    for part in spec.strip().split():
        one = _normalize_single(part)
        if one is None:
            return None
        out.append(one)
    return " ".join(out)


def _normalize_single(part: str) -> str | None:
    if len(part) == 1:
        return part
    raw = part.replace("+", "-")
    pieces = [p for p in raw.split("-") if p != ""]
    if raw.endswith("--"):  # "ctrl+-" style
        pieces.append("-")
    if not pieces:
        return None
    base = pieces[-1]
    base = _KEY_ALIASES.get(base.lower(), base if len(base) == 1 else base.lower())
    mods = []
    for m in pieces[:-1]:
        mm = _MOD_ALIASES.get(m.lower())
        if mm is None:
            return None
        if mm not in mods:
            mods.append(mm)
    if not mods:
        return base
    if base == "_" and mods == ["ctrl"]:
        return "ctrl-_"
    if mods == ["shift"] and len(base) == 1:
        return base.upper()
    order = [m for m in ("ctrl", "alt", "shift") if m in mods]
    if len(base) == 1 and "ctrl" in order:
        base = base.lower()
    return "-".join(order + [base])


def default_bindings() -> dict[str, list[str]]:
    return {a: list(keys) for a, (_d, keys) in ACTIONS.items()}


def _apply_entries(bindings: dict[str, list[str]], entries: Any, errors: list[str]) -> None:
    if isinstance(entries, list):
        for block in entries:
            if isinstance(block, dict) and isinstance(block.get("bindings"), (dict, list)):
                _apply_entries(bindings, block["bindings"], errors)
            else:
                errors.append(f"ignored binding block: {str(block)[:60]}")
        return
    if not isinstance(entries, dict):
        errors.append("'bindings' must be an object or a list of {context, bindings} blocks")
        return
    for k, v in entries.items():
        if k in ACTIONS:  # action -> key(s)
            keys = [v] if isinstance(v, str) else (v if isinstance(v, list) else ([] if v is None else None))
            if keys is None:
                errors.append(f"{k}: expected a key, a list of keys or null")
                continue
            norm = []
            for spec in keys:
                n = normalize_key(spec) if isinstance(spec, str) else None
                if n is None:
                    errors.append(f"{k}: unrecognized key {spec!r}")
                else:
                    norm.append(n)
            for other in bindings.values():
                for n in norm:
                    if n in other:
                        other.remove(n)
            bindings[k] = norm
            continue
        key = normalize_key(k)
        if key is None:
            errors.append(f"unknown action or key {k!r}")
            continue
        if v is not None and v not in ACTIONS:
            errors.append(f"{k}: unknown action {v!r}")
            continue
        for other in bindings.values():
            if key in other:
                other.remove(key)
        if v is not None:
            bindings[v].append(key)


def load_bindings(path: Path | None = None) -> tuple[dict[str, list[str]], list[str]]:
    """(action -> keys, errors) from defaults plus the user file (missing file = defaults)."""
    path = path or keybindings_path()
    bindings = default_bindings()
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return bindings, errors
    except OSError as e:
        return bindings, [f"cannot read {path}: {e}"]
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        return bindings, [f"{path.name}: invalid JSON ({e})"]
    if not isinstance(data, dict):
        return bindings, [f"{path.name}: top level must be an object"]
    entries = data.get("bindings", {k: v for k, v in data.items() if k not in ("$schema", "$docs")})
    _apply_entries(bindings, entries, errors)
    return bindings, errors


def _current() -> tuple[dict[str, list[str]], list[str]]:
    path = keybindings_path()
    try:
        mtime: float | None = path.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        if _cache["map"] is None or _cache["mtime"] != mtime or _cache["path"] != str(path):
            bindings, errors = load_bindings(path)
            _cache.update(mtime=mtime, path=str(path), map=bindings, errors=errors)
        return _cache["map"], list(_cache["errors"])


def effective_keybindings() -> dict[str, list[str]]:
    """Action -> keys currently in effect (defaults merged with ~/.axon/keybindings.json)."""
    bindings, _ = _current()
    return {a: list(k) for a, k in bindings.items()}


def keybinding_errors() -> list[str]:
    return _current()[1]


def describe_action(action: str) -> str:
    return ACTIONS.get(action, ("", ()))[0]


class KeyResolver:
    """Turns raw keys into actions, handling multi-key chords ("ctrl-x ctrl-e")."""

    def __init__(self, bindings: dict[str, list[str]] | None = None) -> None:
        bindings = effective_keybindings() if bindings is None else bindings
        self.map: dict[tuple[str, ...], str] = {}
        for action, keys in bindings.items():
            for k in keys:
                self.map[tuple(k.split(" ")) if k != " " else (" ",)] = action
        self.prefixes = {seq[:i] for seq in self.map for i in range(1, len(seq))}
        self.pending: tuple[str, ...] = ()

    def feed(self, key: str) -> tuple[str | None, list[str]]:
        """Returns (action, keys). action None + keys == [] means "waiting for chord"; action None
        with keys means "no binding, handle these raw keys"."""
        seq = self.pending + (key,)
        if seq in self.map:
            self.pending = ()
            return self.map[seq], [key]
        if seq in self.prefixes:
            self.pending = seq
            return None, []
        self.pending = ()
        if len(seq) > 1:  # broken chord: drop the prefix, resolve the last key alone
            return self.feed(key)
        return None, [key]
