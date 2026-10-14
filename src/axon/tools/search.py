"""
Search tools: Glob (mtime-sorted file matching), Grep (ripgrep with a pure-Python
fallback) and Ls (directory listing / tree). All output paths use forward slashes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, ClassVar, Iterable

from axon.errors import ToolError
from axon.tools._files import HEAVY_DIRS, JUNK_FILES, glob_to_regex, iter_files
from axon.tools.base import (
    Tool, ToolContext, bool_arg, cap_text, default_summary, display_path, int_arg, output_cap, plural, resolve_path,
)
from axon.tools.textio import is_binary_bytes, decode_bytes

RG_TIMEOUT_S = 30
MAX_GREP_FILE_BYTES = 10 * 1024 * 1024

# ripgrep --type names → extensions, used by the Python fallback.
TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "py": (".py", ".pyi"), "python": (".py", ".pyi"),
    "js": (".js", ".mjs", ".cjs", ".jsx"), "ts": (".ts", ".tsx", ".mts", ".cts"),
    "typescript": (".ts", ".tsx"), "go": (".go",), "rust": (".rs",), "rs": (".rs",),
    "java": (".java",), "kotlin": (".kt", ".kts"), "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"), "cs": (".cs",), "csharp": (".cs",),
    "rb": (".rb",), "ruby": (".rb",), "php": (".php",), "swift": (".swift",), "scala": (".scala",),
    "sh": (".sh", ".bash", ".zsh"), "ps": (".ps1", ".psm1"), "powershell": (".ps1", ".psm1"),
    "md": (".md", ".markdown"), "markdown": (".md", ".markdown"), "json": (".json",),
    "yaml": (".yaml", ".yml"), "toml": (".toml",), "html": (".html", ".htm"), "css": (".css", ".scss", ".sass", ".less"),
    "sql": (".sql",), "xml": (".xml",), "lua": (".lua",), "dart": (".dart",), "vue": (".vue",), "svelte": (".svelte",),
    "txt": (".txt",),
}


def find_rg() -> str | None:
    """Locate ripgrep (rg / rg.exe on PATH, or Axon's own ~/.axon/bin)."""
    found = shutil.which("rg")
    if found:
        return found
    exe = "rg.exe" if os.name == "nt" else "rg"
    from axon.storage import axon_home
    cand = axon_home() / "bin" / exe
    return str(cand) if cand.is_file() else None


def _norm(line: str) -> str:
    """Normalise rg output paths: strip ./ prefix, backslashes → forward slashes on Windows."""
    if line.startswith("./") or line.startswith(".\\"):
        line = line[2:]
    return line.replace("\\", "/") if os.sep == "\\" else line


def _rel_prefix(target: Path, workspace: Path) -> str:
    """Prefix to prepend to rg paths (rg runs with cwd=target dir)."""
    d = display_path(target, workspace)
    return "" if d == "." else d.rstrip("/") + "/"


# ─── Glob ────────────────────────────────────────────────────────────────────

class GlobTool(Tool):
    name: ClassVar[str] = "Glob"
    description: ClassVar[str] = (
        "Find files by glob pattern, e.g. \"**/*.py\", \"src/**/*.{ts,tsx}\", \"*.md\" (no slash = top level of `path` "
        "only; use **/ to recurse). Respects .gitignore and skips .git/node_modules/venvs. Results are sorted by "
        "modification time, newest first. Use this to locate files by name; use Grep to search contents."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern relative to `path`"},
            "path": {"type": "string", "description": "Directory to search in (default: workspace root)"},
            "head_limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "Max paths to return (default 100)"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        pattern = str(args.get("pattern") or "").strip()
        if not pattern:
            raise ToolError("Glob requires 'pattern'.")
        limit = int_arg(args, "head_limit", 100, lo=1, hi=2000)
        root = resolve_path(ctx, args.get("path") or ".", allow_git_read=True)

        pattern = pattern.replace("\\", "/")
        # An absolute pattern (or one with ../) is re-rooted at its literal prefix.
        if os.path.isabs(pattern) or pattern.startswith("../") or pattern.startswith("~"):
            parts = pattern.split("/")
            lit: list[str] = []
            for part in parts:
                if any(ch in part for ch in "*?[{"):
                    break
                lit.append(part)
            base = "/".join(lit) or "/"
            root = resolve_path(ctx, base, allow_git_read=True)
            pattern = "/".join(parts[len(lit):]) or "*"
        elif pattern.startswith("./"):
            pattern = pattern[2:]

        if not root.exists():
            raise ToolError(f"Directory not found: {args.get('path') or '.'}")
        if not root.is_dir():
            raise ToolError(f"Glob 'path' must be a directory: {args.get('path')}")

        regex = glob_to_regex(pattern, ignore_case=(os.name == "nt"))

        wanted_heavy = {seg for seg in pattern.split("/") if seg in HEAVY_DIRS}
        matches: list[tuple[float, str]] = []
        for rel in self._candidates(root):
            if any(seg in HEAVY_DIRS and seg not in wanted_heavy for seg in rel.split("/")[:-1]):
                continue
            if regex.match(rel):
                fp = root / rel
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                matches.append((mtime, display_path(fp, ctx.workspace)))

        if not matches:
            return f"No files matched pattern '{pattern}' in {display_path(root, ctx.workspace)}."
        matches.sort(key=lambda x: (-x[0], x[1]))
        paths = [m[1] for m in matches]
        out = "\n".join(paths[:limit])
        if len(paths) > limit:
            out += f"\n\n[... {len(paths) - limit} more files matched. Narrow the pattern or raise head_limit ...]"
        return cap_text(out, output_cap(ctx, 30_000))

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error or output.startswith("No files matched"):
            return default_summary(output, is_error) if is_error else "no files matched"
        paths = [ln for ln in output.split("\n") if ln and not ln.startswith("[")]
        m = re.search(r"\[\.\.\. (\d+) more files", output)
        extra = int(m.group(1)) if m else 0
        return plural(len(paths) + extra, "file") + (f" (showing {len(paths)})" if extra else "")

    @staticmethod
    def _candidates(root: Path) -> Iterable[str]:
        rg = find_rg()
        if rg:
            cmd = [rg, "--files", "--hidden", "--no-config", "--no-require-git", "-g", "!.git", "--no-messages"]
            try:
                res = subprocess.run(
                    cmd + ["."], cwd=str(root), capture_output=True, timeout=RG_TIMEOUT_S,
                    stdin=subprocess.DEVNULL,
                )
                if res.returncode in (0, 1):
                    text = res.stdout.decode("utf-8", errors="replace")
                    return [_norm(ln) for ln in text.splitlines() if ln]
            except (OSError, subprocess.SubprocessError):
                pass
        out: list[str] = []
        for fp in iter_files(root, include_hidden=True):
            try:
                out.append(fp.relative_to(root).as_posix())
            except ValueError:
                continue
            if len(out) >= 200_000:
                break
        return out


# ─── Grep ────────────────────────────────────────────────────────────────────

class GrepTool(Tool):
    name: ClassVar[str] = "Grep"
    description: ClassVar[str] = (
        "Search file contents with a regular expression (ripgrep syntax; literal braces/parens need escaping, "
        "e.g. \"interface\\{\\}\"). Respects .gitignore. output_mode: \"content\" (default; matching lines as "
        "path:line:text, with optional context -A/-B/-C), \"files_with_matches\" (paths only, cheapest), or "
        "\"count\" (matches per file). Filter with `glob` (e.g. \"*.py\", \"src/**/*.ts\") or `type` (py, js, ts, go, "
        "rust, java...). Use -i for case-insensitive, multiline=true for patterns spanning lines. Results are "
        "capped by head_limit (default 100 entries). Always use this instead of grep/rg via Bash."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for"},
            "path": {"type": "string", "description": "File or directory to search (default: workspace root)"},
            "glob": {"type": "string", "description": "Glob filter for files, e.g. \"*.js\" or \"**/*.{ts,tsx}\""},
            "type": {"type": "string", "description": "File type filter, e.g. py, js, ts, go, rust, java, md"},
            "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"],
                            "description": "Output format (default \"content\")"},
            "-i": {"type": "boolean", "description": "Case-insensitive search"},
            "-n": {"type": "boolean", "description": "Show line numbers in content mode (default true)"},
            "-A": {"type": "integer", "minimum": 0, "maximum": 50, "description": "Lines of context after each match (content mode)"},
            "-B": {"type": "integer", "minimum": 0, "maximum": 50, "description": "Lines of context before each match (content mode)"},
            "-C": {"type": "integer", "minimum": 0, "maximum": 50, "description": "Lines of context before and after each match (content mode)"},
            "multiline": {"type": "boolean", "description": "Let . match newlines and patterns span lines (default false)"},
            "head_limit": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "Max output entries (lines/files/counts); default 100"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        pattern = args.get("pattern")
        if not pattern:
            raise ToolError("Grep requires 'pattern'.")
        pattern = str(pattern)
        mode = str(args.get("output_mode") or "content")
        if mode not in ("content", "files_with_matches", "count"):
            raise ToolError("output_mode must be one of: content, files_with_matches, count.")
        opts = {
            "i": bool_arg(args, "-i", False) or bool_arg(args, "case_insensitive", False),
            "n": bool_arg(args, "-n", True),
            "A": int_arg(args, "-A", 0, lo=0, hi=50),
            "B": int_arg(args, "-B", 0, lo=0, hi=50),
            "C": int_arg(args, "-C", 0, lo=0, hi=50),
            "multiline": bool_arg(args, "multiline", False),
            "glob": str(args.get("glob") or "").strip(),
            "type": str(args.get("type") or "").strip().lower(),
        }
        if opts["C"]:
            opts["A"] = opts["A"] or opts["C"]
            opts["B"] = opts["B"] or opts["C"]
        if opts["type"] and opts["type"] not in TYPE_EXTENSIONS and not find_rg():
            raise ToolError(f"Unknown file type '{opts['type']}'. Known: {', '.join(sorted(TYPE_EXTENSIONS))}.")
        limit = int_arg(args, "head_limit", 100, lo=1, hi=5000)

        target = resolve_path(ctx, args.get("path") or ".", allow_git_read=True)
        if not target.exists():
            raise ToolError(f"Path not found: {args.get('path')}")

        entries: list[str] | None = None
        rg = find_rg()
        if rg:
            entries = self._rg(rg, pattern, target, ctx, mode, opts, limit)
        if entries is None:
            entries = self._python(pattern, target, ctx, mode, opts, limit)

        if not entries:
            return f"No matches found for pattern '{pattern}'."
        truncated = len(entries) > limit
        body = "\n".join(entries[:limit])
        if mode == "files_with_matches":
            body = f"Found {min(len(entries), limit)}{'+' if truncated else ''} file(s)\n" + body
        if truncated:
            body += f"\n\n[... results truncated at head_limit={limit}. Narrow the pattern/path/glob ...]"
        return cap_text(body, output_cap(ctx, 30_000), "Narrow the search or use files_with_matches")

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        if output.startswith("No matches"):
            return "no matches"
        mode = str(args.get("output_mode") or "content")
        lines = [ln for ln in output.split("\n") if ln and not ln.startswith(("[", "Found ")) and ln != "--"]
        more = "+" if "truncated at head_limit" in output else ""
        if mode == "files_with_matches":
            return plural(len(lines), "file") + more
        if mode == "count":
            total = 0
            for ln in lines:
                try:
                    total += int(ln.rsplit(":", 1)[1])
                except (IndexError, ValueError):
                    pass
            return f"{plural(total, 'match', 'matches')} in {plural(len(lines), 'file')}{more}"
        files: set[str] = set()
        hits = 0
        rx = re.compile(r"^(.*?):(\d+):") if bool_arg(args, "-n", True) else re.compile(r"^(.*?):")
        for ln in lines:
            m = rx.match(ln)
            if m:
                hits += 1
                files.add(m.group(1))
        return f"{plural(hits, 'match', 'matches')}{more} in {plural(len(files), 'file')}"

    # ── ripgrep backend ──
    def _rg(self, rg: str, pattern: str, target: Path, ctx: ToolContext, mode: str,
            opts: dict[str, Any], limit: int) -> list[str] | None:
        cmd = [rg, "--no-config", "--no-require-git", "--color=never", "--hidden", "-g", "!.git", "--no-messages", "--max-columns=500",
               "--max-columns-preview"]
        if not any(part in HEAVY_DIRS for part in target.parts):
            for heavy in sorted(HEAVY_DIRS - {".git"}):
                cmd += ["-g", f"!{heavy}/"]
        if opts["i"]:
            cmd.append("-i")
        if opts["multiline"]:
            cmd += ["-U", "--multiline-dotall"]
        if opts["glob"]:
            cmd += ["-g", opts["glob"]]
        if opts["type"]:
            cmd += ["-t", opts["type"]]
        cmd.append("--with-filename")
        if mode == "files_with_matches":
            cmd.append("-l")
        elif mode == "count":
            cmd.append("-c")
        else:
            cmd.append("--no-heading")
            if opts["n"]:
                cmd.append("-n")
            if opts["A"]:
                cmd += ["-A", str(opts["A"])]
            if opts["B"]:
                cmd += ["-B", str(opts["B"])]
        cmd += ["-e", pattern, "--"]
        if target.is_file():
            cwd = target.parent
            cmd.append(target.name)
            prefix = _rel_prefix(target.parent, ctx.workspace)
        else:
            cwd = target
            cmd.append(".")
            prefix = _rel_prefix(target, ctx.workspace)
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL)
        except OSError:
            return None
        import threading
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            try:
                proc.kill()
            except OSError:
                pass

        out_lines: list[str] = []
        budget = limit + 1
        timer = threading.Timer(RG_TIMEOUT_S, _kill)
        timer.daemon = True
        timer.start()
        capped = False
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                out_lines.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                if len(out_lines) >= budget:
                    capped = True
                    proc.kill()  # global cap reached; stop early
                    break
            # Drain stderr in a bounded way (rg writes little there with --no-messages).
            stderr = proc.stderr.read(65536).decode("utf-8", errors="replace") if proc.stderr else ""
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = -1
        finally:
            timer.cancel()
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass

        if timed_out.is_set() and not capped:
            if not out_lines:
                raise ToolError(f"Grep timed out after {RG_TIMEOUT_S}s. Narrow 'path', 'glob' or 'type'.")
            out_lines.append(f"[search timed out after {RG_TIMEOUT_S}s; results are partial]")
        elif not out_lines and code == 2:
            raise ToolError(f"Grep failed for pattern '{pattern}': {stderr.strip() or 'invalid regex or path'}")
        result = []
        for ln in out_lines:
            if (mode == "content" and ln == "--") or ln.startswith("[search timed out"):
                result.append(ln)
                continue
            result.append(prefix + _norm(ln))
        if mode == "files_with_matches":
            result = _sort_by_mtime(result, ctx.workspace)
        return result

    # ── pure-Python backend ──
    def _python(self, pattern: str, target: Path, ctx: ToolContext, mode: str,
                opts: dict[str, Any], limit: int) -> list[str]:
        flags = re.IGNORECASE if opts["i"] else 0
        if opts["multiline"]:
            flags |= re.DOTALL | re.MULTILINE
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ToolError(f"Invalid regular expression '{pattern}': {e}") from e

        glob_re = None
        if opts["glob"]:
            g = opts["glob"]
            neg = g.startswith("!")
            g = g[1:] if neg else g
            glob_re = (glob_to_regex(g), "/" in g, neg)
        exts = TYPE_EXTENSIONS.get(opts["type"]) if opts["type"] else None

        base = target if target.is_dir() else target.parent
        results: list[str] = []
        files_hit: list[str] = []
        for fp in iter_files(target, include_hidden=True):
            if exts and fp.suffix.lower() not in exts:
                continue
            if glob_re and target.is_dir():
                rx, has_slash, neg = glob_re
                rel_to_base = fp.relative_to(base).as_posix()
                hit = bool(rx.match(rel_to_base if has_slash else fp.name))
                if hit == neg:
                    continue
            try:
                if fp.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                data = fp.read_bytes()
            except OSError:
                continue
            if is_binary_bytes(data):
                continue
            text, _ = decode_bytes(data)
            rel = display_path(fp, ctx.workspace)

            if opts["multiline"]:
                found = list(regex.finditer(text))
                if not found:
                    continue
                if mode == "files_with_matches":
                    files_hit.append(rel)
                elif mode == "count":
                    results.append(f"{rel}:{len(found)}")
                else:
                    for m in found:
                        ln = text.count("\n", 0, m.start()) + 1
                        snippet = m.group(0).replace("\r", "")
                        for k, part in enumerate(snippet.split("\n")):
                            results.append(f"{rel}:{ln + k}:{part[:500]}" if opts["n"] else f"{rel}:{part[:500]}")
                        if len(results) > limit:
                            return results
                continue

            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            lines = [l[:-1] if l.endswith("\r") else l for l in lines]
            hits = [i for i, l in enumerate(lines) if regex.search(l)]
            if not hits:
                continue
            if mode == "files_with_matches":
                files_hit.append(rel)
                if len(files_hit) > limit * 5:
                    break
                continue
            if mode == "count":
                results.append(f"{rel}:{len(hits)}")
                if len(results) > limit:
                    return results
                continue
            a, b = opts["A"], opts["B"]
            hit_set = set(hits)
            last_printed = -2
            for i in hits:
                lo, hi = max(0, i - b), min(len(lines) - 1, i + a)
                if last_printed >= 0 and lo > last_printed + 1 and (a or b):
                    results.append("--")
                for j in range(max(lo, last_printed + 1), hi + 1):
                    sep = ":" if j in hit_set else "-"
                    txt = lines[j][:500]
                    results.append(f"{rel}{sep}{j + 1}{sep}{txt}" if opts["n"] else f"{rel}{sep}{txt}")
                    last_printed = j
                if len(results) > limit:
                    return results
        if mode == "files_with_matches":
            return _sort_by_mtime(files_hit, ctx.workspace)
        return results


