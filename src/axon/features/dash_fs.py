"""Workspace files for the dashboard: directory chooser, lazy file tree, viewer, jailed editor, git status/diff.

Every path is resolved through ``axon.permissions.paths`` (credential stores, system directories and ``.git``
internals are refused) and must lie in the workspace or one of its additional directories."""
from __future__ import annotations

import hashlib
import itertools
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

from axon.features.dash_common import DashError

MAX_TREE_ENTRIES = 25000
MAX_VIEW_BYTES = 8 * 1024 * 1024        # whole-file reads (and editing) stop here; bigger files are paged by line
MAX_EDIT_BYTES = 2 * 1024 * 1024
MAX_PAGE_LINES = 3000
MAX_JSON_LINE_CHARS = 3_000_000
MAX_LINE_CHARS = 20_000                 # one pathological line must not freeze the page
MAX_DIFF_BYTES = 400_000

_LANG = {".py": "python", ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".json": "json",
         ".sh": "sh", ".bash": "sh", ".zsh": "sh", ".md": "md", ".css": "css", ".html": "html", ".htm": "html",
         ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".rs": "rust", ".go": "go", ".c": "c", ".h": "c",
         ".cpp": "c", ".hpp": "c", ".java": "java", ".rb": "ruby", ".sql": "sql", ".xml": "html", ".svg": "html"}


def language_of(name: str) -> str:
    return _LANG.get(Path(name).suffix.lower(), "text")


# ─── paths ──────────────────────────────────────────────────────────────────

def jail(ws: Path, extra: list[Path], raw: str, *, git_read: bool = False) -> Path:
    """Resolve *raw* (relative to the workspace, or absolute) and refuse anything outside the workspace and
    additional directories, credential stores, system directories and .git internals."""
    from axon.permissions.paths import PermissionDenied, additional_dirs, is_protected_path, is_within, resolve_in_workspace
    raw = str(raw or ".")
    if "\x00" in raw:
        raise DashError("invalid path", 400)
    try:
        p = resolve_in_workspace(ws, raw, allow_git_read=git_read, allow_outside=True)
    except PermissionDenied as e:
        raise DashError(str(e), 403) from None
    allowed = [ws, *additional_dirs(extra)]
    if not any(is_within(a, p) for a in allowed):
        raise DashError("that path is outside the workspace", 403)
    why = is_protected_path(p)
    if why:
        raise DashError(f"refused: {why}", 403)
    return p


def rel_to(ws: Path, p: Path) -> str:
    try:
        return p.relative_to(ws.resolve()).as_posix()
    except ValueError:
        return str(p)


# ─── git ────────────────────────────────────────────────────────────────────

_GIT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_GIT_LOCK = threading.Lock()


