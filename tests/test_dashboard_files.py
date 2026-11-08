"""File viewer of the web app: raw-file route (types, Range, sandboxing), jail, reveal, paging, client script."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_fs  # noqa: E402
from test_dashboard import c, env, server  # noqa: E402,F401  (fixtures)

SAMPLES = {"a.png": "image/png", "a.jpg": "image/jpeg", "a.gif": "image/gif", "a.webp": "image/webp", "a.avif": "image/avif",
           "a.bmp": "image/bmp", "a.ico": "image/x-icon", "a.svg": "image/svg+xml", "v.mp4": "video/mp4", "v.mov": "video/quicktime",
           "v.webm": "video/webm", "s.mp3": "audio/mpeg", "s.wav": "audio/wav", "d.pdf": "application/pdf"}


def put(env, name: str, data: bytes = b"0123456789" * 100) -> Path:
    p = env.ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.mark.parametrize("name,ctype", list(SAMPLES.items()))
def test_content_types_and_headers(c, env, name, ctype):
    put(env, name)
    st, h, body = c.get(f"/api/file/raw?path={name}")
    assert st == 200 and h["content-type"] == ctype and len(body) == 1000
    assert h["x-content-type-options"] == "nosniff" and h["accept-ranges"] == "bytes"
    assert h["content-disposition"] == "inline"


def test_range_requests(c, env):
    data = bytes(range(256)) * 40
    put(env, "clip.mov", data)
    st, h, body = c.get("/api/file/raw?path=clip.mov", headers={"Range": "bytes=10-19"})
    assert st == 206 and body == data[10:20] and h["content-range"] == f"bytes 10-19/{len(data)}" and h["content-length"] == "10"
    st, h, body = c.get("/api/file/raw?path=clip.mov", headers={"Range": "bytes=10000-"})
    assert st == 206 and body == data[10000:]
    st, _, body = c.get("/api/file/raw?path=clip.mov", headers={"Range": "bytes=-5"})
    assert st == 206 and body == data[-5:]
    assert c.get("/api/file/raw?path=clip.mov", headers={"Range": f"bytes={len(data)}-"})[0] == 416
    assert c.get("/api/file/raw?path=clip.mov", headers={"Range": "bytes=junk"})[0] == 416
    st, h, body = c.get("/api/file/raw?path=clip.mov", headers={"Range": "bytes=0-5"})
    assert h["content-type"] == "video/quicktime"


def test_html_and_svg_are_never_active_documents(c, env):
    put(env, "page.html", b"<script>alert(1)</script>")
    put(env, "pic.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>')
    assert c.get("/api/file/raw?path=page.html")[0] == 415                      # no inline preview of HTML at all
    st, h, _ = c.get("/api/file/raw?path=page.html&download=1")
    assert st == 200 and h["content-type"] == "application/octet-stream" and h["content-disposition"].startswith("attachment")
    assert h["x-content-type-options"] == "nosniff" and "sandbox" in h["content-security-policy"]
    st, h, _ = c.get("/api/file/raw?path=pic.svg")
    assert st == 200 and h["content-security-policy"].startswith("sandbox") and "script-src" not in h["content-security-policy"]
    st, h, _ = c.get("/api/file/raw?path=pic.svg&download=1")
    assert h["content-type"] == "application/octet-stream" and h["content-disposition"].startswith("attachment")


def test_pdf_csp_is_relaxed_only_for_pdf(c, env):
    put(env, "d.pdf", b"%PDF-1.4\n")
    put(env, "p.png", b"\x89PNG")
    _, h, _ = c.get("/api/file/raw?path=d.pdf")
    assert "object-src 'self'" in h["content-security-policy"] and h["x-frame-options"] == "SAMEORIGIN"
    _, h, _ = c.get("/api/file/raw?path=p.png")
    assert "object-src" not in h["content-security-policy"] and h["content-security-policy"].startswith("sandbox")
    _, h, _ = c.get("/api/state") if False else c.get("/")
    csp = h["content-security-policy"]
    assert "frame-src 'self'" in csp and "object-src 'none'" in csp and "script-src 'self'" in csp   # the page itself stays strict


def test_traversal_symlink_and_credentials_refused(c, env, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG secret")
    (env.ws / "link.png").symlink_to(outside)
    (env.ws / "dirlink").symlink_to(tmp_path)
    put(env, ".env", b"API_KEY=sk-secret")
    put(env, ".env.local", b"X=1")
    put(env, "config/.npmrc", b"//registry:_authToken=abc")
    put(env, ".ssh/id_rsa.png", b"PRIVATE")
    put(env, ".git/config", b"[core]")
    for rel in ["../outside.png", "/etc/hosts", "link.png", "dirlink/outside.png", ".env", ".env.local", "config/.npmrc",
                ".ssh/id_rsa.png", ".git/config", "..%2foutside.png", "a%00.png"]:
        for suffix in ("", "&download=1"):
            st, _, body = c.get(f"/api/file/raw?path={rel}{suffix}")
            assert st in (400, 403, 404, 415), (rel, suffix, st)
            assert b"secret" not in body and b"sk-secret" not in body and b"PRIVATE" not in body and b"root:" not in body
    for rel in ["link.png", ".ssh/id_rsa.png"]:
        assert c.get(f"/api/file?path={rel}")[0] in (403, 404), rel
        assert c.post("/api/file/reveal", {"path": rel})[0] in (403, 404), rel
    put(env, "certs/site.pem", b"-----BEGIN-----")
    put(env, ".env.example", b"KEY=")
    assert c.get("/api/file/raw?path=certs/site.pem&download=1")[0] == 403
    assert dash_fs.is_secret_file(".env.production") and not dash_fs.is_secret_file(".env.example") and not dash_fs.is_secret_file("env.py")


def test_raw_needs_auth_and_loopback_host(c, env):
    put(env, "a.png", b"\x89PNG")
    assert c.get("/api/file/raw?path=a.png", cookie=False)[0] in (401, 403)
    assert c.get("/api/file/raw?path=a.png", host="evil.example.com")[0] == 403
    assert c.post("/api/file/reveal", {"path": "a.png"}, cookie=False)[0] in (401, 403)
    assert c.post("/api/file/reveal", {"path": "a.png"}, origin="http://evil.example.com")[0] in (400, 403)
    assert c.get("/api/file/reveal?path=a.png")[0] in (404, 405)                   # reveal is a POST route only


def test_reveal_uses_the_platform_file_manager(c, env, monkeypatch):
    put(env, "a.png", b"\x89PNG")
    calls: list[list[str]] = []
    monkeypatch.setattr(dash_fs.subprocess, "Popen", lambda args, **kw: calls.append(list(args)))
    target = str((env.ws / "a.png").resolve())
    for plat, want in [("darwin", ["open", "-R", target]), ("linux", ["xdg-open", str(Path(target).parent)]),
                       ("win32", ["explorer", "/select,", target])]:
        calls.clear()
        monkeypatch.setattr(dash_fs.sys, "platform", plat)
        st, _, body = c.post("/api/file/reveal", {"path": "a.png"})
        assert st == 200 and json.loads(body)["ok"] is True and calls == [want], plat
    assert c.post("/api/file/reveal", {"path": "missing.png"})[0] == 404


def test_file_kinds(c, env):
    put(env, "x.docx", b"PK\x03\x04\x00\x00")
    put(env, "d.csv", b"a,b\n1,2\n")
    put(env, "d.tsv", b"a\tb\n1\t2\n")
    put(env, "n.md", b"# hi\n")
    put(env, "j.json", b"{}")
    put(env, "m.py", b"print(1)\n")
    put(env, "v.mov", b"\x00\x00\x00")
    put(env, "t.xyz", b"plain words\n")
    want = {"x.docx": "docx", "d.csv": "csv", "d.tsv": "csv", "n.md": "markdown", "j.json": "json", "m.py": "code", "v.mov": "video",
            "t.xyz": "text", "d.pdf": "pdf"}
    put(env, "d.pdf", b"%PDF")
    for name, kind in want.items():
        f = c.json(f"/api/file?path={name}")
        assert f["kind"] == kind and {"size", "mtime", "mime", "abs", "name"} <= set(f), name
    assert c.json("/api/file?path=v.mov")["mime"] == "video/quicktime"
    assert c.json("/api/file?path=x.docx")["binary"] is True


def test_large_files_are_paged(c, env):
    n = 100_000
    put(env, "big.log", b"".join(b"line %d\n" % i for i in range(n)))
    f = c.json("/api/file?path=big.log&limit=1000")
    assert f["lines"] == 1000 and f["total_lines"] == n and len(f["text"]) < 20_000 and f["editable"] is False
    g = c.json("/api/file?path=big.log&offset=99000&limit=1000")
    assert g["text"].splitlines()[-1] == "line 99999" and g["offset"] == 99000
    assert c.json("/api/file?path=big.log&offset=100000&limit=1000")["lines"] == 0
    assert c.json("/api/file?path=big.log&limit=999999")["lines"] == dash_fs.MAX_PAGE_LINES


def test_minified_json_is_not_clipped_but_other_long_lines_are(c, env):
    doc = json.dumps({"k": ["x" * 50] * 2000})
    put(env, "min.json", doc.encode())
    f = c.json("/api/file?path=min.json")
    assert f["clipped_lines"] == 0 and json.loads(f["text"]) == json.loads(doc)
    put(env, "long.txt", b"y" * 100_000)
    assert c.json("/api/file?path=long.txt")["clipped_lines"] == 1


def test_client_script_has_the_viewer_and_parses():
    from axon.features.dash_js_files import JS_FILES
    for needle in ("function fileThumb", "window.fileThumb", "function imageView", "function mediaView", "function pdfView",
                   "function csvPaged", "function paged", "Fit to window", "Actual size", "Wrap lines", "Reveal in Finder",
                   "/api/file/reveal", "download=1"):
        assert needle in JS_FILES, needle
    assert "innerHTML" in JS_FILES and JS_FILES.count("innerHTML") == 1             # only the server-rendered markdown
    node = shutil.which("node")
    if node:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "files.js"
            p.write_text(JS_FILES, encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, encoding="utf-8")
            assert r.returncode == 0, r.stderr


# ─── redacted and partial reads must never be written back ──────────────────

SECRET_ENV = b"API_KEY=sk-live-abcdefghijklmnop123456\nPLAIN=1\n"


def _save(c, path, content, sha="", **kw):
    return c.post("/api/file/save", {"path": path, "content": content, "sha": sha, **kw})


def test_env_is_shown_redacted_read_only_and_explained(c, env):
    p = put(env, ".env", SECRET_ENV)
    f = c.json("/api/file?path=.env")
    assert "sk-live" not in f["text"] and "[REDACTED]" in f["text"]
    assert f["editable"] is False and f["redacted"] is True and f["sha"] == ""
    assert "hidden" in f["why_read_only"].lower()
    assert c.get("/api/file/raw?path=.env")[0] == 403
    assert p.read_bytes() == SECRET_ENV


def test_save_of_redacted_text_never_touches_the_file(c, env):
    p = put(env, ".env", SECRET_ENV)
    shown = c.json("/api/file?path=.env")["text"]
    import hashlib
    real_sha = hashlib.sha256(SECRET_ENV).hexdigest()
    for sha in ("", real_sha):                                   # even a client that knows the real hash
        st, _, _ = _save(c, ".env", shown + "NEW=1\n", sha)
        assert st >= 400
        assert p.read_bytes() == SECRET_ENV
    q = put(env, "cfg.txt", SECRET_ENV)                          # same for a non-.env file with a secret in it
    f = c.json("/api/file?path=cfg.txt")
    assert f["editable"] is False and f["redacted"] is True
    st, _, _ = _save(c, "cfg.txt", f["text"], hashlib.sha256(SECRET_ENV).hexdigest())
    assert st >= 400 and q.read_bytes() == SECRET_ENV


def test_marker_is_refused_but_plain_edits_and_literal_markers_still_work(c, env):
    import hashlib
    p = put(env, "n.txt", b"hello\n")
    st, _, _ = _save(c, "n.txt", "token=[REDACTED]\n", hashlib.sha256(b"hello\n").hexdigest())
    assert st == 409 and p.read_bytes() == b"hello\n"
    f = c.json("/api/file?path=n.txt")
    assert f["editable"] is True and f["redacted"] is False
    assert _save(c, "n.txt", "bye\n", f["sha"])[0] == 200 and p.read_bytes() == b"bye\n"
    doc = b"docs say [REDACTED] means hidden\n"                   # a marker already on disk is not new
    p2 = put(env, "d.txt", doc)
    f = c.json("/api/file?path=d.txt")
    assert _save(c, "d.txt", doc.decode() + "more\n", f["sha"])[0] == 200


def test_partly_loaded_file_is_never_saved_over_the_whole(c, env):
    import hashlib
    body = b"".join(b"line %d\n" % i for i in range(50))
    p = put(env, "long.txt", body)
    sha = hashlib.sha256(body).hexdigest()
    st, _, _ = _save(c, "long.txt", "".join("line %d\n" % i for i in range(10)), sha, lines=10)
    assert st == 409 and p.read_bytes() == body
    assert _save(c, "long.txt", body.decode() + "x\n", sha, lines=50)[0] == 200


def test_non_utf8_file_is_read_only(c, env):
    p = put(env, "latin.txt", b"caf\xe9\n")
    f = c.json("/api/file?path=latin.txt")
    assert f["editable"] is False and f["why_read_only"]
    assert p.read_bytes() == b"caf\xe9\n"


def test_binary_info_and_mov_guidance(c, env):
    put(env, "p.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (7).to_bytes(4, "big") + (5).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")
    f = c.json("/api/file?path=p.png")
    assert f["binary"] is True and f["lines"] == 0 and f["total_lines"] is None
    assert f["dimensions"] == [7, 5]
    put(env, "x.docx", b"PK\x03\x04\x00\x00")
    assert c.json("/api/file?path=x.docx")["total_lines"] is None
    from axon.features.dash_js_files import JS_FILES
    for needle in ("Go to line", "Chrome only plays", "No lines to count", "f.why_read_only"):
        assert needle in JS_FILES, needle


def test_a_notebook_opens_as_cells_and_its_source_stays_editable(tmp_path):
    from axon.features import dash_fs
    (tmp_path / "n.ipynb").write_text('{"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}', encoding="utf-8")
    assert dash_fs.read_file(tmp_path, [], "n.ipynb")["kind"] == "ipynb"
    source = dash_fs.read_file(tmp_path, [], "n.ipynb", as_text=True)
    assert source["kind"] != "ipynb" and source["editable"] is True and '"nbformat"' in source["text"]
