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
    typed_hint,
    typed_only,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE

CAT = "Memory & Project"



# ─── memory helpers ─────────────────────────────────────────────────────────

SCOPE_TITLE = {"global": "Global memory", "project": "Project memory (private)", "repo": "Repo memory (shared)"}
SCOPE_COLOR = {"global": GOLD, "project": TEAL, "repo": CYAN}


def _mf() -> Any:
    from axon.features import memory_files
    return memory_files


def _short(p: Any) -> str:
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def _age(updated: str) -> str:
    from datetime import date
    d = _mf()._parse_date(updated)
    if d is None:
        return ""
    n = (date.today() - d).days
    return "today" if n <= 0 else f"{n}d ago" if n < 60 else f"{n // 30}mo ago"


def _instruction_files(ws: Path) -> list[tuple[str, Path]]:
    out = [("user", _axon_home() / "AXON.md"), ("project", ws / "AXON.md"), ("local", ws / "AXON.local.md")]
    for name in ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", ".axon/AXON.md"):
        p = ws / name
        if p.exists():
            out.append(("project", p))
    rules = ws / ".axon" / "rules"
    if rules.is_dir():
        out += [("rule", p) for p in sorted(rules.glob("*.md"))]
    return out


def _store(agent: Any) -> Any:
    from axon.agent.memory import MemoryStore
    return MemoryStore(ws_of(agent))


def _scope_flag(rest: str, default: str = "repo") -> tuple[str, str]:
    """Strip a leading ``--global/--user/--project/--private/--repo`` flag (flags only: "global state" is text)."""
    flags = {"--global": "global", "--user": "global", "--project": "project", "--private": "project",
             "--repo": "repo", "--shared": "repo"}
    head, _, tail = rest.partition(" ")
    if head in flags:
        return flags[head], tail.strip()
    return default, rest


def _say_redaction(labels: list[str]) -> None:
    if labels:
        info(f"Redacted {', '.join(labels)} before saving: secrets are never stored in memory.")


def add_memory(agent: Any, text: str, scope: str, distill: bool) -> None:
    store = _store(agent)
    item = None
    # scope: "global" | "project" (the repo's .axon/memory, the long-standing default) | "auto" (private per-project)
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
    mf = _mf()
    msc = {"global": "global", "auto": "project"}.get(item.scope, "repo")
    where = _short(mf.scope_dir(ws_of(agent), msc))
    badge = {"global": f"{GOLD}[user]{RST}", "auto": f"{CYAN}[private]{RST}"}.get(item.scope, f"{TEAL}[project]{RST}")
    ok(f"Remembered {badge} {BOLD}{item.title}{RST} {SLATE}→ {where}/{item.id}.md{RST}")
    _say_redaction(list(getattr(item, "redaction", []) or []))
    _reload(agent)


def _reload(agent: Any) -> None:
    if hasattr(agent, "reload_memory"):
        try:
            agent.reload_memory()
        except Exception:
            pass


def quick_memory(agent: Any, text: str) -> Any:
    """``#text`` shortcut: save a note to project memory (``#global text`` for user memory)."""
    from axon.commands.registry import CommandResult
    t = text.strip()
    scope = "project"
    for pfx in ("global ", "--global "):     # "#global <note>"; a note that starts with the word "user" is a note
        if t.lower().startswith(pfx):
            scope, t = "global", t[len(pfx):].strip()
            break
    if not t:
        usage("#<note to remember>   (#global <note> for all projects)")
        return CommandResult(handled=True)
    add_memory(agent, t, scope, distill=False)
    return CommandResult(handled=True)


MEMORY_USAGE = """/memory [--scope global|project|repo] [--type user|feedback|project|reference]   browse
/memory add [--global|--project|--repo] <text>   save (LLM-distilled; default --repo = .axon/memory)
/memory view|edit|remove <n|id|scope:id>   edit <user|project|local> opens the AXON.md file
/memory move <n|id> <global|project|repo>   /memory search <text>   /memory clear [scope]
/memory restore [n|name]       list or bring back deleted/overwritten versions
/memory reindex [scope]        rebuild MEMORY.md from the files   /memory doctor   health + stale
/memory why                    what was loaded into this session, with tokens
/memory export <scope> [file]  /memory import <file> [--scope S] [--yes]
#<text>                        quick note (no LLM)"""


