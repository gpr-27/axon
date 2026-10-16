"""/keys: API key manager (interactive menu on a terminal, sub-commands for scripts).

    /keys                                   menu (list without a terminal)
    /keys set <provider|VAR> [key] [--project] [--no-verify]
    /keys test [provider|VAR|all]
    /keys remove <provider|VAR> [--project|--user] [-y]
    /keys where
    /keys edit [--project]

The same engine backs ``axon keys ...`` (axon.features.credentials). A full key is never printed;
keys typed on the command line are registered with the activity journal's redactor at once.
"""
from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

from axon.commands.common import (
    confirm, err, header, hint, info, is_tty, ok, open_in_editor, update_settings, usage, warn,
)
from axon.commands.registry import CommandResult, command
from axon.providers import keystore as ks
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, WHITE

CAT = "Model & Modes"
USAGE = (
    "/keys                                       key manager (menu; a list without a terminal)\n"
    "/keys set <provider|VAR> [key] [--project]  add or replace a key (hidden prompt when the key is omitted)\n"
    "/keys test [provider|VAR|all]               live check: latency and model count, or the exact error\n"
    "/keys remove <provider|VAR> [--project]     delete a key from ~/.axon/.env (or this project's .env)\n"
    "/keys where                                 where keys are read from, in order\n"
    "/keys edit [--project]                      open the .env file in $EDITOR\n"
    "flags: --no-verify saves without a live check · -y skips confirmations"
)
_SAVE_SCOPE = {"scope": "user"}          # menu choice "where new keys are saved", per process
_SET = {"set", "add", "replace", "update", "new"}
_TEST = {"test", "check", "verify"}
_REMOVE = {"remove", "rm", "delete", "unset", "del"}
_WHERE = {"where", "path", "paths", "files", "location", "locations"}
_EDIT = {"edit", "open"}
_LIST = {"list", "ls", "status", "show"}


# ─── argument parsing ──────────────────────────────────────────────────────

def split_args(arg: str | Sequence[str]) -> list[str]:
    if not isinstance(arg, str):
        return [str(a) for a in arg]
    try:
        return shlex.split(arg, posix=True)
    except ValueError:
        return arg.split()


def parse_opts(tokens: list[str], valued: Sequence[str] = ()) -> tuple[list[str], set[str], dict[str, str]]:
    """(positional words, boolean flags, --option values). Accepts --opt value and --opt=value."""
    words: list[str] = []
    flags: set[str] = set()
    opts: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--") and "=" in t:
            k, _, v = t.partition("=")
            opts[k] = v
        elif t in valued:
            opts[t] = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 1
        elif t.startswith("--") or t in ("-y", "-p", "-u"):
            flags.add({"-y": "--yes", "-p": "--project", "-u": "--user"}.get(t, t))
        else:
            words.append(t)
        i += 1
    return words, flags, opts


def _ws(agent: Any, workspace: Path | str | None) -> Path:
    if workspace is not None:
        return Path(workspace)
    s = getattr(agent, "settings", None)
    return Path(getattr(s, "workspace", None) or Path.cwd())


# ─── secrets input / agent sync ────────────────────────────────────────────

def ask_secret(prompt: str) -> str:
    """Hidden input (paste works); '' on cancel or without a terminal."""
    if not is_tty():
        return ""
    import getpass
    try:
        return ks.clean_key(getpass.getpass(f"  {GOLD}?{RST} {prompt} "))
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _active_preset(agent: Any) -> Any:
    s = getattr(agent, "settings", None)
    if s is None:
        return None
    try:
        from axon.providers.catalog import find_preset_by_url, get_preset_by_id
        pid = getattr(s, "provider", None)
        return (get_preset_by_id(str(pid)) if pid else None) or find_preset_by_url(str(getattr(s, "base_url", "") or ""))
    except Exception:
        return None


