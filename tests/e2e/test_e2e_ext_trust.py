"""
E2E: folder trust against the real CLI. A project hook runs under -p as before; with
trust.requireForNonInteractive it does not; the REPL lists the commands and asks first.
"""
from __future__ import annotations

import json

import pytest

from harness import IS_WINDOWS, AxonEnv
from test_e2e_a_common import no_traceback, py_hook, run_tools, write_settings

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(IS_WINDOWS, reason="POSIX hooks and pty")]

STAMP = "open(sys.argv[1], 'a').write('ran\\n')\njson.load(sys.stdin)\n"


def _project_hook(env: AxonEnv):
    log = env.root / "ran.txt"
    h = py_hook(env, "stamp.py", STAMP, str(log))
    write_settings(env, {"hooks": {"SessionStart": [{"hooks": [h]}]}})
    return log, h["command"]


def test_headless_runs_project_hooks_by_default(axon_env: AxonEnv) -> None:
    log, _ = _project_hook(axon_env)
    r, _ev = run_tools(axon_env, [])
    assert r.code == 0, str(r)
    no_traceback(r)
    assert log.exists()


def test_headless_refuses_when_the_user_requires_trust(axon_env: AxonEnv) -> None:
    log, _ = _project_hook(axon_env)
    (axon_env.axon_home / "settings.json").write_text(json.dumps({"trust": {"requireForNonInteractive": True}}))
    r, _ev = run_tools(axon_env, [])
    assert r.code == 0, str(r)
    assert not log.exists()
    assert "not trusted" in r.stderr or "not loaded" in r.stderr


def test_repl_lists_the_command_and_asks_before_running_it(axon_env: AxonEnv) -> None:
    log, cmd = _project_hook(axon_env)
    s = axon_env.pty([])
    s.expect(r"Trust this folder", timeout=25)
    assert "stamp.py" in s.screen                      # the command is listed in full
    assert not log.exists()
    s.sendline("n")
    s.expect(r"(?i)(ask anything|❯)", timeout=25)
    s.drain(0.5)
    assert not log.exists()                            # declined: nothing ran, Axon still works
    s.sendline("/exit")
    s.wait(10)
    s2 = axon_env.pty([])
    s2.expect(r"Trust this folder", timeout=25)
    s2.sendline("y")
    s2.expect(r"(?i)(ask anything|❯)", timeout=25)
    s2.drain(1.0)
    assert log.exists()
    s2.sendline("/exit")
    s2.wait(10)
    s3 = axon_env.pty([])                              # remembered
    s3.expect(r"(?i)(ask anything|❯)", timeout=25)
    assert "Trust this folder" not in s3.screen
    s3.sendline("/exit")
    s3.wait(10)