def _listing(agent: Any, items: list[Any]) -> None:
    ws = ws_of(agent)
    mf = _mf()
    header("Memory", "loaded into every prompt (indexes) · files on disk")
    section("Instruction files")
    for label, p in _instruction_files(ws):
        if p.exists():
            try:
                size = estimate_tokens(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                size = 0
            print(f"    {MINT}●{RST} {SLATE}{label:<8}{RST} {WHITE}{p}{RST} {DARK_SLATE}~{fmt_tokens(size)} tok{RST}")
        else:
            print(f"    {DARK_SLATE}○ {label:<8} {p}  (not created{typed_only(' · /memory edit ' + label)}){RST}")
    for scope in mf.SCOPES:
        d = mf.scope_dir(ws, scope) if scope != "project" else (mf._project_dir_nocreate(ws) or Path("."))
        rows = [(i, m) for i, m in enumerate(items, 1) if m.scope == scope]
        idx = d / mf.INDEX
        state = f"index {len(mf.index_pointers(idx))} pointer(s)" if idx.is_file() else "no index yet"
        section(f"{SCOPE_TITLE[scope]} · {len(rows)}  {DARK_SLATE}{_short(d)}/  ({state}){RST}", SCOPE_COLOR[scope])
        for i, m in rows:
            desc = "" if m.description.lower().startswith(m.name.lower()[:30]) else mf.one_line(m.description, 60)
            print(f"    {CYAN}{i:>2}{RST} {DARK_SLATE}{m.type:<9}{RST} {WHITE}{m.name}{RST} {SLATE}{desc}{RST}"
                  f" {DARK_SLATE}{_age(m.updated)}{RST}")
            print(f"       {DARK_SLATE}{_short(m.path)}{RST}")
    if not items:
        print(f"\n    {SLATE}No saved memories. Type {WHITE}#<note>{SLATE} to remember something.{RST}")
    trash = mf.list_trash(ws)
    if trash:
        print(f"\n    {DARK_SLATE}{len(trash)} older/deleted version(s) kept{typed_only(' · /memory restore')}{RST}")
    print()
    typed_hint("    /memory add · view · edit · remove · move · search · restore · reindex · doctor · why · export · import")
    print()


def _resolve(ws: Path, key: str, items: list[Any]) -> Any:
    m = _mf().find_memory(ws, key, items) if key else None
    if m is None:
        err(f"No memory matching '{key}'. /memory lists them with numbers." if key else "Name a memory: /memory <cmd> <n|id|scope:id>.")
    return m


def _view(m: Any) -> None:
    header(m.name, f"{m.scope} · {m.type} · updated {m.updated} · {m.path}")
    if m.description:
        print(f"    {SLATE}{m.description}{RST}\n")
    for ln in m.body.splitlines():
        print(f"    {WHITE}{ln}{RST}")
    print()


def _after_editor(agent: Any, m: Any) -> None:
    """Back from $EDITOR: keep the index pointer current and redact a secret that was typed in."""
    ws = ws_of(agent)
    mf = _mf()
    cur = mf.parse_memory(m.path, m.scope)
    if cur is None:
        return
    if mf.scan_secrets(m.path.read_text(encoding="utf-8", errors="replace")):
        res = mf.replace_body(ws, cur, mf.redact(cur.body))
        warn_labels = res.redacted or mf.scan_secrets(cur.body)
        info(f"Redacted {', '.join(warn_labels) or 'a secret'} from {_short(m.path)}; the version you saved is kept: /memory restore")
        cur = mf.parse_memory(m.path, m.scope) or cur
    mf.upsert_index(ws, m.scope, cur.name, cur.file, cur.description)
    _reload(agent)


def _do_remove(agent: Any, m: Any, assume_yes: bool = False) -> bool:
    mf = _mf()
    if not assume_yes and not confirm(f"Delete '{m.name}' ({m.scope})? A copy is kept: /memory restore", default=not is_tty()):
        info("Not deleted.")
        return False
    backup = mf.delete_memory(ws_of(agent), m)
    ok(f"Deleted memory {BOLD}{m.name}{RST} {SLATE}({m.scope}:{m.id}){RST}" + (f" {DARK_SLATE}kept at {_short(backup)}{RST}" if backup else ""))
    _reload(agent)
    return True


def _do_move(agent: Any, m: Any, dest: str) -> bool:
    mf = _mf()
    try:
        res = mf.move_memory(ws_of(agent), m, dest)
    except (ValueError, OSError) as e:
        err(str(e))
        return False
    ok(f"Moved {BOLD}{m.name}{RST} {SLATE}{m.scope} → {mf.norm_scope(dest)}: {_short(res.path)}{RST}")
    if mf.norm_scope(dest) == "repo":
        info("The repo scope lives in the workspace's .axon/memory: anything you commit there is shared.")
    for n in res.notes():
        info(n)
    _reload(agent)
    return True


def _search(agent: Any, q: str) -> None:
    ws = ws_of(agent)
    mf = _mf()
    ql = q.lower()
    hits = [m for m in mf.list_memories(ws) if ql in f"{m.name}\n{m.description}\n{m.body}\n{m.type}".lower()]
    files = []
    for label, p in _instruction_files(ws):
        try:
            if p.is_file():
                for n, ln in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if ql in ln.lower():
                        files.append((label, p, n, ln.strip()))
                        break
        except OSError:
            continue
    header("Memory search", f"'{q}' · {len(hits) + len(files)} hit(s) across global, project, repo and instruction files")
    for m in hits:
        print(f"    {CYAN}{m.id}{RST} {WHITE}{m.name}{RST} {SLATE}({m.scope} · {m.type}){RST}")
        print(f"       {DARK_SLATE}{_short(m.path)}{RST}")
    for label, p, n, ln in files:
        print(f"    {GOLD}{label}{RST} {WHITE}{_short(p)}:{n}{RST} {SLATE}{mf.one_line(ln, 70)}{RST}")
    print()


def _restore_cmd(agent: Any, rest: str) -> None:
    ws = ws_of(agent)
    mf = _mf()
    entries = mf.list_trash(ws)
    if not rest:
        header("Memory trash", "every overwritten or deleted version is kept; nothing is purged automatically")
        if not entries:
            print(f"    {SLATE}Nothing to restore.{RST}\n")
            return
        for i, e in enumerate(entries[:40], 1):
            print(f"    {CYAN}{i:>2}{RST} {WHITE}{e.original}{RST} {SLATE}{e.scope} · {e.kind} · {e.when}{RST}")
            print(f"       {DARK_SLATE}{_short(e.path)}{RST}")
        typed_hint("    /memory restore <n|name>   (the newest copy of a name wins)")
        print()
        return
    res = mf.restore(ws, rest)
    if res is None:
        err(f"Nothing in the trash matches '{rest}'. /memory restore lists it.")
        return
    path, backup = res
    ok(f"Restored {_short(path)}" + (f" {SLATE}(the version it replaced is kept at {_short(backup)}){RST}" if backup else ""))
    _reload(agent)


def _reindex_cmd(agent: Any, rest: str) -> None:
    ws = ws_of(agent)
    mf = _mf()
    scopes = [mf.norm_scope(rest)] if rest else list(mf.SCOPES)
    for scope in scopes:
        rep = mf.reindex(ws, scope)
        d = rep["dir"]
        if not Path(d).is_dir():
            continue
        bits = []
        if rep["migrated"]:
            bits.append(f"upgraded {len(rep['migrated'])} file(s) to the standard frontmatter")
        if rep["added"]:
            bits.append(f"added {len(rep['added'])} pointer(s)")
        if rep["removed"]:
            bits.append(f"dropped {len(rep['removed'])} broken pointer(s)")
        ok(f"{SCOPE_TITLE[scope]}: {', '.join(bits) if bits else 'index already up to date'} {SLATE}({_short(Path(d) / mf.INDEX)}){RST}")
        if rep["warning"]:
            info(rep["warning"].removeprefix("Error: "))
    _reload(agent)


def _doctor(agent: Any) -> None:
    ws = ws_of(agent)
    mf = _mf()
    findings = mf.doctor(ws)
    header("Memory doctor", f"{len(mf.list_memories(ws))} memories in global, project and repo scopes")
    if not findings:
        ok("All memory indexes match their files; nothing stale.")
        print()
        return
    order = {"error": 0, "warn": 1, "info": 2}
    mark = {"error": f"{ROSE}✗{RST}", "warn": f"{GOLD}!{RST}", "info": f"{DARK_SLATE}·{RST}"}
    titles = {"orphan": "Not in the index", "broken-link": "Broken index links", "duplicate-pointer": "Duplicate pointers",
              "index-size": "Index size", "legacy-format": "Old format", "bad-type": "Bad type", "duplicate-name": "Duplicate names",
              "duplicate-content": "Duplicate content", "secret": "Possible secrets", "stale": f"Not updated in {mf.STALE_DAYS}+ days",
              "missing-path": "Names a file that no longer exists"}
    seen: list[str] = []
    for f in sorted(findings, key=lambda f: (order[f.level], f.code)):
        if f.code not in seen:
            seen.append(f.code)
            section(titles.get(f.code, f.code))
        print(f"    {mark[f.level]} {SLATE}{f.scope:<7}{RST} {WHITE}{f.message}{RST}")
        if f.path:
            print(f"       {DARK_SLATE}{_short(f.path)}{RST}")
    print(f"\n    {SLATE}{sum(1 for f in findings if f.level == 'error')} error(s), "
          f"{sum(1 for f in findings if f.level == 'warn')} warning(s), {sum(1 for f in findings if f.level == 'info')} note(s).{RST}")
    typed_hint("    /memory reindex fixes the index problems; /memory edit|move|remove handle the rest.")
    print()


def _why(agent: Any) -> None:
    from axon.agent.memory import MemoryLoader
    ws = ws_of(agent)
    mf = _mf()
    loader = getattr(agent, "memory", None)
    if not hasattr(loader, "loaded") or not isinstance(getattr(loader, "loaded", None), list):
        loader = MemoryLoader(ws, auto_memory=bool(getattr(getattr(agent, "settings", None), "auto_memory", True)))
        loader.load_startup()
    rep = mf.why_report(loader, ws)
    header("Why these were loaded", f"{len(rep['loaded'])} file(s) · ~{fmt_tokens(rep['tokens'])} tokens in the system prompt")
    section("Loaded")
    for r in rep["loaded"]:
        pad = "  " * r["depth"]
        print(f"    {MINT}●{RST} {pad}{WHITE}{_short(r['path'])}{RST} {DARK_SLATE}~{fmt_tokens(r['tokens'])} tok{RST}")
        print(f"      {pad}{SLATE}{r['why']}{RST}")
    if not rep["loaded"]:
        print(f"    {SLATE}Nothing loaded.{RST}")
    if rep["waiting"]:
        section("Waiting for a matching file")
        for r in rep["waiting"]:
            print(f"    {GOLD}◌{RST} {WHITE}{_short(r['path'])}{RST}\n      {SLATE}{r['why']}{RST}")
    if rep["skipped"]:
        section("Skipped")
        for r in rep["skipped"]:
            print(f"    {DARK_SLATE}○ {_short(r['path'])}{RST}\n      {SLATE}{r['why']}{RST}")
    for w in rep["warnings"]:
        print(f"    {GOLD}!{RST} {SLATE}{w}{RST}")
    print(f"\n    {DARK_SLATE}{rep['note']}{RST}\n")


def _export_cmd(agent: Any, rest: str) -> None:
    mf = _mf()
    scope, _, dest = rest.partition(" ")
    if not scope:
        usage("/memory export <global|project|repo> [file]")
        return
    try:
        path, n, redacted = mf.export_scope(ws_of(agent), scope, Path(dest.strip()) if dest.strip() else None)
    except ValueError as e:
        err(str(e))
        return
    ok(f"Exported {n} memor{'y' if n == 1 else 'ies'} from {mf.norm_scope(scope)} → {path}")
    if redacted:
        info(f"Secrets were redacted in: {', '.join(redacted)}")


def _import_cmd(agent: Any, rest: str) -> None:
    mf = _mf()
    toks = rest.split()
    yes = "--yes" in toks or "-y" in toks
    scope = "repo"
    args = []
    it = iter(toks)
    for t in it:
        if t in ("--yes", "-y"):
            continue
        if t == "--scope":
            scope = next(it, scope)
            continue
        args.append(t)
    if not args:
        usage("/memory import <file> [--scope global|project|repo] [--yes]")
        return
    ws = ws_of(agent)
    try:
        items, problems = mf.read_import(ws, Path(" ".join(args)), scope)
        scope = mf.norm_scope(scope)
    except ValueError as e:
        err(str(e))
        return
    header("Import preview", f"{len(items)} memor{'y' if len(items) == 1 else 'ies'} → {SCOPE_TITLE[scope]} · {_short(mf.scope_dir(ws, scope))}")
    for i in items:
        flag = {"new": "new", "same": "identical, skipped", "exists": f"name taken, saved as {i.file}"}[i.status]
        print(f"    {CYAN}•{RST} {WHITE}{i.name}{RST} {SLATE}{i.type} · {flag}{RST}")
        print(f"       {DARK_SLATE}{mf.one_line(i.description, 90)}{RST}")
        if i.redacted:
            print(f"       {GOLD}redacted: {', '.join(i.redacted)}{RST}")
    for p in problems:
        info(p)
    todo = [i for i in items if i.status != "same"]
    if not todo:
        info("Nothing new to import.")
        return
    info("Imported text is treated as data. Once added, the index lines are loaded into the model's context like your own memories.")
    if not (yes or confirm(f"Add {len(todo)} memor{'y' if len(todo) == 1 else 'ies'} to the {scope} scope?", default=False)):
        info("Nothing imported." + typed_only("  Re-run with --yes to confirm."))
        return
    results = mf.apply_import(ws, scope, todo)
    ok(f"Imported {len(results)} memor{'y' if len(results) == 1 else 'ies'} into {_short(mf.scope_dir(ws, scope))}")
    _reload(agent)


@command("memory", category=CAT, description="Browse, search and manage memory: global, project and repo scopes",
         aliases=("mem",), usage=MEMORY_USAGE)
def handle_memory(agent: Any, arg: str) -> CommandResult:
    ws = ws_of(agent)
    mf = _mf()
    a = arg.strip()
    sub, _, rest = a.partition(" ")
    sub = sub.lower()
    rest = rest.strip()
    done = CommandResult(handled=True)

    if sub in ("add", "learn", "+"):
        scope, rest = _scope_flag(rest, "repo")
        if not rest:
            usage("/memory add [--global|--project|--repo] <text>")
            return done
        add_memory(agent, rest, {"global": "global", "project": "auto", "repo": "project"}[scope], distill=True)
        return done

    if sub in ("remove", "delete", "rm", "del", "forget"):
        yes = False
        if rest.endswith((" --yes", " -y")) or rest in ("--yes", "-y"):
            yes, rest = True, rest.rsplit(" ", 1)[0] if " " in rest else ""
        m = _resolve(ws, rest, mf.list_memories(ws))
        if m is not None:
            _do_remove(agent, m, assume_yes=yes)
        return done

    if sub in ("view", "show", "read", "cat"):
        m = _resolve(ws, rest, mf.list_memories(ws))
        if m is not None:
            _view(m)
        return done

    if sub in ("edit", "open"):
        target, _, new_text = rest.partition(" ")
        files: dict[str, Path] = {}
        for label, fp in _instruction_files(ws):  # first file per label: "project" is AXON.md, not CLAUDE.md
            if label in ("user", "project", "local"):
                files.setdefault(label, fp)
        if target in files or not target:
            p = files.get(target or "project")
            if p is None:
                return done
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"# {'User' if target == 'user' else ws.name} instructions\n\n", encoding="utf-8")
                if target == "local":
                    _gitignore_add(ws, "AXON.local.md")
            if is_tty():
                open_in_editor(p)
            else:
                info(f"Edit {p} in your editor.")
            return done
        m = mf.find_memory(ws, target)
        if m is None:
            err(f"No memory or file '{target}'. Use user, project, local, a number or an id.")
            return done
        if new_text.strip():
            res = mf.replace_body(ws, m, new_text.strip())
            ok(f"Updated memory {BOLD}{m.name}{RST} {SLATE}{_short(res.path)}{RST}")
            for n in res.notes():
                info(n)
            _reload(agent)
        elif is_tty():
            mf.trash_copy(ws, m.scope, m.path, "replaced")   # the editor writes in place: keep what it started from
            open_in_editor(m.path)
            _after_editor(agent, m)
        else:
            info(f"Edit {m.path} in your editor, or: /memory edit {target} <new text>")
        return done

    if sub in ("search", "find", "grep"):
        _search(agent, rest) if rest else usage("/memory search <text>")
        return done

    if sub in ("move", "mv"):
        key, _, dest = rest.rpartition(" ")
        try:
            dest = mf.norm_scope(dest)
        except ValueError:
            usage("/memory move <n|id> <global|project|repo>")
            return done
        m = _resolve(ws, key.strip(), mf.list_memories(ws))
        if m is not None:
            _do_move(agent, m, dest)
        return done

    if sub in ("restore", "undelete", "trash"):
        _restore_cmd(agent, rest)
        return done

    if sub in ("reindex", "index", "rebuild"):
        try:
            _reindex_cmd(agent, rest)
        except ValueError as e:
            err(str(e))
        return done

    if sub in ("doctor", "check", "health"):
        _doctor(agent)
        return done

    if sub in ("why", "loaded"):
        _why(agent)
        return done

    if sub == "export":
        _export_cmd(agent, rest)
        return done

    if sub == "import":
        _import_cmd(agent, rest)
        return done

    if sub == "clear":
        try:
            scope = mf.norm_scope(rest) if rest else "repo"
        except ValueError as e:
            err(str(e))
            return done
        mems = mf.list_memories(ws, [scope])
        if mems and confirm(f"Delete all {len(mems)} {scope} memories? (copies are kept: /memory restore)", default=not is_tty()):
            for m in mems:
                mf.delete_memory(ws, m)
            ok(f"Cleared {len(mems)} {scope} memories")
            _reload(agent)
        else:
            info("Nothing cleared.")
        return done

    flags: dict[str, str] = {}
    if sub in ("list", "ls", "") or sub.startswith("--"):
        toks = (a if sub.startswith("--") else rest).split()
        for i, t in enumerate(toks):
            if t in ("--scope", "--type") and i + 1 < len(toks):
                flags[t[2:]] = toks[i + 1]
        if sub.startswith("--") and not flags:
            usage(MEMORY_USAGE)
            return done
    elif sub:
        usage(MEMORY_USAGE)
        return done

    try:
        scopes = [mf.norm_scope(flags["scope"])] if flags.get("scope") else None
    except ValueError as e:
        err(str(e))
        return done
    items = mf.list_memories(ws, scopes, flags.get("type", ""))
    if is_tty() and not flags:
        memory_picker(agent, items)   # Esc just closes it: the list is the picker
        return done
    _listing(agent, items if flags else mf.list_memories(ws))
    return done


