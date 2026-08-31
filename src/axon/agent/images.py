"""
Multimodal image ingestion, persistent caching, thumbnail badges, and OCR metadata.
"""
from __future__ import annotations
import base64
import hashlib
import mimetypes
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".tiff")

@dataclass
class ImageAttachment:
    index: int
    label: str  # e.g. "[Image #1]"
    original_path: str
    cached_path: Path
    mime_type: str
    width: int
    height: int
    size_bytes: int
    ocr_text: str = ""

    def read_base64(self) -> str:
        if self.cached_path.exists():
            return base64.b64encode(self.cached_path.read_bytes()).decode("utf-8")
        return ""

    def get_ocr_text(self) -> str:
        if not self.ocr_text and self.cached_path.exists():
            self.ocr_text = _extract_ocr_text(self.cached_path)
        return self.ocr_text

def save_clipboard_image(dest_path: Path) -> bool:
    """Instantly save image from macOS/system clipboard to dest_path (< 40ms)."""
    # 1. Native macOS Cocoa helper
    axon_bin = Path.home() / ".axon" / "bin"
    axon_bin.mkdir(parents=True, exist_ok=True)
    helper = axon_bin / "axon_paste_helper"

    if not helper.exists():
        src_c = """#import <Cocoa/Cocoa.h>
#import <stdio.h>
int main(int argc, const char * argv[]) {
    @autoreleasepool {
        if (argc < 2) return 1;
        NSPasteboard *pb = [NSPasteboard generalPasteboard];
        NSArray *classes = @[[NSImage class]];
        if (![pb canReadObjectForClasses:classes options:@{}]) return 2;
        NSArray *items = [pb readObjectsForClasses:classes options:@{}];
        if (items.count == 0) return 2;
        NSImage *image = items[0];
        NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithData:[image TIFFRepresentation]];
        NSData *pngData = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
        if (!pngData) return 3;
        NSString *dest = [NSString stringWithUTF8String:argv[1]];
        if ([pngData writeToFile:dest atomically:YES]) {
            printf("OK\\n");
            return 0;
        }
        return 4;
    }
}"""
        src_file = axon_bin / "axon_paste_helper.m"
        try:
            src_file.write_text(src_c.strip(), encoding="utf-8")
            subprocess.run(["clang", "-O3", "-framework", "Cocoa", str(src_file), "-o", str(helper)], capture_output=True, timeout=5)
        except Exception:
            pass

    if helper.exists():
        try:
            res = subprocess.run([str(helper), str(dest_path)], capture_output=True, text=True, timeout=0.8)
            if res.returncode == 0 and dest_path.exists() and dest_path.stat().st_size > 0:
                return True
        except Exception:
            pass

    # 2. Fallback to pngpaste if installed
    try:
        res = subprocess.run(["pngpaste", str(dest_path)], capture_output=True, timeout=0.5)
        if res.returncode == 0 and dest_path.exists() and dest_path.stat().st_size > 0:
            return True
    except Exception:
        pass

    # 3. Fallback to osascript with PNG and TIFF support
    try:
        osa = f'''set dest to POSIX file "{dest_path}"
try
    set img to the clipboard as «class PNGf»
    set f to open for access dest with write permission
    set eof f to 0
    write img to f
    close access f
    return "OK"
on error
    try
        set img to the clipboard as «class TIFF»
        set f to open for access dest with write permission
        set eof f to 0
        write img to f
        close access f
        return "OK"
    on error
        return "FAIL"
    end try
end try'''
        res = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True, timeout=1.0)
        if res.returncode == 0 and "OK" in res.stdout and dest_path.exists() and dest_path.stat().st_size > 0:
            return True
    except Exception:
        pass

    return False