def refresh_agent_key(agent: Any, workspace: Path | None = None) -> bool:
    """Point the running session at the current key of its provider. True when it changed."""
    if agent is None or getattr(agent, "settings", None) is None:
        return False
    preset = _active_preset(agent)
    if preset is None or not preset.requires_key:
        return False
    st = ks.preset_status(preset, workspace)
    if not st.is_set:
        return False
    cur = agent.settings.api_key
    cur_val = cur.get_secret_value() if hasattr(cur, "get_secret_value") else str(cur or "")
    if cur_val == st.value:
        return False
    try:
        from pydantic import SecretStr

        from axon.providers.registry import provider_for
        update_settings(agent, api_key=SecretStr(st.value))
        agent.provider = provider_for(agent.settings.model, agent.settings)
    except Exception:
        return False
    return True


def _check_label(st: ks.KeyStatus) -> str:
    state = ks.check_state(st.var, st.value)
    return {"verified": f"{MINT}✓ verified{RST}", "failed": f"{ROSE}✗ failed last check{RST}"}.get(state, f"{SLATE}untested{RST}")


def _preset_label(preset: Any) -> str:
    return getattr(preset, "name", "") or "custom variable"


# ─── listing / where ───────────────────────────────────────────────────────

def _key_rows(ws: Path) -> list[tuple[Any, ks.KeyStatus]]:
    rows: list[tuple[Any, ks.KeyStatus]] = []
    seen: set[str] = set()
    for p in ks.key_presets():
        st = ks.preset_status(p, ws)
        if not st.var or st.var in seen:
            continue
        seen.add(st.var)
        rows.append((p, st))
    return rows


def list_keys(agent: Any = None, workspace: Path | None = None) -> int:
    ws = _ws(agent, workspace)
    header("API keys", "values are masked · /keys where shows the files")
    active = _active_preset(agent)
    for p, st in _key_rows(ws):
        mark = f"{GOLD}▸{RST}" if active is not None and active.id == p.id else " "
        if st.is_set:
            extra = f" {DARK_SLATE}(also in {', '.join(ks.SOURCE_LABELS[s] for s in st.also_in)}){RST}" if st.also_in else ""
            print(f"  {mark} {MINT}●{RST} {WHITE}{st.var:<24}{RST} {CYAN}{st.masked:<12}{RST} "
                  f"{SLATE}{p.name} · {st.source_label}{RST} · {_check_label(st)}{extra}")
        else:
            print(f"  {mark} {DARK_SLATE}○ {st.var:<24} {'not set':<12} {p.name}{RST}")
    try:
        from axon.providers.catalog import PROVIDER_PRESETS
        local = [p.name for p in PROVIDER_PRESETS if not p.requires_key]
    except Exception:
        local = []
    if local:
        print(f"\n    {SLATE}No key needed: {', '.join(local)}{RST}")
    print()
    hint("    /keys set <provider> [key] · /keys test [provider|all] · /keys remove <provider> · /keys where · /keys edit")
    print()
    return 0


def _file_line(path: Path, ws: Path, scope: str) -> str:
    if not path.exists():
        return f"{DARK_SLATE}not created yet{RST}"
    vals = ks.read_env_file(path) if hasattr(ks, "read_env_file") else {}
    known = set()
    try:
        from axon.config import preset_key_env_vars
        known = set(preset_key_env_vars())
    except Exception:
        pass
    n = sum(1 for k, v in vals.items() if (k in known or "KEY" in k.upper() or "TOKEN" in k.upper()) and ks.usable(v))
    bits = [f"{n} key{'s' if n != 1 else ''}"]
    if os.name != "nt":
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            bits.append(f"{MINT}owner-only{RST}" if not mode & 0o077 else f"{ROSE}readable by others ({oct(mode)[2:]}) - chmod 600 {path}{RST}")
        except OSError:
            pass
    if scope == "project":
        state = ks.gitignore_state(ws)
        bits.append({"ignored": f"{MINT}git-ignored{RST}", "not-ignored": f"{GOLD}NOT git-ignored{RST}",
                     "tracked": f"{ROSE}tracked by git!{RST}", "no-repo": f"{SLATE}no git repo{RST}"}[state])
    return " · ".join(bits)


