"""The filesystem root and the home folder itself are refused as a workspace, in the app and in /cd."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard import c, env, llm, server  # noqa: E402,F401

from axon.permissions.paths import too_broad_for_workspace  # noqa: E402


def test_helper_accepts_a_project_inside_home_and_refuses_the_two():
    assert too_broad_for_workspace("/") and "whole disk" in too_broad_for_workspace("/")
    assert "home folder" in too_broad_for_workspace(Path.home())
    sub = Path.home() / "proj"
    sub.mkdir()
    assert too_broad_for_workspace(sub) is None


def test_app_refuses_root_and_home_with_a_plain_message(c, env):
    for target, word in (("/", "whole disk"), (str(Path.home()), "home folder"), ("~", "home folder")):
        st, _, body = c.post("/api/ws/set", {"path": target})
        assert st == 400, (target, st, body)
        msg = json.loads(body)["error"]
        assert word in msg and "refused" not in msg.lower()
    assert c.post("/api/ws/set", {"path": str(env.ws)})[0] == 200


def test_cd_refuses_root_and_home(tmp_path, capsys):
    from axon.commands.workspace_cmds import handle_cd
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path), permissions=None)
    for target in ("/", str(Path.home())):
        handle_cd(agent, target)
        out = capsys.readouterr().out
        assert "too broad" in out
    assert Path.cwd() != Path("/")