def _ensure_instruction_file(ws: Path, label: str, p: Path) -> None:
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {'User' if label == 'user' else ws.name} instructions\n\n", encoding="utf-8")
    if label == "local":
        _gitignore_add(ws, "AXON.local.md")


def browser_entries(agent: Any, items: list[Any]) -> list[Any]:
    """Rows of the interactive browser: memories grouped by scope (type and age on each row), then actions."""
    from axon.ui.picker import SelectItem
    ws = ws_of(agent)
    mf = _mf()
    entries: list[Any] = []
    for scope in mf.SCOPES:
        d = mf.scope_dir(ws, scope) if scope != "project" else (mf._project_dir_nocreate(ws) or Path("."))
        group = f"{SCOPE_TITLE[scope]}  {_short(d)}/"
        rows = [m for m in items if m.scope == scope]
        for m in rows:
            entries.append(SelectItem(label=f"{m.type:<9} {m.name[:56]}", value=("mem", m), group=group,
                                      hint=f"{_age(m.updated)} · {m.file}",
                                      search=f"{m.description} {m.body[:300]} {scope} {m.type}"))
        if not rows:
            entries.append(SelectItem(label="(empty)", value=("none", None), group=group))
    for label, p in _instruction_files(ws):
        entries.append(SelectItem(label=f"{label:<8} {_short(p)}", value=("file", label, p), group="Instruction files (AXON.md …)",
                                  hint="" if p.exists() else "create", badge=f"{MINT}●{RST}" if p.exists() else "○"))
    tools = [("add", "Remember something new…", "save to a scope"), ("why", "Why was each file loaded?", "tokens and reasons"),
             ("doctor", "Doctor: index health, stale, broken paths", ""), ("reindex", "Rebuild MEMORY.md indexes", "fixes orphans"),
             ("restore", "Restore a deleted or overwritten version", "")]
    for key, label, hint_ in tools:
        entries.append(SelectItem(label=label, value=("cmd", key), group="Tools", hint=hint_))
    auto = bool(getattr(getattr(agent, "settings", None), "auto_memory", True))
    entries.append(SelectItem(label=f"Auto memory: {'on' if auto else 'off'}", value=("toggle",), group="Auto memory", hint="Enter toggles"))
    try:
        entries.append(SelectItem(label=f"Open project memory folder {_short(mf.scope_dir(ws, 'project'))}", value=("dir", mf.scope_dir(ws, "project")),
                                  group="Auto memory"))
    except Exception:
        pass
    return entries


