"""
Hierarchical memory: instruction files, rules, auto memory and learned items.

Load order (broadest first; files are concatenated, never overridden):

1. Managed policy  /Library/Application Support/Axon/AXON.md (macOS), /etc/axon/AXON.md (Linux),
                   C:\\Program Files\\Axon\\AXON.md (Windows)  (+ ClaudeCode/CLAUDE.md compat paths)
2. User            ~/.axon/AXON.md, then ~/.axon/rules/**/*.md
3. Project         for every directory from the filesystem root down to the workspace:
                   AXON.md, .axon/AXON.md, CLAUDE.md, .claude/CLAUDE.md, unconditional
                   .axon/rules/**/*.md (+ .claude/rules), then AXON.local.md / CLAUDE.local.md.
                   AGENTS.md / .axon/AGENTS.md are read only when no AXON/CLAUDE file exists
                   in the chain (setting ``instruction_files``).
4. On demand       AXON.md files in subdirectories below the workspace and path-scoped rules
                   (frontmatter ``paths:``) load the first time a tool touches a matching file.
5. Auto memory     first 200 lines / 25 KB of ~/.axon/projects/<project>/memory/MEMORY.md
6. Saved items     ~/.axon/memory/*.md and <ws>/.axon/memory/*.md

``@path`` imports (relative to the importing file, ``~`` and absolute allowed, ``\\ `` escapes a
space, quoted paths and code spans/fences are ignored) are followed up to 4 hops. Block-level
``<!-- -->`` comments are stripped. Files over 4 MiB are skipped.

Nothing here writes into the workspace except :meth:`MemoryStore.learn` / :meth:`MemoryStore.add`
with ``scope="project"``, which only the user triggers.
"""
from __future__ import annotations
import fnmatch
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MAX_IMPORT_DEPTH = 4
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILE_CHARS = 60_000
LONG_FILE_LINES = 200
AUTO_MEMORY_MAX_LINES = 200
AUTO_MEMORY_MAX_BYTES = 25 * 1024
BRACE_BUDGET = 1000

# Project-level primary instruction files per directory, in load order.
PRIMARY_FILES: tuple[str, ...] = ("AXON.md", "axon.md", ".axon/AXON.md", "CLAUDE.md", ".claude/CLAUDE.md")
LOCAL_FILES: tuple[str, ...] = ("AXON.local.md", "CLAUDE.local.md")
AGENTS_FILES: tuple[str, ...] = ("AGENTS.md", ".axon/AGENTS.md")
RULE_DIRS: tuple[str, ...] = (".axon/rules", ".claude/rules")
INSTRUCTION_FILENAMES: tuple[str, ...] = PRIMARY_FILES + (".axon/CLAUDE.md",) + LOCAL_FILES + AGENTS_FILES

_IMPORT_RE = re.compile(r"(?<![\w`@/\"'])@((?:~[/\\]|\.{1,2}[/\\]|/|[A-Za-z]:[/\\])?(?:\\ |[\w.\-~/\\])+)")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_NETWORK_PREFIXES = ("\\\\", "//", "/net/", "/Network/")


@dataclass
class MemoryItem:
    id: str
    title: str
    content: str
    category: str  # "conventions", "architecture", "debugging", "preferences"
    scope: str = "project"  # "global", "project"
    created_at: float = field(default_factory=time.time)


@dataclass
class InstructionFile:
    path: Path
    scope: str          # managed | user | project | auto
    content: str
    imports: list["InstructionFile"] = field(default_factory=list)
    reason: str = "startup"   # startup | on-demand | path-rule
    trigger: str = ""
    paths: list[str] = field(default_factory=list)  # path-scope globs for rules

    @property
    def tokens(self) -> int:
        return int(len(self.content) / 3.7) + sum(i.tokens for i in self.imports)


# ─── helpers ───────────────────────────────────────────────────────────────