def get_images_cache_dir() -> Path:
    """Return the persistent image cache directory."""
    d = Path.home() / ".axon" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _get_image_dimensions(p: Path) -> tuple[int, int]:
    """Extract width and height from image headers or macOS sips."""
    # 1. Try macOS sips if available
    try:
        res = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(p)], capture_output=True, text=True, timeout=1)
        if res.returncode == 0:
            w_match = re.search(r"pixelWidth:\s*(\d+)", res.stdout)
            h_match = re.search(r"pixelHeight:\s*(\d+)", res.stdout)
            if w_match and h_match:
                return int(w_match.group(1)), int(h_match.group(1))
    except Exception:
        pass

    # 2. Fallback basic binary header parsing
    try:
        with open(p, "rb") as f:
            data = f.read(32)
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                w, h = struct.unpack(">II", data[16:24])
                return int(w), int(h)
            elif data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
                w, h = struct.unpack("<HH", data[6:10])
                return int(w), int(h)
    except Exception:
        pass
    return (0, 0)

def _extract_ocr_text(p: Path) -> str:
    """Extract OCR text from image using native macOS Vision OCR or tesseract."""
    # 1. Try precompiled /tmp/axon_ocr if available
    try:
        if os.path.exists("/tmp/axon_ocr"):
            res = subprocess.run(["/tmp/axon_ocr", str(p)], capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    except Exception:
        pass

    # 2. Try swift Vision script fallback
    try:
        swift_code = (
            'import Foundation, Vision, AppKit\n'
            'guard CommandLine.arguments.count > 1, let img = NSImage(contentsOfFile: CommandLine.arguments[1]), '
            'let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(0) }\n'
            'let req = VNRecognizeTextRequest { r, _ in\n'
            '  guard let obs = r.results as? [VNRecognizedTextObservation] else { return }\n'
            '  print(obs.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\\n"))\n'
            '}\n'
            'try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])'
        )
        res = subprocess.run(["swift", "-e", swift_code, str(p)], capture_output=True, text=True, timeout=3)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    # 3. Try tesseract if installed
    try:
        res = subprocess.run(["tesseract", str(p), "stdout"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    return ""

def unescape_path(raw: str) -> str:
    """Convert shell-escaped paths, quoted paths, and drag-and-drop file:// URLs into actual filesystem paths."""
    clean = raw.strip()
    # Strip enclosing quotes
    if (clean.startswith('"') and clean.endswith('"')) or (clean.startswith("'") and clean.endswith("'")):
        clean = clean[1:-1].strip()
    # Strip drag-and-drop file:// prefix
    if clean.startswith("file://"):
        clean = clean[7:]
    # Unquote URL-encoded characters (%20, %E2%80%AF, etc.)
    import urllib.parse
    clean = urllib.parse.unquote(clean)
    # Replace escaped spaces and quotes
    clean = clean.replace(r"\ ", " ").replace(r"\'", "'").replace(r'\"', '"').replace(r"\:", ":")
    return clean.strip()

def resolve_fuzzy_image_path(raw_str: str) -> Path | None:
    """
    Resolve image path supporting:
    - Absolute paths
    - Relative paths (from current working directory or common parent dirs)
    - Unicode space normalization (\u202f narrow non-breaking space used in macOS screenshots, \u00a0, etc.)
    - Quoted and shell-escaped paths
    """
    clean = unescape_path(raw_str).strip()
    if not clean:
        return None

    # 1. Direct path check
    p = Path(clean).expanduser()
    if p.exists() and p.is_file():
        return p.resolve()

    # 2. Try relative to cwd
    cwd_p = (Path.cwd() / clean).resolve()
    if cwd_p.exists() and cwd_p.is_file():
        return cwd_p

    # 3. Unicode space normalization (e.g. 'Screenshot...at 1.26.15\u202fPM.png')
    import unicodedata
    norm_clean = unicodedata.normalize("NFKD", clean).replace("\u202f", " ").replace("\u00a0", " ")

    candidate_parents = [Path.cwd()]
    if p.parent != Path("."):
        candidate_parents.append(p.parent.resolve())

    for parent in candidate_parents:
        if parent.exists() and parent.is_dir():
            target_name = Path(norm_clean).name
            for item in parent.iterdir():
                if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS:
                    item_norm = unicodedata.normalize("NFKD", item.name).replace("\u202f", " ").replace("\u00a0", " ")
                    if item_norm == target_name or item_norm == norm_clean:
                        return item.resolve()

    return None

# Global cache of ingested image attachments across turns
_INGESTED_IMAGES: dict[str, ImageAttachment] = {}
_IMAGE_COUNTER: int = 1

def ingest_image_file(raw_path: str, custom_index: int | None = None) -> ImageAttachment | None:
    """
    Immediately copy a transient image (such as a macOS screenshot in /var/folders/...)
    to persistent storage, inspect its dimensions, and assign a thumbnail badge [Image #N].
    """
    global _IMAGE_COUNTER
    clean_path = unescape_path(raw_path)
    p = resolve_fuzzy_image_path(clean_path)

    if not p or not p.exists() or not p.is_file():
        # Check if already ingested by label or filename
        for att in _INGESTED_IMAGES.values():
            if att.original_path == clean_path or att.cached_path.name == Path(clean_path).name:
                return att
        return None

    try:
        data = p.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:12]
        cache_dir = get_images_cache_dir()
        ext = p.suffix.lower() if p.suffix else ".png"
        cached_file = cache_dir / f"img_{digest}_{p.stem[:20]}{ext}"

        if not cached_file.exists() or cached_file.stat().st_size != len(data):
            cached_file.write_bytes(data)

        idx = custom_index or _IMAGE_COUNTER
        if custom_index is None:
            _IMAGE_COUNTER += 1

        mime, _ = mimetypes.guess_type(str(cached_file))
        mime = mime or "image/png"
        w, h = _get_image_dimensions(cached_file)

        att = ImageAttachment(
            index=idx,
            label=f"[Image #{idx}]",
            original_path=str(p),
            cached_path=cached_file,
            mime_type=mime,
            width=w,
            height=h,
            size_bytes=len(data),
            ocr_text="",
        )

        _INGESTED_IMAGES[att.label] = att
        _INGESTED_IMAGES[str(p)] = att
        _INGESTED_IMAGES[str(cached_file)] = att
        _INGESTED_IMAGES[clean_path] = att
        return att
    except Exception:
        return None

def compact_image_paths(text: str) -> tuple[str, list[ImageAttachment]]:
    """
    Scan user prompt for image paths (including relative paths, escaped macOS paths and [Image: ...]),
    immediately ingest them into persistent storage, and replace them with [Image #1], [Image #2], etc.
    """
    attachments: list[ImageAttachment] = []

    # 1. Match [Image: <path>] or [Image #N]
    bracket_re = re.compile(r"\[Image:\s*([^\s\]]+)\]", re.IGNORECASE)
    for match in bracket_re.findall(text):
        att = ingest_image_file(match)
        if att:
            attachments.append(att)
            text = text.replace(f"[Image: {match}]", att.label).replace(f"[Image:{match}]", att.label)

    # 2. Match existing [Image #N]
    num_bracket_re = re.compile(r"\[Image\s*#(\d+)\]", re.IGNORECASE)
    for match in num_bracket_re.finditer(text):
        lbl = f"[Image #{match.group(1)}]"
        if lbl in _INGESTED_IMAGES:
            attachments.append(_INGESTED_IMAGES[lbl])

    # 3. Match raw and relative paths with image extensions
    candidates: list[tuple[str, str]] = []
    for ext in IMAGE_EXTENSIONS:
        for m in re.finditer(re.escape(ext), text, re.IGNORECASE):
            end_idx = m.end()
            start_idx = end_idx - len(ext)
            while start_idx > 0:
                prev_ch = text[start_idx - 1]
                if prev_ch in ('\r', '\n'):
                    break
                start_idx -= 1
                cand = text[start_idx:end_idx].strip("\"' ")
                resolved = resolve_fuzzy_image_path(cand)
                if resolved:
                    candidates.append((text[start_idx:end_idx], str(resolved)))
                    break

    for raw_str, clean_str in candidates:
        att = ingest_image_file(clean_str)
        if att:
            attachments.append(att)
            text = text.replace(raw_str, att.label)

    # Deduplicate attachments while preserving order
    seen: set[str] = set()
    unique_atts: list[ImageAttachment] = []
    for a in attachments:
        if a.label not in seen:
            seen.add(a.label)
            unique_atts.append(a)

    return text.strip(), unique_atts

def get_attachment_by_label(label: str) -> ImageAttachment | None:
    """Retrieve attachment by label like [Image #1]."""
    return _INGESTED_IMAGES.get(label)