def memory_picker(agent: Any, items: list[Any]) -> bool:
    """Interactive memory browser: pick a memory or instruction file, then view / edit in $EDITOR / move / delete.
    Loops until Esc. False when closed without doing anything."""
    from axon.ui.picker import Selector
    ws = ws_of(agent)
    mf = _mf()
    acted = False
    while True:
        items = mf.list_memories(ws)
        chosen = Selector(browser_entries(agent, items), title="Memory",
                          subtitle="by scope · type to filter · Enter opens", filterable=True, max_visible=14).run()
        if chosen is None:
            return acted
        kind = chosen.value[0]
        acted = True
        if kind == "none":
            continue
        if kind == "mem":
            _memory_actions(agent, chosen.value[1])
        elif kind == "file":
            _, label, path = chosen.value
            _ensure_instruction_file(ws, label, path)
            open_in_editor(Path(path))
            _reload(agent)
            ok(f"Saved {path}")
        elif kind == "cmd":
            key = chosen.value[1]
            if key == "add":
                from axon.ui.menus import ask_text
                text = ask_text("What should Axon remember?")
                if text:
                    pick = Selector([_sel("repo", "This project, in the repo (.axon/memory)"), _sel("project", "This project, private (~/.axon/projects/…)"),
                                     _sel("global", "All projects (~/.axon/memory)")], title="Save to which scope?").run()
                    if pick is not None:
                        handle_memory(agent, f"add --{pick.value} {text}")
            else:
                handle_memory(agent, key)
        elif kind == "toggle":
            from axon.commands.common import update_settings, update_settings_file
            auto = bool(getattr(agent.settings, "auto_memory", True))
            update_settings(agent, auto_memory=not auto)
            saved = update_settings_file({"autoMemoryEnabled": not auto})
            ok(f"Auto memory {'on' if not auto else 'off'}" + (f" {SLATE}(saved to {saved}){RST}" if saved else ""))
        elif kind == "dir":
            _open_folder(Path(chosen.value[1]))