def find_git_root(start: Path) -> Path | None:
    try:
        curr = start.resolve()
    except OSError:
        return None
    for parent in [curr, *curr.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _file_key(p: Path) -> tuple[int, int] | str:
    try:
        st = p.stat()
        if st.st_ino:
            return (st.st_dev, st.st_ino)
    except OSError:
        pass
    return os.path.normcase(str(p))


def _is_network_path(p: Path) -> bool:
    s = str(p)
    return s.startswith(_NETWORK_PREFIXES) or s.replace("\\", "/").startswith(_NETWORK_PREFIXES)


def _read_text(p: Path, warnings: list[str] | None = None) -> str:
    try:
        if _is_network_path(p) or _is_network_path(p.resolve()):
            return ""
        if p.stat().st_size > MAX_FILE_BYTES:
            if warnings is not None:
                warnings.append(f"Skipped {p} (larger than 4 MiB)")
            return ""
        raw = p.read_bytes()
    except (OSError, RuntimeError):  # RuntimeError: symlink loop on resolve()
        return ""
    try:
        from axon.config import decode_config_bytes
        text = decode_config_bytes(raw)
    except Exception:
        text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + "\n\n[... truncated ...]"
    return text


def strip_block_comments(text: str) -> str:
    """Remove ``<!-- -->`` comments outside fenced code blocks."""
    out: list[str] = []
    chunk: list[str] = []
    in_fence = False

    def flush() -> None:
        if chunk:
            out.append(_COMMENT_RE.sub("", "\n".join(chunk)))
            chunk.clear()

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            if not in_fence:
                flush()
                in_fence = True
                out.append(line)
            else:
                in_fence = False
                out.append(line)
            continue
        if in_fence:
            out.append(line)
        else:
            chunk.append(line)
    flush()
    return "\n".join(out)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a tiny YAML subset (``key: value``, ``key: [a, b]`` and ``- item`` lists)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, Any] = {}
    current: str | None = None
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if current and stripped.startswith("- "):
            meta.setdefault(current, []).append(stripped[2:].strip().strip("'\""))
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        current = None
        if not val:
            current = key
            meta[key] = []
        elif val.startswith("[") and val.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        else:
            meta[key] = val.strip("'\"")
    return meta, "\n".join(lines[end + 1:])


def expand_braces(pattern: str, budget: int = BRACE_BUDGET) -> list[str]:
    """``src/{a,b}/*.py`` -> [``src/a/*.py``, ``src/b/*.py``]; over budget the pattern is literal."""
    results = [pattern]
    while True:
        changed = False
        nxt: list[str] = []
        for p in results:
            m = re.search(r"\{([^{}]*)\}", p)
            if not m:
                nxt.append(p)
                continue
            changed = True
            for alt in m.group(1).split(","):
                nxt.append(p[: m.start()] + alt + p[m.end():])
            if len(nxt) > budget:
                return [pattern]
        results = nxt
        if not changed:
            return results


def glob_to_regex(pattern: str) -> re.Pattern[str] | None:
    """Glob with ``**`` (any depth), ``*``, ``?`` and ``[...]``; invalid ``[`` -> matches nothing."""
    pat = pattern.replace("\\", "/").lstrip("./") if not pattern.startswith("/") else pattern.replace("\\", "/")
    i, out = 0, ""
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 3] == "**/":
                out += "(?:.*/)?"
                i += 3
                continue
            if pat[i:i + 2] == "**":
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                return None
            body = pat[i + 1:j].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            out += f"[{body}]"
            i = j
        else:
            out += re.escape(c)
        i += 1
    try:
        return re.compile(out + r"\Z")
    except re.error:
        return None


def path_matches(rel_path: str, patterns: Iterable[str]) -> bool:
    rel = rel_path.replace("\\", "/")
    for p in patterns:
        for expanded in expand_braces(p):
            rx = glob_to_regex(expanded)
            if rx is not None and rx.match(rel):
                return True
    return False


# ─── imports ───────────────────────────────────────────────────────────────

def _import_targets(text: str) -> list[str]:
    found: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = re.sub(r"`[^`]*`", "", line)
        for m in _IMPORT_RE.finditer(stripped):
            raw = m.group(1).rstrip(".,;:)").replace("\\ ", " ")
            if raw:
                found.append(raw)
    return found