def show_where(agent: Any = None, workspace: Path | None = None) -> int:
    ws = _ws(agent, workspace)
    header("Where API keys are read from", "first match wins")
    shell = [st.var for _, st in _key_rows(ws) if st.source == "env"]
    user_p, proj_p = ks.user_env_path(), ks.project_env_path(ws)
    print(f"    {WHITE}1{RST}  {BOLD}shell environment{RST}   {SLATE}variables exported before Axon starts"
          f"{(': ' + ', '.join(shell)) if shell else ' (none set)'}{RST}")
    print(f"    {WHITE}2{RST}  {BOLD}project .env{RST}        {CYAN}{proj_p}{RST}  {_file_line(proj_p, ws, 'project')}")
    print(f"    {WHITE}3{RST}  {BOLD}user .env{RST}           {CYAN}{user_p}{RST}  {_file_line(user_p, ws, 'user')}")
    cfg = ks.key_status("AXON_API_KEY", ws, environ={})
    if cfg.source == "config":
        print(f"    {WHITE}4{RST}  {BOLD}config.toml{RST}         {CYAN}{cfg.path}{RST}  api_key = {cfg.masked} "
              f"{SLATE}(used when no AXON_API_KEY variable is set){RST}")
    scope = _SAVE_SCOPE["scope"]
    print(f"\n    {SLATE}New keys are saved to {WHITE}{ks.scope_label(scope, ws)}{RST}"
          f"{SLATE} · --project / --user choose per command{RST}")
    print(f"    {SLATE}Base URLs and custom endpoints live in ~/.axon/config.toml: /endpoint{RST}\n")
    return 0


# ─── set / test / remove / edit ────────────────────────────────────────────

def _resolve(token: str) -> tuple[Any, str] | None:
    hit = ks.resolve_target(token)
    if hit is None:
        known = ", ".join(p.id for p in ks.key_presets())
        err(f"Unknown provider or variable '{token}'. Providers: {known} (or an env var name like MY_API_KEY)")
    return hit


def _verify(preset: Any, var: str, key: str, with_models: bool = False) -> ks.KeyTest:
    from axon.providers.catalog import effective_base_url
    try:
        from axon.providers import endpoints as ep
        if ep.is_custom(preset) and not preset.default_model:
            t = ep.test_endpoint(preset.base_url, key, preset.api_format)
            return ks.KeyTest(t.ok, t.message, t.latency_s, t.models)
    except Exception:
        pass
    return ks.test_key(preset, key, base_url=effective_base_url(preset), with_models=with_models)


def set_key(agent: Any, ws: Path, token: str, key: str = "", *, scope: str = "user", verify: bool = True,
            interactive: bool | None = None) -> int:
    """Add or replace a key: hidden prompt, live verification, save (atomic, owner-only), apply."""
    interactive = is_tty() if interactive is None else interactive
    hit = _resolve(token)
    if hit is None:
        return 2
    preset, var = hit
    if preset is not None and not preset.requires_key:
        info(f"{preset.name} needs no API key.")
        return 0
    if not var:
        err(f"No key variable is known for {token}.")
        return 2
    key = ks.clean_key(key)
    if key:
        ks.register_secret(key)
    if not key:
        if not interactive:
            usage(f"/keys set {getattr(preset, 'id', var)} <api-key>   (or run it in a terminal for a hidden prompt)")
            return 2
        if getattr(preset, "docs_url", ""):
            print(f"  {SLATE}Get a key: {preset.docs_url}{RST}")
        key = ask_secret(f"New API key for {_preset_label(preset)} ({var}, input hidden, Enter cancels):")
        ks.register_secret(key)
    if not ks.usable(key):
        info("Cancelled - nothing was saved.")
        return 1
    tested: ks.KeyTest | None = None
    while verify and preset is not None:
        print(f"  {SLATE}Verifying with {preset.name}…{RST}", flush=True)
        tested = _verify(preset, var, key)
        if tested.ok:
            print(f"  {MINT}✓ Key works{RST} {SLATE}· {tested.summary()}{RST}")
            break
        print(f"  {ROSE}✗ {preset.name} rejected the key:{RST} {WHITE}{tested.message}{RST}")
        if not interactive:
            hint("    Not saved. Pass --no-verify to save it anyway.")
            print()
            return 1
        from axon.ui.picker import choose
        action = choose([("Try a different key", "retry"), ("Save it anyway", "save", "e.g. you are offline"),
                         ("Cancel", "cancel")], title="The key did not verify", index=0)
        if action == "save":
            break
        if action != "retry":
            info("Cancelled - nothing was saved.")
            return 1
        key = ask_secret(f"API key for {preset.name} ({var}, hidden):")
        ks.register_secret(key)
        if not ks.usable(key):
            info("Cancelled - nothing was saved.")
            return 1
    if preset is None and verify:
        print(f"  {SLATE}{var} belongs to no known provider, so it is saved without a live check.{RST}")
    return _save(agent, ws, preset, var, key, scope, tested)


