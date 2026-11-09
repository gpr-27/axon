"""
Prompt-editor key bindings with user overrides from ``~/.axon/keybindings.json``.

Action names (``category:name``) are documented in :data:`ACTIONS`. Key names are the normalized
names produced by :func:`axon.platform.read_key` ("ctrl-e", "alt-enter", "shift-tab", "up", "f",
...). A chord is a space-separated sequence ("ctrl-x ctrl-e"). In the JSON file keys may be
written Claude-Code style too ("ctrl+e", "Ctrl+Shift+Tab" -> not supported combos are ignored,
"escape", "return", "option+enter", "meta+b").

A block's ``context`` says where its keys apply. ``Chat`` and ``Global`` bindings drive the prompt; a binding in any
other context (``Select``, ``Confirmation``, ``Transcript``, ``HistorySearch``, ``Footer``, ...) is resolved with
:func:`translate`, which turns the key into the one that dialog already understands. Action names of features Axon
does not have (mod panes, agent view, ...) are accepted and do nothing, so a file written for Claude Code loads
cleanly. ``command:<name>`` runs ``/<name>``. Without ``~/.axon/keybindings.json``, ``~/.claude/keybindings.json``
is read.

Validation warnings (:func:`keybinding_errors`, shown by /keybindings and /doctor): invalid JSON or structure, a
misspelled modifier, a space next to ``+`` (never fires), an invalid
context, an action that is not a string or null, an unknown action (the default stays), a reserved shortcut
(Ctrl+C, Ctrl+D, Ctrl+M, Ctrl+[, Ctrl+I, Ctrl+H), and a duplicate key in one context.

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
import time
from pathlib import Path
from typing import Any

# action -> (description, default keys)
ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chat:submit": ("Submit the prompt (or accept the highlighted command)", ("enter",)),
    "chat:newline": ("Insert a newline", ("shift-enter", "alt-enter", "ctrl-j")),
    "chat:queueSubmit": ("Submit the draft as typed, even with a suggestion highlighted", ("ctrl-x enter",)),
    "chat:sendNow": ("Send the draft now (while a turn runs: send what is queued right away)", ("ctrl-enter", "ctrl-x ctrl-s")),
    "chat:killAgents": ("Stop all background subagents (press twice within 3 seconds)", ("ctrl-x ctrl-k",)),
    "chat:complete": ("Accept completion · complete from shell history in ! mode", ("tab",)),
    "chat:cycleMode": ("Cycle permission mode", ("shift-tab", "alt-m")),
    "chat:cancel": ("Dismiss popup · Esc Esc clears draft / opens rewind", ("esc",)),
    "chat:interrupt": ("Clear the draft · on empty prompt exit (twice)", ("ctrl-c",)),
    "chat:exit": ("Delete forward · on empty prompt exit (twice)", ("ctrl-d",)),
    "chat:stash": ("Stash / restore the draft", ("ctrl-s",)),
    "chat:externalEditor": ("Edit the prompt in $EDITOR", ("ctrl-g", "ctrl-x ctrl-e")),
    "chat:paste": ("Paste image or text from the clipboard", ("ctrl-v", "alt-v")),
    "chat:expandPaste": ("Expand a [Pasted text] chip in place, or edit a large one in $EDITOR", ("alt-e",)),
    "history:search": ("Reverse-search prompt history", ("ctrl-r",)),
    "history:prev": ("Previous prompt from history", ()),
    "history:next": ("Next prompt from history", ("ctrl-n",)),
    "cursor:up": ("Move up a row · popup up · older history at the top", ("up",)),
    "cursor:down": ("Move down a row · popup down · newer history at the bottom", ("down",)),
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
    "edit:killWord": ("Cut back to the previous whitespace", ("ctrl-w",)),
    "edit:killWordBack": ("Cut the previous word (letters and digits)", ("alt-backspace", "ctrl-backspace")),
    "edit:killWordForward": ("Cut the next word", ("alt-d",)),
    "edit:yank": ("Paste the last cut", ("ctrl-y",)),
    "edit:yankPop": ("Replace the paste with an older cut", ("alt-y",)),
    "edit:undo": ("Undo", ("ctrl-_",)),
    "app:redraw": ("Clear the screen and redraw", ("ctrl-l",)),
    "app:fileFinder": ("Fuzzy file finder (inserts @path)", ("ctrl-p",)),
    "app:todos": ("Show the task list", ("ctrl-t",)),
    "app:traceCycle": ("Cycle trace level compact → normal → detailed → debug", ("alt-l", "ctrl-x l")),
    "app:transcript": ("Full-screen transcript: click/enter expands a tool call", ("ctrl-o", "ctrl-f")),
    "app:clickExpand": ("Mouse: click a block above the prompt to expand it · wheel up scrolls history "
                        "(⌥/shift+drag selects text; setting mouse_click_expand)", ("click",)),
    "app:modelPicker": ("Open the model picker", ("alt-p",)),
    "app:menu": ("Walk through every command with the arrow keys", ("alt-h",)),
    "app:thinkingToggle": ("Toggle reasoning display", ("alt-t",)),
    "app:suspend": ("Suspend Axon (POSIX job control)", ("ctrl-z",)),
    "app:fastToggle": ("Toggle fast mode", ("alt-o",)),
    "app:vimToggle": ("Toggle vim editing mode", ()),
}

CHORD_TIMEOUT_S = 3.0

CONTEXTS = frozenset({
    "Global", "Chat", "Autocomplete", "Settings", "Confirmation", "Tabs", "Help", "Transcript", "HistorySearch", "Task",
    "ThemePicker", "Attachments", "Footer", "MessageSelector", "DiffDialog", "DiffPanel", "ModelPicker", "EffortSlider",
    "Select", "Plugin", "AbovePrompt", "AbovePromptInput", "AbovePromptSelect", "Pane", "PaneField", "Agents", "Scroll",
})
RESERVED = {"ctrl-c": "Ctrl+C is the interrupt", "ctrl-d": "Ctrl+D is exit", "ctrl-m": "Ctrl+M always arrives as Enter",
            "ctrl-[": "Ctrl+[ always arrives as Escape", "ctrl-i": "Ctrl+I always arrives as Tab",
            "ctrl-h": "Ctrl+H sends the backspace byte"}

# Documented action names -> the prompt action that does the same thing.
ACTION_ALIASES = {
    "app:interrupt": "chat:interrupt", "app:exit": "chat:exit", "app:toggleTodos": "app:todos",
    "app:toggleTranscript": "app:transcript", "history:previous": "history:prev", "chat:clearInput": "app:redraw",
    "chat:clearScreen": "app:redraw", "chat:fastMode": "app:fastToggle", "chat:imagePaste": "chat:paste",
    "chat:undo": "edit:undo",
}

_SELECT_KEYS = {"select:next": "down", "select:previous": "up", "select:pageUp": "pageup", "select:pageDown": "pagedown",
                "select:first": "home", "select:last": "end", "select:accept": "enter", "select:cancel": "esc",
                "messageSelector:up": "up", "messageSelector:down": "down", "messageSelector:top": "home",
                "messageSelector:bottom": "end", "messageSelector:select": "enter"}
# Context action -> the key the dialog already handles.
CONTEXT_KEYS: dict[str, dict[str, str]] = {
    "Select": _SELECT_KEYS, "MessageSelector": _SELECT_KEYS, "ModelPicker": {"modelPicker:decreaseEffort": "left",
        "modelPicker:increaseEffort": "right", "modelPicker:thisSessionOnly": "s", **_SELECT_KEYS},
    "EffortSlider": {"effortSlider:decreaseEffort": "left", "effortSlider:increaseEffort": "right",
                     "effortSlider:thisSessionOnly": "s", "effortSlider:toggleUltracode": ""},
    "Confirmation": {"confirm:yes": "enter", "confirm:no": "esc", "confirm:previous": "up", "confirm:next": "down",
                     "confirm:nextField": "tab", "confirm:toggle": " ", "confirm:cycleMode": "shift-tab", **_SELECT_KEYS},
    "Transcript": {"transcript:exit": "q", "transcript:toggleShowAll": "e", "scroll:lineUp": "k", "scroll:lineDown": "j",
                   "scroll:pageUp": "ctrl-u", "scroll:pageDown": "ctrl-d", "scroll:halfPageUp": "ctrl-u",
                   "scroll:halfPageDown": "ctrl-d", "scroll:fullPageUp": "pageup", "scroll:fullPageDown": "pagedown",
                   "scroll:top": "g", "scroll:bottom": "G"},
    "HistorySearch": {"historySearch:next": "ctrl-r", "historySearch:accept": "esc", "historySearch:cancel": "ctrl-c",
                      "historySearch:execute": "enter"},
    "Autocomplete": {"autocomplete:accept": "tab", "autocomplete:dismiss": "esc", "autocomplete:previous": "up",
                     "autocomplete:next": "down"},
    "Footer": {"footer:next": "down", "footer:previous": "up", "footer:up": "up", "footer:down": "down",
               "footer:openSelected": "enter", "footer:clearSelection": "esc", "footer:close": "x"},
    "Task": {"task:background": "ctrl-b"},
    "Help": {"help:dismiss": "esc"},
    "Tabs": {"tabs:next": "tab", "tabs:previous": "shift-tab"},
    "Settings": {"settings:search": "/", "settings:retry": "r", "select:accept": "enter", "confirm:no": "esc"},
}
# Valid names with nothing to do in Axon (or that the docs say do nothing).
INERT_ACTIONS = frozenset({
    "app:toggleReplTab", "app:cycleDiffBase", "app:diffFileListUp", "app:diffFileListDown", "app:toggleDiffNoiseFilter",
    "app:toggleDiffPreSession", "confirm:previousField", "permission:toggleDebug", "footer:dismiss", "voice:pushToTalk",
    "theme:toggleSyntaxHighlighting", "attachments:next", "attachments:previous", "attachments:remove", "attachments:exit",
    "plugin:toggle", "plugin:install", "plugin:favorite", "settings:fixUsage", "diff:dismiss", "diff:previousSource",
    "diff:nextSource", "diff:previousFile", "diff:nextFile", "diff:back", "diff:viewDetails", "abovePrompt:toggle",
    "abovePrompt:focus", "abovePrompt:next", "abovePrompt:previous", "abovePrompt:press", "abovePrompt:leave",
    "abovePrompt:highlightNext", "abovePrompt:highlightPrevious", "pane:scrollUp", "pane:scrollDown", "pane:pageUp",
    "pane:pageDown", "pane:top", "pane:bottom", "pane:grow", "pane:shrink", "pane:close", "pane:next", "pane:previous",
    "agents:switchView", "agents:togglePin", "agents:find", "agents:rename", "agents:previousGroup", "agents:nextGroup",
    "historySearch:cycleScope", "selection:copy", "selection:clear", "selection:extendLeft", "selection:extendRight",
    "selection:extendUp", "selection:extendDown", "selection:extendLineStart", "selection:extendLineEnd",
    "scroll:lineUp", "scroll:lineDown", "scroll:pageUp", "scroll:pageDown", "scroll:top", "scroll:bottom",
    "scroll:halfPageUp", "scroll:halfPageDown", "scroll:fullPageUp", "scroll:fullPageDown",
})
ALL_CONTEXT_ACTIONS = frozenset(a for table in CONTEXT_KEYS.values() for a in table)

_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "path": None, "map": None, "errors": [], "contexts": {}}

_KEY_ALIASES = {
    "escape": "esc", "return": "enter", "ret": "enter", "cr": "enter", "bs": "backspace",
    "del": "delete", "pgup": "pageup", "pgdn": "pagedown", "page_up": "pageup", "page_down": "pagedown",
    "space": " ", "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}
_MOD_ALIASES = {"ctrl": "ctrl", "control": "ctrl", "c": "ctrl", "alt": "alt", "meta": "alt", "option": "alt",
                "opt": "alt", "m": "alt", "shift": "shift", "s": "shift", "cmd": "cmd", "command": "cmd", "super": "cmd",
                "win": "cmd"}


def keybindings_path() -> Path:
    from axon.storage import user_file
    path = user_file("keybindings.json")
    if not path.exists():
        claude = Path.home() / ".claude" / "keybindings.json"
        if claude.exists():
            return claude
    return path


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
        return part.lower()         # K is the same key as k; write shift+k for the shifted one
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
    order = [m for m in ("ctrl", "alt", "shift", "cmd") if m in mods]
    if len(base) == 1 and "ctrl" in order:
        base = base.lower()
    return "-".join(order + [base])


def default_bindings() -> dict[str, list[str]]:
    return {a: list(keys) for a, (_d, keys) in ACTIONS.items()}


def _known_action(name: str) -> bool:
    return name in ACTIONS or name in ACTION_ALIASES or name in INERT_ACTIONS or name in ALL_CONTEXT_ACTIONS \
        or (name.startswith("command:") and len(name) > 8)


def _key_warnings(spec: str, errors: list[str]) -> str | None:
    """Normalize a key from the file, noting what the validator reports. None: not bindable."""
    if "+ " in spec or " +" in spec:
        errors.append(f"{spec!r}: a space next to '+' makes this a chord that never fires; remove the space")
    key = normalize_key(spec)
    if key is None:
        errors.append(f"unknown action or key {spec!r}")
        return None
    if key.split(" ")[0] in RESERVED and " " not in key:
        errors.append(f"{spec!r} cannot be rebound: {RESERVED[key]}")
        return None
    return key


def _apply_context(ctx_maps: dict[str, dict[str, str | None]], context: str, entries: dict[str, Any], errors: list[str]) -> None:
    for k, v in entries.items():
        key = _key_warnings(k, errors)
        if key is None:
            continue
        if v is not None and (not isinstance(v, str) or not _known_action(v)):
            errors.append(f"{k}: unknown action {v!r}")
            continue
        ctx_maps.setdefault(context, {})[key] = v


def _apply_entries(bindings: dict[str, list[str]], entries: Any, errors: list[str],
                   ctx_maps: dict[str, dict[str, str | None]] | None = None) -> None:
    ctx_maps = ctx_maps if ctx_maps is not None else {}
    if isinstance(entries, list):
        for block in entries:
            if not (isinstance(block, dict) and isinstance(block.get("bindings"), (dict, list))):
                errors.append(f"ignored binding block: {str(block)[:60]}")
                continue
            context = block.get("context")
            if context is not None and context not in CONTEXTS:
                errors.append(f"unknown context {context!r}")
                continue
            if context in (None, "Chat", "Global") or not isinstance(block["bindings"], dict):
                _apply_entries(bindings, block["bindings"], errors, ctx_maps)
            else:
                _apply_context(ctx_maps, context, block["bindings"], errors)
        return
    if not isinstance(entries, dict):
        errors.append("'bindings' must be an object or a list of {context, bindings} blocks")
        return
    for k, v in entries.items():
        k = ACTION_ALIASES.get(k, k)
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
        if ":" in k and " " not in k and normalize_key(k) in (None, k):   # looks like an action name, not a key
            errors.append(f"unknown action or key {k!r}")
            continue
        key = _key_warnings(k, errors)
        if key is None:
            continue
        if v is not None and not (isinstance(v, str) and _known_action(v)):   # a list / object value is unhashable
            errors.append(f"{k}: unknown action {v!r}")
            continue
        if isinstance(v, str) and v not in ACTION_ALIASES and v not in ACTIONS and not v.startswith("command:"):
            ctx = next((c for c, table in CONTEXT_KEYS.items() if v in table), None)
            if ctx is not None:
                ctx_maps.setdefault(ctx, {})[key] = v
            continue                                    # a valid name with nothing to do at the prompt
        for other in bindings.values():
            if key in other:
                other.remove(key)
        if v is not None:
            bindings.setdefault(ACTION_ALIASES.get(v, v), []).append(key)


def _no_duplicates(errors: list[str]) -> Any:
    """json.loads hook: a key written twice in one object is reported, and the last one wins as in JSON."""
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                errors.append(f"duplicate binding {k!r} in one context")
            out[k] = v
        return out
    return hook


def load_bindings(path: Path | None = None) -> tuple[dict[str, list[str]], list[str]]:
    """(action -> keys, errors) from defaults plus the user file (missing file = defaults)."""
    return load_all_bindings(path)[:2]


def load_all_bindings(path: Path | None = None) -> tuple[dict[str, list[str]], list[str], dict[str, dict[str, str | None]]]:
    """(action -> keys, errors, context -> {key: action or None}) from defaults plus the user file."""
    path = path or keybindings_path()
    bindings = default_bindings()
    errors: list[str] = []
    contexts: dict[str, dict[str, str | None]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return bindings, errors, contexts
    except OSError as e:
        return bindings, [f"cannot read {path}: {e}"], contexts
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates(errors)) if text.strip() else {}
    except ValueError as e:
        return bindings, [f"{path.name}: invalid JSON ({e})"], contexts
    if not isinstance(data, dict):
        return bindings, [f"{path.name}: top level must be an object"], contexts
    entries = data.get("bindings", {k: v for k, v in data.items() if k not in ("$schema", "$docs")})
    _apply_entries(bindings, entries, errors, contexts)
    return bindings, errors, contexts


def _current() -> tuple[dict[str, list[str]], list[str]]:
    path = keybindings_path()
    try:
        mtime: float | None = path.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        if _cache["map"] is None or _cache["mtime"] != mtime or _cache["path"] != str(path):
            bindings, errors, contexts = load_all_bindings(path)
            _cache.update(mtime=mtime, path=str(path), map=bindings, errors=errors, contexts=contexts)
        return _cache["map"], list(_cache["errors"])


def effective_keybindings() -> dict[str, list[str]]:
    """Action -> keys currently in effect (defaults merged with ~/.axon/keybindings.json)."""
    bindings, _ = _current()
    return {a: list(k) for a, k in bindings.items()}


def keybinding_errors() -> list[str]:
    return _current()[1]


def describe_action(action: str) -> str:
    if action.startswith("command:"):
        return f"Run /{action.split(':', 1)[1]}"
    return ACTIONS.get(action, ("", ()))[0]


def translate(context: str, key: str) -> str:
    """The key a dialog should act on: a user binding in *context* becomes the key that does that action there;
    a key the user unbound there becomes "" (ignored). ``MessageSelector`` and ``DiffDialog`` also inherit ``Select``."""
    if not isinstance(key, str) or len(key) > 40:
        return key
    _current()
    contexts = _cache.get("contexts") or {}
    if not contexts:
        return key
    for ctx in ((context, "Select") if context in ("MessageSelector", "DiffDialog", "Confirmation", "ModelPicker") else (context,)):
        table = contexts.get(ctx)
        if table and key in table:
            action = table[key]
            if action is None:
                return ""
            return CONTEXT_KEYS.get(ctx, {}).get(action) or CONTEXT_KEYS.get("Select", {}).get(action) or key
    return key


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
        self.pending_since = 0.0
        self.timeout_s = CHORD_TIMEOUT_S

    def expire(self, now: float | None = None) -> bool:
        """Cancel a chord whose next key is overdue. True when one was cancelled."""
        if self.pending and (time.monotonic() if now is None else now) - self.pending_since > self.timeout_s:
            self.pending = ()
            return True
        return False

    def feed(self, key: str) -> tuple[str | None, list[str]]:
        """Returns (action, keys). action None + keys == [] means "waiting for chord"; action None
        with keys means "no binding, handle these raw keys"."""
        self.expire()
        seq = self.pending + (key,)
        if seq in self.prefixes:            # a chord keeps its prefix reserved until every chord on it is unbound
            self.pending = seq
            self.pending_since = time.monotonic()
            return None, []
        if seq in self.map:
            self.pending = ()
            return self.map[seq], [key]
        self.pending = ()
        if len(seq) > 1:  # broken chord: drop the prefix, resolve the last key alone
            return self.feed(key)
        return None, [key]
