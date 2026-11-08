"""A file whose body holds a secret is flagged before it is sent: a line in the terminal, a notice on the chip in
the app. The person can remove it, nothing is changed in the file or its text, and nothing is blocked."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard import c, env, llm, server  # noqa: E402,F401

SECRET_BODY = 'API_KEY = "sk-live-abcdef0123456789abcdef0123456789"\nDEBUG = True\n'


def test_terminal_mention_warns_and_keeps_the_text_unchanged(tmp_path, capsys):
    from axon.cli import _warn_secret_in_mention, expand_file_mentions
    (tmp_path / "config.py").write_text(SECRET_BODY)
    out = expand_file_mentions("explain @config.py", tmp_path, warn_secrets=_warn_secret_in_mention)
    printed = capsys.readouterr().out
    assert "config.py" in printed and "secret" in printed.lower()
    assert SECRET_BODY.strip() in out                        # not blocked, not altered


def test_terminal_person_can_leave_the_file_out(tmp_path):
    from axon.cli import expand_file_mentions
    (tmp_path / "config.py").write_text(SECRET_BODY)
    out = expand_file_mentions("explain @config.py", tmp_path, warn_secrets=lambda name, labels: False)
    assert "sk-live" not in out and "left out at your request" in out


def test_clean_files_do_not_warn(tmp_path):
    from axon.cli import expand_file_mentions
    (tmp_path / "ok.py").write_text("print('hi')\n")
    calls = []
    out = expand_file_mentions("see @ok.py", tmp_path, warn_secrets=lambda *a: calls.append(a) or True)
    assert not calls and "print('hi')" in out


def test_app_attach_flags_the_chip(c, env):
    (env.ws / "config.py").write_text(SECRET_BODY)
    (env.ws / "plain.py").write_text("x = 1\n")
    r = json.loads(c.post("/api/attach/paths", {"paths": ["config.py", "plain.py"]})[2])["items"]
    by = {i["name"]: i for i in r}
    assert by["config.py"]["secrets"] and not by["plain.py"].get("secrets")
    assert (env.ws / "config.py").read_text() == SECRET_BODY


def test_app_upload_flags_the_chip(c, env):
    from axon.features.dash_runtime import Attachments
    res = Attachments().add_file("settings.py", SECRET_BODY.encode(), env.ws)
    assert res["secrets"]


def test_terminal_drop_notes_the_secret(tmp_path, monkeypatch):
    from axon.agent import attachments as att
    f = tmp_path / "outside" / "config.py"
    f.parent.mkdir()
    f.write_text(SECRET_BODY)
    got = att.ingest_drop(str(f))
    assert got is not None and "secret" in got[1].lower() and "Delete" in got[1]


def test_page_shows_the_warning_on_the_chip():
    from axon.features.dashboard_web import APP_JS
    assert "May hold a secret" in APP_JS and "meta.secrets" in APP_JS