def _save(agent: Any, ws: Path, preset: Any, var: str, key: str, scope: str, tested: ks.KeyTest | None) -> int:
    prev = ks.key_status(var, ws)
    path = ks.env_file_for(scope, ws)
    try:
        ks.set_env_value(path, var, key)
    except (OSError, ValueError) as e:
        err(f"Could not save {var}: {e}")
        return 1
    os.environ[var] = key
    if tested is not None:
        ks.record_check(var, key, tested.ok, tested.latency_s)
    ok(f"Saved {BOLD}{var}{RST} ({ks.mask_key(key)}) to {ks.scope_label(scope, ws)}")
    if scope == "project":
        state, msg = ks.ensure_gitignored(ws)
        (warn if state in ("tracked", "not-ignored") else info)(msg)
    if prev.source == "env" and prev.value != key:
        warn(f"{var} is also exported in your shell; that value wins the next time Axon starts. "
             "Update or remove it in your shell profile.")
    if scope == "user" and var in ks.read_env_file(ks.project_env_path(ws)):
        warn(f"This project's .env also sets {var} and takes precedence here. "
             f"Use --project to replace that one, or /keys remove {var} --project.")
    if refresh_agent_key(agent, ws):
        print(f"  {MINT}✓ This session now uses the new key.{RST}\n")
    return 0


def _targets_for_test(agent: Any, ws: Path, token: str) -> list[tuple[Any, ks.KeyStatus]]:
    if token.lower() == "all" or (not token and agent is None):
        return [(p, st) for p, st in _key_rows(ws) if st.is_set]
    if not token:
        preset = _active_preset(agent)
        if preset is None:
            return [(p, st) for p, st in _key_rows(ws) if st.is_set]
        return [(preset, ks.preset_status(preset, ws))]
    hit = _resolve(token)
    if hit is None:
        return []
    preset, var = hit
    st = ks.key_status(var, ws) if preset is None or var != ks.primary_var(preset) else ks.preset_status(preset, ws)
    return [(preset, st)]


def test_keys(agent: Any, ws: Path, token: str = "") -> int:
    targets = _targets_for_test(agent, ws, token)
    if not targets:
        if token.lower() in ("", "all"):
            info("No API keys are set. Add one with /keys set <provider>.")
            return 1
        return 2
    header("Key check", "live request to each provider")
    failures = 0
    for preset, st in targets:
        if preset is not None and not preset.requires_key:
            print(f"    {MINT}✓{RST} {WHITE}{preset.name}{RST} {SLATE}needs no key{RST}")
            continue
        if not st.is_set:
            print(f"    {DARK_SLATE}○ {st.var:<24} not set{RST}")
            failures += 1
            continue
        if preset is None:
            print(f"    {SLATE}? {st.var:<24} {st.masked} - no provider uses this variable, nothing to test{RST}")
            continue
        res = _verify(preset, st.var, st.value, with_models=True)
        ks.record_check(st.var, st.value, res.ok, res.latency_s)
        if res.ok:
            print(f"    {MINT}✓{RST} {WHITE}{st.var:<24}{RST} {CYAN}{st.masked:<12}{RST} {SLATE}{preset.name} · {res.summary()}{RST}")
        else:
            failures += 1
            print(f"    {ROSE}✗{RST} {WHITE}{st.var:<24}{RST} {CYAN}{st.masked:<12}{RST} {SLATE}{preset.name} · "
                  f"{res.latency_s * 1000:.0f} ms ·{RST} {WHITE}{res.message}{RST}")
    print()
    return 1 if failures else 0