def _sel(value: str, label: str) -> Any:
    from axon.ui.picker import SelectItem
    return SelectItem(label=label, value=value)


def _memory_actions(agent: Any, m: Any) -> None:
    from axon.ui.picker import SelectItem, Selector
    mf = _mf()
    others = [s for s in mf.SCOPES if s != m.scope]
    acts = [SelectItem(label="View", value="view", hint=_short(m.path)), SelectItem(label="Edit in $EDITOR", value="edit"),
            *[SelectItem(label=f"Move to {s}", value=f"move:{s}") for s in others],
            SelectItem(label="Delete (a copy is kept)", value="delete"), SelectItem(label="Back", value="back")]
    pick = Selector(acts, title=m.name, subtitle=f"{m.scope} · {m.type} · {_short(m.path)}").run()
    if pick is None or pick.value == "back":
        return
    v = pick.value
    if v == "view":
        _view(m)
        input("  (Enter to go back) ") if is_tty() else None
    elif v == "edit":
        mf.trash_copy(ws_of(agent), m.scope, m.path, "replaced")
        open_in_editor(m.path)
        _after_editor(agent, m)
    elif v.startswith("move:"):
        _do_move(agent, m, v.split(":", 1)[1])
    elif v == "delete":
        _do_remove(agent, m)


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
            info(f"{p.name} already exists.{typed_only(' /memory edit local opens it.')}")
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
# The prompts, their fields, storage, import and export live in axon.features.prompt_library (shared with the
# app's "Saved prompts"). Files are the same markdown files the earlier /prompt used; fields are {{name}}.

