"""Local secret vault: one place for API keys, MCP OAuth/bearer tokens and webhook secrets.

Backends, best first (auto-detected, no new hard dependency):

* ``keychain``       macOS Keychain through the ``security`` CLI. The secret never appears in argv: it goes to
                     ``security -i`` on stdin, hex-encoded (``-X``) so no quoting can break the line.
* ``secret-service`` Linux Secret Service through ``secret-tool`` (the secret is read from stdin).
* ``encrypted``      ``~/.axon/vault.enc``: Fernet (AES-128-CBC + HMAC) from the ``cryptography`` package when it is
                     importable, key derived with ``hashlib.scrypt`` from a passphrase (``AXON_VAULT_PASSPHRASE`` or
                     entered in ``/vault backend encrypted``, held in memory only). The stdlib has no authenticated
                     cipher and none is invented here.
* ``plain-file``     ``~/.axon/vault.json`` mode 0600, NOT encrypted at rest (status says so). Windows Credential
                     Manager is not used (``cmdkey`` cannot read secrets back), so Windows lands here.

Metadata (name, type, bound origin, created/updated/last used, archived) lives in ``~/.axon/vault-index.json``;
values only ever live in the backend. Listing never returns a value. A credential with a bound origin is released
only to that exact scheme+host+port. Project settings can neither read the vault nor choose the backend: the backend
choice is stored in the user-level index only and nothing here reads a workspace file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SERVICE = "axon-vault"
TYPES = ("api_key", "mcp_bearer", "mcp_oauth", "webhook_secret", "generic")
NAME_RE = re.compile(r"^[A-Za-z0-9_.:/@-]{1,128}$")
USED_WRITE_EVERY_S = 300
REF_PREFIX = "vault:"

_lock = threading.RLock()
_passphrase: str | None = None          # unlocked in this process only


class VaultError(Exception):
    pass


# ─── Paths / platform / process (patched in tests) ─────────────────────────

def _user(name: str) -> Path:
    from axon.storage import user_file
    return user_file(name)


def index_path() -> Path:
    return _user("vault-index.json")


def platform_name() -> str:
    return sys.platform


def _run(argv: list[str], input_text: str | None = None, timeout: float = 10.0) -> tuple[int, str]:
    """Run a keychain CLI. Returns (returncode, stdout). The output is a secret for 'get' calls: callers never log it."""
    try:
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return p.returncode, p.stdout


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── Origin binding ────────────────────────────────────────────────────────

def origin_of(url: str) -> str:
    """Normalized ``scheme://host[:port]`` (default ports dropped), '' when it is not an http(s)/ws(s) URL."""
    try:
        u = urlsplit((url or "").strip())
        scheme, host, port = (u.scheme or "").lower(), (u.hostname or "").lower(), u.port
    except ValueError:
        return ""
    if scheme not in ("http", "https", "ws", "wss") or not host:
        return ""
    if port is not None and port == {"http": 80, "https": 443, "ws": 80, "wss": 443}[scheme]:
        port = None
    return f"{scheme}://{host}" + (f":{port}" if port else "")


# ─── Backends ──────────────────────────────────────────────────────────────

class Backend:
    id = ""
    label = ""
    protected = True

    def available(self) -> bool:
        return False

    def get(self, account: str) -> str | None:
        raise NotImplementedError

    def put(self, account: str, payload: str) -> None:
        raise NotImplementedError

    def remove(self, account: str) -> None:
        raise NotImplementedError

    def warning(self) -> str:
        return ""


class KeychainBackend(Backend):
    id, label = "keychain", "macOS Keychain"

    def available(self) -> bool:
        return platform_name() == "darwin" and bool(_which("security"))

    def get(self, account: str) -> str | None:
        rc, out = _run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"])
        return out.rstrip("\n") if rc == 0 and out.strip() else None

    def put(self, account: str, payload: str) -> None:
        hexed = payload.encode("utf-8").hex()
        # `security -i` reads its commands from stdin: the secret is never an argv element visible in `ps`.
        rc, _ = _run(["security", "-i"], f'add-generic-password -U -s {SERVICE} -a {account} -l "Axon vault" -X {hexed}\n')
        if rc != 0 or self.get(account) is None:      # `security -i` can exit 0 after a failed command: read it back
            raise VaultError("the macOS Keychain refused the write (is it locked?)")

    def remove(self, account: str) -> None:
        _run(["security", "delete-generic-password", "-s", SERVICE, "-a", account])