def _resolve_imports(text: str, base_dir: Path, scope: str, depth: int, seen: set[Any],
                     warnings: list[str] | None = None) -> list[InstructionFile]:
    if depth >= MAX_IMPORT_DEPTH:
        return []
    out: list[InstructionFile] = []
    for raw in _import_targets(text):
        if raw.startswith("~"):
            target = Path(os.path.expanduser(raw))
        elif os.path.isabs(raw):
            target = Path(raw)
        else:
            target = base_dir / raw
        try:
            if _is_network_path(target) or not target.is_file():
                continue
        except (OSError, RuntimeError):
            continue
        key = _file_key(target)
        if key in seen:
            continue
        seen.add(key)
        content = strip_block_comments(_read_text(target, warnings))
        node = InstructionFile(path=target, scope=scope, content=content, reason="import")
        node.imports = _resolve_imports(content, target.parent, scope, depth + 1, seen, warnings)
        out.append(node)
    return out


def _load_instruction(path: Path, scope: str, seen: set[Any], warnings: list[str] | None = None,
                      reason: str = "startup", trigger: str = "") -> InstructionFile | None:
    try:
        if not path.is_file():
            return None
    except (OSError, RuntimeError):
        return None
    key = _file_key(path)
    if key in seen:
        return None
    seen.add(key)
    text = _read_text(path, warnings)
    meta, body = split_frontmatter(text)
    body = strip_block_comments(body)
    if not body.strip():
        return None
    if warnings is not None and body.count("\n") > LONG_FILE_LINES:
        warnings.append(f"{path} has {body.count(chr(10))} lines (over {LONG_FILE_LINES}); consider trimming it")
    node = InstructionFile(path=path, scope=scope, content=body, reason=reason, trigger=trigger)
    paths = meta.get("paths")
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split(",") if p.strip()]
    if isinstance(paths, list):
        node.paths = [str(p) for p in paths if str(p).strip()]
    node.imports = _resolve_imports(body, path.parent, scope, 0, seen, warnings)
    return node


def managed_instruction_paths() -> list[Path]:
    if sys.platform == "darwin":
        bases = [Path("/Library/Application Support/Axon"), Path("/Library/Application Support/ClaudeCode")]
    elif sys.platform == "win32":
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        bases = [pf / "Axon", pf / "ClaudeCode"]
    else:
        bases = [Path("/etc/axon"), Path("/etc/claude-code")]
    return [bases[0] / "AXON.md", bases[1] / "CLAUDE.md"]


def _rule_files(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in directory.rglob("*.md") if p.is_file()) if directory.is_dir() else []
    except OSError:
        return []


def _ancestor_chain(ws: Path) -> list[Path]:
    return list(reversed([ws, *ws.parents]))