def parse_template_args(rest: str, names: list[str]) -> tuple[dict[str, str], list[str]]:
    """``key=value`` pairs for /prompt. A value runs until the next ``key=`` (so
    ``description=login fails`` keeps both words) and may be quoted; words before any key are free."""
    # quote-aware split that keeps backslashes (Windows paths): key="a b" -> key=a b
    tokens = [re.sub(r'"([^"]*)"|\'([^\']*)\'', lambda m: m.group(1) if m.group(1) is not None else m.group(2), t)
              for t in re.findall(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')+|["\']\S*', rest)]
    given: dict[str, str] = {}
    free: list[str] = []
    last: str | None = None
    for tok in tokens:
        k, eq, v = tok.partition("=")
        if eq and (k in names or re.fullmatch(r"[A-Za-z_][\w-]*", k)):
            given[k] = v
            last = k
        elif last is not None:
            given[last] = f"{given[last]} {tok}".strip()
        else:
            free.append(tok)
    return given, free


PROMPT_USAGE = """/prompt                         the saved prompts (menu)
/prompt <name> [key=value ...]  fill in the fields (asks for missing ones) and send
/prompt list [words]            list or search the saved prompts
/prompt show <name>             show one prompt and its fields
/prompt save <name> [--global] [-d description] <text with {{fields}}>   save for this project (or for every project)
/prompt delete <name>           delete a saved prompt, or remove a starter
/prompt export [file]           write the saved prompts to one file
/prompt import <file> [--global] [--replace|--rename]   add the prompts of an exported file
/prompt restore-starters        bring back removed starter prompts"""


