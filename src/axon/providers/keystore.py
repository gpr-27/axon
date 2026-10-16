"""
API key storage helpers shared by ``/keys``, ``axon keys``, ``/settings`` and ``axon setup``.

* where a key comes from (shell environment, project ``.env``, ``~/.axon/.env``, ``config.toml``)
* masking (``sk-a…9f3c``; a full key is never printed)
* atomic, comment-preserving writes / removals in a dotenv file (owner-only permissions)
* git-ignore checks for a project ``.env``
* live verification (latency + model count) and a small "last verified" cache that stores only a
  fingerprint of the key, never the key itself
* redaction of ``/keys set <provider> <key>`` command lines before anything records them

Load order (see axon.config.Settings.load): a variable already set in the shell wins, then the
project's ``.env``, then ``~/.axon/.env``. ``api_key`` in ``~/.axon/config.toml`` is used when no
environment variable provides a key.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axon.providers.catalog import PROVIDER_PRESETS, ProviderPreset, axon_env_file, get_preset_by_id, read_env_file

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REDACTED = "[REDACTED]"

SOURCE_LABELS = {
    "env": "shell environment",
    "project": "project .env",
    "user": "~/.axon/.env",
    "config": "~/.axon/config.toml",
}
SCOPES = ("user", "project")
_INVALID = {"", "none", "null", "local", "skip", "sk-placeholder", "your_api_key_here", "your-api-key-here", "replace_me"}
_TEST_KEY_PREFIXES = ("sk-test-", "test-", "mock-", "AIzaSy_test_")


# ─── Masking / validity ────────────────────────────────────────────────────

def usable(value: str | None) -> bool:
    v = (value or "").strip()
    return bool(v) and not v.startswith("/") and v.lower() not in _INVALID


def mask_key(value: str | None) -> str:
    """``sk-a…9f3c`` for real keys; short values show no characters at all."""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) < 12:
        return "•" * 4
    if len(v) < 20:
        return f"{v[:2]}…{v[-2:]}"
    return f"{v[:4]}…{v[-4:]}"


def clean_key(raw: str | None) -> str:
    v = (raw or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return v


# ─── Files ─────────────────────────────────────────────────────────────────

def user_env_path() -> Path:
    return axon_env_file()


def project_env_path(workspace: Path | str | None) -> Path:
    return Path(workspace or Path.cwd()) / ".env"


def env_file_for(scope: str, workspace: Path | str | None = None) -> Path:
    return project_env_path(workspace) if scope == "project" else user_env_path()


def scope_label(scope: str, workspace: Path | str | None = None) -> str:
    return f"this project's .env ({project_env_path(workspace)})" if scope == "project" else f"~/.axon/.env ({user_env_path()})"


def _config_api_key() -> str:
    try:
        from axon.providers.catalog import axon_config_file
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]
        path = axon_config_file()
        if not path.is_file():
            return ""
        val = tomllib.loads(path.read_text(encoding="utf-8", errors="replace")).get("api_key", "")
        return str(val) if usable(str(val)) else ""
    except Exception:
        return ""


def _quote(value: str) -> str:
    return f"'{value}'" if '"' in value and "'" not in value else f'"{value}"'


def _atomic_write(path: Path, text: str, private: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if private:
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


def _line_pattern(var: str) -> re.Pattern[str]:
    return re.compile(rf"^(\s*(?:export\s+)?){re.escape(var)}\s*=")


def set_env_value(path: Path, var: str, value: str) -> Path:
    """Write ``var=value`` into a dotenv file atomically (owner-only). The first existing line for
    ``var`` is replaced in place (an ``export`` prefix is kept), duplicates are dropped, every other
    line - comments included - is preserved."""
    if not ENV_NAME.match(var or ""):
        raise ValueError(f"invalid environment variable name: {var!r}")
    clean = clean_key(value)
    if not clean or "\n" in clean or "\r" in clean:
        raise ValueError("the value must be a non-empty single line")
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        existing = []
    pat = _line_pattern(var)
    out: list[str] = []
    replaced = False
    for line in existing:
        m = pat.match(line)
        if m:
            if not replaced:
                out.append(f"{m.group(1)}{var}={_quote(clean)}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        while out and not out[-1].strip():
            out.pop()
        out.append(f"{var}={_quote(clean)}")
    _atomic_write(path, "\n".join(out).rstrip("\n") + "\n")
    return path


def remove_env_value(path: Path, var: str) -> bool:
    """Delete every line defining ``var``; other lines are preserved. False when it wasn't there."""
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    pat = _line_pattern(var)
    kept = [ln for ln in existing if not pat.match(ln)]
    if len(kept) == len(existing):
        return False
    _atomic_write(path, ("\n".join(kept).rstrip("\n") + "\n") if any(k.strip() for k in kept) else "")
    return True