def _git(ws: Path, *args: str, timeout: float = 8.0, raw: bool = False) -> subprocess.CompletedProcess[bytes] | None:
    git = shutil.which("git")
    if not git:
        return None
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    try:
        return subprocess.run([git, "-C", str(ws), *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def git_info(ws: Path, fresh: bool = False) -> dict[str, Any]:
    """{"repo": bool, "branch": str, "files": {relpath: code}} (cached for a few seconds)."""
    key = str(ws)
    with _GIT_LOCK:
        hit = _GIT_CACHE.get(key)
        if hit and not fresh and time.time() - hit[0] < 4:
            return hit[1]
    info: dict[str, Any] = {"repo": False, "branch": "", "files": {}, "ahead": 0, "behind": 0}
    top = _git(ws, "rev-parse", "--show-toplevel")
    if top is not None and top.returncode == 0:
        info["repo"] = True
        root = Path(top.stdout.decode("utf-8", "replace").strip())
        br = _git(ws, "rev-parse", "--abbrev-ref", "HEAD")
        info["branch"] = br.stdout.decode().strip() if br is not None and br.returncode == 0 else ""
        st = _git(ws, "status", "--porcelain=v1", "-z", "--untracked-files=normal", timeout=10)
        if st is not None and st.returncode == 0:
            parts = st.stdout.decode("utf-8", "replace").split("\0")
            i = 0
            while i < len(parts):
                ent = parts[i]
                i += 1
                if len(ent) < 4:
                    continue
                code, path = ent[:2].strip() or "M", ent[3:]
                if ent[0] in "RC":
                    i += 1                       # the old name follows
                try:
                    rel = (root / path).resolve().relative_to(ws.resolve()).as_posix()
                except ValueError:
                    continue
                info["files"][rel] = "U" if code == "??" else code[0]
    with _GIT_LOCK:
        _GIT_CACHE[key] = (time.time(), info)
    return info


def git_diff(ws: Path, p: Path) -> dict[str, Any]:
    rel = rel_to(ws, p)
    info = git_info(ws)
    if not info["repo"]:
        return {"repo": False, "diff": "", "note": "This folder is not a git repository."}
    out = _git(ws, "diff", "HEAD", "--no-color", "--", rel, timeout=10)
    text = out.stdout.decode("utf-8", "replace") if out is not None and out.returncode == 0 else ""
    if not text and info["files"].get(rel) == "U":
        out = _git(ws, "diff", "--no-index", "--no-color", "--", os.devnull, str(p), timeout=10)
        text = out.stdout.decode("utf-8", "replace") if out is not None else ""
    text = redact_diff_text(text, is_secret_file(p.name))
    truncated = len(text) > MAX_DIFF_BYTES
    return {"repo": True, "diff": text[:MAX_DIFF_BYTES], "truncated": truncated, "status": info["files"].get(rel, "")}


# ─── tree ───────────────────────────────────────────────────────────────────

def list_dir(ws: Path, extra: list[Path], rel: str, show_ignored: bool = False) -> dict[str, Any]:
    from axon.permissions.paths import credential_location
    from axon.tools._files import HEAVY_DIRS, JUNK_FILES, IgnoreMatcher, _find_ignore_base
    target = jail(ws, extra, rel or ".")
    if not target.is_dir():
        raise DashError("not a folder", 404)
    base = _find_ignore_base(ws.resolve())
    matcher = IgnoreMatcher(base)
    chain = [target, *[p for p in target.parents if p == base or base in p.parents]]
    for d in reversed(chain):
        matcher.load_dir(d)
    git = git_info(ws)["files"]
    entries: list[dict[str, Any]] = []
    truncated = False
    ws_root = ws.resolve()
    inside = target == ws_root or ws_root in target.parents
    rel_dir = "" if target == ws_root else (rel_to(ws, target) if inside else "")   # resolved once, not once per entry
    try:
        with os.scandir(target) as it:
            for n, e in enumerate(it):
                if n > 60_000:
                    truncated = True
                    break
                name = e.name
                if name == ".git" or name in JUNK_FILES:
                    continue
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                path = target / name
                ignored = (name in HEAVY_DIRS and is_dir) or matcher.ignored(path, is_dir)
                if ignored and not show_ignored:
                    continue
                prot = bool(credential_location(path)) if (name.startswith(".") or name in ("secrets", "credentials")) else False
                size = 0
                if not is_dir:
                    try:
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
                relp = (f"{rel_dir}/{name}" if rel_dir else name) if inside else rel_to(ws, path)
                if is_dir:
                    pref = relp + "/"
                    code = next((c for k, c in git.items() if k.startswith(pref)), "")
                else:
                    code = git.get(relp, "")
                entries.append({"name": name, "path": relp, "type": "dir" if is_dir else "file", "size": size,
                                "ignored": ignored, "git": code, "protected": prot, "link": e.is_symlink()})
    except PermissionError:
        raise DashError("permission denied", 403) from None
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    if len(entries) > MAX_TREE_ENTRIES:
        entries, truncated = entries[:MAX_TREE_ENTRIES], True
    return {"path": rel_to(ws, target) if target != ws.resolve() else "", "entries": entries, "truncated": truncated,
            "branch": git_info(ws)["branch"]}


def search_files(ws: Path, q: str, limit: int = 60) -> list[str]:
    from axon.tools._files import workspace_files
    q = (q or "").strip().lower()
    if not q:
        return []
    out: list[str] = []
    parts = q.split()
    for f in workspace_files(ws, limit=60_000):
        low = f.lower()
        if all(p in low for p in parts):
            out.append(f)
            if len(out) >= limit:
                break
    out.sort(key=lambda f: (len(f), f))
    return out


# ─── viewer / editor ────────────────────────────────────────────────────────

def _is_binary(chunk: bytes) -> bool:
    return b"\x00" in chunk


def _image_dims(p: Path) -> list[int] | None:
    """[width, height] of a PNG, GIF or JPEG from its header, or None."""
    import struct
    try:
        with open(p, "rb") as f:
            head = f.read(262144)
    except OSError:
        return None
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return list(struct.unpack(">II", head[16:24]))
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return list(struct.unpack("<HH", head[6:10]))
        if head[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                m = head[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7 or m == 0xFF:
                    i += 1 if m == 0xFF else 2
                    continue
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    return [w, h]
                i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    except (struct.error, IndexError):
        return None
    return None


_NO_EDIT_SECRET = ("Secrets in this file are hidden, so the page cannot edit it: saving would overwrite the real values with "
                   "the hidden placeholder. Open it in your own editor to change it.")


def _redacted_view(text: str) -> tuple[str, bool]:
    from axon.features.dashboard import redact_text
    shown = redact_text(text)
    return shown, shown != text


_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_KEY_VALUE = re.compile(r"^(\s*(?:export\s+)?[\"']?[\w.\-]+[\"']?\s*[:=]\s*)(.*)$")
_STRUCTURE = {"", "{", "}", "[", "]", "},", "],", "---", "..."}


def mask_secret_line(line: str) -> str:
    """One line of a credential file (.env, credentials.json, a key): the value is hidden, the key name stays."""
    s = line.strip()
    if s in _STRUCTURE or s.startswith("#"):
        return line
    m = _KEY_VALUE.match(line)
    if m and m.group(2).strip() not in _STRUCTURE:
        return m.group(1) + "[REDACTED]"
    return "[REDACTED]"


def redact_lines(lines: list[str], secret_file: bool = False) -> list[str]:
    """Secrets masked line by line without moving any line. A private key spans many lines, so its body is hidden
    from the BEGIN line to the END line, which a one-line-at-a-time redactor cannot see. In a credential file
    (*secret_file*) every value is hidden, whatever the key is called."""
    from axon.features.dashboard import redact_text
    out: list[str] = []
    in_key = False
    for ln in lines:
        if in_key or _PEM_BEGIN.search(ln):
            in_key = not _PEM_END.search(ln)
            out.append("[REDACTED]")
        else:
            ln = redact_text(ln)
            out.append(mask_secret_line(ln) if secret_file else ln)
    return out


def redact_diff_text(text: str, secret_file: bool = False) -> str:
    """A unified diff with secrets masked in its content lines (file and hunk headers are left alone)."""
    lines = text.split("\n")
    body: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if ln[:1] in ("+", "-", " ") and not ln.startswith(("+++ ", "--- ")):
            body.append((i, ln))
    bare = redact_lines([b[1:] for _, b in body], secret_file)
    for (i, ln), new in zip(body, bare):
        lines[i] = ln[:1] + new
    return "\n".join(lines)


def safe_text(name: str, text: str) -> str:
    """File text as the page may show it: secrets masked, and a credential file's values always hidden."""
    from axon.features.dashboard import redact_text
    shown = redact_text(text)
    return "\n".join(redact_lines(shown.split("\n"), True)) if is_secret_file(name) else shown


# ─── shared extraction (docx, xlsx, pptx, sqlite, archives, ...): the same code the Read tool uses ───────────

_FMT_VIEWS = {".html": "html", ".htm": "html", ".xhtml": "html", ".xml": "xml", ".plist": "xml", ".yaml": "yaml", ".yml": "yaml",
              ".toml": "toml", ".jsonl": "jsonl", ".ndjson": "jsonl"}


def _web_extract_kind(p: Path, head: bytes, browser_kind: str) -> str:
    """The extraction kind for files the browser cannot show itself ('' keeps the normal text/media/PDF views)."""
    from axon.tools import file_formats as ff
    if browser_kind in ("image", "video", "audio", "pdf") or is_secret_file(p.name):
        return ""
    k = ff.detect_kind(p, head)
    if k in ff.EXTRACT_KINDS:
        return k
    if k == "text" and ff.sniff_utf16(head):
        return "text"
    if k == "ipynb" and p.stat().st_size <= 20 * 1024 * 1024:
        return "ipynb"
    return ""


def _text_extras(p: Path, kind: str, window: list[str]) -> dict[str, Any]:
    """Hints for the text view: a detected CSV delimiter, and which formats have a formatted/readable second view."""
    out: dict[str, Any] = {}
    if kind == "csv":
        from axon.tools import file_formats as ff
        out["delimiter"] = ff.detect_delimiter("\n".join(window[:200]), p.suffix.lower())
    elif p.suffix.lower() in _FMT_VIEWS:
        out["fmt"] = _FMT_VIEWS[p.suffix.lower()]
    return out


def _strings(o: Any, acc: list[str]) -> None:
    if isinstance(o, str):
        acc.append(o)
    elif isinstance(o, dict):
        for k, v in o.items():
            if k != "b64":
                _strings(v, acc)
    elif isinstance(o, list):
        for v in o:
            _strings(v, acc)


def _mask(o: Any, red: Any) -> Any:
    if isinstance(o, str):
        return red(o)
    if isinstance(o, dict):
        return {k: (v if k == "b64" else _mask(v, red)) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask(v, red) for v in o]
    return o


def extract_file(ws: Path, extra: list[Path], raw: str, pages: str | None = None, sheet: str | None = None, offset: int = 0,
                 limit: int = 500) -> dict[str, Any]:
    """JSON for the page's view of a document, spreadsheet, deck, notebook, database, archive, hex dump and so on."""
    import base64
    from axon.tools import file_formats as ff
    p = jail(ws, extra, raw)
    if not p.is_file():
        raise DashError("file not found", 404)
    if is_secret_file(p.name):
        raise DashError("refused: credential files are never opened by the viewer", 403)
    ex = ff.extract(p, pages=pages or None, sheet=sheet or None, limit=max(1, min(limit, 5000)), offset=max(0, offset), want_preview=True)
    from axon.features.dashboard import redact_text
    payload: dict[str, Any] = {"text": ex.text, "meta": ex.meta}
    acc: list[str] = []
    _strings(payload, acc)
    if redact_text("\n".join(acc)) != "\n".join(acc):
        payload = _mask(payload, redact_text)
    images, total = [], 0
    for im in ex.images[:5]:
        total += len(im.data)
        if total > 6 * 1024 * 1024:
            break
        images.append({"mime": im.mime, "b64": base64.b64encode(im.data).decode("ascii"), "label": im.label})
    return {"kind": ex.kind, "path": rel_to(ws, p), "name": p.name, "text": payload["text"], "meta": payload["meta"], "images": images,
            "error": ex.meta.get("error", "")}


def read_file(ws: Path, extra: list[Path], raw: str, offset: int = 0, limit: int = 1200, as_text: bool = False) -> dict[str, Any]:
    p = jail(ws, extra, raw)
    if not p.exists():
        raise DashError("file not found", 404)
    if p.is_dir():
        raise DashError("that is a folder", 400)
    st = p.stat()
    limit = max(1, min(limit, MAX_PAGE_LINES))
    offset = max(0, offset)
    rel = rel_to(ws, p)
    import mimetypes
    base = {"path": rel, "abs": str(p), "name": p.name, "size": st.st_size, "mtime": st.st_mtime, "language": language_of(p.name),
            "git": git_info(ws)["files"].get(rel, ""), "mime": _RAW_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            "kind": file_kind(p.name)}
    with open(p, "rb") as f:
        head = f.read(8192)
        ex_kind = _web_extract_kind(p, head, base["kind"])
        if ex_kind == "ipynb" and as_text:      # a notebook is JSON: its source stays editable
            ex_kind = ""
        if ex_kind:
            return {**base, "kind": ex_kind, "binary": True, "extractable": True, "text": "", "lines": 0, "total_lines": None,
                    "editable": False, "redacted": False, "why_read_only": "This is not a plain text file, so it cannot be edited here."}
        if _is_binary(head):
            dims = _image_dims(p)
            return {**base, "kind": file_kind(p.name, True), "binary": True, "text": "", "lines": 0, "total_lines": None,
                    "editable": False, "redacted": False, "why_read_only": "This is not a text file, so it cannot be edited here.",
                    **({"dimensions": dims} if dims else {})}
        f.seek(0)
        whole = st.st_size <= MAX_VIEW_BYTES
        if whole:
            data = f.read()
            sha = hashlib.sha256(data).hexdigest()
            text = data.decode("utf-8", "replace")
            all_lines = text.split("\n")
            if all_lines and all_lines[-1] == "":
                all_lines.pop()
            total = len(all_lines)
            window = all_lines[offset:offset + limit]
        else:
            sha = ""
            window = []
            for line in itertools.islice(f, offset, offset + limit):
                window.append(line.decode("utf-8", "replace").rstrip("\n"))
            total = None
    clipped = 0
    window_text = "\n".join(window)
    shown, redacted = _redacted_view(window_text)
    secret_name = is_secret_file(p.name)
    if redacted:
        window = shown.split("\n")
    if secret_name:                              # a credential file never shows a value, whatever the key is called
        window = redact_lines(window, True)
    utf8_ok = True
    if whole:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            utf8_ok = False
    line_cap = MAX_JSON_LINE_CHARS if p.suffix.lower() == ".json" else MAX_LINE_CHARS    # minified JSON is one long line
    for i, ln in enumerate(window):
        if len(ln) > line_cap:
            window[i] = ln[:line_cap]
            clipped += 1
    hidden = redacted or secret_name
    can_edit = (whole and st.st_size <= MAX_EDIT_BYTES and clipped == 0 and offset == 0
                and (total is not None and len(window) == total) and not hidden and utf8_ok)
    if hidden:
        why = _NO_EDIT_SECRET
    elif not utf8_ok:
        why = "This file is not plain UTF-8 text, so editing it here could damage it."
    elif not can_edit:
        why = "This file is too large, or only partly loaded, so it is read-only here. Saving a partial copy would cut the file."
    else:
        why = ""
    return {**base, **_text_extras(p, base["kind"], window), "binary": False, "text": "\n".join(window), "offset": offset, "lines": len(window), "total_lines": total,
            "sha": "" if hidden else sha, "editable": can_edit, "redacted": hidden, "why_read_only": why, "clipped_lines": clipped,
            "eol": "crlf" if whole and b"\r\n" in data[:200_000] else "lf"}


def save_file(ws: Path, extra: list[Path], raw: str, content: str, expected_sha: str, create: bool = False,
              lines: int | None = None) -> dict[str, Any]:
    p = jail(ws, extra, raw)
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise DashError("file too large to save from the page (2 MB limit)", 413)
    existed = p.exists()
    eol = "\n"
    mode = None
    if existed:
        if p.is_dir():
            raise DashError("that is a folder", 400)
        if p.stat().st_size > MAX_EDIT_BYTES:
            raise DashError("file too large to edit from the page", 413)
        if is_secret_file(p.name):
            raise DashError("refused: this is a credential file (secrets are hidden in the page), so it cannot be saved from here. "
                            "Open it in your own editor. The file was not changed.", 403)
        cur = p.read_bytes()
        if _is_binary(cur[:8192]):
            raise DashError("binary files cannot be edited", 415)
        if not expected_sha or hashlib.sha256(cur).hexdigest() != expected_sha:    # re-read before overwrite
            raise DashError("this file changed on disk since you opened it; reload it before saving", 409)
        cur_text = cur.decode("utf-8", "replace")
        if "[REDACTED]" in content and (content.count("[REDACTED]") > cur_text.count("[REDACTED]") or _redacted_view(cur_text)[1]):
            raise DashError("refused: this text contains the [REDACTED] placeholder the page shows instead of secrets, "
                            "and saving it would overwrite the real values. The file was not changed.", 409)
        if lines is not None:
            disk_lines = cur_text.split("\n")
            n = len(disk_lines) - (1 if disk_lines and disk_lines[-1] == "" else 0)
            if lines != n:
                raise DashError("only part of this file was loaded, so saving would cut it; reload it before saving", 409)
        eol = "\r\n" if b"\r\n" in cur[:200_000] else "\n"
        mode = p.stat().st_mode & 0o7777
    elif not create:
        raise DashError("file does not exist", 404)
    elif is_secret_file(p.name):
        raise DashError("refused: this name is a credential file, which cannot be created or changed from here, because the page "
                        "never shows its secrets. Create it in your own editor.", 403)
    elif not p.parent.is_dir():
        raise DashError("the folder does not exist", 404)
    if p.suffix.lower() == ".ipynb":
        try:
            ok = isinstance(json.loads(content), dict)
        except ValueError:
            ok = False
        if not ok:
            raise DashError("a notebook has to stay valid JSON, and this text is not, so it was not saved. "
                            "Fix the JSON, or close the editor to discard the change.", 422)
    data = content.replace("\r\n", "\n")
    if eol == "\r\n":
        data = data.replace("\n", "\r\n")
    raw_bytes = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".axon-save-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw_bytes)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, p)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise DashError(f"could not save: {e.strerror or e}", 500) from None
    st = p.stat()
    with _GIT_LOCK:
        _GIT_CACHE.pop(str(ws), None)
    return {"ok": True, "sha": hashlib.sha256(raw_bytes).hexdigest(), "mtime": st.st_mtime, "size": st.st_size,
            "created": not existed}


# ─── directory chooser ──────────────────────────────────────────────────────

def list_subdirs(raw: str, show_hidden: bool = False) -> dict[str, Any]:
    from axon.permissions.paths import credential_location, is_protected_path
    p = Path(raw).expanduser() if raw else Path.home()
    try:
        p = p.resolve()
    except OSError:
        raise DashError("cannot resolve that folder", 400) from None
    why = is_protected_path(p)
    if why:
        raise DashError(f"refused: {why}", 403)
    if not p.is_dir():
        raise DashError("not a folder", 404)
    dirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            try:
                if not e.is_dir(follow_symlinks=True):
                    continue
            except OSError:
                continue
            if e.name.startswith(".") and not show_hidden:
                continue
            child = p / e.name
            if credential_location(child) or is_protected_path(child):
                continue                                   # credential stores are not even listed
            dirs.append({"name": e.name, "path": str(child), "git": (child / ".git").exists()})
    except PermissionError:
        raise DashError("permission denied", 403) from None
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "home": str(Path.home()),
            "git": (p / ".git").exists(), "dirs": dirs[:2000]}


def recent_projects(limit: int = 12) -> list[dict[str, Any]]:
    from axon import storage
    root = storage.axon_home() / "projects"
    rows: list[tuple[float, dict[str, Any]]] = []
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []
    for d in dirs:
        try:
            ws = json.loads((d / "project.json").read_text(encoding="utf-8")).get("workspace")
        except (OSError, ValueError, AttributeError):
            continue
        if not ws or not Path(ws).is_dir():
            continue
        try:
            mt = (d / "sessions").stat().st_mtime
        except OSError:
            mt = d.stat().st_mtime
        rows.append((mt, {"path": str(ws), "name": Path(ws).name, "used": mt}))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r[1] for r in rows[:limit]]


# ─── content search, previews, raw bytes ────────────────────────────────────

_RAW_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
              ".bmp": "image/bmp", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".avif": "image/avif",
              ".pdf": "application/pdf",
              ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".ogv": "video/ogg",
              ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".m4a": "audio/mp4",
              ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg"}
MAX_RAW_BYTES = 8 * 1024 * 1024 * 1024
_MEDIA_KINDS = {"image/": "image", "video/": "video", "audio/": "audio"}


def file_kind(name: str, binary: bool = False) -> str:
    """How the viewer shows a file: image, video, audio, pdf, markdown, csv, json, code, text or binary."""
    ext = Path(name).suffix.lower()
    ctype = _RAW_TYPES.get(ext, "")
    for prefix, kind in _MEDIA_KINDS.items():
        if ctype.startswith(prefix):
            return kind
    if ext == ".pdf":
        return "pdf"
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext in (".csv", ".tsv"):
        return "csv"
    if ext == ".json":
        return "json"
    if binary:
        return "binary"
    return "code" if language_of(name) != "text" else "text"


_SECRET_NAMES = {".npmrc", ".pypirc", ".netrc", ".htpasswd", ".pgpass", ".git-credentials", "credentials", "credentials.json", "secrets.json",
                 "secrets.yml", "secrets.yaml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".asc", ".gpg")


def is_secret_file(name: str) -> bool:
    """.env-style credential stores that the raw route never sends, even to the person who owns them."""
    n = name.lower()
    if n == ".env" or (n.startswith(".env.") and n.rsplit(".", 1)[-1] not in ("example", "sample", "template", "dist")):
        return True
    return n in _SECRET_NAMES or n.startswith(("id_rsa", "id_ed25519")) or n.endswith(_SECRET_SUFFIXES)


def raw_info(ws: Path, extra: list[Path], raw: str, download: bool = False) -> tuple[Path, str]:
    """(path, content type) of a file the page may fetch as bytes. Previews are limited to images, audio, video and PDF;
    everything else is only ever a download (never rendered as a document), and SVG is served sandboxed."""
    p = jail(ws, extra, raw)
    if not p.is_file():
        raise DashError("file not found", 404)
    if is_secret_file(p.name):
        raise DashError("refused: this looks like a credential file", 403)
    ctype = _RAW_TYPES.get(p.suffix.lower())
    if download:
        return p, "application/octet-stream"
    if ctype is None:
        raise DashError("this kind of file has no preview; download it instead", 415)
    return p, ctype


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """(start, end) inclusive for a single byte range, None when there is no Range header, "bad" when it cannot be met."""
    if not header:
        return None
    import re as _re
    m = _re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return "bad"
    if m.group(1) == "":
        n = int(m.group(2))
        if n == 0:
            return "bad"
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return "bad"
    return start, min(end, size - 1)


def reveal(ws: Path, extra: list[Path], raw: str) -> dict[str, Any]:
    """Show a file in the system file manager (Finder, Explorer, the default Linux file manager)."""
    p = jail(ws, extra, raw)
    if not p.exists():
        raise DashError("file not found", 404)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise DashError(f"could not open the file manager: {e.strerror or e}", 500) from None
    return {"ok": True, "path": str(p)}


def grep_files(ws: Path, query: str, limit: int = 200, budget_s: float = 5.0) -> dict[str, Any]:
    """Text search across file contents (case-insensitive substring), skipping ignored, huge and binary files."""
    from axon.tools._files import iter_files
    q = (query or "").strip()
    if len(q) < 2:
        return {"matches": [], "files": 0, "partial": False}
    needle = q.lower()
    t0 = time.time()
    matches: list[dict[str, Any]] = []
    scanned = 0
    skipped = 0
    partial = False
    for f in iter_files(ws, include_hidden=False):
        if time.time() - t0 > budget_s or len(matches) >= limit:
            partial = True
            break
        try:
            st = f.stat()
            if st.st_size > 1_500_000:
                skipped += 1
                continue
            with open(f, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue
        if is_secret_file(f.name):
            continue                              # credential files are not searched: a match would print a secret
        scanned += 1
        text = data.decode("utf-8", "replace")
        if needle not in text.lower():
            continue
        for n, line in enumerate(redact_lines(text.split("\n")), 1):
            if needle in line.lower():
                matches.append({"path": rel_to(ws, f), "line": n, "text": line.strip()[:200]})
                if len(matches) >= limit:
                    break
    return {"matches": matches, "files": scanned, "partial": partial, "skipped_large": skipped}