test_keys.__test__ = False  # type: ignore[attr-defined]


def remove_key(agent: Any, ws: Path, token: str, *, scope: str | None = None, assume_yes: bool = False,
               interactive: bool | None = None) -> int:
    interactive = is_tty() if interactive is None else interactive
    hit = _resolve(token)
    if hit is None:
        return 2
    preset, var = hit
    if preset is not None:
        st = ks.preset_status(preset, ws)
        var = st.var if st.is_set and st.source in ("user", "project") else var
    st = ks.key_status(var, ws)
    files = st.files(ws)
    if scope is not None:
        files = [(s, p) for s, p in files if s == scope]
    if not files:
        where = ks.scope_label(scope, ws) if scope else "~/.axon/.env or this project's .env"
        if st.source == "env":
            info(f"{var} is set in your shell environment, not in {where}. Remove it from your shell profile "
                 f"(e.g. `unset {var}`).")
        elif st.source == "config":
            info(f"{var} comes from api_key in {st.path}; edit that file to remove it.")
        else:
            info(f"{var} is not stored in {where}.")
        return 1
    if len(files) > 1:
        if interactive:
            from axon.ui.picker import choose
            picked = choose([(ks.scope_label(s, ws), s) for s, _ in files] + [("Both", "both")],
                            title=f"Remove {var} from")
            if not picked:
                info("Cancelled.")
                return 1
            if picked != "both":
                files = [(s, p) for s, p in files if s == picked]
        elif scope is None:
            usage(f"/keys remove {token} --project | --user   ({var} is in both files)")
            return 2
    for s, p in files:
        if not assume_yes and interactive and not confirm(f"Remove {var} ({ks.mask_key(ks.read_env_file(p).get(var, ''))}) from {p}?"):
            info("Kept.")
            return 1
    removed_vals = {s: ks.read_env_file(p).get(var, "") for s, p in files}
    for s, p in files:
        try:
            ks.remove_env_value(p, var)
        except OSError as e:
            err(f"Could not update {p}: {e}")
            return 1
        ok(f"Removed {var} from {ks.scope_label(s, ws)}")
    if os.environ.get(var) in removed_vals.values():
        os.environ.pop(var, None)
        after = ks.key_status(var, ws)
        if after.is_set:
            os.environ[var] = after.value
            info(f"{var} still has a value from {after.source_label} ({after.masked}).")
    if agent is not None and not ks.key_status(var, ws).is_set and _active_preset(agent) is not None \
            and var in ks.preset_vars(_active_preset(agent)):
        warn("The running session keeps its current key until you switch provider or restart.")
    else:
        refresh_agent_key(agent, ws)
    return 0


def _sync_environ(before: dict[str, str], after: dict[str, str]) -> int:
    changed = 0
    for k in set(before) | set(after):
        old, new = before.get(k), after.get(k)
        if old == new:
            continue
        if k in os.environ and os.environ.get(k) != old:
            continue  # set elsewhere (shell): the file never applied
        if new is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = new
        changed += 1
    return changed


def edit_keys(agent: Any, ws: Path, scope: str = "user") -> int:
    path = ks.env_file_for(scope, ws)
    try:
        ks.ensure_env_file(path)
    except OSError as e:
        err(f"Could not create {path}: {e}")
        return 1
    if scope == "project":
        state, msg = ks.ensure_gitignored(ws)
        if state in ("tracked", "not-ignored"):
            warn(msg)
    if not is_tty():
        info(f"Key file: {path}  (run this in a terminal to open it in $EDITOR)")
        return 0
    before = ks.read_env_file(path)
    if not open_in_editor(path):
        return 1
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    n = _sync_environ(before, ks.read_env_file(path))
    ok(f"{path} saved · {n} variable{'s' if n != 1 else ''} changed" if n else f"{path}: no changes")
    if refresh_agent_key(agent, ws):
        print(f"  {MINT}✓ This session now uses the updated key.{RST}\n")
    return 0


