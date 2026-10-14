"""
Repository map: a fast, incremental, ranked outline of the workspace (aider-style).

* Files come from ``axon.tools._files.workspace_files`` (git-aware, .gitignore honoured).
* Symbols per file come from ``axon.tools.code_graph``'s extractor for Python, JS/TS, Go and
  Rust, and from built-in regex extractors for Java, C#, Kotlin, Swift, C/C++, Ruby and PHP
  (and for every language when code_graph is unavailable).
* The index is cached at ``storage.project_dir(ws)/index.json`` keyed by ``mtime_ns + size``,
  so a re-index only re-parses files that changed.
* Files are ranked by how often other files reference their symbols, entrypoint names, recent
  git activity and size. The rendered map is deterministic for a given tree (prompt-cache safe).

Public API:
    build_index(ws, rebuild=False) -> tuple[dict, IndexStats]
    build_repo_map(ws, max_tokens=1500, subpath="") -> str
    repo_map_for_prompt(ws_or_settings, settings=None, max_tokens=None) -> str | None
Commands: /map [path] [--tokens N], /index [--rebuild].  CLI: ``axon map [path] [--tokens N]``.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from axon.features import FeatureCommand

INDEX_VERSION = 2
INDEX_FILE = "index.json"
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 20_000
MAX_IDENTS_PER_FILE = 4000
DEFAULT_TOKENS = 1500

# Extension → language. Superset of code_graph's table.
LANGUAGES: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift", ".cs": "csharp",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".rb": "ruby", ".php": "php",
}

# Languages for which code_graph has a dedicated (better than generic) extractor.
_CODE_GRAPH_LANGS = frozenset({"python", "javascript", "typescript", "go", "rust"})

ENTRYPOINT_NAMES = frozenset({
    "main.py", "__main__.py", "cli.py", "app.py", "manage.py", "server.py", "wsgi.py", "asgi.py",
    "index.js", "index.ts", "index.tsx", "main.js", "main.ts", "main.tsx", "app.js", "app.ts",
    "app.tsx", "server.js", "server.ts", "main.go", "main.rs", "lib.rs", "mod.rs", "main.c",
    "main.cpp", "Main.java", "Application.java", "Program.cs", "Startup.cs", "index.php",
    "application.rb", "routes.rb", "config.ru",
})

_KEYWORDS = frozenset("""
and as assert async await break case catch class const continue def default del delete do elif
else enum except export extends false final finally for from func function global go if impl
import in interface is lambda let match mod module new nil none not null or package pass private
protected pub public raise return self static struct super switch this throw true try type
typeof use var void while with yield fn mut ref let impl trait where loop crate unsafe extern
string int bool float double char long short byte object list dict str len print println
""".split())

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_SHOWN_KINDS = frozenset({"class", "function", "method", "interface", "type", "struct", "enum",
                          "trait", "impl", "module", "record", "protocol", "object", "union",
                          "symbol", "constant"})


# ─── symbol extraction ──────────────────────────────────────────────────────

try:  # reuse the shared extractor when available
    from axon.tools.code_graph import MultiLanguageSymbolExtractor as _CG  # type: ignore
except Exception:  # pragma: no cover - exercised only when code_graph is broken/missing
    _CG = None


_MODS = r"(?:(?:public|private|protected|internal|static|abstract|final|sealed|partial|data|open|" \
        r"virtual|override|async|synchronized|native|extern|inline|unsafe|readonly|new)\s+)"
_RX: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "python": [
        (re.compile(r"^(\s*)class\s+([A-Za-z_]\w*)\s*(\([^)]*\))?"), "class"),
        (re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*(\([^)]*\)?)"), "function"),
    ],
    "javascript": [
        (re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
        (re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*(\([^)]*\)?)"), "function"),
        (re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"), "function"),
        (re.compile(r"^(\s*)(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"), "interface"),
        (re.compile(r"^(\s*)(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*="), "type"),
    ],
    "go": [
        (re.compile(r"^()func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*(\([^)]*\)?)"), "function"),
        (re.compile(r"^()type\s+([A-Za-z_]\w*)\s+(struct|interface)"), "struct"),
    ],
    "rust": [
        (re.compile(r"^(\s*)(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*(\([^)]*\)?)"), "function"),
        (re.compile(r"^(\s*)(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|union)\s+([A-Za-z_]\w*)"), "struct"),
        (re.compile(r"^(\s*)(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_]\w*)"), "trait"),
        (re.compile(r"^(\s*)impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_]\w*)"), "impl"),
    ],
    "java": [
        (re.compile(r"^(\s*)" + _MODS + r"*(?:class|interface|enum|record|@interface)\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"^(\s*)" + _MODS + r"+(?:<[^>]+>\s+)?[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*(\([^)]*\)?)\s*(?:throws\s+[\w., ]+)?\s*\{?\s*$"), "method"),
    ],
    "csharp": [
        (re.compile(r"^(\s*)" + _MODS + r"*(?:class|interface|enum|record|struct)\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"^(\s*)" + _MODS + r"+[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*(\([^)]*\)?)\s*(?:where\s+.*)?\{?\s*$"), "method"),
    ],
    "kotlin": [
        (re.compile(r"^(\s*)" + _MODS + r"*(?:enum\s+|data\s+|sealed\s+)?(?:class|interface|object)\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"^(\s*)" + _MODS + r"*fun\s+(?:<[^>]+>\s+)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*(\([^)]*\)?)"), "function"),
    ],
    "swift": [
        (re.compile(r"^(\s*)" + _MODS + r"*(?:final\s+)?(?:class|struct|enum|protocol|extension|actor)\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"^(\s*)" + _MODS + r"*(?:@\w+\s+)*func\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*(\([^)]*\)?)"), "function"),
    ],
    "c": [
        (re.compile(r"^()(?:typedef\s+)?(?:struct|enum|union)\s+([A-Za-z_]\w*)\s*\{?\s*$"), "struct"),
        (re.compile(r"^()(?!(?:if|for|while|switch|return|else|do|case|typedef)\b)(?:[A-Za-z_][\w*&<>,:]*\s+)+\**([A-Za-z_]\w*)\s*(\([^;]*\)?)\s*\{?\s*$"), "function"),
        (re.compile(r"^()#\s*define\s+([A-Za-z_]\w*)\("), "function"),
    ],
    "cpp": [
        (re.compile(r"^(\s*)(?:template\s*<[^>]*>\s*)?(?:typedef\s+)?(?:class|struct|enum(?:\s+class)?|union|namespace)\s+([A-Za-z_]\w*)\s*(?:final\s*)?(?:[:{]|$)"), "class"),
        (re.compile(r"^()(?!(?:if|for|while|switch|return|else|do|case|typedef|using|namespace)\b)(?:[A-Za-z_][\w*&<>,:]*\s+)+[*&]*((?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*)\s*(\([^;]*\)?)\s*(?:const\s*)?(?:override\s*)?\{?\s*$"), "function"),
    ],
    "ruby": [
        (re.compile(r"^(\s*)(?:class|module)\s+([A-Z]\w*(?:::\w+)*)"), "class"),
        (re.compile(r"^(\s*)def\s+((?:self\.)?[A-Za-z_]\w*[?!=]?)\s*(\([^)]*\)?)?"), "method"),
    ],
    "php": [
        (re.compile(r"^(\s*)(?:abstract\s+|final\s+|readonly\s+)*(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)"), "class"),
        (re.compile(r"^(\s*)(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+&?([A-Za-z_]\w*)\s*(\([^)]*\)?)"), "function"),
    ],
}
_RX["typescript"] = _RX["javascript"]

_CLASSY = frozenset({"class", "struct", "interface", "trait", "impl", "enum", "record", "protocol", "object"})


def _regex_symbols(text: str, lang: str) -> list[list[Any]]:
    """[name, kind, line, signature, parent] via line regexes; nesting from indentation."""
    pats = _RX.get(lang)
    if not pats:
        return []
    out: list[list[Any]] = []
    stack: list[tuple[int, str]] = []  # (indent, class name)
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if not stripped:
            continue
        if stripped.startswith(("//", "/*", "*", "--")):
            continue
        if stripped.startswith("#") and not re.match(r"#\s*define\b", stripped):
            continue
        indent = len(line) - len(stripped)
        for pat, kind in pats:
            m = pat.match(line)
            if not m:
                continue
            name = m.group(2)
            if not name or name in _KEYWORDS:
                break
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else None
            k = kind
            if k == "function" and parent:
                k = "method"
            if k == "method" and not parent and lang in ("python",):
                k = "function"
            sig = stripped.rstrip("{: \t")
            out.append([name, k, lineno, sig[:100], parent])
            if kind in _CLASSY or kind == "class":
                stack.append((indent, name))
            break
    return out


def extract_symbols(path: Path, rel: str, lang: str, text: str) -> list[list[Any]]:
    """Symbols of one file as compact lists ``[name, kind, line, signature, parent]``."""
    if _CG is not None and lang in _CODE_GRAPH_LANGS:
        try:
            nodes = _CG.extract_symbols(path, rel)
            out = []
            for n in nodes:
                kind = n.kind
                if kind in ("error",):
                    continue
                if kind == "variable":
                    if not n.name.isupper():
                        continue
                    kind = "constant"
                out.append([n.name, kind, int(n.line_number), (n.signature or n.name)[:100], n.parent])
            if out or lang != "python":
                return out
        except Exception:
            pass
    return _regex_symbols(text, lang)


def _identifiers(text: str) -> list[str]:
    seen: set[str] = set()
    for m in _IDENT_RE.finditer(text):
        w = m.group(0)
        if w in seen or w.lower() in _KEYWORDS:
            continue
        seen.add(w)
        if len(seen) >= MAX_IDENTS_PER_FILE:
            break
    return sorted(seen)


# ─── index ──────────────────────────────────────────────────────────────────

@dataclass
class IndexStats:
    files: int = 0
    symbols: int = 0
    reused: int = 0
    updated: int = 0
    removed: int = 0
    elapsed: float = 0.0
    languages: Counter = field(default_factory=Counter)
    index_path: str = ""


def _ws(ws: Any) -> Path:
    return Path(getattr(ws, "workspace", ws) or ".").resolve()


def index_path(ws: Path | str) -> Path:
    from axon import storage
    return storage.project_dir(Path(ws)) / INDEX_FILE


def _load_index(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == INDEX_VERSION and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": INDEX_VERSION, "files": {}}


def _save_index(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


_INDEX_LOCK = threading.Lock()


def build_index(ws: Path | str, rebuild: bool = False) -> tuple[dict[str, Any], IndexStats]:
    """Scan the workspace, re-parsing only files whose mtime/size changed. Returns (index, stats)."""
    t0 = time.perf_counter()
    root = _ws(ws)
    stats = IndexStats()
    with _INDEX_LOCK:
        ipath = index_path(root)
        stats.index_path = str(ipath)
        old = {"version": INDEX_VERSION, "files": {}} if rebuild else _load_index(ipath)
        old_files: dict[str, Any] = old.get("files", {})
        try:
            from axon.tools._files import workspace_files
            rels = workspace_files(root, limit=MAX_FILES)
        except Exception:
            rels = []
        new_files: dict[str, Any] = {}
        for rel in rels:
            lang = LANGUAGES.get(os.path.splitext(rel)[1].lower())
            if not lang:
                continue
            fp = root / rel
            try:
                st = fp.stat()
            except OSError:
                continue
            if st.st_size > MAX_FILE_BYTES:
                continue
            prev = old_files.get(rel)
            if prev and prev.get("m") == st.st_mtime_ns and prev.get("s") == st.st_size:
                new_files[rel] = prev
                stats.reused += 1
            else:
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                new_files[rel] = {
                    "m": st.st_mtime_ns, "s": st.st_size, "l": lang,
                    "sym": extract_symbols(fp, rel, lang, text),
                    "id": _identifiers(text),
                }
                stats.updated += 1
            stats.languages[lang] += 1
            stats.symbols += len(new_files[rel]["sym"])
        stats.removed = len(set(old_files) - set(new_files))
        stats.files = len(new_files)
        index = {"version": INDEX_VERSION, "root": str(root), "files": new_files}
        if stats.updated or stats.removed or rebuild or not ipath.exists():
            _save_index(ipath, index)
    stats.elapsed = time.perf_counter() - t0
    return index, stats


# ─── ranking ────────────────────────────────────────────────────────────────

def _git_recent_files(root: Path, n_commits: int = 200) -> list[str]:
    """Workspace-relative paths in order of most recent commit activity (most recent first)."""
    git = shutil.which("git")
    if not git or not any((p / ".git").exists() for p in (root, *root.parents)):
        return []
    try:
        res = subprocess.run(
            [git, "-c", "core.quotepath=off", "log", f"-n{n_commits}", "--name-only", "--relative",
             "--format=", "--no-renames"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    seen: dict[str, None] = {}
    for line in res.stdout.splitlines():
        line = line.strip().replace("\\", "/")
        if line and line not in seen:
            seen[line] = None
    return list(seen)


_TEST_RE = re.compile(r"(^|/)(tests?|spec|__tests__|testdata|fixtures)(/|$)|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$")


@dataclass
class RankedFile:
    path: str
    score: float
    symbols: list[list[Any]]
    symbol_refs: dict[str, float]


def rank_files(index: dict[str, Any], recent: Iterable[str] = ()) -> list[RankedFile]:
    files: dict[str, Any] = index.get("files", {})
    defs: dict[str, list[str]] = defaultdict(list)
    const_names: set[str] = set()
    for rel in sorted(files):
        names = {s[0] for s in files[rel]["sym"] if s[1] in _SHOWN_KINDS and len(s[0]) >= 3}
        const_names.update(s[0] for s in files[rel]["sym"] if s[1] == "constant")
        for name in names:
            defs[name].append(rel)
    # Names defined in many files (``run``, ``__init__``...) carry no signal.
    defs = {k: v for k, v in defs.items() if len(v) <= 6 and not (k.startswith("__") and k.endswith("__"))}

    file_refs: dict[str, float] = defaultdict(float)
    sym_refs: dict[tuple[str, str], float] = defaultdict(float)
    for rel in sorted(files):
        for ident in files[rel].get("id", ()):
            owners = defs.get(ident)
            if not owners:
                continue
            w = (0.3 if ident in const_names else 1.0) / len(owners)
            for owner in owners:
                if owner != rel:
                    file_refs[owner] += w
                    sym_refs[(owner, ident)] += w

    recent_list = [r for r in recent if r in files]
    recent_rank = {p: i for i, p in enumerate(recent_list)}
    n_recent = max(1, len(recent_list))

    ranked: list[RankedFile] = []
    for rel in sorted(files):
        info = files[rel]
        score = 2.0 * math.log1p(file_refs.get(rel, 0.0))
        base = rel.rsplit("/", 1)[-1]
        if base in ENTRYPOINT_NAMES:
            score += 3.0
        if rel in recent_rank:
            score += 2.0 * (1.0 - recent_rank[rel] / n_recent)
        score += 0.25 * math.log10(max(10, info.get("s", 0)))
        score += 0.05 * min(40, len(info["sym"]))
        if _TEST_RE.search(rel):
            score *= 0.4
        if not info["sym"]:
            score *= 0.3
        refs = {name: v for (owner, name), v in sym_refs.items() if owner == rel}
        ranked.append(RankedFile(rel, round(score, 6), info["sym"], refs))
    ranked.sort(key=lambda r: (-r.score, r.path))
    return ranked


# ─── rendering ──────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


_SELF_RE = re.compile(r"\((?:self|cls)(?:,\s*)?")


def _sig(sym: list[Any]) -> str:
    name, kind, _line, sig, _parent = sym
    sig = _SELF_RE.sub("(", str(sig or name)).strip()
    if len(sig) > 80:
        sig = sig[:77] + "..."
    if kind == "constant" and "=" in sig:
        sig = sig.split("=", 1)[0].strip()
    return sig


_KIND_PRIORITY = {"class": 0, "interface": 0, "struct": 0, "trait": 0, "enum": 1, "type": 1, "impl": 1,
                  "function": 1, "method": 2, "symbol": 2, "constant": 4}


def _file_block(rf: RankedFile, max_syms: int) -> list[str]:
    all_syms = [s for s in rf.symbols if s[1] in _SHOWN_KINDS]
    syms = all_syms
    if len(syms) > max_syms:
        # Keep the most referenced (classes/functions before constants), shown in source order;
        # a kept method always brings its class along so the tree stays truthful.
        order = sorted(syms, key=lambda s: (-rf.symbol_refs.get(s[0], 0.0) * (0.3 if s[1] == "constant" else 1.0),
                                            _KIND_PRIORITY.get(s[1], 3), s[2], s[0]))
        by_name = {s[0]: s for s in syms if s[1] in _CLASSY}
        keep: dict[int, list[Any]] = {}
        for s in order:
            if len(keep) >= max_syms:
                break
            parent = by_name.get(s[4]) if s[4] else None
            if parent is not None and id(parent) not in keep:
                if len(keep) + 2 > max_syms:
                    continue
                keep[id(parent)] = parent
            keep[id(s)] = s
        syms = [s for s in syms if id(s) in keep]
    parents = {s[0] for s in syms if s[1] in _CLASSY}
    lines = []
    for s in syms:
        indent = "    " if s[4] and s[4] in parents else "  "
        lines.append(f"{indent}{_sig(s)}")
    hidden = len(all_syms) - len(syms)
    if hidden > 0:
        lines.append(f"  ... +{hidden} more")
    return lines


def render_map(ranked: list[RankedFile], max_tokens: int = DEFAULT_TOKENS, header: bool = True) -> str:
    """Greedy, budgeted selection by rank; rendered as a directory-grouped tree sorted by path."""
    budget = max(64, int(max_tokens))
    chosen: dict[str, list[str]] = {}
    dirs: set[str] = set()
    used = 40 if header else 0
    for rf in ranked:
        d, _, name = rf.path.rpartition("/")
        dir_cost = 0 if d in dirs else estimate_tokens(d + "/\n") + 1
        block = None
        for max_syms in (8, 4, 1, 0):
            lines = _file_block(rf, max_syms) if max_syms else []
            cost = estimate_tokens(" " + name + "\n" + "\n".join(" " + ln for ln in lines)) + 1 + dir_cost
            if used + cost <= budget:
                dirs.add(d)
                block = lines
                used += cost
                break
        if block is None:
            if budget - used < 8:
                break
            continue
        chosen[rf.path] = block
    out: list[str] = []
    if header:
        out.append(f"Repository map ({len(chosen)} of {len(ranked)} source files; "
                   "ranked by references, entrypoints and recent changes):")
    last_dir = None
    for path in sorted(chosen, key=lambda p: (p.rpartition("/")[0], p)):
        d, _, name = path.rpartition("/")
        if d != last_dir:
            out.append(f"{d}/" if d else "./")
            last_dir = d
        out.append(f" {name}")
        out.extend(" " + ln for ln in chosen[path])
    rest = len(ranked) - len(chosen)
    if rest > 0:
        out.append(f"(+{rest} more files not shown)")
    return "\n".join(out)


def build_repo_map(ws: Path | str, max_tokens: int = DEFAULT_TOKENS, subpath: str = "") -> str:
    """Compact, ranked, deterministic repository map within ``max_tokens``."""
    root = _ws(ws)
    index, _ = build_index(root)
    return _map_from_index(root, index, max_tokens, subpath)


def _map_from_index(root: Path, index: dict[str, Any], max_tokens: int, subpath: str = "") -> str:
    prefix = subpath.replace("\\", "/").strip().strip("/")
    if prefix in (".", ""):
        prefix = ""
    if prefix:
        files = {k: v for k, v in index.get("files", {}).items() if k == prefix or k.startswith(prefix + "/")}
        index = {**index, "files": files}
    ranked = rank_files(index, _git_recent_files(root))
    if not ranked:
        return ""
    return render_map(ranked, max_tokens)


_PROMPT_CACHE: dict[str, tuple[float, str | None]] = {}
_PROMPT_TTL = 20.0


def repo_map_for_prompt(ws: Any, settings: Any = None, max_tokens: int | None = None) -> str | None:
    """Map text for the system prompt, or None when ``settings.repo_map`` is off/unset or empty.

    ``ws`` may be a path or a Settings object (its ``.workspace`` is used and it doubles as settings).
    ``settings.repo_map_tokens`` (int) overrides the token budget."""
    if settings is None and hasattr(ws, "workspace"):
        settings = ws
    if settings is None or not getattr(settings, "repo_map", False):
        return None
    root = _ws(ws)
    budget = max_tokens or int(getattr(settings, "repo_map_tokens", 0) or DEFAULT_TOKENS)
    key = f"{root}|{budget}"
    now = time.monotonic()
    hit = _PROMPT_CACHE.get(key)
    if hit and now - hit[0] < _PROMPT_TTL:
        return hit[1]
    try:
        text = build_repo_map(root, max_tokens=budget) or None
    except Exception:
        text = None
    _PROMPT_CACHE[key] = (now, text)
    return text


# ─── output helpers ─────────────────────────────────────────────────────────

def _theme() -> dict[str, str]:
    try:
        from axon.ui import theme as t
        return {k: getattr(t, k, "") for k in ("BOLD", "DIM", "RST", "CYAN", "TEAL", "GOLD", "MINT", "SLATE", "ROSE")}
    except Exception:
        return {k: "" for k in ("BOLD", "DIM", "RST", "CYAN", "TEAL", "GOLD", "MINT", "SLATE", "ROSE")}


def _out(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _block_char() -> str:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█".encode(enc)
        return "█"
    except (UnicodeEncodeError, LookupError):
        return "#"


def _bar(n: int, total: int, width: int = 24) -> str:
    if total <= 0 or n <= 0:
        return ""
    return _block_char() * max(1, round(width * n / total))


def _parse_args(arg: str) -> tuple[str, int, bool, bool]:
    path, tokens, rebuild, bad = "", DEFAULT_TOKENS, False, False
    parts = (arg or "").split()
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("--tokens", "-t") and i + 1 < len(parts):
            try:
                tokens = max(64, int(parts[i + 1]))
            except ValueError:
                bad = True
            i += 2
            continue
        if p.startswith("--tokens="):
            try:
                tokens = max(64, int(p.split("=", 1)[1]))
            except ValueError:
                bad = True
        elif p in ("--rebuild", "-r", "--force"):
            rebuild = True
        elif not path:
            path = p
        i += 1
    return path, tokens, rebuild, bad


def _agent_ws(agent: Any) -> Path:
    settings = getattr(agent, "settings", None)
    return _ws(getattr(settings, "workspace", None) or Path.cwd())


def _stats_line(stats: IndexStats, c: dict[str, str]) -> str:
    return (f"{c['DIM']}{stats.files} files · {stats.symbols} symbols · "
            f"{stats.updated} parsed, {stats.reused} cached"
            + (f", {stats.removed} removed" if stats.removed else "")
            + f" · {stats.elapsed * 1000:.0f} ms{c['RST']}")


# ─── commands ───────────────────────────────────────────────────────────────

def handle_map(agent: Any, arg: str) -> None:
    c = _theme()
    path, tokens, _rebuild, bad = _parse_args(arg)
    if bad:
        _out(f"{c['ROSE']}Usage: /map [path] [--tokens N]{c['RST']}")
        return
    root = _agent_ws(agent)
    if path:
        p = Path(path)
        target = (root / p) if not p.is_absolute() else p
        try:
            path = target.resolve().relative_to(root).as_posix()
        except ValueError:
            _out(f"{c['ROSE']}Path is outside the workspace: {path}{c['RST']}")
            return
        if path == ".":
            path = ""
    t0 = time.perf_counter()
    index, stats = build_index(root)
    text = _map_from_index(root, index, tokens, path)
    total = time.perf_counter() - t0
    if not text:
        _out(f"{c['GOLD']}No indexable source files{(' under ' + path) if path else ''}.{c['RST']}")
        _out(_stats_line(stats, c))
        return
    _out(f"{c['BOLD']}{c['CYAN']}Repository map{c['RST']}  {c['DIM']}{root}{('/' + path) if path else ''}{c['RST']}")
    _out()
    for line in text.splitlines()[1:]:
        if line.endswith("/") and not line.startswith(" "):
            _out(f"{c['TEAL']}{line}{c['RST']}")
        elif line.startswith("  "):
            _out(f"{c['DIM']}{line}{c['RST']}")
        else:
            _out(line)
    _out()
    _out(_stats_line(stats, c) + f"{c['DIM']} · map ~{estimate_tokens(text)}/{tokens} tok · "
         f"total {total * 1000:.0f} ms{c['RST']}")


def handle_index(agent: Any, arg: str) -> None:
    c = _theme()
    _path, _tokens, rebuild, _bad = _parse_args(arg)
    root = _agent_ws(agent)
    _out(f"{c['DIM']}{'Rebuilding' if rebuild else 'Updating'} index for {root} ...{c['RST']}")
    _index, stats = build_index(root, rebuild=rebuild)
    _out(f"{c['BOLD']}{c['CYAN']}Index {'rebuilt' if rebuild else 'updated'}{c['RST']}")
    rows = [
        ("files scanned", str(stats.files)),
        ("symbols", str(stats.symbols)),
        ("parsed / cached", f"{stats.updated} / {stats.reused}"),
        ("removed", str(stats.removed)),
        ("time", f"{stats.elapsed * 1000:.0f} ms"),
        ("stored at", stats.index_path),
    ]
    for k, v in rows:
        _out(f"  {c['SLATE']}{k:<16}{c['RST']} {v}")
    if stats.languages:
        _out()
        _out(f"  {c['BOLD']}Languages{c['RST']}")
        total = sum(stats.languages.values())
        width = max(len(k) for k in stats.languages)
        for lang, n in sorted(stats.languages.items(), key=lambda kv: (-kv[1], kv[0])):
            pct = 100.0 * n / total
            _out(f"  {lang:<{width}}  {c['MINT']}{_bar(n, total):<24}{c['RST']} {n:>5}  {c['DIM']}{pct:4.1f}%{c['RST']}")


def _cli_map(argv: list[str]) -> int:
    class _A:
        settings = type("S", (), {"workspace": Path.cwd()})()
    handle_map(_A(), " ".join(argv))
    return 0


COMMANDS = [
    FeatureCommand(name="/map", handler=handle_map, category="Code",
                   description="Ranked repository map: files and their key symbols",
                   usage="/map [path] [--tokens N]", aliases=("/repomap",)),
    FeatureCommand(name="/index", handler=handle_index, category="Code",
                   description="Build or refresh the incremental symbol index",
                   usage="/index [--rebuild]"),
]
CLI_SUBCOMMANDS = {"map": _cli_map}

__all__ = [
    "build_index", "build_repo_map", "repo_map_for_prompt", "rank_files", "render_map",
    "extract_symbols", "IndexStats", "COMMANDS", "CLI_SUBCOMMANDS",
]
