"""Sample data for the dashboard tests and the manual browser run (no tests in this module)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SECRET_KEY = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
BEARER = "Bearer abcdef0123456789abcdef0123456789"
XSS_USER = "<script>alert('xss-user')</script> what does this do?"
XSS_TOOL = "</pre><script>alert('xss-tool')</script><img src=x onerror=alert(2)>"


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _session(root: Path, sid: str, workspace: Path, turns: list[dict[str, Any]], *, age_h: float = 1,
             title: str | None = None, tag: str | None = None, star: bool = False) -> Path:
    t0 = time.time() - age_h * 3600
    recs: list[dict[str, Any]] = [{"type": "session_meta", "timestamp": t0, "data": {"workspace": str(workspace), "version": 1}}]
    for n, turn in enumerate(turns):
        ts = t0 + n * 7
        recs.append({"type": "user_message", "timestamp": ts, "data": {"role": "user", "content": turn["user"]}})
        tool_uses = turn.get("tools", [])
        recs.append({"type": "assistant_turn", "timestamp": ts + 2, "data": {
            "stop_reason": "tool_use" if tool_uses else "end_turn", "text": turn.get("text", ""), "thinking": turn.get("thinking"),
            "tool_uses": [{"id": f"tu_{sid[-4:]}_{n}_{i}", "name": t["name"], "input": t["input"]} for i, t in enumerate(tool_uses)],
            "native": None, "model": turn.get("model", "claude-sonnet-4-5"), "cost": turn.get("cost", 0.0123),
            "duration_s": 3.2, "usage": {"input": 12000, "output": 640, "cache_read": 9000, "cache_write": 1200, "reasoning": 0}}})
        if tool_uses:
            recs.append({"type": "tool_results", "timestamp": ts + 4, "data": [
                {"tool_use_id": f"tu_{sid[-4:]}_{n}_{i}", "content": t["result"], "is_error": t.get("error", False)}
                for i, t in enumerate(tool_uses)]})
            recs.append({"type": "assistant_turn", "timestamp": ts + 6, "data": {
                "stop_reason": "end_turn", "text": turn.get("after", "Done."), "tool_uses": [], "native": None,
                "model": turn.get("model", "claude-sonnet-4-5"), "cost": 0.004, "duration_s": 1.1,
                "usage": {"input": 13000, "output": 120, "cache_read": 12000, "cache_write": 0, "reasoning": 0}}})
            for i, t in enumerate(tool_uses):
                if t.get("denied"):
                    recs.append({"type": "permission", "timestamp": ts + 3, "data": {
                        "tool_use_id": f"tu_{sid[-4:]}_{n}_{i}", "tool": t["name"], "outcome": "deny", "reason": "needs approval in non-interactive mode"}})
    if title:
        recs.append({"type": "session_rename", "timestamp": t0 + 99, "data": {"title": title}})
    if tag:
        recs.append({"type": "session_tag", "timestamp": t0 + 100, "data": {"tag": tag}})
    if star:
        recs.append({"type": "session_star", "timestamp": t0 + 101, "data": {"starred": True}})
    path = root / sid / "session.jsonl"
    _write(path, recs)
    import os
    os.utime(path, (t0 + 200, t0 + 200))
    return path


def build_sample(axon_home: Path, workspace: Path, other: Path | None = None) -> dict[str, Any]:
    """Create sessions, usage and traces under ``axon_home`` for ``workspace`` (and one other project)."""
    from axon import storage
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "app").mkdir(parents=True, exist_ok=True)
    (workspace / "docs").mkdir(exist_ok=True)
    (workspace / "src" / "app" / "auth.py").write_text("import os\n\n\ndef login(user):\n    # validate the user\n    return True  # TODO: real check\n\n\nclass Session:\n    ttl = 3600\n", encoding="utf-8")
    (workspace / "src" / "app" / "main.js").write_text("// entry\nconst port = 8080;\nfunction start() { return `listening on ${port}`; }\n", encoding="utf-8")
    (workspace / "README.md").write_text("# Sample project\n\nA **sample** with `code`.\n\n- one\n- two\n", encoding="utf-8")
    (workspace / "docs" / "data.csv").write_text("name,qty\napple,3\npear,5\n", encoding="utf-8")
    (workspace / "docs" / "dot.png").write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"))
    (workspace / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
    (workspace / "ignored").mkdir(exist_ok=True)
    (workspace / "ignored" / "x.txt").write_text("skip me", encoding="utf-8")
    (workspace / "build.log").write_text("log", encoding="utf-8")
    sdir = storage.sessions_dir(workspace)
    ids = {
        "refactor": "session_20261101_120000_proj_aaaa01",
        "xss": "session_20261102_120000_proj_bbbb02",
        "huge": "session_20261103_120000_proj_cccc03",
        "short": "session_20261104_120000_proj_dddd04",
    }
    diff_tool = {"name": "Edit", "input": {"file_path": "src/auth.py", "old_string": "def login(user):\n    return True\n",
                                              "new_string": "def login(user):\n    if not user:\n        raise ValueError('no user')\n    return True\n"},
                 "result": "Edited src/auth.py"}
    _session(sdir, ids["refactor"], workspace, [
        {"user": "Refactor the auth module so login validates its input", "text": "I'll **tighten** `login` and add a test.\n\n```python\ndef login(user):\n    ...\n```\n\n- validates input\n- raises on empty\n\n| a | b |\n|---|---|\n| 1 | 2 |",
         "tools": [diff_tool, {"name": "Bash", "input": {"command": "pytest -q tests/test_auth.py"}, "result": "2 passed in 0.12s"},
                   {"name": "Write", "input": {"file_path": "x.py", "content": "print(1)"}, "result": "denied", "error": True, "denied": True}],
         "after": "Login now validates its input; tests pass."},
        {"user": "Thanks. Can you also update the docs?", "text": "Sure, updating `docs/auth.md`."},
    ], age_h=2, title="Refactor the auth module", tag="auth", star=True)
    _session(sdir, ids["xss"], workspace, [
        {"user": XSS_USER, "text": f"Output: <img src=x onerror=alert(1)> and [click](javascript:alert(3)) [ok](https://example.com/a?b=1&c=2)\nkey {SECRET_KEY}\nAuthorization: {BEARER}\n\nOPENAI_API_KEY=sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa\nDB_HOST=prod-db.internal.example.com\nDEBUG=true",
         "tools": [{"name": "Bash", "input": {"command": "cat .env"}, "result": XSS_TOOL + "\nANTHROPIC_API_KEY=" + SECRET_KEY}]},
    ], age_h=30, title="Weird transcript")
    huge = "\n".join(f"line {i}: lorem ipsum dolor sit amet" for i in range(100_000))
    _session(sdir, ids["huge"], workspace, [
        {"user": "Here is a giant log:\n" + huge, "text": "That was a lot of lines.", "tools": [
            {"name": "Bash", "input": {"command": "cat big.log"}, "result": huge}]},
    ], age_h=50, title="Giant log paste")
    _session(sdir, ids["short"], workspace, [{"user": "hi", "text": "Hello! How can I help?"}], age_h=80)
    for k in range(12):
        _session(sdir, f"session_20261001_1200{k:02d}_proj_f{k:03d}", workspace,
                 [{"user": f"Investigate flaky test number {k}", "text": "Looking."}], age_h=100 + k * 9,
                 tag=("flaky" if k % 3 == 0 else None))
    if other is not None:
        other.mkdir(parents=True, exist_ok=True)
        _session(storage.sessions_dir(other), "session_20261105_120000_other_eeee05", other,
                 [{"user": "Set up CI for the other project", "text": "Done.", "model": "gpt-4o"}], age_h=5)
    # usage + traces
    now = datetime.now(timezone.utc)
    usage = []
    for d in range(14):
        for j, (model, src) in enumerate([("claude-sonnet-4-5", "main"), ("claude-sonnet-4-5", "subagent_1"), ("gpt-4o", "main")]):
            ts = now - timedelta(days=d, hours=j)
            usage.append({"ts": ts.isoformat(), "project": storage.project_key(workspace), "workspace": str(workspace),
                          "session": ids["refactor"] if d == 0 else f"session_old_{d}", "model": model, "source": src,
                          "usage": {"input": 40000 + d * 900, "output": 2500 + d * 40, "cache_read": 30000, "cache_write": 3000},
                          "cost": round(0.05 + 0.01 * ((d * 7 + j * 3) % 9), 5), "duration_s": 2.0 + j})
    _write(storage.user_file("usage.jsonl"), usage)
    tr = []
    for n in range(3):
        tr.append({"turn": n + 1, "prompt": f"turn {n}", "started_at": time.time() - 3600 + n * 60, "ended_at": time.time() - 3500 + n * 60,
                   "cost": 0.03, "llm_calls": 2, "tool_calls": 2, "input_tokens": 20000, "output_tokens": 800, "model": "claude-sonnet-4-5",
                   "steps": [{"kind": "tool", "data": {"name": "Bash", "duration_s": 1.5, "is_error": n == 2}},
                             {"kind": "tool", "data": {"name": "Edit", "duration_s": 0.2}}]})
    _write(sdir / ids["refactor"] / "trace.jsonl", tr)
    return ids