class MemoryLoader:
    """Loads memory once (startup) and on demand as tools touch files."""

    def __init__(self, workspace: Path, instruction_files: str = "axon-md-or-agents-md",
                 auto_memory: bool = True, excludes: Iterable[str] = ()) -> None:
        try:
            self.workspace = Path(workspace).resolve()
        except OSError:
            self.workspace = Path(workspace)
        self.mode = instruction_files
        self.auto_memory = auto_memory and os.environ.get("AXON_DISABLE_AUTO_MEMORY", "") not in ("1", "true")
        self.excludes = list(excludes)
        self.seen: set[Any] = set()
        self.warnings: list[str] = []
        self.loaded: list[InstructionFile] = []
        self.pending_rules: list[InstructionFile] = []   # path-scoped rules not yet triggered
        self._dirs_checked: set[str] = set()

    def _excluded(self, p: Path) -> bool:
        s = str(p).replace("\\", "/")
        return any(fnmatch.fnmatch(s, pat) for pat in self.excludes)

    def _take(self, path: Path, scope: str, reason: str = "startup", trigger: str = "") -> InstructionFile | None:
        if self._excluded(path):
            return None
        node = _load_instruction(path, scope, self.seen, self.warnings, reason, trigger)
        if node is None:
            return None
        if node.paths:
            self.pending_rules.append(node)
            return None
        self.loaded.append(node)
        return node

    def load_startup(self, include_user: bool = True, include_managed: bool = True) -> list[InstructionFile]:
        start = len(self.loaded)
        if include_managed:
            for p in managed_instruction_paths():
                self._take(p, "managed")
        home = Path.home()
        axon_home = _axon_home()
        if include_user:
            self._take(axon_home / "AXON.md", "user")
            for rule in _rule_files(axon_home / "rules"):
                self._take(rule, "user")
        if self.mode != "managed-only":
            chain = _ancestor_chain(self.workspace)
            has_primary = any((d / name).is_file() for d in chain for name in PRIMARY_FILES + LOCAL_FILES)
            for d in chain:
                if d in (home / ".axon", axon_home):
                    continue
                for name in PRIMARY_FILES:
                    self._take(d / name, "project")
                for rd in RULE_DIRS:
                    for rule in _rule_files(d / rd):
                        self._take(rule, "project")
                use_agents = self.mode == "axon-md-and-agents-md" or (self.mode == "axon-md-or-agents-md" and not has_primary)
                if use_agents:
                    for name in AGENTS_FILES:
                        self._take(d / name, "project")
                for name in LOCAL_FILES:
                    self._take(d / name, "project")
        if self.auto_memory:
            idx = auto_memory_dir(self.workspace) / "MEMORY.md"
            text = load_auto_memory_index(self.workspace)
            if text:
                node = InstructionFile(path=idx, scope="auto", content=text)
                self.seen.add(_file_key(idx))
                self.loaded.append(node)
        return self.loaded[start:]

    def on_file_access(self, file_path: Path | str) -> list[InstructionFile]:
        """Nested AXON.md files below the workspace and path-scoped rules triggered by a file access."""
        try:
            fp = Path(file_path)
            if not fp.is_absolute():
                fp = self.workspace / fp
            fp = fp.resolve()
        except (OSError, RuntimeError):
            return []
        new: list[InstructionFile] = []
        try:
            rel_dir = fp.parent.relative_to(self.workspace)
        except ValueError:
            rel_dir = None
        if rel_dir is not None and rel_dir.parts:
            d = self.workspace
            for part in rel_dir.parts:
                d = d / part
                key = str(d)
                if key in self._dirs_checked:
                    continue
                self._dirs_checked.add(key)
                for name in PRIMARY_FILES + LOCAL_FILES:
                    node = self._take(d / name, "project", "on-demand", str(fp))
                    if node:
                        new.append(node)
        for rule in list(self.pending_rules):
            root = rule.path.parent
            while root.name in ("rules",) or root.name in (".axon", ".claude"):
                root = root.parent
            try:
                rel = fp.relative_to(root).as_posix()
            except ValueError:
                continue
            if path_matches(rel, rule.paths):
                self.pending_rules.remove(rule)
                rule.reason, rule.trigger = "path-rule", rel
                self.loaded.append(rule)
                new.append(rule)
        return new

    def render(self, files: list[InstructionFile] | None = None) -> str:
        files = self.loaded if files is None else files
        blocks: list[str] = []
        for f in files:
            blocks.extend(_render_instruction(f, self.workspace))
        return "\n\n".join(b for b in blocks if b).strip()


# ─── Auto memory ───────────────────────────────────────────────────────────

def project_key(workspace: Path) -> str:
    from axon import storage
    return storage.project_key(workspace)


def auto_memory_dir(workspace: Path) -> Path:
    """~/.axon/projects/<project>/memory (``AXON_AUTO_MEMORY_DIR`` overrides)."""
    from axon import storage
    return storage.memory_dir(workspace)


def _axon_home() -> Path:
    from axon import storage
    return storage.axon_home()


def load_auto_memory_index(workspace: Path) -> str:
    idx = auto_memory_dir(workspace) / "MEMORY.md"
    try:
        if not idx.is_file():
            return ""
        raw = idx.read_bytes()[:AUTO_MEMORY_MAX_BYTES]
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="ignore")
    lines = text.splitlines()[:AUTO_MEMORY_MAX_LINES]
    return "\n".join(lines).strip()


