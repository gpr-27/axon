"""Regressions found by driving the Files viewer, the tree and the composer in a real browser (the e2e round on files
and attachments). Each one failed before its fix."""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_attach, dash_fs  # noqa: E402
from axon.features.dash_css import APP_LAYOUT_CSS  # noqa: E402
from axon.features.dashboard_web import APP_JS  # noqa: E402


def _save(ws: Path, name: str, content: str, sha: str = "", **kw):
    return dash_fs.save_file(ws, [], name, content, sha, **kw)


def test_creating_a_credential_file_from_the_page_is_refused(tmp_path):
    for name in (".env", ".env.local", "id_rsa2", "credentials.json"):
        with pytest.raises(dash_fs.DashError) as e:
            _save(tmp_path, name, "SECRET=1\n", create=True)
        assert getattr(e.value, "status", 0) == 403
        assert not (tmp_path / name).exists()
    assert _save(tmp_path, "notes.txt", "hello\n", create=True)["created"]


def test_a_notebook_cannot_be_saved_as_broken_json(tmp_path):
    nb = tmp_path / "a.ipynb"
    nb.write_text(json.dumps({"cells": [], "nbformat": 4}))
    sha = dash_fs.read_file(tmp_path, [], "a.ipynb", as_text=True)["sha"]
    with pytest.raises(dash_fs.DashError) as e:
        _save(tmp_path, "a.ipynb", "{not json", sha)
    assert getattr(e.value, "status", 0) == 422
    assert json.loads(nb.read_text())["nbformat"] == 4            # untouched
    good = json.dumps({"cells": [], "nbformat": 5})
    _save(tmp_path, "a.ipynb", good, sha)
    assert json.loads(nb.read_text())["nbformat"] == 5


def test_text_search_says_how_many_big_files_it_skipped(tmp_path):
    (tmp_path / "small.txt").write_text("needle here\n")
    (tmp_path / "big.txt").write_text("needle " * 400_000)        # 2.8 MB: over the search limit
    r = dash_fs.grep_files(tmp_path, "needle")
    assert [m["path"] for m in r["matches"]] == ["small.txt"] and r["skipped_large"] == 1
    assert "skipped_large" in APP_JS and "not searched" in APP_JS


def test_overwrite_anyway_uses_the_line_count_of_the_changed_file():
    edit = APP_JS[APP_JS.index("async function edit(f)"):]
    assert "lines = cur.lines" in edit.split("const save = async")[1].split("};")[0]


def test_ask_axon_fills_the_open_chat_instead_of_waiting_for_the_next_one():
    assert "function axAsk(" in APP_JS and "C.prefill = (text)" in APP_JS
    assert APP_JS.count("axAsk(f.path)") == 2                       # the viewer toolbar and the formats card
    assert "lsSet('axon-prefill', '@' + f.path" not in APP_JS


def test_toolbar_path_and_deep_folders_stay_readable():
    assert ".ftoolbar .p{" in APP_LAYOUT_CSS and "flex:1 1 240px" in APP_LAYOUT_CSS.split(".ftoolbar .p{")[1].split("}")[0]
    assert "min(var(--d,0),8)" in APP_LAYOUT_CSS                    # 30 levels of folders no longer push names off screen


def _docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType='
                   '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   '<w:body><w:p><w:r><w:t>Quarterly revenue grew twelve percent</w:t></w:r></w:p></w:body></w:document>')


def test_a_dragged_in_word_file_reaches_the_model_as_text_not_as_not_text(tmp_path):
    _docx(tmp_path / "r.docx")
    items = dash_attach.attach_paths(tmp_path, [], ["r.docx"])
    body = dash_attach.expand(items[0]["chip"] + " please summarise")
    assert "Quarterly revenue grew twelve percent" in body and "not text" not in body


@pytest.mark.skipif(not shutil.which("sips"), reason="needs the macOS converter")
def test_bmp_tiff_and_heic_pictures_are_converted_and_not_enlarged(tmp_path, monkeypatch):
    import subprocess
    from axon.agent import images
    monkeypatch.setattr(images, "get_images_cache_dir", lambda: tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    png = tmp_path / "p.png"
    import struct
    import zlib
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + b"\x20\x60\xc0" * 64 for _ in range(32))
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 32, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    for ext in ("bmp", "tiff"):
        src = tmp_path / f"p.{ext}"
        if subprocess.run(["sips", "-s", "format", ext, str(png), "--out", str(src)], capture_output=True).returncode:
            pytest.skip("sips could not make the sample")
        att = images.ingest_image_file(str(src))
        if att is None:
            pytest.skip("sample is not a valid picture on this machine")
        assert att.mime_type == "image/png" and att.cached_path.suffix == ".png"
        assert att.cached_path.read_bytes()[:4] == b"\x89PNG"
        assert max(att.width, att.height) <= 64                       # not blown up to 4096


def test_credential_name_is_shown_with_its_leading_dot(tmp_path, monkeypatch):
    from axon.features import dash_runtime
    from axon.features.dash_common import DashError
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    with pytest.raises(DashError) as e:
        dash_runtime.Attachments().add_file(".env", b"A=1\n", tmp_path)
    assert str(e.value).startswith(".env is a credential file")