def _prompt_rows(prompts: list[Any]) -> None:
    for p in prompts:
        flds = ", ".join(f.name for f in p.fields)
        where = {"project": "this project", "global": "all projects", "starter": "starter"}[p.scope]
        print(f"    {CYAN}{p.name:<20}{RST} {WHITE}{p.description[:44]:<44}{RST} {DARK_SLATE}{where:<13}{('{{' + flds + '}}') if flds else ''}{RST}")


def _fill_in(p: Any, given: dict[str, str], free: list[str]) -> str | None:
    """The prompt text with every field filled: values from key=value, free words for a lone gap, then asked."""
    from axon.features import prompt_library as pl
    values = dict(given)
    gaps = [f.name for f in p.fields if f.name not in values and not f.default]
    if free and len(gaps) == 1:
        values[gaps[0]] = " ".join(free)
    for f in p.fields:
        if f.name in values or not is_tty():
            continue
        v = ask_text(f"{f.name}{' (a file in the project)' if f.kind == 'file' else ''}" + (f" [{f.default}]" if f.default else "") + ":")
        if v:
            values[f.name] = v
    try:
        return pl.fill(p.text, values)
    except pl.PromptError as e:
        err(f"{e}. Use key=value, for example {p.fields[0].name}=value.")
        return None


def _use_prompt(agent: Any, p: Any, given: dict[str, str], free: list[str]) -> None:
    text = _fill_in(p, given, free)
    if text is None:
        return
    print(f"\n  {TEAL}▸ /prompt {p.name}{RST}  {SLATE}{text[:100]}{'…' if len(text) > 100 else ''}{RST}\n")
    agent.run_turn(text)


def _save_prompt(ws: Path, name: str, text: str, desc: str, scope: str, **kw: str) -> bool:
    from axon.features import prompt_library as pl
    try:
        p = pl.save(ws, name, text, desc, scope, **kw)
    except pl.PromptError as e:
        err(str(e))
        return False
    ok(f"Saved prompt {BOLD}{p.name}{RST} {SLATE}({'this project' if scope == 'project' else 'all projects'}) · {p.path}{RST}")
    return True


def _prompt_menu(agent: Any, ws: Path) -> None:
    from axon.features import prompt_library as pl
    from axon.ui.menus import Action, browse
    from axon.ui.picker import SelectItem

    def rows() -> list[Any]:
        return [SelectItem(label=p.name, value=p, hint=(p.description[:50] + ("  " + "{{" + ", ".join(f.name for f in p.fields) + "}}" if p.fields else "")),
                           badge={"project": "project", "global": "global", "starter": "starter"}[p.scope]) for p in pl.list_prompts(ws)]

    def edit(p: Any) -> None:
        if p.scope == "starter":                    # edit a copy: the starter itself is part of Axon
            if not _save_prompt(ws, p.name, p.text, p.description, "global", replace=p.name, replace_scope="starter"):
                return
            p = pl.get(ws, p.name)
        open_in_editor(Path(p.path))

    def delete(p: Any) -> None:
        try:
            ok(pl.delete(ws, p.name, p.scope))
        except pl.PromptError as e:
            err(str(e))

    def new() -> None:
        name = ask_text("Name (letters, digits, - and _):")
        text = ask_text("Prompt text; write {{file}} or {{language}} where a value goes:") if name else ""
        if name and text:
            scope = "global" if confirm("Keep it for every project (No = this project only)?", default=False) else "project"
            _save_prompt(ws, name, text, "", scope)

    def show(p: Any) -> None:
        header(p.name, p.description)
        print(f"    {WHITE}{p.text}{RST}\n")

    def use(p: Any) -> None:
        _use_prompt(agent, p, {}, [])

    def actions_for(p: Any) -> list[Any]:
        return [Action("Use it", lambda: use(p), "fill in the fields and send"), Action("Show", lambda: show(p)),
                Action("Edit in your editor", lambda: edit(p)),
                Action("Delete" if p.scope != "starter" else "Remove this starter", lambda: delete(p),
                       confirm=f"Delete the prompt {p.name}?")]

    def extra() -> list[Any]:
        return [Action("Save a new prompt…", new, "name, text and where to keep it"),
                Action("Bring back removed starters", lambda: ok(f"Restored {pl.restore_starters()} starter prompt(s)"))]

    browse("Saved prompts", rows, actions_for, extra, subtitle="choose one to use, edit or delete")


