"""Attaching files and folders by path (dragged from the file tree, the search results or the viewer).

An image is copied into Axon's image cache and becomes ``[Image #N]``, exactly like a dropped OS image, so the
model receives the picture. A code or text file stays where it is and becomes an ``@path`` reference that is
replaced by its content (or, when large, by a note telling the model to use Read) when the message is sent. A
folder becomes ``[Folder #N: name/]`` and reaches the model as its path plus a short, flat listing."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

from axon.features import dash_fs
from axon.features.dash_common import DashError

IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".heif")   # the last five are converted to PNG on the way in
MAX_IMAGE_BYTES = 20 * 1024 * 1024      # larger pictures are refused with a reason; models reject them anyway
MAX_PATHS = 50                          # one drop attaches at most this many items
LIST_SHOWN = 60                         # entries of a folder shown to the model (one level, never recursive)
LIST_SCANNED = 2000                     # entries looked at before the folder is called big
INLINE_CHARS = 20_000                   # text of a referenced file sent inline; more is left for Read
INLINE_BYTES = 256 * 1024
TEXT_KINDS = ("text", "csv", "json", "jsonl", "yaml", "toml", "xml", "html", "svg", "log")
SKIP_NAMES = {".git", "__pycache__", "node_modules", ".venv", ".DS_Store", ".mypy_cache", ".pytest_cache"}
MAX_REGISTERED = 300

_lock = threading.Lock()
_refs: dict[str, dict[str, Any]] = {}       # "@path" -> {"abs"}
_folders: dict[str, dict[str, Any]] = {}    # "[Folder #N: x/]" -> {"abs"} or {"names": [...]} for a Finder folder
_counter = 0


def _bounded(d: dict[str, Any]) -> None:
    while len(d) > MAX_REGISTERED:
        d.pop(next(iter(d)))


def _shown_name(ws: Path, p: Path) -> str:
    return dash_fs.rel_to(ws, p)


def list_folder(p: Path) -> tuple[list[str], int, bool]:
    """(names to show, entries seen, scan stopped early): one level, folders first, junk folders left out."""
    names: list[tuple[bool, str]] = []
    seen = 0
    stopped = False
    try:
        with os.scandir(p) as it:
            for e in it:
                if e.name in SKIP_NAMES:
                    continue
                seen += 1
                if seen > LIST_SCANNED:
                    stopped = True
                    break
                try:
                    names.append((not e.is_dir(follow_symlinks=False), e.name))
                except OSError:
                    names.append((True, e.name))
    except OSError as e:
        raise DashError(f"could not read the folder: {e.strerror or e}", 500) from None
    names.sort(key=lambda t: (t[0], t[1].lower()))
    return [n + ("" if f else "/") for f, n in names[:LIST_SHOWN]], min(seen, LIST_SCANNED), stopped


def _item_error(raw: str, why: str) -> dict[str, Any]:
    return {"path": raw, "name": Path(raw).name or raw, "error": why}


def attach_paths(ws: Path, extra: list[Path], raw_paths: Any) -> list[dict[str, Any]]:
    global _counter
    if not isinstance(raw_paths, list) or not raw_paths:
        raise DashError("'paths' must be a list of file paths")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_paths[:MAX_PATHS]:
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or raw in seen:
            continue
        seen.add(raw)
        try:
            p = dash_fs.jail(ws, extra, raw)
        except DashError as e:
            msg = str(e)
            if "outside the workspace" in msg:
                msg = "it is outside the project folder"
            elif getattr(e, "status", 0) == 403:
                msg = "this is a credential or system file, so Axon does not attach it"
            items.append(_item_error(raw, msg))
            continue
        if not p.exists():
            items.append(_item_error(raw, "it no longer exists on disk"))
            continue
        if dash_fs.is_secret_file(p.name):
            items.append(_item_error(raw, "this is a credential file, so Axon does not attach it"))
            continue
        rel = _shown_name(ws, p)
        name = p.name or rel
        try:
            if p.is_dir():
                shown, seen_n, stopped = list_folder(p)
                with _lock:
                    _counter += 1
                    chip = f"[Folder #{_counter}: {name}/]"
                    _folders[chip] = {"abs": str(p), "rel": rel}
                    _bounded(_folders)
                items.append({"kind": "folder", "chip": chip, "name": name + "/", "path": rel, "entries": seen_n,
                              "more": stopped, "shown": len(shown)})
                continue
            size = p.stat().st_size
            ext = p.suffix.lower()
            if ext in IMAGE_TYPES:
                if size > MAX_IMAGE_BYTES:
                    items.append(_item_error(raw, f"the picture is {size / 1048576:.0f} MB; pictures over "
                                                  f"{MAX_IMAGE_BYTES // 1048576} MB are not sent to the model"))
                    continue
                from axon.agent.images import ingest_image_file
                image = ingest_image_file(str(p))
                if image is None:
                    items.append(_item_error(raw, "the picture could not be read"))
                    continue
                n = int(re.sub(r"\D", "", image.label) or 0)
                items.append({"kind": "image", "chip": image.label, "name": name, "path": rel, "size": size,
                              "width": image.width, "height": image.height, "thumb": f"/api/attachment/image?n={n}"})
                continue
            chip = "@" + rel
            with _lock:
                _refs[chip] = {"abs": str(p)}
                _bounded(_refs)
            item = {"kind": "pdf" if ext == ".pdf" else "ref", "chip": chip, "name": name, "path": rel, "size": size}
            if ext != ".pdf" and size <= INLINE_BYTES:
                from axon.agent.attachments import secret_labels
                labels = secret_labels(p)
                if labels:
                    item["secrets"] = labels
            items.append(item)
        except OSError as e:
            items.append(_item_error(raw, f"it could not be read ({e.strerror or e})"))
    if len(raw_paths) > MAX_PATHS:
        items.append({"path": "", "name": "", "error": f"only the first {MAX_PATHS} items were attached"})
    return items


def attach_dropped_folder(name: Any, entries: Any, truncated: Any) -> dict[str, Any]:
    """A folder dragged in from the operating system: the browser gives names, never a path, so only they are kept."""
    global _counter
    if not isinstance(name, str) or not name.strip():
        raise DashError("'name' must be a string")
    if not isinstance(entries, list):
        raise DashError("'entries' must be a list")
    names = [str(e)[:300] for e in entries[:LIST_SHOWN * 2] if isinstance(e, str)]
    with _lock:
        _counter += 1
        chip = f"[Folder #{_counter}: {name.strip()[:80]}/]"
        _folders[chip] = {"names": names, "truncated": bool(truncated) or len(entries) > len(names), "name": name.strip()[:80]}
        _bounded(_folders)
    return {"kind": "folder", "chip": chip, "name": name.strip()[:80] + "/", "entries": len(names), "more": bool(truncated),
            "note": "Finder gives the page names only, not the folder's location, so Axon sees the list of names. "
                    "Drag files from inside it to attach their contents."}


def _file_text(chip: str, p: Path) -> str:
    rel = chip[1:]
    from axon.permissions.paths import is_protected_path
    if is_protected_path(p) or dash_fs.is_secret_file(p.name):
        return chip
    try:
        size = p.stat().st_size
    except OSError:
        return chip + " (this file is gone)"
    kb = f"{size / 1024:.1f} KB"
    if p.suffix.lower() == ".pdf":
        return f"[PDF: {rel}, {kb}. Read it with the Read tool; for a long one pass pages, for example pages=\"1-5\".]"
    from axon.tools.file_formats import detect_kind, extract
    try:
        kind = detect_kind(p)
    except OSError:
        return chip
    if kind not in TEXT_KINDS:
        # Word, Excel, slides, archives, databases and the like: the readable text, as @path typed in the box gives.
        text = extract(p, limit=2000).text
        note = ""
        if len(text) > INLINE_CHARS:
            text, note = text[:INLINE_CHARS], f"\n[... cut at {INLINE_CHARS:,} characters; Read {rel} for the rest ...]"
        return f"\n\n[File: {rel}]\n```\n{text}{note}\n```\n"
    if size <= INLINE_BYTES:
        try:
            data = p.read_bytes()
        except OSError:
            return chip
        if b"\x00" not in data[:4096]:
            text = data.decode("utf-8", errors="replace")
            note = ""
            if len(text) > INLINE_CHARS:
                text, note = text[:INLINE_CHARS], f"\n[... cut at {INLINE_CHARS:,} characters; Read {rel} for the rest ...]"
            return f"\n\n[File: {rel}]\n```\n{text}{note}\n```\n"
        return f"\n\n[File: {rel}]\n```\n{extract(p, limit=2000).text[:INLINE_CHARS]}\n```\n"
    return f"[File: {rel}, {kb}, too large to include here. Read {rel} with the Read tool, in parts.]"


def _folder_text(chip: str, meta: dict[str, Any]) -> str:
    if "abs" in meta:
        p = Path(meta["abs"])
        try:
            shown, seen, stopped = list_folder(p)
        except DashError:
            return chip
        total = f"{seen}+" if stopped else str(seen)
        more = f"\n... and more: {total} entries in all, only the first {LIST_SHOWN} are listed." if seen > len(shown) else ""
        return (f"\n\n[Folder: {meta['rel']}/ at {p}, {total} entries, one level only. "
                f"Use Glob or Read to look inside.]\n```\n{chr(10).join(shown) or '(empty folder)'}{more}\n```\n")
    names = meta.get("names", [])
    cut = "\n... (the list was cut; the folder has more)" if meta.get("truncated") else ""
    return (f"\n\n[Folder {meta.get('name', '')}/ dragged in from outside the project; its location is unknown, "
            f"so this is only the list of names inside it.]\n```\n{chr(10).join(names) or '(empty folder)'}{cut}\n```\n")


def expand(text: str) -> str:
    """Registered ``@path`` and ``[Folder #N: x/]`` chips in *text* become what the model should receive."""
    if "@" not in text and "[Folder #" not in text:
        return text
    with _lock:
        folders = dict(_folders)
        refs = dict(_refs)
    for chip, meta in folders.items():
        if chip in text:
            text = text.replace(chip, _folder_text(chip, meta))
    for chip in sorted(refs, key=len, reverse=True):
        if chip not in text:
            continue
        pat = re.compile(r"(?<![\w@/.\-])" + re.escape(chip) + r"(?![\w/\-]|\.\w)")
        if pat.search(text):
            body = _file_text(chip, Path(refs[chip]["abs"]))
            text = pat.sub(lambda _m, b=body: b, text)
    return text
