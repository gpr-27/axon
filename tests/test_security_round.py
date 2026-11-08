"""Security round: file overwrite through a git ref, secret leaks in diff/search/preview, same-site API calls,
connection bursts and stalls, and the Git tool reading files it must not."""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_fs, worktree_view  # noqa: E402
from test_dashboard import c, env, server  # noqa: E402,F401  (fixtures)

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEPEMBODYSECRET\nSECONDBODYLINE\n-----END RSA PRIVATE KEY-----\n"


def git(ws: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True)


@pytest.fixture
def repo(env):
    ws = env.ws
    git(ws, "init", "-q")
    git(ws, "config", "user.email", "t@t")
    git(ws, "config", "user.name", "t")
    (ws / "notes.txt").write_text("a\n")
    (ws / "creds.txt").write_text("")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "init")
    return ws


# ─── git ref is never an option ─────────────────────────────────────────────

def test_compare_ref_cannot_be_a_git_option(repo, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS\n")
    (repo / "notes.txt").write_text("a\nb\n")
    for ref in (f"--output={victim}", "--stat", "-p"):
        ch = worktree_view.changes(repo, "branch", ref)
        assert ch["files"] == [] and ch["error"]
        assert worktree_view.file_diff(repo, "notes.txt", "branch", ref)["rows"] == []
    assert victim.read_text() == "PRECIOUS\n"
    assert worktree_view.changes(repo, "branch", "HEAD")["error"] == ""      # a real ref still works


def test_compare_ref_over_http(c, repo, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS\n")
    (repo / "notes.txt").write_text("a\nb\n")
    st, _, _ = c.get(f"/api/workbench/changes?mode=branch&ref=--output%3D{victim}")
    assert st == 200 and victim.read_text() == "PRECIOUS\n"


def test_discard_never_deletes_the_project_or_git(c, repo):
    ok, msg = worktree_view.discard(repo, ".", "U")
    assert not ok and repo.is_dir() and (repo / ".git").is_dir()
    ok, _ = worktree_view.discard(repo, ".git", "U")
    assert not ok and (repo / ".git").is_dir()
    st, _, _ = c.post("/api/workbench/stage", {"action": "discard", "path": ".", "code": "U", "confirm": True})
    assert st == 409 and (repo / "notes.txt").exists()


# ─── secrets in diffs, search and previews ──────────────────────────────────

def test_private_key_body_is_hidden_in_every_diff_and_search_view(c, repo):
    (repo / "creds.txt").write_text(PEM)
    st, _, b = c.get("/api/git/diff?path=creds.txt")
    assert st == 200 and b"MIIEPEMBODYSECRET" not in b and b"SECONDBODYLINE" not in b
    for code in ("", "U"):
        st, _, b = c.get(f"/api/workbench/diff?path=creds.txt&code={code}")
        assert st == 200 and b"MIIEPEMBODYSECRET" not in b and b"SECONDBODYLINE" not in b
    (repo / "plain.txt").write_text(PEM)
    st, _, b = c.get("/api/files/grep?q=SECONDBODY")
    assert b"SECONDBODYLINE" not in b
    st, _, b = c.get("/api/files/grep?q=MIIEPEM")
    assert b"MIIEPEMBODYSECRET" not in b


def test_credential_file_values_are_hidden_whatever_the_key_is_called(c, repo):
    (repo / ".env").write_text("MYVAR=plainsecretvalue\n")
    (repo / "credentials.json").write_text('{"x": "foo bar baz", "note":"sometext-in-credentials"}\n')
    for name, secret in ((".env", b"plainsecretvalue"), ("credentials.json", b"sometext-in-credentials")):
        for url in (f"/api/file?path={name}", f"/api/file/preview?path={name}", f"/api/git/diff?path={name}",
                    f"/api/workbench/diff?path={name}&code=U"):
            st, _, b = c.get(url)
            assert st == 200 and secret not in b and b"foo bar baz" not in b, url
    st, _, b = c.get("/api/files/grep?q=sometext")
    assert b"sometext-in-credentials" not in b
    assert b"MYVAR" in c.get("/api/file?path=.env")[2]                  # the key name stays, so the file is still recognisable


def test_redact_lines_keeps_line_count():
    out = dash_fs.redact_lines(["a", "-----BEGIN PRIVATE KEY-----", "zzz", "-----END PRIVATE KEY-----", "b"])
    assert out == ["a", "[REDACTED]", "[REDACTED]", "[REDACTED]", "b"]
    assert dash_fs.mask_secret_line("TOKEN=abc") == "TOKEN=[REDACTED]"
    assert dash_fs.mask_secret_line("# comment") == "# comment"


# ─── the API answers this page only ─────────────────────────────────────────

@pytest.mark.parametrize("hdr", [{"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://127.0.0.1:3000"}])
def test_api_get_refuses_other_ports_and_sites(c, hdr):
    assert c.get("/api/info", headers=hdr)[0] == 403
    assert c.get("/api/info", headers={"Sec-Fetch-Site": "same-origin"})[0] == 200
    assert c.get("/api/info")[0] == 200                                 # curl and tests send neither header


# ─── bursts and stalls ──────────────────────────────────────────────────────

def test_burst_of_connections_is_all_served(server, c):
    results: list[int] = []

    def one() -> None:
        try:
            results.append(c.get("/api/info")[0])
        except Exception as e:  # noqa: BLE001
            results.append(type(e).__name__)  # type: ignore[arg-type]

    ts = [threading.Thread(target=one) for _ in range(120)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(200) == 120, {r: results.count(r) for r in set(results)}


def test_stalled_client_does_not_block_others_and_is_dropped(server, c, monkeypatch):
    from axon.features import dashboard as dash
    assert dash._Handler.timeout and dash._Handler.timeout <= 300
    s = socket.create_connection(("127.0.0.1", server.port))
    s.sendall(b"GET /api/info HTTP/1.1\r\nHost: 127.0.0.1\r\nX-a: ")
    assert c.get("/api/info")[0] == 200
    s.close()


# ─── the Git tool and the credential stores ─────────────────────────────────

def test_git_tool_cannot_read_a_credential_file(tmp_path, monkeypatch):
    from axon.config import Settings
    from axon.permissions.engine import PermissionEngine
    from axon.tools.base import FileState, TodoState, ToolContext
    from axon.tools.git_tool import GitTool
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh" / "id_rsa"
    key.write_text("PRIVATEKEYBODY\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q")
    git(ws, "config", "user.email", "t@t")
    git(ws, "config", "user.name", "t")
    (ws / "a.txt").write_text("hi\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "i")
    (ws / "link").symlink_to(key)
    settings = Settings(workspace=ws)
    eng = PermissionEngine(settings)
    tool = GitTool()
    ctx = ToolContext(workspace=ws, file_state=FileState(), todos=TodoState(), settings=settings)
    calls = [["--contents", str(key), "a.txt"], ["--contents", "link", "a.txt"], ["--ignore-revs-file", "link", "a.txt"],
             ["-S", "link", "a.txt"], [f"--ignore-revs-file={key}", "a.txt"]]
    for extra in calls:
        args = {"subcommand": "blame", "args": extra}
        verdict = eng.check(tool, args, "default").outcome
        if verdict != "deny":
            with pytest.raises(Exception) as ei:
                tool.run(args, ctx)
            assert "PRIVATEKEYBODY" not in str(ei.value)
    assert eng.check(tool, {"subcommand": "diff", "args": ["HEAD", "--", str(key)]}, "default").outcome == "deny"
    assert eng.check(tool, {"subcommand": "log", "args": ["-3"]}, "default").outcome == "allow"


# ─── one writer per session, even when several processes race for a stale lease ──

def test_racing_processes_never_share_a_session(tmp_path):
    import json
    import os
    import time
    src = str(Path(__file__).resolve().parent.parent / "src")
    code = ("import sys, time\nsys.path.insert(0, %r)\nfrom pathlib import Path\nfrom axon.session import lease\n"
            "d, go = Path(sys.argv[1]), float(sys.argv[2])\nwhile time.time() < go: pass\n"
            "l = lease.acquire(d, 'web')\nprint('GOT' if l else 'NO', flush=True)\ntime.sleep(1.5)\n") % src
    for trial in range(5):
        d = tmp_path / f"s{trial}"
        d.mkdir()
        (d / "writer.lease").write_text(json.dumps({"pid": 999999, "host": socket.gethostname(), "owner": "x", "started": 1}))
        go = time.time() + 1.5
        procs = [subprocess.Popen([sys.executable, "-c", code, str(d), str(go)], stdout=subprocess.PIPE, text=True,
                                  env={**os.environ, "PYTHONPATH": src}) for _ in range(6)]
        outs = [p.communicate()[0].strip() for p in procs]
        assert outs.count("GOT") == 1, outs


# ─── shell guards: symlinks cloned with a repo, more credential stores, bypass-mode invariants ─────────────

@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("k")
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text("t")
    (home / ".axon").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    ws = tmp_path / "ws"
    ws.mkdir()
    return home, ws


def test_bare_word_symlink_to_a_credential_is_caught(guard_home):
    from axon.permissions.engine import shell_credential_access
    home, ws = guard_home
    (ws / "link").symlink_to(home / ".ssh" / "id_rsa")
    (ws / "plain").write_text("x")
    assert shell_credential_access("cat link", ws)
    assert shell_credential_access("cd . && head -c 5 link", ws)
    assert shell_credential_access("cat plain", ws) is None


def test_github_cli_token_is_a_credential(guard_home):
    from axon.permissions.engine import shell_credential_access
    from axon.permissions.paths import credential_location, is_protected_path
    home, ws = guard_home
    assert credential_location(home / ".config" / "gh" / "hosts.yml")
    assert is_protected_path(home / ".config" / "gh" / "hosts.yml")
    assert shell_credential_access("cat ~/.config/gh/hosts.yml", ws)
    assert credential_location(home / ".config" / "other" / "x") is None


@pytest.mark.parametrize("cmd", ["find / -delete", "find ~ -exec rm -rf {} +", "chmod -R 777 /", "chown -R me ~", "mv ~ /tmp/x",
                                 "rm -rf /Users", "rm -rf /home/", "sudo rm -rf /usr"])
def test_bypass_mode_cannot_wipe_the_machine(cmd):
    from axon.permissions.engine import catastrophic_command
    assert catastrophic_command(cmd)


@pytest.mark.parametrize("cmd", ["find . -name '*.pyc' -delete", "find ~/proj -delete", "chmod -R 755 ./dist", "mv a.txt b.txt",
                                 "rm -rf /tmp/build", "rm -rf /Users/me/proj/build", "rm -rf /var/folders/ab/tmp", "find / -name x"])
def test_ordinary_commands_are_not_catastrophic(cmd):
    from axon.permissions.engine import catastrophic_command
    assert catastrophic_command(cmd) is None


def test_a_fresh_session_is_leased_at_its_first_write(tmp_path, monkeypatch):
    """The session a process starts with was never opened, so it held no lease: a second process could resume it."""
    from axon.session import lease
    from axon.session.store import SessionStore
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SessionStore(workspace=ws)
    folder = store.active_file.parent
    assert lease.holder(folder) is None
    store.append_user("hello")
    assert lease.holder(folder)["pid"] == __import__("os").getpid()


def test_git_tool_refuses_shortened_dangerous_flags(tmp_path):
    from axon.config import Settings
    from axon.errors import ToolError
    from axon.tools.base import FileState, TodoState, ToolContext
    from axon.tools.git_tool import GitTool
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q")
    marker = tmp_path / "marker"
    ctx = ToolContext(workspace=ws, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=ws))
    for flag in ("--upl=touch " + str(marker), "--upload=touch " + str(marker), "--rec=x", "--outp=" + str(marker), "--no-ind"):
        with pytest.raises(ToolError, match="not allowed"):
            GitTool().run({"subcommand": "fetch", "args": [flag, "origin"]}, ctx)
    assert not marker.exists()