class SecretServiceBackend(Backend):
    id, label = "secret-service", "Linux Secret Service"

    def available(self) -> bool:
        if not platform_name().startswith("linux") or not _which("secret-tool"):
            return False
        rc, _ = _run(["secret-tool", "lookup", "service", SERVICE, "account", "__probe__"], timeout=5)
        return rc in (0, 1)                      # 1 = not found: the daemon answered

    def get(self, account: str) -> str | None:
        rc, out = _run(["secret-tool", "lookup", "service", SERVICE, "account", account])
        return out if rc == 0 and out else None

    def put(self, account: str, payload: str) -> None:
        rc, _ = _run(["secret-tool", "store", "--label=Axon vault", "service", SERVICE, "account", account], payload)
        if rc != 0:
            raise VaultError("the Secret Service refused the write (is the keyring unlocked?)")

    def remove(self, account: str) -> None:
        _run(["secret-tool", "clear", "service", SERVICE, "account", account])


class _FileBackend(Backend):
    def _path(self) -> Path:
        raise NotImplementedError

    def _load(self) -> dict[str, str]:
        raise NotImplementedError

    def _save(self, data: dict[str, str]) -> None:
        raise NotImplementedError

    def get(self, account: str) -> str | None:
        return self._load().get(account)

    def put(self, account: str, payload: str) -> None:
        d = self._load()
        d[account] = payload
        self._save(d)

    def remove(self, account: str) -> None:
        d = self._load()
        if d.pop(account, None) is not None:
            self._save(d)


class PlainFileBackend(_FileBackend):
    id, label, protected = "plain-file", "plain file ~/.axon/vault.json (mode 0600)", False

    def available(self) -> bool:
        return True

    def _path(self) -> Path:
        return _user("vault.json")

    def _load(self) -> dict[str, str]:
        try:
            d = json.loads(self._path().read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, str]) -> None:
        _atomic_write(self._path(), json.dumps(data))

    def warning(self) -> str:
        return ("No keychain was found, so secrets are kept in a 0600 file that is NOT encrypted at rest. "
                "Install the `cryptography` package and run `/vault backend encrypted` for an encrypted file.")


def _crypto() -> Any:
    try:
        from cryptography.fernet import Fernet  # type: ignore[import-not-found]
        return Fernet
    except Exception:
        return None


def unlock(passphrase: str) -> None:
    """Hold the passphrase of the encrypted vault in memory for this process (never written anywhere)."""
    global _passphrase
    _passphrase = passphrase or None


def _pass() -> str | None:
    return _passphrase or os.environ.get("AXON_VAULT_PASSPHRASE") or None


class EncryptedFileBackend(_FileBackend):
    id, label = "encrypted", "encrypted file ~/.axon/vault.enc"

    def available(self) -> bool:
        return _crypto() is not None and _pass() is not None

    def _path(self) -> Path:
        return _user("vault.enc")

    @staticmethod
    def _key(passphrase: str, salt: bytes) -> bytes:
        return base64.urlsafe_b64encode(hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1,
                                                       maxmem=64 * 1024 * 1024, dklen=32))

    def _load(self) -> dict[str, str]:
        fernet = _crypto()
        pw = _pass()
        if fernet is None or pw is None:
            raise VaultError("the encrypted vault is locked: set AXON_VAULT_PASSPHRASE or run /vault backend encrypted")
        try:
            raw = json.loads(self._path().read_text(encoding="utf-8"))
        except OSError:
            return {}
        except ValueError:
            raise VaultError("vault.enc is damaged")
        try:
            blob = fernet(self._key(pw, base64.b64decode(raw["salt"]))).decrypt(raw["token"].encode("ascii"))
            d = json.loads(blob.decode("utf-8"))
        except Exception:
            raise VaultError("wrong passphrase, or vault.enc is damaged")
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}

    def _save(self, data: dict[str, str]) -> None:
        fernet, pw = _crypto(), _pass()
        if fernet is None or pw is None:
            raise VaultError("the encrypted vault is locked")
        try:
            salt = base64.b64decode(json.loads(self._path().read_text(encoding="utf-8"))["salt"])
        except Exception:
            salt = os.urandom(16)
        token = fernet(self._key(pw, salt)).encrypt(json.dumps(data).encode("utf-8")).decode("ascii")
        _atomic_write(self._path(), json.dumps({"v": 1, "salt": base64.b64encode(salt).decode("ascii"), "token": token}))


