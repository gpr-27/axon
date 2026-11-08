"""
Plugin marketplaces (Claude Code compatible).

A marketplace is a directory with ``.claude-plugin/marketplace.json`` (``name``, ``owner``, ``plugins``)::

    {"name": "my-marketplace", "owner": {"name": "Me"},
     "plugins": [{"name": "my-first-plugin", "source": "./plugins/my-first-plugin", "description": "..."}]}

It is added from a local directory or JSON file, a GitHub ``owner/repo[#ref]``, any git URL (``https://``
or ``git@host:path``, optional ``#ref``) or a hosted ``https://.../marketplace.json``. A plugin entry's
``source`` is a path relative to the marketplace root, ``{"source": "github", "repo", "ref"}``,
``{"source": "git-subdir", "url", "path", "ref"}`` or ``{"source": "url", "url", "ref"}``
(``archive`` / ``npm`` / ``command`` sources are not supported). ``name@marketplace`` installs a plugin
into ``~/.axon/plugins`` (user scope) or ``<ws>/.axon/plugins`` (project / local scope), records it in
``.axon-install.json`` and sets ``enabledPlugins`` in that scope's settings file.

State: ``~/.axon/plugins/marketplaces.json`` (name -> source) and clones under
``~/.axon/plugins/marketplaces/<name>/``. Local marketplaces are read in place.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from axon.platform import rmtree as _rmtree
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axon.storage import axon_home as _axon_home
from axon.commands import plugins as pl

MAX_MARKETPLACE_BYTES = 5_000_000
COMMIT_SIDECAR = "commit.txt"          # next to the fetched plugin (not inside it): the commit it came from
RESERVED_MARKETPLACE_ERROR = "The name '{name}' is reserved for official Anthropic marketplaces"


class MarketplaceError(RuntimeError):
    pass


def state_file() -> Path:
    return _axon_home() / "plugins" / "marketplaces.json"


def clone_root() -> Path:
    return _axon_home() / "plugins" / "marketplaces"


def load_state() -> dict[str, dict[str, Any]]:
    data = pl._read_json_dict(state_file()) or {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    from axon.session.store import atomic_write_text
    state_file().parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_file(), json.dumps(state, indent=2) + "\n")


# ─── sources ────────────────────────────────────────────────────────────────

def parse_source(text: str) -> dict[str, Any]:
    """Classify a marketplace source string: path | github | git | url."""
    s = text.strip()
    if not s:
        raise MarketplaceError("A marketplace source is required")
    ref = ""
    base = s
    if "#" in s and not s.lower().endswith(".json"):
        base, _, ref = s.partition("#")
    if base.lower().startswith(("http://", "https://")):
        if base.lower().split("?")[0].endswith(".json"):
            if not base.lower().startswith("https://"):
                raise MarketplaceError("A hosted marketplace.json must use https://")
            return {"kind": "url", "url": base}
        return {"kind": "git", "url": base, "ref": ref}
    if re.match(r"^[\w.-]+@[\w.-]+:", base) or base.startswith("ssh://"):
        return {"kind": "git", "url": base, "ref": ref}
    p = Path(base).expanduser()
    explicit = base.startswith(("./", "../", "/", "~", ".\\", "..\\")) or re.match(r"^[A-Za-z]:[\\/]", base)
    if explicit or p.exists():
        return {"kind": "path", "path": str(p.resolve())}
    if re.fullmatch(r"[\w.-]+/[\w.-]+", base):
        return {"kind": "github", "repo": base, "ref": ref}
    raise MarketplaceError(
        f"Could not interpret '{text}'. Use owner/repo, a git URL, an https://…/marketplace.json URL, or a path starting with ./ or /")


def _run_git(git: str, args: list[str], cwd: Path | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    return subprocess.run([git, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL, env=env)


def head_sha(repo: Path) -> str:
    """Full commit id of HEAD in ``repo`` ('' when it cannot be read)."""
    git = shutil.which("git")
    if not git:
        return ""
    proc = _run_git(git, ["-C", str(repo), "rev-parse", "HEAD"], timeout=30)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_clone(url: str, dest: Path, ref: str = "", sha: str = "") -> None:
    """Clone ``url`` into ``dest``. With ``sha`` the result is that exact commit (verified against HEAD): a
    shallow ``git fetch <sha>`` first, a full clone when the server refuses to serve a bare commit."""
    git = shutil.which("git")
    if not git:
        raise MarketplaceError("git is required for this marketplace or plugin source")
    sha = sha.strip().lower()
    if sha and not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise MarketplaceError(f"not a commit sha: {sha!r}")

    def fail(message: str) -> None:
        _rmtree(dest, ignore_errors=True)
        raise MarketplaceError(message)

    if sha and len(sha) == 40:
        dest.mkdir(parents=True, exist_ok=True)
        ok = all(_run_git(git, a, cwd=dest, timeout=180).returncode == 0 for a in (
            ["init", "-q"], ["remote", "add", "origin", url], ["fetch", "-q", "--depth", "1", "origin", sha],
            ["checkout", "-q", "FETCH_HEAD"]))
        if ok and head_sha(dest) == sha:
            return
        _rmtree(dest, ignore_errors=True)          # the server would not serve the bare commit
    cmd = ["clone"] + ([] if sha else ["--depth", "1"])
    if ref and not sha:
        cmd += ["--branch", ref]
    proc = _run_git(git, cmd + ["--", url, str(dest)])
    if proc.returncode != 0:
        fail(f"git clone failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    if sha:
        co = _run_git(git, ["-C", str(dest), "checkout", "--quiet", sha], timeout=60)
        if co.returncode != 0:
            fail(f"could not check out {sha}: {co.stderr.strip()[:200]}")
        if not head_sha(dest).startswith(sha):
            fail(f"checked out {head_sha(dest)[:12]} instead of {sha[:12]}")


def _github_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def marketplace_file(root: Path) -> Path | None:
    """``marketplace.json`` for a marketplace root directory (or the JSON file itself)."""
    if root.is_file():
        return root
    for cand in (root / ".claude-plugin" / "marketplace.json", root / "marketplace.json"):
        if cand.is_file():
            return cand
    return None


def marketplace_root_of(path: Path) -> Path:
    """Directory that relative plugin sources resolve against."""
    if path.is_file():
        return path.parent.parent if path.parent.name == ".claude-plugin" else path.parent
    return path


def find_skill_dirs(root: Path, limit: int = 200) -> list[Path]:
    """Skill folders (holding a SKILL.md) of a source: the root itself, ``root/skills/*`` or ``root/*``."""
    root = Path(root)
    if root.is_file():
        return []
    if (root / "SKILL.md").is_file():
        return [root]
    found: list[Path] = []
    for base in (root / "skills", root):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.name.startswith(".") or d.name in ("node_modules", "__pycache__") or d.is_symlink() or not d.is_dir():
                continue
            if (d / "SKILL.md").is_file():
                found.append(d)
        if found:
            break
    return found[:limit]


def read_marketplace(root: Path) -> dict[str, Any]:
    f = marketplace_file(root)
    if f is None and find_skill_dirs(root):       # a skills-only source: no marketplace.json, just skill folders
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(root).name).strip("-") or "skills"
        return {"name": slug, "owner": {"name": "local"}, "plugins": [], "skillsOnly": True}
    if f is None:
        raise MarketplaceError(f"No .claude-plugin/marketplace.json (and no skill folders with a SKILL.md) in {root}")
    try:
        if f.stat().st_size > MAX_MARKETPLACE_BYTES:
            raise MarketplaceError("marketplace.json is too large")
        data = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise MarketplaceError(f"json: Invalid JSON syntax: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise MarketplaceError("marketplace.json must be an object with a 'plugins' array")
    return data


def marketplace_dir(name: str, entry: dict[str, Any]) -> Path:
    """Where a registered marketplace's files are."""
    if entry.get("kind") == "path":
        return Path(entry["path"])
    return clone_root() / name


# ─── validation ─────────────────────────────────────────────────────────────

def _valid_marketplace_name(name: Any) -> bool:
    return isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name))


def validate_marketplace(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = read_marketplace(root)
    except MarketplaceError as e:
        return [str(e)], warnings
    name = data.get("name")
    if not _valid_marketplace_name(name):
        errors.append("name: required (letters, digits, '.', '_' and '-')")
    elif name in pl.OFFICIAL_MARKETPLACES:
        warnings.append(f"name: '{name}' is reserved for official Anthropic marketplaces and is refused when added")
    owner = data.get("owner")
    if not isinstance(owner, dict) or not owner.get("name"):
        errors.append("owner: Invalid input (an object with a name)")
    base = marketplace_root_of(Path(root))
    seen: set[str] = set()
    for i, entry in enumerate(data.get("plugins", [])):
        tag = f"plugins[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{tag}: must be an object")
            continue
        pname = entry.get("name")
        if not _valid_marketplace_name(pname):
            errors.append(f"{tag}.name: required")
            continue
        if pname in seen:
            errors.append(f"{tag}.name: duplicate plugin name '{pname}'")
        seen.add(pname)
        src = entry.get("source")
        if isinstance(src, str):
            if ".." in Path(src).parts:
                errors.append(f'{tag}.source: Path contains "..": {src}')
                continue
            if not src.startswith("./"):
                errors.append(f'{tag}.source: relative path must start with "./" ({src})')
            target = (base / src).resolve()
            if target.is_dir():
                e, w = pl.validate_plugin_dir(target)
                errors += [f"{tag} plugin.json → {x}" for x in e]
                warnings += [f"{tag} plugin.json → {x}" for x in w]
                mname = (pl._read_json_dict(pl._manifest_path(target) or target / "x") or {}).get("name")
                if mname and mname != pname:
                    warnings.append(f"{tag}: entry name '{pname}' differs from plugin.json name '{mname}'")
        elif isinstance(src, dict):
            kind = src.get("source")
            if kind not in ("github", "git-subdir", "url", "archive", "npm", "command"):
                errors.append(f"{tag}.source.source: unknown source type '{kind}'")
            elif kind == "github" and not src.get("repo"):
                errors.append(f"{tag}.source.repo: required")
            elif kind in ("git-subdir", "url") and not src.get("url"):
                errors.append(f"{tag}.source.url: required")
            elif kind == "git-subdir" and not src.get("path"):
                errors.append(f"{tag}.source.path: required")
            if kind in ("archive", "npm", "command"):
                warnings.append(f"{tag}.source: '{kind}' sources are not supported by Axon")
        else:
            errors.append(f"{tag}.source: required")
    for k in data:
        if k not in ("$schema", "name", "owner", "plugins", "description", "metadata", "version", "allowCrossMarketplaceDependenciesOn"):
            warnings.append(f"Unknown field '{k}' is ignored")
    return errors, warnings


# ─── add / list / update / remove ───────────────────────────────────────────

def add_marketplace(source: str) -> tuple[str, dict[str, Any]]:
    """Register a marketplace; returns (name, state entry). Raises MarketplaceError."""
    spec = parse_source(source)
    tmp_clone: Path | None = None
    try:
        if spec["kind"] == "path":
            root = Path(spec["path"])
            if not root.exists():
                raise MarketplaceError(f"{root} does not exist")
            data = read_marketplace(root)
        elif spec["kind"] == "url":
            data = _fetch_hosted(spec["url"])
            root = None  # type: ignore[assignment]
        else:
            url = _github_url(spec["repo"]) if spec["kind"] == "github" else spec["url"]
            tmp_clone = Path(tempfile.mkdtemp(prefix="axon-mkt-"))
            git_clone(url, tmp_clone / "repo", spec.get("ref", ""))
            root = tmp_clone / "repo"
            data = read_marketplace(root)
        name = data.get("name")
        if data.get("skillsOnly"):
            base = (spec.get("repo", "").split("/")[-1] if spec["kind"] == "github"
                    else re.sub(r"\.git$", "", (spec.get("url") or spec.get("path") or "").rstrip("/").split("/")[-1]))
            base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-") or "skills"
            name = base if base.endswith("skills") else base + "-skills"
        if not _valid_marketplace_name(name):
            raise MarketplaceError("marketplace.json has no valid 'name'")
        if name in pl.OFFICIAL_MARKETPLACES:
            raise MarketplaceError(RESERVED_MARKETPLACE_ERROR.format(name=name))
        errors, _ = validate_marketplace(root) if root is not None else ([], [])
        if errors:
            raise MarketplaceError("Invalid marketplace: " + "; ".join(errors[:3]))
        state = load_state()
        entry: dict[str, Any] = {**spec, "added": int(time.time())}
        if data.get("skillsOnly"):
            entry["skillsOnly"] = True
        dest = clone_root() / name
        if spec["kind"] in ("github", "git"):
            _rmtree(dest, ignore_errors=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root), str(dest))
        elif spec["kind"] == "url":
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".claude-plugin").mkdir(exist_ok=True)
            (dest / ".claude-plugin" / "marketplace.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        state[name] = entry
        save_state(state)
        return name, entry
    finally:
        if tmp_clone is not None:
            _rmtree(tmp_clone, ignore_errors=True)


def _fetch_hosted(url: str) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "axon-plugin-marketplace"})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - https only (checked by parse_source)
            raw = r.read(MAX_MARKETPLACE_BYTES + 1)
        if len(raw) > MAX_MARKETPLACE_BYTES:
            raise MarketplaceError("marketplace.json is too large")
        data = json.loads(raw.decode("utf-8-sig"))
    except MarketplaceError:
        raise
    except (OSError, ValueError) as e:
        raise MarketplaceError(f"could not fetch {url}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise MarketplaceError("marketplace.json must be an object with a 'plugins' array")
    return data


def list_marketplaces() -> list[tuple[str, dict[str, Any]]]:
    return sorted(load_state().items())


def update_marketplace(name: str) -> int:
    """Refresh a marketplace listing (git pull / re-download / re-read). Returns the plugin count."""
    state = load_state()
    entry = state.get(name)
    if entry is None:
        raise MarketplaceError(f"Marketplace '{name}' is not added")
    kind = entry.get("kind")
    root = marketplace_dir(name, entry)
    if kind in ("github", "git"):
        git = shutil.which("git")
        if not git:
            raise MarketplaceError("git is required to update this marketplace")
        proc = subprocess.run([git, "-C", str(root), "pull", "--ff-only", "--quiet"], capture_output=True, text=True,
                              timeout=180, stdin=subprocess.DEVNULL,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"})
        if proc.returncode != 0:
            raise MarketplaceError(f"git pull failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    elif kind == "url":
        data = _fetch_hosted(entry["url"])
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    data = read_marketplace(root)
    entry["updated"] = int(time.time())
    state[name] = entry
    save_state(state)
    return len(data.get("plugins", []))


def remove_marketplace(name: str, workspace: Path) -> list[str]:
    """Unregister a marketplace and uninstall every plugin installed from it. Returns removed plugin ids."""
    state = load_state()
    entry = state.pop(name, None)
    if entry is None:
        raise MarketplaceError(f"Marketplace '{name}' is not added")
    removed: list[str] = []
    for p in pl.load_plugins(workspace):
        if p.marketplace == name:
            uninstall(p, workspace)
            removed.append(p.id)
    if entry.get("kind") != "path":
        _rmtree(marketplace_dir(name, entry), ignore_errors=True)
    save_state(state)
    return removed


def marketplace_plugins(name: str) -> list[dict[str, Any]]:
    entry = load_state().get(name)
    if entry is None:
        raise MarketplaceError(f"Marketplace '{name}' is not added")
    return [e for e in read_marketplace(marketplace_dir(name, entry)).get("plugins", []) if isinstance(e, dict)]


def find_plugin_entry(plugin: str, marketplace: str | None = None) -> tuple[str, dict[str, Any]]:
    """(marketplace name, entry) for ``plugin`` (searching every marketplace when none is named)."""
    state = load_state()
    names = [marketplace] if marketplace else sorted(state)
    if marketplace and marketplace not in state:
        raise MarketplaceError(f"Marketplace '{marketplace}' is not added. /plugin marketplace add <source>")
    hits: list[tuple[str, dict[str, Any]]] = []
    for m in names:
        try:
            for e in marketplace_plugins(m):
                if e.get("name") == plugin:
                    hits.append((m, e))
        except MarketplaceError:
            continue
    if not hits:
        where = f'marketplace "{marketplace}"' if marketplace else "any added marketplace"
        raise MarketplaceError(f'Plugin "{plugin}" not found in {where}')
    if len(hits) > 1:
        raise MarketplaceError(f'"{plugin}" is in several marketplaces ({", ".join(m for m, _ in hits)}); use {plugin}@<marketplace>')
    return hits[0]


# ─── install / update / uninstall ───────────────────────────────────────────

def _copy_plugin(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))


def materialize(marketplace: str, entry: dict[str, Any], workdir: Path) -> Path:
    """Fetch the plugin source of ``entry`` into ``workdir`` and return the plugin root."""
    src = entry.get("source")
    state = load_state()
    mroot = marketplace_dir(marketplace, state[marketplace]) if marketplace in state else None
    if isinstance(src, str):
        if mroot is None:
            raise MarketplaceError("this marketplace has no files on disk; its plugins need object sources")
        if ".." in Path(src).parts:
            raise MarketplaceError(f'Path contains "..": {src}')
        base = marketplace_root_of(mroot)
        target = (base / src).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError as e:
            raise MarketplaceError(f"Source path escapes the marketplace: {src}") from e
        if not target.is_dir():
            raise MarketplaceError(f"Source path does not exist: {target}")
        out = workdir / "plugin"
        _copy_plugin(target, out)
        return out
    if not isinstance(src, dict):
        raise MarketplaceError("plugin entry has no source")
    kind = src.get("source")
    ref, sha = str(src.get("ref") or ""), str(src.get("sha") or "")
    if kind == "github":
        out = workdir / "plugin"
        git_clone(_github_url(str(src.get("repo"))), out, ref, sha)
        (workdir / COMMIT_SIDECAR).write_text(head_sha(out), encoding="utf-8")
        _rmtree(out / ".git", ignore_errors=True)
        return out
    if kind in ("url", "git-subdir"):
        url = str(src.get("url") or "")
        if kind == "git-subdir" and re.fullmatch(r"[\w.-]+/[\w.-]+", url):
            url = _github_url(url)
        clone = workdir / "clone"
        git_clone(url, clone, ref, sha)
        (workdir / COMMIT_SIDECAR).write_text(head_sha(clone), encoding="utf-8")
        if kind == "git-subdir":
            sub = str(src.get("path") or "")
            if ".." in Path(sub).parts:
                raise MarketplaceError(f'Path contains "..": {sub}')
            target = (clone / sub).resolve()
            try:
                target.relative_to(clone.resolve())
            except ValueError as e:
                raise MarketplaceError("git-subdir path escapes the repository") from e
            if not target.is_dir():
                raise MarketplaceError(f"git-subdir path not found: {sub}")
            out = workdir / "plugin"
            _copy_plugin(target, out)
            return out
        _rmtree(clone / ".git", ignore_errors=True)
        return clone
    raise MarketplaceError(f"Plugin source type '{kind}' is not supported by Axon (use a path, github, git-subdir or url source)")


# ─── integrity: what a plugin runs, what is installed, what an update changes ───

_RECORD_ONLY = frozenset({pl.INSTALL_RECORD})


def content_hash(plugin_dir: Path) -> str:
    """Hash of every file of an installed plugin (the install record itself excluded)."""
    from axon.skills import bundle
    return bundle.hash_dir(plugin_dir, _RECORD_ONLY)


def plugin_runs(plugin_dir: Path, workspace: Path, name: str = "") -> list[str]:
    """Every hook command and MCP server a plugin folder would start, in full ("what enabling it runs")."""
    from axon.hooks import trust
    mp = pl._manifest_path(plugin_dir)
    manifest = (pl._read_json_dict(mp) if mp else {}) or {}
    plug = pl.Plugin(name=name or str(manifest.get("name") or plugin_dir.name), path=plugin_dir, scope="user",
                     manifest=manifest)
    try:
        items = trust.hook_items(pl.plugin_hooks(plug, workspace), "hooks", "hook")
        items += trust._mcp_items(pl.plugin_mcp_servers(plug, workspace), "mcp", "mcp")
    except Exception:  # noqa: BLE001 - a broken manifest runs nothing
        return []
    return [i.line() for i in items]


def interactive_review(lines: list[str], question: str = "Enable it?") -> bool:
    """Show ``lines`` and ask, but only in a terminal; anywhere else the caller keeps its own behaviour."""
    if not (sys.stdin and sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    print("\n".join(lines))
    try:
        return input(f"  {question} [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def install_review_lines(name: str, runs: list[str], commit: str) -> list[str]:
    head = f"  Plugin {name}" + (f" at commit {commit[:10]}" if commit else "") + " would run:"
    return [head] + ([f"    {r}" for r in runs] if runs else ["    (no hooks and no MCP servers; commands, agents and skills only)"])


def read_commit(workdir: Path) -> str:
    try:
        return (workdir / COMMIT_SIDECAR).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def install_root(workspace: Path, scope: str) -> Path:
    return (pl._axon_home() / "plugins") if scope == "user" else (Path(workspace) / ".axon" / "plugins")


def split_id(spec: str) -> tuple[str, str | None]:
    name, _, market = spec.strip().partition("@")
    return name, (market or None)


def install(spec: str, workspace: Path, scope: str = "user", _depth: int = 0,
            review: Any = interactive_review) -> tuple[pl.Plugin, list[str]]:
    """Install ``name[@marketplace]``; returns (plugin, ids of dependencies installed too).
    ``review(lines) -> bool`` sees what the plugin would run (hooks, MCP commands) before it is copied in and
    enabled; the default asks in a terminal and says yes elsewhere. False aborts with nothing installed."""
    if scope not in ("user", "project", "local"):
        raise MarketplaceError("scope must be user, project or local")
    name, market = split_id(spec)
    market_name, entry = find_plugin_entry(name, market)
    root = install_root(workspace, scope)
    dest = root / name
    if dest.exists():
        raise MarketplaceError(f"Plugin '{name}' is already installed at {dest}")
    root.mkdir(parents=True, exist_ok=True)
    from axon.hooks import trust
    was_ok = scope != "user" and trust.status(workspace) in ("none", "trusted")
    work = Path(tempfile.mkdtemp(prefix="axon-plugin-"))
    try:
        src_root = materialize(market_name, entry, work)
        errors, _ = pl.validate_plugin_dir(src_root) if (pl._manifest_path(src_root) or pl._has_standard_layout(src_root)) else ([], [])
        hard = [e for e in errors if "path escapes" in e or "traversal" in e]
        if hard:
            raise MarketplaceError("Plugin rejected: " + hard[0])
        commit = read_commit(work)
        runs = plugin_runs(src_root, workspace, name)
        if review is not None and not review(install_review_lines(f"{name}@{market_name}", runs, commit)):
            raise MarketplaceError("installation cancelled; nothing was installed")
        _copy_plugin(src_root, dest)
    finally:
        _rmtree(work, ignore_errors=True)
    mp = pl._manifest_path(dest)
    manifest = pl._read_json_dict(mp) if mp else {}
    manifest = manifest or {}
    record = {
        "name": name, "marketplace": market_name, "scope": scope, "version": str(manifest.get("version") or entry.get("version") or ""),
        "source": entry.get("source"), "entry": {k: v for k, v in entry.items() if k != "source"},
        "installed_at": int(time.time()), "commit": commit, "content_hash": content_hash(dest),
    }
    (dest / pl.INSTALL_RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    plugin_id = f"{name}@{market_name}"
    pl.set_enabled_in_settings(workspace, plugin_id, True, scope)
    if was_ok:                 # the user just reviewed this install: it does not make the folder untrusted
        trust.trust(workspace)
    installed_deps: list[str] = []
    merged = pl.merge_marketplace_entry(manifest, record["entry"], mp is not None)
    if _depth < 5:
        for dep in _normalize_deps(merged.get("dependencies"), market_name):
            dname, dmarket = dep
            if any(p.name == dname for p in pl.load_plugins(workspace)):
                continue
            try:
                p2, more = install(f"{dname}@{dmarket}", workspace, scope, _depth + 1, review)
                installed_deps += [p2.id] + more
            except MarketplaceError as e:
                raise MarketplaceError(f"Installed {plugin_id}, but dependency {dname}@{dmarket} failed: {e}") from e
    plugin = next((p for p in pl.load_plugins(workspace) if p.name == name), None)
    if plugin is None:
        raise MarketplaceError(f"Installed {plugin_id} but it could not be loaded from {dest}")
    return plugin, installed_deps


def _normalize_deps(deps: Any, default_market: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for d in deps if isinstance(deps, list) else []:
        if isinstance(d, str) and d.strip():
            n, m = split_id(d)
            out.append((n, m or default_market))
        elif isinstance(d, dict) and d.get("name"):
            out.append((str(d["name"]), str(d.get("marketplace") or default_market)))
    return out


def dependents_of(plugin: pl.Plugin, workspace: Path) -> list[pl.Plugin]:
    """Enabled plugins that declare ``plugin`` as a dependency."""
    out = []
    for p in pl.load_plugins(workspace):
        if p.enabled and p.name != plugin.name:
            for dn, _ in _normalize_deps(p.manifest.get("dependencies"), p.marketplace):
                if dn == plugin.name:
                    out.append(p)
    return out


def uninstall(plugin: pl.Plugin, workspace: Path) -> None:
    """Delete the plugin folder, its ``enabledPlugins`` entries and its (default) data directory."""
    for scope in ("user", "project", "local"):
        try:
            pl.set_enabled_in_settings(workspace, plugin.id, None, scope)
            if plugin.id != plugin.name:
                pl.set_enabled_in_settings(workspace, plugin.name, None, scope)
        except (OSError, ValueError):   # a settings file we must not rewrite: the stale entry is harmless
            pass
    pl.remove_plugin(plugin)
    _rmtree(pl.plugin_data_dir(plugin), ignore_errors=True)


@dataclass
class UpdateReview:
    """What an update would change, shown before anything is replaced."""
    plugin: str
    lines: list[str]
    runs_changed: bool = False        # a hook or MCP command was added or edited
    local_edits: bool = False         # the installed files differ from what was recorded at install
    old_commit: str = ""
    new_commit: str = ""


def update_plugin(plugin: pl.Plugin, workspace: Path, review: Any = None) -> bool:
    """Re-fetch an installed marketplace plugin; True when the version / content was replaced.
    ``review(UpdateReview) -> bool`` is called with the file changes and the changed hook / MCP commands
    before anything is replaced; False leaves the plugin untouched (returns False)."""
    if not plugin.marketplace:
        raise MarketplaceError(f"'{plugin.name}' was not installed from a marketplace")
    from axon.skills import bundle as bundle_mod
    market = plugin.marketplace
    _, entry = find_plugin_entry(plugin.name, market)
    work = Path(tempfile.mkdtemp(prefix="axon-plugin-"))
    try:
        src_root = materialize(market, entry, work)
        mp = pl._manifest_path(src_root)
        new_manifest = (pl._read_json_dict(mp) if mp else {}) or {}
        new_version = str(new_manifest.get("version") or entry.get("version") or "")
        new_commit = read_commit(work)
        old_files = bundle_mod.read_dir(plugin.path, _RECORD_ONLY)
        new_files = bundle_mod.read_dir(src_root, _RECORD_ONLY)
        same_content = bundle_mod.hash_files(old_files) == bundle_mod.hash_files(new_files)
        if same_content or (new_version and new_version == plugin.version and not new_commit):
            return False
        recorded = str(plugin.record.get("content_hash") or "")
        local_edits = bool(recorded) and bundle_mod.hash_files(old_files) != recorded
        old_runs, new_runs = plugin_runs(plugin.path, workspace, plugin.name), plugin_runs(src_root, workspace, plugin.name)
        runs_changed = sorted(old_runs) != sorted(new_runs)
        info = UpdateReview(plugin.name, [], runs_changed, local_edits, str(plugin.record.get("commit") or ""), new_commit)
        info.lines = ([f"  {plugin.id}: {plugin.version or '?'}@{info.old_commit[:10] or '?'} -> "
                       f"{new_version or '?'}@{new_commit[:10] or '?'}"]
                      + (["  WARNING: installed files were edited after install; updating overwrites them."] if local_edits else [])
                      + bundle_mod.diff_summary(old_files, new_files)
                      + ([f"    now runs: {r}" for r in new_runs if r not in old_runs]
                         + [f"    no longer runs: {r}" for r in old_runs if r not in new_runs] if runs_changed else []))
        if review is not None and not review(info):
            return False
        record = dict(plugin.record)
        record["version"] = new_version
        record["entry"] = {k: v for k, v in entry.items() if k != "source"}
        record["source"] = entry.get("source")
        record["updated_at"] = int(time.time())
        record["commit"] = new_commit
        backup = plugin.path.with_name(plugin.path.name + ".old")
        _rmtree(backup, ignore_errors=True)
        plugin.path.rename(backup)
        try:
            _copy_plugin(src_root, plugin.path)
            record["content_hash"] = content_hash(plugin.path)
            (plugin.path / pl.INSTALL_RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        except Exception:
            _rmtree(plugin.path, ignore_errors=True)
            backup.rename(plugin.path)
            raise
        _rmtree(backup, ignore_errors=True)
        return True
    finally:
        _rmtree(work, ignore_errors=True)