def ensure_env_file(path: Path) -> Path:
    if not path.exists():
        _atomic_write(path, "# API keys for Axon (NAME=\"value\"). Keep this file private; never commit it.\n")
    return path


# ─── Source detection ──────────────────────────────────────────────────────

@dataclass
class KeyStatus:
    var: str
    value: str = ""
    source: str = ""                      # env | project | user | config | ""
    path: Path | None = None
    also_in: list[str] = field(default_factory=list)   # other places that define it (shadowed)

    @property
    def is_set(self) -> bool:
        return usable(self.value)

    @property
    def masked(self) -> str:
        return mask_key(self.value)

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, "")

    def files(self, workspace: Path | str | None = None) -> list[tuple[str, Path]]:
        """(scope, path) of the dotenv files that define this variable."""
        out: list[tuple[str, Path]] = []
        for scope in ("project", "user"):
            p = env_file_for(scope, workspace)
            if self.var in read_env_file(p):
                out.append((scope, p))
        return out


def key_status(var: str, workspace: Path | str | None = None, environ: Any = None) -> KeyStatus:
    """Where the effective value of ``var`` comes from (mirrors Settings.load's load order)."""
    env = os.environ if environ is None else environ
    user_vals = read_env_file(user_env_path())
    proj_path = project_env_path(workspace) if workspace is not None else None
    proj_vals = read_env_file(proj_path) if proj_path is not None and proj_path != user_env_path() else {}
    defined = [(s, v) for s, v in (("project", proj_vals.get(var)), ("user", user_vals.get(var))) if usable(v)]
    cur = str(env.get(var, "") or "").strip()
    st = KeyStatus(var=var)
    if usable(cur):
        st.value = cur
        if proj_vals.get(var) == cur:
            st.source, st.path = "project", proj_path
        elif user_vals.get(var) == cur:
            st.source, st.path = "user", user_env_path()
        else:
            st.source = "env"
    elif defined:
        st.source, st.value = defined[0]
        st.path = proj_path if st.source == "project" else user_env_path()
    elif var == "AXON_API_KEY":
        cfg = _config_api_key()
        if cfg:
            st.value, st.source = cfg, "config"
            try:
                from axon.providers.catalog import axon_config_file
                st.path = axon_config_file()
            except Exception:
                pass
    st.also_in = [s for s, _ in defined if s != st.source]
    return st


def preset_vars(preset: ProviderPreset) -> list[str]:
    names = [v for v in (getattr(preset, "env_vars", ()) or ()) if v]
    if preset.env_var and preset.env_var not in names:
        names.insert(0, preset.env_var)
    if not names and preset.requires_key:
        names = ["AXON_API_KEY" if preset.id == "agentrouter" else f"{preset.id.upper().replace('-', '_')}_API_KEY"]
    return names


def primary_var(preset: ProviderPreset) -> str:
    names = preset_vars(preset)
    return names[0] if names else ""


def preset_status(preset: ProviderPreset, workspace: Path | str | None = None) -> KeyStatus:
    """Status of the first variable of ``preset`` that holds a key (else the primary, unset)."""
    names = preset_vars(preset)
    for var in names:
        st = key_status(var, workspace)
        if st.is_set:
            return st
    return KeyStatus(var=names[0] if names else "")


def key_presets() -> list[ProviderPreset]:
    return [p for p in PROVIDER_PRESETS if p.requires_key]


def resolve_target(token: str) -> tuple[ProviderPreset | None, str] | None:
    """A provider id/name or an environment variable name -> (preset or None, variable)."""
    t = (token or "").strip()
    if not t:
        return None
    preset = get_preset_by_id(t) or next((p for p in PROVIDER_PRESETS if p.name.lower() == t.lower()), None)
    if preset is not None:
        return preset, primary_var(preset)
    if ENV_NAME.match(t) and (t.isupper() or "_" in t):
        var = t.upper() if t.isupper() else t
        owner = next((p for p in PROVIDER_PRESETS if var in preset_vars(p)), None)
        return owner, var
    low = t.lower()
    preset = next((p for p in PROVIDER_PRESETS if low in p.id.lower() or low in p.name.lower()), None)
    if preset is not None:
        return preset, primary_var(preset)
    return None