BACKENDS: dict[str, type[Backend]] = {c.id: c for c in (KeychainBackend, SecretServiceBackend, EncryptedFileBackend,
                                                         PlainFileBackend)}


def available_backends() -> list[str]:
    return [i for i, c in BACKENDS.items() if c().available()]


def backend() -> Backend:
    """The chosen backend when set and usable, else the best available one."""
    chosen = _read_index().get("backend")
    if chosen in BACKENDS:
        b = BACKENDS[chosen]()
        if b.available() or chosen == "encrypted":      # an encrypted vault stays chosen while locked
            return b
    for bid in ("keychain", "secret-service"):
        b = BACKENDS[bid]()
        if b.available():
            return b
    enc = BACKENDS["encrypted"]()
    if enc.available() and enc._path().exists():
        return enc
    return BACKENDS["plain-file"]()


# ─── Index (metadata only) ─────────────────────────────────────────────────

def _read_index() -> dict[str, Any]:
    try:
        d = json.loads(index_path().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("items", {})
            return d
    except (OSError, ValueError):
        pass
    return {"items": {}}


def _write_index(d: dict[str, Any]) -> None:
    _atomic_write(index_path(), json.dumps(d, indent=2, sort_keys=True))


def active() -> bool:
    """True once the vault holds something or a migration happened: loaders then read it first."""
    d = _read_index()
    return bool(d.get("items")) or bool(d.get("migrated_at"))


def _check_name(name: str) -> str:
    n = (name or "").strip()
    if not NAME_RE.match(n):
        raise VaultError("a name is 1-128 characters of letters, digits and _ . : / @ -")
    return n


def _register(*values: str) -> None:
    try:
        from axon.session.activity import add_secret
        for v in values:
            add_secret(v)
    except Exception:
        pass


def _payload(value: str, data: dict[str, Any] | None) -> str:
    return json.dumps({"value": value, "data": data or {}}, ensure_ascii=True)


def _unpack(raw: str) -> tuple[str, dict[str, Any]]:
    try:
        d = json.loads(raw)
        return str(d.get("value") or ""), dict(d.get("data") or {})
    except (ValueError, AttributeError, TypeError):
        return raw, {}


def _secret_values(value: str, data: dict[str, Any]) -> list[str]:
    out = [value]
    for k in ("access_token", "refresh_token", "client_secret", "id_token"):
        if isinstance(data.get(k), str):
            out.append(data[k])
    return [v for v in out if v]


def _meta(name: str, rec: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "type": rec.get("type", "generic"), "bound": rec.get("bound") or "",
            "created": rec.get("created"), "updated": rec.get("updated"), "last_used": rec.get("last_used"),
            "archived": bool(rec.get("archived")), "backend": rec.get("backend", "")}


# ─── API ───────────────────────────────────────────────────────────────────

def set_secret(name: str, value: str, *, type: str = "generic", bound: str | None = None,
               data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create or replace an item. Write-only: the stored value is never returned by any listing."""
    name = _check_name(name)
    if type not in TYPES:
        raise VaultError(f"type must be one of {', '.join(TYPES)}")
    if (not value or "\n" in value or "\r" in value) and not data:
        raise VaultError("the secret must be a non-empty single line")
    origin = ""
    if bound:
        origin = origin_of(bound)
        if not origin:
            raise VaultError("a bound URL must be an http(s) address")
    elif type in ("mcp_bearer", "mcp_oauth"):
        raise VaultError("an MCP credential must be bound to its server URL")
    with _lock:
        b = backend()
        b.put(name, _payload(value, data))
        idx = _read_index()
        old = idx["items"].get(name, {})
        now = time.time()
        idx["items"][name] = {"type": type, "bound": origin, "created": old.get("created", now), "updated": now,
                              "last_used": old.get("last_used"), "archived": False, "backend": b.id}
        _write_index(idx)
    _register(*_secret_values(value, data or {}))
    return _meta(name, idx["items"][name])


def _fetch(name: str, for_url: str | None, include_archived: bool) -> tuple[str, dict[str, Any]] | None:
    name = _check_name(name)
    with _lock:
        idx = _read_index()
        rec = idx["items"].get(name)
        if not rec or (rec.get("archived") and not include_archived):
            return None
        bound = rec.get("bound") or ""
        if bound and origin_of(for_url or "") != bound:
            raise VaultError(f"{name} is bound to {bound}; it is not released to {origin_of(for_url or '') or 'another destination'}")
        raw = (BACKENDS.get(rec.get("backend", "")) or type(backend()))().get(name)
        if raw is None:
            return None
        now = time.time()
        if now - float(rec.get("last_used") or 0) > USED_WRITE_EVERY_S:
            rec["last_used"] = now
            try:
                _write_index(idx)
            except OSError:
                pass
    value, data = _unpack(raw)
    _register(*_secret_values(value, data))
    return value, data


def get_secret(name: str, for_url: str | None = None, include_archived: bool = False) -> str | None:
    got = _fetch(name, for_url, include_archived)
    return got[0] if got else None


def get_data(name: str, for_url: str | None = None) -> dict[str, Any] | None:
    got = _fetch(name, for_url, False)
    return got[1] if got else None


def has_item(name: str) -> bool:
    return name in _read_index()["items"]


def list_items(include_archived: bool = True) -> list[dict[str, Any]]:
    """Metadata only (never a value)."""
    items = [_meta(n, r) for n, r in sorted(_read_index()["items"].items())]
    return items if include_archived else [i for i in items if not i["archived"]]


def delete(name: str) -> bool:
    name = _check_name(name)
    with _lock:
        idx = _read_index()
        rec = idx["items"].pop(name, None)
        if rec is None:
            return False
        (BACKENDS.get(rec.get("backend", "")) or type(backend()))().remove(name)
        _write_index(idx)
    return True


def archive(name: str, flag: bool = True) -> dict[str, Any]:
    """Soft delete: the item stays (restorable) but is never released while archived."""
    name = _check_name(name)
    with _lock:
        idx = _read_index()
        rec = idx["items"].get(name)
        if rec is None:
            raise VaultError(f"no vault item named {name}")
        rec["archived"] = bool(flag)
        rec["updated"] = time.time()
        _write_index(idx)
    return _meta(name, rec)


def rotate(name: str, new_value: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Replace the value of an existing, active item; type and bound URL are kept."""
    name = _check_name(name)
    rec = _read_index()["items"].get(name)
    if rec is None:
        raise VaultError(f"no vault item named {name}")
    if rec.get("archived"):
        raise VaultError(f"{name} is archived: restore it first")
    return set_secret(name, new_value, type=rec.get("type", "generic"), bound=rec.get("bound") or None, data=data)


def move_backend(target_id: str) -> int:
    """Switch backend and move every item. Returns how many moved."""
    if target_id not in BACKENDS:
        raise VaultError(f"backend must be one of {', '.join(BACKENDS)}")
    new = BACKENDS[target_id]()
    if not new.available():
        raise VaultError(f"the {new.label} backend is not available here")
    moved = 0
    with _lock:
        idx = _read_index()
        for name, rec in idx["items"].items():
            old = BACKENDS.get(rec.get("backend", ""), type(new))()
            if old.id == new.id:
                continue
            raw = old.get(name)
            if raw is None:
                continue
            new.put(name, raw)
            old.remove(name)
            rec["backend"] = new.id
            moved += 1
        idx["backend"] = new.id
        _write_index(idx)
    return moved


# ─── References and loaders ────────────────────────────────────────────────

def resolve_ref(value: str, for_url: str | None = None) -> str:
    """``vault:NAME`` -> the secret (host-checked); any other value is returned unchanged. '' when missing/archived."""
    if isinstance(value, str) and value.startswith(REF_PREFIX):
        try:
            return get_secret(value[len(REF_PREFIX):], for_url) or ""
        except VaultError:
            return ""
    return value


def env_key_name(var: str) -> str:
    return f"env:{var}"


def api_key_for(var: str) -> str | None:
    """The vault's API key for an env var name, or None (vault empty, item absent/archived, backend locked)."""
    if not active():
        return None
    try:
        return get_secret(env_key_name(var))
    except VaultError:
        return None


def api_keys_for_environ(names: list[str] | tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not active():
        return out
    for n in names:
        v = api_key_for(n)
        if v:
            out[n] = v
    return out


def oauth_name(endpoint: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.:/@-]", "_", re.sub(r"^[a-z]+://", "", endpoint))[:100]
    return f"mcp-oauth:{base}.{hashlib.sha256(endpoint.encode()).hexdigest()[:8]}"


def oauth_tokens(endpoint: str) -> dict[str, Any] | None:
    if not active() or not has_item(oauth_name(endpoint)):
        return None
    try:
        return get_data(oauth_name(endpoint), endpoint)
    except VaultError:
        return None


def store_oauth_tokens(endpoint: str, tokens: dict[str, Any]) -> bool:
    """Keep OAuth tokens in the vault when it is active. False = not stored (caller keeps its legacy file)."""
    if not active():
        return False
    try:
        set_secret(oauth_name(endpoint), str(tokens.get("access_token") or ""), type="mcp_oauth", bound=endpoint,
                   data=tokens)
        return True
    except VaultError:
        return False


def forget_oauth(endpoint: str) -> None:
    try:
        if has_item(oauth_name(endpoint)):
            delete(oauth_name(endpoint))
    except VaultError:
        pass


def load_all_into_scrubber() -> int:
    """Register every readable vault secret with the activity scrubber (call at startup)."""
    n = 0
    if not active():
        return 0
    for it in list_items(include_archived=False):
        try:
            got = _fetch(it["name"], it["bound"] or None, False)
        except VaultError:
            continue
        if got:
            n += 1
    return n


# ─── Validation of MCP OAuth tokens ────────────────────────────────────────

def validate_oauth(name: str, spec_for: Any = None, manager_for: Any = None) -> dict[str, Any]:
    """Probe the stored token and refresh it when needed. Returns {ok, result} (no token text).

    ``spec_for(endpoint)`` finds the configured server (default: the MCP config); ``manager_for(spec)`` builds the
    OAuthManager (default: the real one).
    """
    rec = _read_index()["items"].get(_check_name(name))
    if rec is None or rec.get("type") != "mcp_oauth":
        raise VaultError(f"{name} is not an MCP OAuth item")
    endpoint = rec.get("bound") or ""
    data = get_data(name, endpoint) or {}
    if not data.get("access_token"):
        return {"ok": False, "result": "no access token stored"}
    expired = bool(data.get("expires_at")) and float(data["expires_at"]) < time.time()
    if not (expired or data.get("refresh_token")):
        return {"ok": True, "result": "token present (no expiry known, no refresh token)"}
    if not data.get("refresh_token"):
        return {"ok": False, "result": "expired and there is no refresh token: sign in again"}
    spec = spec_for(endpoint) if spec_for else _find_spec(endpoint)
    if spec is None:
        return {"ok": not expired, "result": "no configured MCP server uses this token; cannot refresh"}
    try:
        mgr = manager_for(spec) if manager_for else _manager(spec)
        refreshed = bool(mgr.refresh())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "result": f"refresh failed ({type(e).__name__})"}
    return {"ok": refreshed, "result": "refreshed" if refreshed else "refresh was refused: sign in again"}


def _find_spec(endpoint: str) -> Any:
    try:
        from axon.mcp.config import MCPConfig, normalize_url
        cfg = MCPConfig(Path.cwd())
        for s in cfg.load_specs().values():
            if normalize_url(s.url) == endpoint:
                return s
    except Exception:
        pass
    return None


def _manager(spec: Any) -> Any:
    from axon.mcp.oauth import OAuthManager
    return OAuthManager(spec)


# ─── Status and migration ──────────────────────────────────────────────────

def _env_key_vars() -> set[str]:
    try:
        from axon.config import PROVIDER_KEY_ENV_VARS, preset_key_env_vars
        return set(PROVIDER_KEY_ENV_VARS) | set(preset_key_env_vars())
    except Exception:
        return set()


def plaintext_items() -> list[dict[str, str]]:
    """What still lives in plaintext files (names and files only)."""
    from axon.providers import keystore as ks
    from axon.providers.catalog import read_env_file
    out: list[dict[str, str]] = []
    keyvars = _env_key_vars()
    for var, val in sorted(read_env_file(ks.user_env_path()).items()):
        if (var in keyvars or var.endswith("_API_KEY")) and ks.usable(val):
            out.append({"kind": "api_key", "name": var, "source": str(ks.user_env_path()), "bound": ""})
    try:
        from axon.mcp.oauth import TokenStore, auth_store_path
        store = TokenStore()
        for endpoint, rec in sorted(store._read().items()):
            if isinstance(rec, dict) and isinstance(rec.get("tokens"), dict) and rec["tokens"].get("access_token"):
                out.append({"kind": "mcp_oauth", "name": endpoint, "source": str(auth_store_path()),
                            "bound": origin_of(endpoint)})
    except Exception:
        pass
    return out


def status() -> dict[str, Any]:
    b = backend()
    items = list_items()
    counts: dict[str, int] = {}
    for i in items:
        counts[i["type"]] = counts.get(i["type"], 0) + 1
    return {"backend": b.id, "label": b.label, "protected": b.protected, "warning": b.warning(),
            "available": available_backends(), "items": len(items), "archived": sum(1 for i in items if i["archived"]),
            "by_type": counts, "plaintext": plaintext_items(), "migrated": bool(_read_index().get("migrated_at")),
            "backups": [str(p) for p in backup_dirs()]}


def backup_root() -> Path:
    return _user("vault-backup")


def backup_dirs() -> list[Path]:
    root = backup_root()
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def plan_migration() -> list[dict[str, str]]:
    """Dry run: what would move (no values)."""
    return plaintext_items()


def apply_migration(plan: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Move the planned items into the vault. Only call after the user confirmed. A 0600 backup of each source file is
    kept until ``purge_backups``; a source entry is removed only after its vault copy reads back identically."""
    from axon.providers import keystore as ks
    from axon.providers.catalog import read_env_file
    plan = plan if plan is not None else plan_migration()
    if not plan:
        return {"moved": 0, "failed": [], "backup": ""}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = backup_root() / stamp
    bdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup_root(), 0o700)
        os.chmod(bdir, 0o700)
    except OSError:
        pass
    for src in {p["source"] for p in plan}:
        sp = Path(src)
        if sp.is_file():
            dest = bdir / sp.name
            shutil.copy2(sp, dest)
            os.chmod(dest, 0o600)
    moved, failed = 0, []
    env_vals = read_env_file(ks.user_env_path())
    for item in plan:
        try:
            if item["kind"] == "api_key":
                val = env_vals.get(item["name"], "")
                if not ks.usable(val):
                    continue
                set_secret(env_key_name(item["name"]), val, type="api_key")
                if get_secret(env_key_name(item["name"])) != val:
                    raise VaultError("read-back mismatch")
                ks.remove_env_value(ks.user_env_path(), item["name"])
            elif item["kind"] == "mcp_oauth":
                from axon.mcp.oauth import TokenStore
                store = TokenStore()
                toks = store.get(item["name"]).get("tokens")
                if not isinstance(toks, dict):
                    continue
                set_secret(oauth_name(item["name"]), str(toks.get("access_token") or ""), type="mcp_oauth",
                           bound=item["name"], data=toks)
                if (get_data(oauth_name(item["name"]), item["name"]) or {}) != toks:
                    raise VaultError("read-back mismatch")
                data = store._read()
                data[item["name"]].pop("tokens", None)
                store._write(data)
            moved += 1
        except (VaultError, OSError, KeyError) as e:
            failed.append(f"{item.get('name')}: {e}")
    idx = _read_index()
    idx["migrated_at"] = time.time()
    _write_index(idx)
    return {"moved": moved, "failed": failed, "backup": str(bdir)}


def purge_backups() -> int:
    """Delete the migration backups (only after the user confirmed). Returns how many folders were removed."""
    n = 0
    for d in backup_dirs():
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    return n


def mask(value: str) -> str:
    return "" if not value else "••••" + ("" if len(value) < 12 else value[-2:])


def store_api_key(var: str, key: str) -> bool:
    """Save an API key into the vault when it is active (and drop a stale plaintext copy). False = not handled."""
    if not active():
        return False
    try:
        set_secret(env_key_name(var), key, type="api_key")
    except VaultError:
        return False
    try:
        from axon.providers import keystore as ks
        if var in ks.read_env_file(ks.user_env_path()):
            ks.remove_env_value(ks.user_env_path(), var)
    except Exception:
        pass
    os.environ[var] = key
    return True
