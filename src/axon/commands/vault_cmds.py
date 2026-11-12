"""/vault: the local secret vault (see axon.features.vault). Values are typed at a hidden prompt, never as an argument
and never printed; the journal keeps only the subcommand and the item name."""
from __future__ import annotations

import time
from typing import Any

from axon.commands.common import confirm, err, header, hint, info, is_tty, kv, ok, section, usage, warn
from axon.commands.registry import CommandResult, command
from axon.features import vault
from axon.ui.theme import GOLD, RST, SLATE, WHITE

USAGE = (
    "/vault list                              metadata only: name, type, bound server, dates (never a value)\n"
    "/vault set <name> [--type T] [--bound URL]  add or replace (hidden prompt; types: api_key mcp_bearer mcp_oauth webhook_secret generic)\n"
    "/vault rotate <name>                      replace the value, keeping type and bound URL\n"
    "/vault archive <name> | restore <name>    soft delete / bring back\n"
    "/vault delete <name>                      remove for good\n"
    "/vault validate <name>                    probe an MCP OAuth token and refresh it\n"
    "/vault status                             backend, counts, what still lives in plaintext\n"
    "/vault migrate [--purge-backup]           move plaintext keys and tokens into the vault (asks first)\n"
    "/vault backend [keychain|secret-service|encrypted|plain-file]   show or switch the backend\n"
    "API keys are items named env:<VAR> (e.g. env:OPENAI_API_KEY)."
)


def journal_arg(arg: str) -> str:
    """The part of a /vault line that is safe to journal: subcommand and name only."""
    return " ".join(arg.split()[:2])


def _ts(t: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t))) if t else "-"


def _flag(words: list[str], name: str) -> str:
    if name in words:
        i = words.index(name)
        if i + 1 < len(words):
            v = words[i + 1]
            del words[i:i + 2]
            return v
        del words[i]
    return ""


def _ask(prompt: str) -> str:
    from axon.commands.keys_cmds import ask_secret
    return ask_secret(prompt)


def run_vault(arg: str) -> int:
    words = arg.split()
    sub = (words.pop(0).lower() if words else "status")
    try:
        if sub in ("list", "ls"):
            return _list()
        if sub == "status":
            return _status()
        if sub == "backend":
            return _backend(words)
        if sub == "migrate":
            return _migrate("--purge-backup" in words)
        if sub in ("set", "add"):
            typ, bound = _flag(words, "--type") or "", _flag(words, "--bound")
            return _set(words, typ, bound)
        if sub in ("rotate", "archive", "restore", "delete", "remove", "validate"):
            if len(words) != 1:
                usage(f"/vault {sub} <name>")
                return 2
            return _one(sub, words[0])
    except vault.VaultError as e:
        err(str(e))
        return 1
    usage(USAGE)
    return 2


def _list() -> int:
    items = vault.list_items()
    header("Vault", f"{len(items)} item(s) · metadata only")
    for i in items:
        flag = "  archived" if i["archived"] else ""
        bound = f"  bound {i['bound']}" if i["bound"] else ""
        print(f"    {WHITE}{i['name']}{RST}  {SLATE}{i['type']}{bound}  updated {_ts(i['updated'])}  used {_ts(i['last_used'])}{flag}{RST}")
    if not items:
        hint("Nothing stored. /vault set <name> adds a secret; /vault migrate moves existing plaintext keys.")
    return 0


def _status() -> int:
    s = vault.status()
    header("Vault status")
    kv("Backend", f"{s['backend']} ({s['label']})")
    kv("Available", ", ".join(s["available"]) or "none")
    kv("Items", f"{s['items']} ({s['archived']} archived)" + (" · " + ", ".join(f"{k} {v}" for k, v in s["by_type"].items()) if s["by_type"] else ""))
    kv("Migrated", "yes" if s["migrated"] else "no (loaders read env and files as before)")
    if s["warning"]:
        warn(s["warning"])
    section("Still in plaintext")
    if not s["plaintext"]:
        hint("Nothing found.")
    for p in s["plaintext"]:
        print(f"    {WHITE}{p['name']}{RST}  {SLATE}{p['kind']} in {p['source']}{RST}")
    if s["plaintext"]:
        hint("/vault migrate moves these after you confirm.")
    for b in s["backups"]:
        hint(f"Backup kept: {b} (delete with /vault migrate --purge-backup)")
    return 0