# ─── git-ignore ────────────────────────────────────────────────────────────

def _git(ws: Path, *args: str) -> tuple[int, str]:
    exe = shutil.which("git")
    if not exe:
        return 127, ""
    try:
        p = subprocess.run([exe, *args], cwd=str(ws), capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _gitignore_mentions_env(ws: Path) -> bool:
    try:
        lines = (ws / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    pats = {".env", "/.env", ".env*", "*.env", ".env.*", "/.env*"}
    return any(ln.strip() in pats for ln in lines)


def gitignore_state(workspace: Path | str) -> str:
    """'ignored' | 'not-ignored' | 'tracked' (committed to git!) | 'no-repo'."""
    ws = Path(workspace)
    code, out = _git(ws, "rev-parse", "--is-inside-work-tree")
    if code == 127:
        if not (ws / ".git").exists():
            return "no-repo"
        return "ignored" if _gitignore_mentions_env(ws) else "not-ignored"
    if code != 0 or out != "true":
        return "no-repo"
    if _git(ws, "ls-files", "--error-unmatch", ".env")[0] == 0:
        return "tracked"
    return "ignored" if _git(ws, "check-ignore", "-q", ".env")[0] == 0 else "not-ignored"


def ensure_gitignored(workspace: Path | str) -> tuple[str, str]:
    """Make sure the project's .env cannot be committed. Returns (state, message for the user)."""
    ws = Path(workspace)
    state = gitignore_state(ws)
    if state == "ignored":
        return state, ".env is git-ignored"
    if state == "tracked":
        return state, (".env is tracked by git - the key would be committed. Run `git rm --cached .env`, "
                       "add .env to .gitignore and rotate any key that was already pushed.")
    if state == "no-repo":
        return state, "not a git repository - keep .env out of version control if you add one"
    gi = ws / ".gitignore"
    try:
        text = gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
        sep = "" if not text or text.endswith("\n") else "\n"
        gi.write_text(text + sep + ("\n" if text.strip() else "") + "# local secrets (added by axon)\n.env\n",
                      encoding="utf-8")
    except OSError as e:
        return state, f".env is NOT git-ignored and .gitignore could not be updated ({e}); add it by hand"
    return "ignored", f"added .env to {gi}"


# ─── Verification ──────────────────────────────────────────────────────────

def verify_fn(preset: Any, key: str, base_url: str | None = None, model: str | None = None) -> tuple[bool, str]:
    """Live check (a 5-token request). Indirection so tests can replace it."""
    from axon.providers.verifier import verify_api_key
    return verify_api_key(preset, key, base_url=base_url, model=model)


def _is_fixture_key(key: str) -> bool:
    return key.startswith(_TEST_KEY_PREFIXES) or key.endswith("-test")


def count_models(preset: ProviderPreset | None, key: str, base_url: str | None = None, timeout: float = 6.0) -> int | None:
    """Number of models the endpoint lists for this key (None when unknown / unsupported)."""
    if _is_fixture_key(key):
        return None
    try:
        import httpx
        from axon.providers.catalog import effective_base_url
        from axon.providers.transport import _api_root, default_headers, is_azure, is_official_anthropic
        url = base_url or (effective_base_url(preset) if preset is not None else "")
        if not url or is_azure(url):
            return None
        headers = default_headers(url)
        fmt = preset.api_format if preset is not None else ("anthropic" if is_official_anthropic(url) else "openai")
        if fmt == "anthropic":
            base = url.rstrip("/").removesuffix("/v1")
            endpoint = f"{base}/v1/models"
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            if not is_official_anthropic(url):
                headers["authorization"] = f"Bearer {key}"
        else:
            endpoint = f"{_api_root(url)}/models"
            if key and key != "local":
                headers["authorization"] = f"Bearer {key}"
        resp = httpx.get(endpoint, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("data", data.get("models")) if isinstance(data, dict) else data
        return len(items) if isinstance(items, list) else None
    except Exception:
        return None


@dataclass
class KeyTest:
    ok: bool
    message: str
    latency_s: float = 0.0
    models: int | None = None

    def summary(self) -> str:
        bits = [f"{self.latency_s * 1000:.0f} ms"]
        if self.models is not None:
            bits.append(f"{self.models} model{'s' if self.models != 1 else ''} available")
        return " · ".join(bits)


def test_key(preset: ProviderPreset | None, key: str, base_url: str | None = None, model: str | None = None,
             with_models: bool = True) -> KeyTest:
    t0 = time.perf_counter()
    try:
        good, msg = verify_fn(preset, key, base_url=base_url, model=model)
    except Exception as e:
        good, msg = False, f"{type(e).__name__}: {e}"
    latency = time.perf_counter() - t0
    models = count_models(preset, key, base_url) if (good and with_models) else None
    return KeyTest(bool(good), str(msg or ("OK" if good else "failed")), latency, models)


test_key.__test__ = False  # type: ignore[attr-defined]  (not a pytest test)


def _checks_path() -> Path:
    from axon.storage import axon_home
    return axon_home() / "cache" / "key_checks.json"


def fingerprint(value: str) -> str:
    return hashlib.sha256(clean_key(value).encode("utf-8")).hexdigest()[:16]


def _load_checks() -> dict[str, Any]:
    try:
        data = json.loads(_checks_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_check(var: str, value: str, ok: bool, latency_s: float = 0.0) -> None:
    """Remember the outcome of a live check (stores a fingerprint, never the key)."""
    data = _load_checks()
    data[var] = {"fp": fingerprint(value), "ok": bool(ok), "ts": int(time.time()), "ms": int(latency_s * 1000)}
    try:
        _atomic_write(_checks_path(), json.dumps(data, indent=1, sort_keys=True) + "\n")
    except OSError:
        pass


def check_state(var: str, value: str) -> str:
    """'verified' | 'failed' | 'untested' for the current value of ``var``."""
    rec = _load_checks().get(var)
    if not usable(value) or not isinstance(rec, dict) or rec.get("fp") != fingerprint(value):
        return "untested"
    return "verified" if rec.get("ok") else "failed"


def register_secret(value: str) -> None:
    """Mask this literal value in the activity journal and other redacted records."""
    try:
        from axon.session import activity
        activity.add_secret(value)
    except Exception:
        pass


# ─── Redaction of key-bearing command lines ────────────────────────────────

_KEY_CMD = re.compile(r"^(\s*/(?:keys|key|apikey))(?=\s|$)(.*)$", re.IGNORECASE | re.DOTALL)
_SET_WORDS = {"set", "add", "replace", "update", "new"}
_SAFE_WORDS = {"", "list", "ls", "status", "test", "check", "verify", "remove", "rm", "delete", "unset",
               "where", "path", "paths", "files", "edit", "open", "help", "menu"}


def redact_keys_arg(arg: str) -> str:
    """The argument string of a /keys command with any key value replaced by [REDACTED]."""
    words = (arg or "").split()
    if not words or words[0].lower() in _SAFE_WORDS - {""}:
        return arg
    out: list[str] = []
    i = 0
    if words[0].lower() in _SET_WORDS:
        out.append(words[0])
        i = 1
    if i < len(words):
        target = words[i]
        if "=" in target and not target.startswith("-"):
            name, _, val = target.partition("=")
            out.append(f"{name}={REDACTED}" if val else target)
        else:
            out.append(target)
        i += 1
    redacted = False
    for w in words[i:]:
        if w.startswith("--") or w in ("-y", "-p"):
            out.append(w)
        elif not redacted:
            out.append(REDACTED)
            redacted = True
    return " ".join(out)


def redact_command_line(text: str) -> str:
    """``/keys set gemini AIza…`` -> ``/keys set gemini [REDACTED]`` (anything else is returned unchanged).

    Apply before a typed line is echoed, written to prompt history or journaled."""
    if not text:
        return text
    m = _KEY_CMD.match(text)
    if not m:
        return text
    rest = m.group(2)
    red = redact_keys_arg(rest.strip())
    return m.group(1) + (" " + red if red else "")


__all__ = [
    "KeyStatus", "KeyTest", "REDACTED", "SOURCE_LABELS", "check_state", "clean_key", "count_models", "ensure_env_file",
    "ensure_gitignored", "env_file_for", "fingerprint", "gitignore_state", "key_presets", "key_status", "mask_key",
    "preset_status", "preset_vars", "primary_var", "project_env_path", "record_check", "redact_command_line",
    "redact_keys_arg", "register_secret", "remove_env_value", "resolve_target", "scope_label", "set_env_value",
    "test_key", "usable", "user_env_path", "verify_fn",
]