# ─── interactive menu ──────────────────────────────────────────────────────

def _menu(agent: Any, ws: Path) -> int:
    from axon.ui.picker import SelectItem, Selector, choose
    index = 0
    while True:
        rows = _key_rows(ws)
        items: list[SelectItem] = []
        active = _active_preset(agent)
        for p, st in rows:
            if st.is_set:
                state = ks.check_state(st.var, st.value)
                tick = {"verified": "✓ verified", "failed": "✗ failed"}.get(state, "untested")
                hint_txt = f"{st.var} · {st.masked} · {st.source_label} · {tick}"
                badge = f"{MINT}●{RST}" if state != "failed" else f"{ROSE}●{RST}"
            else:
                hint_txt, badge = f"{st.var} · not set", f"{DARK_SLATE}○{RST}"
            label = p.name + ("  (active)" if active is not None and active.id == p.id else "")
            items.append(SelectItem(label=label, value=("provider", p), hint=hint_txt, badge=badge,
                                    group="Providers", search=f"{p.id} {st.var}"))
        scope = _SAVE_SCOPE["scope"]
        more = [
            ("Test all keys", ("test_all", None), "live check of every key that is set"),
            ("Add a key for another variable…", ("custom", None), "any NAME_API_KEY, saved without a check"),
            ("Show where keys are stored", ("where", None), "files, order of precedence, git-ignore status"),
            ("Open ~/.axon/.env in $EDITOR", ("edit", "user"), str(ks.user_env_path())),
            ("Open this project's .env in $EDITOR", ("edit", "project"), str(ks.project_env_path(ws))),
            (f"Save new keys to: {'this project' if scope == 'project' else '~/.axon/.env (all projects)'}",
             ("scope", None), "Enter toggles user / project"),
            ("Endpoints and base URLs…", ("endpoint", None), "/endpoint"),
        ]
        items += [SelectItem(label=lbl, value=val, hint=h, group="More") for lbl, val, h in more]
        picked = Selector(items, title="API keys", subtitle="Enter: manage · type to filter · Esc: close",
                          index=min(index, len(items) - 1), filterable=True, max_visible=16,
                          placeholder="type to filter providers").run()
        if not isinstance(picked, SelectItem):
            return 0
        index = items.index(picked) if picked in items else 0
        kind, val = picked.value
        if kind == "provider":
            _provider_actions(agent, ws, val, choose)
        elif kind == "test_all":
            test_keys(agent, ws, "all")
        elif kind == "custom":
            from axon.commands.common import ask_text
            var = ask_text("Variable name (e.g. MY_GATEWAY_API_KEY):").strip()
            if var and ks.ENV_NAME.match(var):
                set_key(agent, ws, var.upper() if var.isupper() or "_" in var else var, scope=scope, interactive=True)
            elif var:
                err(f"'{var}' is not a valid environment variable name.")
        elif kind == "where":
            show_where(agent, ws)
        elif kind == "edit":
            edit_keys(agent, ws, val)
        elif kind == "scope":
            _SAVE_SCOPE["scope"] = "project" if scope == "user" else "user"
            if _SAVE_SCOPE["scope"] == "project":
                state, msg = ks.ensure_gitignored(ws)
                (warn if state in ("tracked", "not-ignored") else info)(f"New keys go to {ks.project_env_path(ws)} · {msg}")
        elif kind == "endpoint":
            from axon.commands.endpoint_cmds import run_endpoint
            run_endpoint("", agent=agent, workspace=ws)