def _backend(words: list[str]) -> int:
    if not words:
        b = vault.backend()
        kv("Backend", f"{b.id} ({b.label})")
        kv("Available", ", ".join(vault.available_backends()))
        return 0
    target = words[0]
    if target == "encrypted":
        if vault._crypto() is None:
            err("The encrypted file needs the `cryptography` package (pip install cryptography).")
            return 1
        pw = _ask("Vault passphrase (kept in memory only; set AXON_VAULT_PASSPHRASE to unlock on later runs):")
        if not pw:
            info("Cancelled.")
            return 1
        vault.unlock(pw)
    n = vault.move_backend(target)
    ok(f"Backend is now {target}; {n} item(s) moved.")
    return 0


def _migrate(purge: bool) -> int:
    if purge:
        dirs = vault.backup_dirs()
        if not dirs:
            info("No backups to delete.")
            return 0
        if not confirm(f"Delete {len(dirs)} backup folder(s) of the old plaintext files?"):
            info("Kept.")
            return 1
        ok(f"Deleted {vault.purge_backups()} backup folder(s).")
        return 0
    plan = vault.plan_migration()
    if not plan:
        info("Nothing in plaintext to move.")
        return 0
    b = vault.backend()
    header("Migrate to the vault", f"backend: {b.id}")
    for p in plan:
        print(f"    {WHITE}{p['name']}{RST}  {SLATE}{p['kind']} from {p['source']}{RST}")
    if b.warning():
        warn(b.warning())
    if not is_tty():
        info("Dry run only (no terminal to confirm). Run /vault migrate in a terminal to apply.")
        return 0
    if not confirm(f"Move these {len(plan)} item(s) into the vault? A 0600 backup of the old files is kept."):
        info("Nothing changed.")
        return 1
    res = vault.apply_migration(plan)
    ok(f"Moved {res['moved']} item(s). Backup: {res['backup']}")
    for f in res["failed"]:
        warn(f"Not moved: {f}")
    hint("Check that everything still works, then /vault migrate --purge-backup deletes the backup.")
    return 0 if not res["failed"] else 1


def _set(words: list[str], typ: str, bound: str) -> int:
    if len(words) != 1:
        usage("/vault set <name> [--type T] [--bound URL]   (the value is asked at a hidden prompt, never typed here)")
        return 2
    name = words[0]
    if not typ:
        typ = "api_key" if name.startswith("env:") else "generic"
    value = _ask(f"Value for {name}:")
    if not value:
        info("Cancelled - nothing was saved.")
        return 1
    m = vault.set_secret(name, value, type=typ, bound=bound or None)
    ok(f"Saved {m['name']} ({m['type']}) in the {m['backend']} backend.")
    return 0


def _one(sub: str, name: str) -> int:
    if sub == "delete" or sub == "remove":
        if not confirm(f"Delete {name} from the vault for good?"):
            info("Kept.")
            return 1
        ok("Deleted.") if vault.delete(name) else err(f"No item named {name}.")
    elif sub == "archive":
        vault.archive(name, True)
        ok(f"{name} archived: it is no longer released to anything. /vault restore {name} brings it back.")
    elif sub == "restore":
        vault.archive(name, False)
        ok(f"{name} restored.")
    elif sub == "rotate":
        value = _ask(f"New value for {name}:")
        if not value:
            info("Cancelled - nothing changed.")
            return 1
        vault.rotate(name, value)
        ok(f"{name} rotated.")
    elif sub == "validate":
        r = vault.validate_oauth(name)
        (ok if r["ok"] else warn)(f"{name}: {r['result']}")
        return 0 if r["ok"] else 1
    return 0


@command("vault", category="System", description="Secret vault: API keys, MCP tokens and webhook secrets (list, set, rotate, archive, migrate)",
         aliases=("secrets",), usage=USAGE)
def handle_vault(agent: Any, arg: str) -> CommandResult:
    code = run_vault(arg)
    return CommandResult(handled=True, success=(code == 0))