def _sort_by_mtime(paths: list[str], workspace: Path) -> list[str]:
    def key(rel: str) -> float:
        p = Path(rel)
        p = p if p.is_absolute() else workspace / rel
        try:
            return -p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(paths, key=key)


# ─── Ls ──────────────────────────────────────────────────────────────────────

def _fmt_size(sz: int) -> str:
    if sz >= 1_048_576:
        return f"{sz / 1_048_576:.1f} MB"
    if sz >= 1024:
        return f"{sz / 1024:.1f} KB"
    return f"{sz} B"


class LsTool(Tool):
    name: ClassVar[str] = "Ls"
    description: ClassVar[str] = (
        "List a directory. depth=1 (default) shows immediate children (directories end with '/'); depth>1 renders "
        "a tree (max 6) that skips .git, node_modules, virtualenvs and caches. Use `ignore` for extra glob "
        "patterns to hide, detailed=true for file sizes. Prefer Glob/Grep when you know what you are looking for."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (default: workspace root)"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 6, "description": "Levels to descend (default 1)"},
            "ignore": {"type": "array", "items": {"type": "string"}, "description": "Glob patterns of names to hide"},
            "detailed": {"type": "boolean", "description": "Show file sizes and totals (default false)"},
        },
        "required": [],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"
    MAX_ENTRIES: ClassVar[int] = 600

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw = args.get("path") or "."
        p = resolve_path(ctx, raw, allow_git_read=True)
        if not p.exists():
            raise ToolError(f"Directory not found: {raw}")
        if not p.is_dir():
            raise ToolError(f"Path is a file, not a directory: {raw}. Use Read to view it.")
        depth = int_arg(args, "depth", int_arg(args, "max_depth", 1, lo=1, hi=6), lo=1, hi=6)
        detailed = bool_arg(args, "detailed", False)
        include_hidden = bool_arg(args, "include_hidden", True)
        ignore = args.get("ignore") or []
        if isinstance(ignore, str):
            ignore = [ignore]
        ignore = [str(x) for x in ignore]

        stats = {"dirs": 0, "files": 0, "truncated": False}
        lines: list[str] = []

        def visible(e: Path) -> bool:
            if e.name in JUNK_FILES or e.name == ".git":
                return False
            if not include_hidden and e.name.startswith("."):
                return False
            return not any(fnmatch(e.name, pat) for pat in ignore)

        def entries_of(d: Path) -> list[Path]:
            try:
                items = [e for e in d.iterdir() if visible(e)]
            except OSError:
                return []
            return sorted(items, key=lambda x: (not _is_dir(x), x.name.lower()))

        def label(e: Path) -> str:
            if _is_dir(e):
                return e.name + "/"
            if detailed:
                try:
                    return f"{e.name} ({_fmt_size(e.stat().st_size)})"
                except OSError:
                    return e.name
            return e.name

        if depth == 1:
            for e in entries_of(p):
                if len(lines) >= self.MAX_ENTRIES:
                    stats["truncated"] = True
                    break
                stats["dirs" if _is_dir(e) else "files"] += 1
                lines.append(label(e))
            if not lines:
                return "(Empty directory)"
        else:
            lines.append(f"{display_path(p, ctx.workspace).rstrip('/') or '.'}/")

            def walk(d: Path, prefix: str, level: int) -> None:
                items = entries_of(d)
                for idx, e in enumerate(items):
                    if len(lines) >= self.MAX_ENTRIES:
                        stats["truncated"] = True
                        return
                    last = idx == len(items) - 1
                    conn = "└── " if last else "├── "
                    is_dir = _is_dir(e)
                    stats["dirs" if is_dir else "files"] += 1
                    if is_dir and e.name in HEAVY_DIRS:
                        lines.append(f"{prefix}{conn}{e.name}/ (skipped)")
                        continue
                    lines.append(f"{prefix}{conn}{label(e)}")
                    if is_dir and level < depth and not e.is_symlink():
                        walk(e, prefix + ("    " if last else "│   "), level + 1)

            walk(p, "", 1)

        out = "\n".join(lines)
        if stats["truncated"]:
            out += f"\n... [truncated after {self.MAX_ENTRIES} entries; list a subdirectory or use Glob]"
        if detailed or depth > 1:
            out += f"\n\n({stats['dirs']} directories, {stats['files']} files)"
        return cap_text(out, output_cap(ctx, 30_000))

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        if output.startswith("(Empty"):
            return "empty directory"
        m = re.search(r"\((\d+) directories, (\d+) files\)", output)
        if m:
            return f"{plural(int(m.group(1)), 'directory', 'directories')}, {plural(int(m.group(2)), 'file')}"
        entries = [ln for ln in output.split("\n") if ln and not ln.startswith("...")]
        dirs = sum(1 for e in entries if e.endswith("/"))
        return f"{plural(dirs, 'directory', 'directories')}, {plural(len(entries) - dirs, 'file')}"


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False