@command("prompt", category=CAT, description="Saved prompts with fill-in fields",
         aliases=("prompts", "template", "templates"), usage=PROMPT_USAGE)
def handle_prompt(agent: Any, arg: str) -> CommandResult:
    from axon.features import prompt_library as pl
    ws = ws_of(agent)
    a = arg.strip()
    sub, _, rest = a.partition(" ")
    low = sub.lower()
    if not a:
        if is_tty():
            _prompt_menu(agent, ws)
            return CommandResult(handled=True)
        low = "list"
    if low in ("list", "ls", "search", "find"):
        prompts = pl.search(ws, rest.strip())
        header("Saved prompts", f"{len(prompts)} found · fields are written {{{{name}}}}")
        _prompt_rows(prompts)
        typed_hint("\n    /prompt <name> key=value · /prompt save <name> <text> · /prompt delete <name>")
        print()
        return CommandResult(handled=True)
    if low == "show":
        p = pl.get(ws, rest.strip())
        if not p:
            err(f"No prompt '{rest.strip()}'.")
            return CommandResult(handled=True)
        header(p.name, f"{p.scope} · {p.description}")
        print(f"    {WHITE}{p.text}{RST}\n")
        return CommandResult(handled=True)
    if low == "save":
        import shlex
        try:
            words = shlex.split(rest, posix=os.name != "nt")
        except ValueError:
            words = rest.split()
        scope = "global" if "--global" in words else "project"
        desc = ""
        if "-d" in words:
            i = words.index("-d")
            desc = words[i + 1] if i + 1 < len(words) else ""
            del words[i:i + 2]
        words = [w for w in words if w not in ("--global", "--project")]
        if len(words) < 2:
            usage("/prompt save <name> [--global] [-d description] <text with {{fields}}>")
            return CommandResult(handled=True)
        _save_prompt(ws, words[0], " ".join(words[1:]), desc, scope)
        return CommandResult(handled=True)
    if low in ("delete", "remove", "rm"):
        name = rest.strip()
        p = pl.get(ws, name)
        if p is None:
            err(f"No prompt '{name}'.")
            return CommandResult(handled=True)
        if not confirm(f"Delete the prompt {p.name}?", default=False):
            info("Kept it.")
            return CommandResult(handled=True)
        try:
            ok(pl.delete(ws, p.name, p.scope))
        except pl.PromptError as e:
            err(str(e))
        return CommandResult(handled=True)
    if low == "restore-starters":
        ok(f"Restored {pl.restore_starters()} starter prompt(s).")
        return CommandResult(handled=True)
    if low == "export":
        import json as _json
        target = Path(rest.strip() or "axon-prompts.json").expanduser()
        if not target.is_absolute():
            target = ws / target
        bundle = pl.export_bundle(ws)
        if not bundle["prompts"]:
            info("No saved prompts to export (the starters are built in).")
            return CommandResult(handled=True)
        from axon.session.store import atomic_write_text
        atomic_write_text(target, _json.dumps(bundle, indent=1, ensure_ascii=False) + "\n")
        ok(f"Exported {len(bundle['prompts'])} prompt(s) → {target}")
        return CommandResult(handled=True)
    if low == "import":
        words = rest.split()
        flags = [w for w in words if w.startswith("--")]
        files = [w for w in words if not w.startswith("--")]
        if not files:
            usage("/prompt import <file> [--global] [--replace|--rename]")
            return CommandResult(handled=True)
        src = Path(" ".join(files)).expanduser()
        if not src.is_absolute():
            src = ws / src
        try:
            raw = src.read_text(encoding="utf-8") if src.stat().st_size <= pl.MAX_BUNDLE_BYTES else None
        except OSError as e:
            err(f"Could not read {src}: {e.strerror or e}")
            return CommandResult(handled=True)
        mode = "replace" if "--replace" in flags else "rename" if "--rename" in flags else "skip"
        try:
            res = pl.import_bundle(ws, raw if raw is not None else "x" * (pl.MAX_BUNDLE_BYTES + 1), "global" if "--global" in flags else "project", mode)
        except pl.PromptError as e:
            err(str(e))
            return CommandResult(handled=True)
        ok(f"Added {len(res['added'])} prompt(s)" + (f", skipped {len(res['skipped'])}" if res["skipped"] else ""))
        for s in res["skipped"]:
            print(f"    {SLATE}• {s['name']}: {s['why']}{RST}")
        return CommandResult(handled=True)
    p = pl.get(ws, sub)
    if p is None:
        err(f"No prompt '{sub}'.{typed_only(' /prompt lists them.')}")
        return CommandResult(handled=True)
    given, free = parse_template_args(rest, [f.name for f in p.fields])
    _use_prompt(agent, p, given, free)
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
        typed_hint("    copy it, or /queue it")
        print()
    return CommandResult(handled=True)