def check_memory_index(path: Path) -> str | None:
    """Size check after a write to MEMORY.md: a reminder near the limit, an error over it."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    n_lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    if n_lines > AUTO_MEMORY_MAX_LINES or len(data) > AUTO_MEMORY_MAX_BYTES:
        return (f"Error: MEMORY.md is {n_lines} lines / {len(data):,} bytes, over the {AUTO_MEMORY_MAX_LINES}-line / "
                f"{AUTO_MEMORY_MAX_BYTES // 1024} KB load limit. Only the beginning is loaded each session: rewrite the "
                "index to one short line per memory and move details into topic files.")
    if n_lines > AUTO_MEMORY_MAX_LINES * 0.9 or len(data) > AUTO_MEMORY_MAX_BYTES * 0.9:
        return "Reminder: MEMORY.md is close to its load limit; keep entries to one short line each."
    return None


class AutoMemory:
    """Per-project auto memory: MEMORY.md index + topic files with ``type``/``modified`` frontmatter."""

    TYPES = ("user", "feedback", "project", "reference")

    def __init__(self, workspace: Path) -> None:
        self.dir = auto_memory_dir(workspace)
        self.index = self.dir / "MEMORY.md"

    def save(self, title: str, body: str, mem_type: str = "project") -> tuple[Path, str | None]:
        mem_type = mem_type if mem_type in self.TYPES else "project"
        self.dir.mkdir(parents=True, exist_ok=True)
        slug = _generate_clean_slug(title) or f"memory-{int(time.time())}"
        topic = self.dir / f"{slug}.md"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        topic.write_text(f"---\ntype: {mem_type}\nmodified: {stamp}\n---\n\n# {title.strip()}\n\n{body.strip()}\n", encoding="utf-8")
        line = f"- [{title.strip()}]({topic.name}): {' '.join(body.split())[:120]}"
        existing = self.index.read_text(encoding="utf-8", errors="replace") if self.index.is_file() else "# Memory index\n"
        kept = [ln for ln in existing.splitlines() if f"]({topic.name})" not in ln]
        self.index.write_text("\n".join(kept + [line]).rstrip() + "\n", encoding="utf-8")
        return topic, check_memory_index(self.index)

    def list(self) -> list[Path]:
        return sorted(p for p in self.dir.glob("*.md") if p.name != "MEMORY.md") if self.dir.is_dir() else []


# ─── rendering / compat API ────────────────────────────────────────────────

def discover_instruction_files(workspace: Path, include_user: bool = True) -> list[InstructionFile]:
    """Startup instruction files (managed, user, project chain, auto memory) in load order."""
    loader = MemoryLoader(workspace, auto_memory=include_user)
    return loader.load_startup(include_user=include_user, include_managed=include_user)


def _display_path(p: Path, workspace: Path) -> str:
    try:
        return p.resolve().relative_to(workspace.resolve()).as_posix()
    except (ValueError, OSError):
        home = Path.home()
        try:
            return "~/" + p.resolve().relative_to(home.resolve()).as_posix()
        except (ValueError, OSError):
            return str(p)


_SCOPE_LABEL = {"managed": "Managed policy", "user": "User instructions", "project": "Project instructions", "auto": "Auto memory"}


def _render_instruction(node: InstructionFile, workspace: Path, level: int = 0) -> list[str]:
    label = "Imported" if level else _SCOPE_LABEL.get(node.scope, "Instructions")
    if node.reason == "path-rule":
        label = f"Rule (applies to {', '.join(node.paths)})"
    parts = [f"### {label}: {_display_path(node.path, workspace)}", node.content.strip()]
    for child in node.imports:
        parts.extend(_render_instruction(child, workspace, level + 1))
    return parts


def render_instruction_files(workspace: Path, include_user: bool = True) -> str:
    loader = MemoryLoader(workspace, auto_memory=include_user)
    loader.load_startup(include_user=include_user, include_managed=include_user)
    return loader.render()


def compact_instructions(text: str) -> str:
    """Body of a ``## Compact Instructions`` section in instruction text (steers /compact)."""
    m = re.search(r"^##\s+Compact Instructions\s*$(.*?)(?=^#{1,2}\s|\Z)", text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


# ─── Learned memory items ───────────────────────────────────────────────────

def _generate_clean_slug(text: str) -> str:
    """Generate a readable, semantic slug without chopping words."""
    cleaned = text.strip()
    prefixes = [
        "this is the", "this is a", "please remember that", "always remember to",
        "i want to", "we want to", "remember that", "the project is", "note that",
    ]
    lowered = cleaned.lower()
    for p in prefixes:
        if lowered.startswith(p):
            cleaned = cleaned[len(p):].strip(" ,:.-")
            break

    words = re.findall(r"[a-z0-9]+", cleaned.lower())
    if not words:
        words = ["learned", "rule", str(int(time.time()))]

    stopwords = {"a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of", "with", "that", "this", "it", "is"}
    key_words = [w for w in words if w not in stopwords] or words
    slug = "-".join(key_words[:5])[:40].rstrip("-")
    return slug or f"rule-{int(time.time())}"


def _generate_clean_title(text: str) -> str:
    """Extract a clean title up to first punctuation or word boundary."""
    first_sentence = re.split(r"[.\n!?]", text.strip())[0].strip()
    if len(first_sentence) <= 70:
        return first_sentence
    truncated = first_sentence[:65].rsplit(" ", 1)[0]
    return truncated + "..."


def _fm_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            val = json.loads(raw)
            return str(val)
        except json.JSONDecodeError:
            return raw.strip('"')
    return raw.strip("'")


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class MemoryStore:
    def __init__(self, workspace: Path, global_dir: Path | None = None) -> None:
        self.workspace = Path(workspace)
        self.project_memory_dir = self.workspace / ".axon" / "memory"
        self.global_memory_dir = global_dir if global_dir is not None else _axon_home() / "memory"

    def learn(
        self,
        text: str,
        category: str = "conventions",
        scope: str = "project",
        custom_title: str | None = None,
    ) -> MemoryItem:
        """Save a learned rule or pattern to persistent memory (project or global)."""
        scope = "global" if scope in ("global", "user") else "project"
        slug = _generate_clean_slug(custom_title or text)
        title = custom_title.strip() if custom_title else _generate_clean_title(text)
        target_dir = self.global_memory_dir if scope == "global" else self.project_memory_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        # Avoid clobbering a different memory that happens to share a slug.
        target = target_dir / f"{slug}.md"
        n = 2
        while target.exists():
            existing = self._load_file(target, scope=scope)
            if existing and existing.content.strip() == text.strip():
                break
            target = target_dir / f"{slug}-{n}.md"
            n += 1

        item = MemoryItem(
            id=target.stem,
            title=title,
            content=text.strip(),
            category=category,
            scope=scope,
            created_at=time.time(),
        )
        file_content = (
            "---\n"
            f"title: {json.dumps(item.title, ensure_ascii=False)}\n"
            f"category: {json.dumps(item.category, ensure_ascii=False)}\n"
            f"scope: {json.dumps(item.scope)}\n"
            f"created_at: {item.created_at}\n"
            "---\n\n"
            f"# {item.title}\n\n"
            f"{item.content}\n"
        )
        target.write_text(file_content, encoding="utf-8")
        return item

    def add(self, text: str, scope: str = "project") -> MemoryItem:
        """Quick-add (``# remember this``) a memory note to user or project memory."""
        clean = text.strip().lstrip("#").strip()
        if not clean:
            raise ValueError("Memory text is empty.")
        scope = "global" if scope in ("global", "user") else "project"
        return self.learn(clean, category="preferences" if scope == "global" else "conventions", scope=scope)

    def list_all(self) -> list[MemoryItem]:
        """List memory items from global and project stores; project items override same id."""
        by_id: dict[str, MemoryItem] = {}
        for directory, scope in ((self.global_memory_dir, "global"), (self.project_memory_dir, "project")):
            if scope == "project" and directory == self.global_memory_dir:
                continue
            if not directory.is_dir():
                continue
            files = sorted(directory.glob("*.md"), key=lambda f: (f.stat().st_mtime, f.name))
            for p in files:
                item = self._load_file(p, scope=scope)
                if item:
                    by_id[item.id] = item
        return sorted(by_id.values(), key=lambda it: it.created_at, reverse=True)

    def _load_file(self, p: Path, scope: str = "project") -> MemoryItem | None:
        try:
            txt = _read_text(p)
            title = p.stem
            cat = "conventions"
            created = p.stat().st_mtime
            body = txt
            if txt.startswith("---"):
                parts = txt.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if ":" not in line:
                            continue
                        key, val = line.split(":", 1)
                        key = key.strip()
                        if key == "title":
                            title = _fm_value(val) or title
                        elif key == "category":
                            cat = _fm_value(val) or cat
                        elif key == "created_at":
                            try:
                                created = float(val.strip())
                            except ValueError:
                                pass
                    body = parts[2].strip()
            # Body starts with the "# Title" heading that learn() writes; strip it.
            body = re.sub(r"^#\s+.*?(?:\n+|$)", "", body, count=1).strip() if body.startswith("# ") else body
            # The scope comes from where the file lives, not what it claims.
            return MemoryItem(id=p.stem, title=title, content=body, category=cat, scope=scope, created_at=created)
        except Exception:
            return None

    def delete(self, item_id: str) -> bool:
        """Delete a memory item from the project or global store."""
        item_id = str(item_id).strip()
        if item_id.endswith(".md"):
            item_id = item_id[:-3]
        if not _SAFE_ID_RE.match(item_id):
            return False
        for directory in (self.project_memory_dir, self.global_memory_dir):
            target = directory / f"{item_id}.md"
            if target.is_file():
                target.unlink()
                return True
        return False

    def search(self, query: str) -> list[MemoryItem]:
        """Search memory items by keyword across global and project memories."""
        q = query.lower()
        return [it for it in self.list_all() if q in it.title.lower() or q in it.content.lower() or q in it.category.lower()]

    def clear(self) -> None:
        """Clear all project memory items."""
        if not self.project_memory_dir.is_dir():
            return
        for p in self.project_memory_dir.glob("*.md"):
            try:
                p.unlink()
            except OSError:
                pass


def render_saved_memories(workspace: Path) -> str:
    """Learned items from ~/.axon/memory and <ws>/.axon/memory (stable ordering)."""
    try:
        items = MemoryStore(workspace).list_all()
    except Exception:
        items = []
    if not items:
        return ""
    lines = ["### Saved memories"]
    for it in sorted(items, key=lambda i: (i.scope != "global", i.id)):
        prefix = "user" if it.scope == "global" else "project"
        body = " ".join(it.content.split())
        if len(body) > 600:
            body = body[:597] + "..."
        title = it.title.strip()
        if title and not body.lower().startswith(title.lower()[: max(10, len(title) // 2)]):
            lines.append(f"- [{prefix}] {title}: {body}")
        else:
            lines.append(f"- [{prefix}] {body}")
    return "\n".join(lines)


def render_memory_context(workspace: Path, loader: MemoryLoader | None = None) -> str:
    """Startup memory (instruction files, rules, auto memory) + saved items, for the prompt."""
    if loader is None:
        loader = MemoryLoader(workspace)
        loader.load_startup()
    sections = [loader.render(), render_saved_memories(workspace)]
    return "\n\n".join(s for s in sections if s).strip()


def distill_and_learn(
    provider: Any,
    text: str,
    workspace: Path,
    scope: str = "project",
    ledger: Any = None,
    model: str | None = None,
) -> MemoryItem:
    """Use an LLM call to turn conversational input into a clean structured memory."""
    prompt = f"""You are a memory distillation module for an AI coding assistant.
Given the following user statement, extract the core rule, preference, or fact into a clean structured format.

Input:
\"\"\"{text}\"\"\"

Generate a JSON object with:
- "title": A concise descriptive title (under 50 characters, capitalize properly, NO conversational words like "Create a memory" or "Save this").
- "category": One of "preferences", "conventions", "architecture", "debugging".
- "content": Clean, direct statement or markdown bullet points of the rule or fact (omit conversational phrases).

Respond ONLY with valid JSON:
{{"title": "...", "category": "...", "content": "..."}}"""

    title = ""
    category = "conventions"
    content = text.strip()

    if provider is not None:
        try:
            model_name = model or getattr(getattr(provider, "settings", None), "model", None)
            if not model_name:
                from axon.config import Settings
                model_name = str(Settings.model_fields["model"].default)
            stream = provider.stream(
                model=model_name,
                system=[{"type": "text", "text": "You are a strict JSON generator. Output only valid JSON."}],
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                max_tokens=500,
                effort="low",
                thinking=False,
            )
            for _ in stream:
                pass
            turn = provider.finalize()
            if ledger is not None and getattr(turn, "usage", None):
                from axon.agent.state import record_usage
                record_usage(ledger, model_name, turn.usage, source="memory_learn")
            raw = turn.text.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
            data = json.loads(raw)
            if isinstance(data, dict):
                title = str(data.get("title", "")).strip()
                category = str(data.get("category", "conventions"))
                content = str(data.get("content", "")).strip() or content
        except Exception:
            pass

    store = MemoryStore(workspace)
    return store.learn(
        text=content or text,
        category=category or "conventions",
        scope=scope,
        custom_title=title or None,
    )