def _provider_actions(agent: Any, ws: Path, preset: Any, choose: Any) -> None:
    st = ks.preset_status(preset, ws)
    files = st.files(ws) if st.is_set else []
    opts: list[tuple[str, Any, str]] = [
        ("Replace key" if st.is_set else "Add key", "set",
         f"hidden input · verified before saving · to {'this project' if _SAVE_SCOPE['scope'] == 'project' else '~/.axon/.env'}"),
    ]
    if st.is_set:
        opts.append(("Test key", "test", f"{st.masked} from {st.source_label}"))
    if files:
        opts.append(("Remove key", "remove", " + ".join(ks.SOURCE_LABELS[s] for s, _ in files)))
    elif st.source == "env":
        opts.append(("Remove key", "remove-env", "set in your shell - shows how"))
    if getattr(preset, "docs_url", ""):
        opts.append(("Where do I get a key?", "docs", preset.docs_url))
    opts.append(("Back", None, ""))
    action = choose(opts, title=preset.name, subtitle=f"{st.var} · {st.masked or 'not set'}")
    if action == "set":
        set_key(agent, ws, preset.id, scope=_SAVE_SCOPE["scope"], interactive=True)
    elif action == "test":
        test_keys(agent, ws, preset.id)
    elif action in ("remove", "remove-env"):
        remove_key(agent, ws, preset.id, interactive=True)
    elif action == "docs":
        info(f"Create a key for {preset.name} at {preset.docs_url}")


# ─── entry points ──────────────────────────────────────────────────────────

def run_keys(arg: str | Sequence[str] = "", agent: Any = None, workspace: Path | str | None = None) -> int:
    """Shared engine of /keys and `axon keys`. Returns an exit code."""
    ws = _ws(agent, workspace)
    words, flags, _ = parse_opts(split_args(arg))
    scope = "project" if "--project" in flags else "user"
    explicit_scope = "project" if "--project" in flags else ("user" if "--user" in flags else None)
    verify = not ({"--no-verify", "--force", "--skip-verify"} & flags)
    yes = "--yes" in flags
    if "--help" in flags or (words and words[0].lower() in ("help", "-h")):
        usage(USAGE)
        return 0
    if not words:
        if is_tty() and not ({"--list"} & flags):
            return _menu(agent, ws)
        return list_keys(agent, ws)
    sub, rest = words[0].lower(), words[1:]
    if sub in _LIST:
        return list_keys(agent, ws)
    if sub in _WHERE:
        return show_where(agent, ws)
    if sub in _EDIT:
        return edit_keys(agent, ws, scope)
    if sub in _TEST:
        return test_keys(agent, ws, rest[0] if rest else "")
    if sub in _REMOVE:
        if not rest:
            usage("/keys remove <provider|VAR> [--project|--user] [-y]")
            return 2
        return remove_key(agent, ws, rest[0], scope=explicit_scope, assume_yes=yes)
    if sub in _SET:
        if not rest:
            usage("/keys set <provider|VAR> [key] [--project] [--no-verify]")
            return 2
        target, key = rest[0], " ".join(rest[1:])
    else:
        target, key = words[0], " ".join(rest)      # legacy: /keys <provider> [key]
    if "=" in target and not key:
        target, _, key = target.partition("=")
    return set_key(agent, ws, target, key, scope=scope, verify=verify)


@command("keys", category=CAT, description="API keys: add/replace (verified), test, remove, where they are stored",
         aliases=("key", "apikey"), usage=USAGE)
def handle_keys(agent: Any, arg: str) -> CommandResult:
    code = run_keys(arg, agent=agent)
    return CommandResult(handled=True, success=code == 0)


def keys_main(argv: list[str]) -> int:
    """`axon keys ...` (no REPL)."""
    _prepare_cli()
    return run_keys(list(argv), agent=None, workspace=Path.cwd())


def _prepare_cli() -> None:
    try:
        from axon import platform as plat
        plat.setup_console()
    except Exception:
        pass
    try:
        from axon.providers.endpoints import apply_endpoint_overrides
        apply_endpoint_overrides()
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


__all__ = ["USAGE", "ask_secret", "edit_keys", "handle_keys", "keys_main", "list_keys", "parse_opts",
           "refresh_agent_key", "remove_key", "run_keys", "set_key", "show_where", "split_args", "test_keys"]
