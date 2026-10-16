"""E2E-B: session storage layout, transcript content, resume / continue / fork / name, and the
session slash commands (/rename /branch /clear /export /resume), driven through the real CLI."""
from __future__ import annotations

import json

import pytest

from harness import IS_WINDOWS, AxonEnv
from test_e2e_b_util import (
    has_tool_result, no_crash, project_dir, records, repl_cmd, run_json, session_files, start_repl,
    text_in_request, parallel, pbatch,  # noqa: F401 (pbatch is a fixture)
)

pytestmark = pytest.mark.e2e


def _tool_turn(env: AxonEnv, prompt: str = "make a file", *extra: str) -> dict:
    env.llm.enqueue(
        [{"text": "Writing."}, {"tool": "Write", "args": {"path": "made.txt", "content": "v1\n"}},
         {"usage": {"input": 900, "output": 40, "cached": 100}}],
        {"steps": [{"text": "File made."}], "expect": [has_tool_result()]},
    )
    return run_json(env, *extra, prompt)


# ─── storage layout ─────────────────────────────────────────────────────────

@parallel
def test_storage_layout_sessions_traces_checkpoints(axon_env: AxonEnv) -> None:
    """A turn that edits a file creates sessions/, traces/ and checkpoints/<sid>/ under the project dir."""
    data = _tool_turn(axon_env)
    sid = data["session_id"]
    pdir = project_dir(axon_env)
    assert (pdir / "sessions" / f"{sid}.jsonl").is_file()
    assert (pdir / "traces").is_dir() and list((pdir / "traces").glob(f"{sid}*")), list(pdir.rglob("*"))
    assert (pdir / "checkpoints").is_dir(), list(pdir.rglob("*"))
    meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    assert meta["workspace"] == str(axon_env.workspace.resolve())


@parallel
def test_checkpoint_snapshot_written_after_edit_of_existing_file(axon_env: AxonEnv) -> None:
    """Editing an existing file stores its original content in the session's checkpoint dir."""
    (axon_env.workspace / "made.txt").write_text("ORIGINAL-CONTENT\n", encoding="utf-8")
    axon_env.llm.enqueue([{"tool": "Read", "args": {"path": "made.txt"}}])  # read-before-write guard
    data = _tool_turn(axon_env)
    assert (axon_env.workspace / "made.txt").read_text(encoding="utf-8") == "v1\n"
    cdir = project_dir(axon_env) / "checkpoints" / data["session_id"]
    assert cdir.is_dir(), list(project_dir(axon_env).rglob("*"))
    blobs = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in cdir.rglob("*") if p.is_file())
    assert "ORIGINAL-CONTENT" in blobs


@parallel
def test_transcript_first_record_is_session_meta(axon_env: AxonEnv) -> None:
    """Every transcript starts with a session_meta record naming the workspace."""
    axon_env.llm.enqueue([{"text": "hi"}])
    sid = run_json(axon_env, "hello")["session_id"]
    recs = records(axon_env.sessions_dir() / f"{sid}.jsonl")
    assert recs[0]["type"] == "session_meta"
    assert recs[0]["data"]["workspace"] == str(axon_env.workspace)
    assert all("timestamp" in r for r in recs)


@parallel
def test_transcript_contains_input_output_tools_usage_cost(axon_env: AxonEnv) -> None:
    """The transcript stores the prompt, assistant text, tool inputs, tool results, usage and cost."""
    data = _tool_turn(axon_env, "PROMPT-MARKER-123 please")
    recs = records(axon_env.sessions_dir() / f"{data['session_id']}.jsonl")
    kinds = [r["type"] for r in recs]
    assert "user_message" in kinds and "assistant_turn" in kinds and "tool_results" in kinds, kinds
    user = next(r for r in recs if r["type"] == "user_message")
    assert "PROMPT-MARKER-123" in user["data"]["content"]
    turns = [r["data"] for r in recs if r["type"] == "assistant_turn"]
    assert turns[0]["tool_uses"][0]["name"] == "Write"
    assert turns[0]["tool_uses"][0]["input"]["content"] == "v1\n"
    assert turns[0]["usage"]["input"] >= 900 and turns[0]["usage"]["output"] == 40
    assert turns[0]["model"] == "gpt-4o"
    assert isinstance(turns[0]["cost"], float) and turns[0]["cost"] > 0
    assert any("File made." in (t.get("text") or "") for t in turns)
    res = next(r for r in recs if r["type"] == "tool_results")["data"]
    assert res[0]["tool_use_id"] == turns[0]["tool_uses"][0]["id"]
    assert "is_error" in res[0]


@parallel
def test_transcript_records_permission_decisions(axon_env: AxonEnv) -> None:
    """Permission outcomes for tool calls are persisted as `permission` records."""
    data = _tool_turn(axon_env, "write it", "--mode", "acceptEdits")
    recs = records(axon_env.sessions_dir() / f"{data['session_id']}.jsonl")
    perms = [r["data"] for r in recs if r["type"] == "permission"]
    assert perms, [r["type"] for r in recs]
    assert perms[0]["tool"] == "Write" and perms[0]["outcome"]


@parallel
def test_transcript_records_denied_permission_in_plan_mode(axon_env: AxonEnv) -> None:
    """A tool blocked by plan mode is still recorded with a non-allow outcome."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "x.txt", "content": "no"}}], [{"text": "ok"}])
    data = run_json(axon_env, "--mode", "plan", "write x")
    recs = records(axon_env.sessions_dir() / f"{data['session_id']}.jsonl")
    perms = [r["data"] for r in recs if r["type"] == "permission"]
    assert perms and perms[0]["outcome"] != "allow", perms
    res = [r["data"] for r in recs if r["type"] == "tool_results"]
    assert res and res[0][0]["is_error"] is True


@parallel
def test_usage_jsonl_logged_per_call(axon_env: AxonEnv) -> None:
    """Every LLM call is appended to AXON_HOME/usage.jsonl with tokens and cost."""
    _tool_turn(axon_env)
    path = axon_env.axon_home / "usage.jsonl"
    assert path.is_file()
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) >= 2
    blob = json.dumps(rows)
    assert "gpt-4o" in blob


@parallel
def test_auto_title_from_first_prompt(axon_env: AxonEnv) -> None:
    """Without -n, the session is auto-titled from the first prompt (session_rename record)."""
    axon_env.llm.enqueue([{"text": "sure"}])
    sid = run_json(axon_env, "Refactor the payment module. Then add tests.")["session_id"]
    recs = records(axon_env.sessions_dir() / f"{sid}.jsonl")
    titles = [r["data"]["title"] for r in recs if r["type"] == "session_rename"]
    assert titles, [r["type"] for r in recs]
    assert "payment" in titles[-1].lower()


@parallel
def test_name_flag_sets_title_and_skips_auto_title(axon_env: AxonEnv) -> None:
    """`-n NAME` records the name; the auto title does not overwrite it."""
    axon_env.llm.enqueue([{"text": "sure"}])
    sid = run_json(axon_env, "-n", "my-named-session", "Some unrelated first prompt")["session_id"]
    recs = records(axon_env.sessions_dir() / f"{sid}.jsonl")
    titles = [r["data"]["title"] for r in recs if r["type"] == "session_rename"]
    assert titles and titles[-1] == "my-named-session", titles


# ─── continue / resume / fork ───────────────────────────────────────────────

@parallel
def test_continue_without_sessions_fails_cleanly(axon_env: AxonEnv) -> None:
    """--continue with no prior session exits 1 with a clear message."""
    r = axon_env.run(["-p", "--continue", "hi"])
    assert r.code == 1, str(r)
    assert "No conversation found" in r.output
    no_crash(r)


@parallel
def test_continue_reuses_same_session_id(axon_env: AxonEnv) -> None:
    """--continue appends to the latest transcript instead of creating a new one."""
    axon_env.llm.enqueue([{"text": "one"}], [{"text": "two"}])
    sid1 = run_json(axon_env, "first")["session_id"]
    sid2 = run_json(axon_env, "--continue", "second")["session_id"]
    assert sid1 == sid2
    assert len(session_files(axon_env)) == 1


@parallel
def test_resume_by_id(axon_env: AxonEnv) -> None:
    """--resume <id> loads that session's history into the next request."""
    axon_env.llm.enqueue([{"text": "Codeword ALBATROSS."}])
    sid = run_json(axon_env, "remember")["session_id"]
    axon_env.llm.enqueue([{"text": "other"}])
    run_json(axon_env, "unrelated new session")
    axon_env.llm.enqueue({"steps": [{"text": "ALBATROSS it was"}], "expect": [text_in_request("ALBATROSS")]})
    data = run_json(axon_env, "--resume", sid, "what was it?")
    assert data["session_id"] == sid
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_resume_by_unique_id_prefix(axon_env: AxonEnv) -> None:
    """--resume accepts a unique prefix of a session id."""
    axon_env.llm.enqueue([{"text": "x"}])
    sid = run_json(axon_env, "hello")["session_id"]
    axon_env.llm.enqueue([{"text": "y"}])
    assert run_json(axon_env, "--resume", sid[:-3], "again")["session_id"] == sid


@parallel
def test_resume_by_name(axon_env: AxonEnv) -> None:
    """--resume <name> finds a session that was named with -n."""
    axon_env.llm.enqueue([{"text": "Codeword HERON."}])
    sid = run_json(axon_env, "-n", "heron-work", "remember")["session_id"]
    axon_env.llm.enqueue([{"text": "z"}])
    run_json(axon_env, "another")
    axon_env.llm.enqueue({"steps": [{"text": "HERON"}], "expect": [text_in_request("HERON")]})
    data = run_json(axon_env, "--resume", "heron-work", "which?")
    assert data["session_id"] == sid, data
    assert not axon_env.llm.failures


@parallel
def test_resume_unknown_id_errors(axon_env: AxonEnv) -> None:
    """--resume with an unknown id exits 1 with 'No conversation found'."""
    axon_env.llm.enqueue([{"text": "x"}])
    run_json(axon_env, "hello")
    r = axon_env.run(["-p", "--resume", "does-not-exist-zzz", "hi"])
    assert r.code == 1 and "No conversation found" in r.output, str(r)
    no_crash(r)


@parallel
def test_resume_without_id_needs_tty(axon_env: AxonEnv) -> None:
    """--resume with no id in a non-interactive run explains that a terminal is required."""
    r = axon_env.run(["-p", "--resume", "--", "hi"])
    no_crash(r)
    assert r.code != 0 or "interactive" in r.output, str(r)


@parallel
def test_fork_session_creates_new_id_with_history(axon_env: AxonEnv) -> None:
    """--continue --fork-session copies history into a new session; the original is unchanged."""
    axon_env.llm.enqueue([{"text": "Codeword OSPREY."}])
    sid = run_json(axon_env, "remember")["session_id"]
    before = (axon_env.sessions_dir() / f"{sid}.jsonl").read_text(encoding="utf-8")
    axon_env.llm.enqueue({"steps": [{"text": "OSPREY"}], "expect": [text_in_request("OSPREY")]})
    data = run_json(axon_env, "--continue", "--fork-session", "which?")
    assert data["session_id"] != sid
    assert not axon_env.llm.failures
    assert (axon_env.sessions_dir() / f"{sid}.jsonl").read_text(encoding="utf-8") == before
    forked = (axon_env.sessions_dir() / f"{data['session_id']}.jsonl").read_text(encoding="utf-8")
    assert "OSPREY" in forked and "which?" in forked


@parallel
def test_resume_restores_cost_ledger(axon_env: AxonEnv) -> None:
    """After --continue, /cost shows the earlier session's API calls."""
    _tool_turn(axon_env)
    r = axon_env.run(["-p", "--continue", "/cost"])
    no_crash(r)
    assert "No API calls yet" not in r.output, str(r)
    assert "$" in r.output


@parallel
def test_continue_after_tool_turn_sends_valid_history(axon_env: AxonEnv) -> None:
    """Resuming a session that contains tool calls replays tool_calls + tool results in order."""
    _tool_turn(axon_env)

    def valid(req):
        msgs = [m for m in req.messages if m.get("role") != "system"]
        roles = [m["role"] for m in msgs]
        if "tool" not in roles:
            return f"no tool message in resumed history: {roles}"
        i = roles.index("tool")
        return bool(msgs[i - 1].get("tool_calls")) or f"tool message not preceded by tool_calls: {roles}"
    axon_env.llm.enqueue({"steps": [{"text": "fine"}], "expect": [valid]})
    run_json(axon_env, "--continue", "next")
    assert not axon_env.llm.failures, axon_env.llm.failures


# ─── session slash commands (print mode on a resumed session) ───────────────

@parallel
def test_rename_command_persists_title(axon_env: AxonEnv) -> None:
    """`/rename NEW` on a resumed session appends a session_rename record and is findable by name."""
    axon_env.llm.enqueue([{"text": "x"}])
    sid = run_json(axon_env, "hello")["session_id"]
    r = axon_env.run(["-p", "--continue", "/rename Shiny Title"])
    no_crash(r)
    assert "Shiny Title" in r.output
    recs = records(axon_env.sessions_dir() / f"{sid}.jsonl")
    assert [x["data"]["title"] for x in recs if x["type"] == "session_rename"][-1] == "Shiny Title"
    axon_env.llm.enqueue([{"text": "y"}])
    assert run_json(axon_env, "--resume", "Shiny Title", "again")["session_id"] == sid


@parallel
def test_rename_without_title_generates_one(axon_env: AxonEnv) -> None:
    """`/rename` with no argument asks the model for a title and saves it."""
    axon_env.llm.enqueue([{"text": "x"}])
    sid = run_json(axon_env, "-n", "placeholder", "build a rocket")["session_id"]
    axon_env.llm.enqueue([{"text": "Rocket Building Session"}])
    r = axon_env.run(["-p", "--continue", "/rename"])
    no_crash(r)
    recs = records(axon_env.sessions_dir() / f"{sid}.jsonl")
    assert [x["data"]["title"] for x in recs if x["type"] == "session_rename"][-1] == "Rocket Building Session"


@parallel
def test_branch_command_creates_named_branch_session(axon_env: AxonEnv) -> None:
    """`/branch exp` copies the conversation into <sid>_branch_exp and names it."""
    axon_env.llm.enqueue([{"text": "Codeword KESTREL."}])
    sid = run_json(axon_env, "remember")["session_id"]
    r = axon_env.run(["-p", "--continue", "/branch exp"])
    no_crash(r)
    branch = axon_env.sessions_dir() / f"{sid}_branch_exp.jsonl"
    assert branch.is_file(), [p.name for p in session_files(axon_env)]
    text = branch.read_text(encoding="utf-8")
    assert "KESTREL" in text and "remember" in text


@parallel
def test_branch_of_tool_session_is_resumable(axon_env: AxonEnv) -> None:
    """A branch made from a session with tool calls can be resumed and produces a valid request."""
    data = _tool_turn(axon_env)
    sid = data["session_id"]
    no_crash(axon_env.run(["-p", "--continue", "/branch b1"]))

    def valid(req):
        msgs = [m for m in req.messages if m.get("role") != "system"]
        roles = [m["role"] for m in msgs]
        for i, m in enumerate(msgs):
            if m["role"] == "tool" and not (msgs[i - 1].get("tool_calls") or msgs[i - 1]["role"] == "tool"):
                return f"orphan tool message: {roles}"
            if m["role"] == "tool" and not m.get("tool_call_id"):
                return f"tool message without tool_call_id: {m}"
        return "tool" in roles or f"tool results lost in branch: {roles}"
    axon_env.llm.enqueue({"steps": [{"text": "ok"}], "expect": [valid]})
    run_json(axon_env, "--resume", f"{sid}_branch_b1", "continue")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_clear_starts_new_session_and_keeps_old(axon_env: AxonEnv) -> None:
    """`/clear` on a resumed session switches to a fresh session id; the old transcript stays."""
    axon_env.llm.enqueue([{"text": "x"}])
    sid = run_json(axon_env, "hello")["session_id"]
    r = axon_env.run(["-p", "--continue", "/clear"])
    no_crash(r)
    assert "New session" in r.output
    assert (axon_env.sessions_dir() / f"{sid}.jsonl").is_file()


def _export(env: AxonEnv, fmt: str, needle: str) -> str:
    env.llm.enqueue([{"text": "EXPORTED-ANSWER"}])
    run_json(env, "export me")
    out = env.root / f"out.{fmt}"
    r = env.run(["-p", "--continue", f"/export {fmt} {out}"])
    no_crash(r)
    assert out.is_file(), str(r)
    text = out.read_text(encoding="utf-8")
    assert needle in text and "EXPORTED-ANSWER" in text and "export me" in text
    return text


@parallel
def test_export_markdown_to_file(axon_env: AxonEnv) -> None:
    """`/export md <path>` writes a Markdown transcript with role headings."""
    _export(axon_env, "md", "## User")


@parallel
def test_export_json_to_file(axon_env: AxonEnv) -> None:
    """`/export json <path>` writes a JSON document with model, cost and messages."""
    doc = json.loads(_export(axon_env, "json", '"messages"'))
    assert doc["model"] == "gpt-4o" and len(doc["messages"]) >= 2 and "cost_usd" in doc


@parallel
def test_export_txt_to_file(axon_env: AxonEnv) -> None:
    """`/export txt <path>` writes a plain-text transcript ("> prompt", "< answer")."""
    _export(axon_env, "txt", "> export me")


@parallel
def test_export_infers_format_from_extension(axon_env: AxonEnv) -> None:
    """`/export path.json` picks JSON from the extension."""
    axon_env.llm.enqueue([{"text": "A"}])
    run_json(axon_env, "q")
    r = axon_env.run(["-p", "--continue", "/export conv.json"])
    no_crash(r)
    doc = json.loads((axon_env.workspace / "conv.json").read_text(encoding="utf-8"))
    assert doc["messages"]


@parallel
def test_export_default_path_under_axon_home(axon_env: AxonEnv) -> None:
    """`/export md` with no path writes AXON_HOME/projects/<key>/exports/<sid>.md."""
    axon_env.llm.enqueue([{"text": "A"}])
    sid = run_json(axon_env, "q")["session_id"]
    no_crash(axon_env.run(["-p", "--continue", "/export md"]))
    assert list((axon_env.axon_home / "projects").glob(f"*/exports/{sid}.md"))


@parallel
def test_export_md_includes_tool_calls(axon_env: AxonEnv) -> None:
    """Markdown export shows tool calls with their JSON input."""
    _tool_turn(axon_env)
    out = axon_env.root / "t.md"
    no_crash(axon_env.run(["-p", "--continue", f"/export md {out}"]))
    text = out.read_text(encoding="utf-8")
    assert "Write" in text and "made.txt" in text


@parallel
def test_resume_slash_command_by_id(axon_env: AxonEnv) -> None:
    """`/resume <id>` in print mode loads the session without crashing."""
    axon_env.llm.enqueue([{"text": "x"}])
    sid = run_json(axon_env, "hello there")["session_id"]
    r = axon_env.run(["-p", f"/resume {sid}"])
    no_crash(r)
    assert "not found" not in r.output.lower(), str(r)


@parallel
def test_resume_slash_command_unknown(axon_env: AxonEnv) -> None:
    """`/resume nope` reports the session was not found."""
    axon_env.llm.enqueue([{"text": "x"}])
    run_json(axon_env, "hello")
    r = axon_env.run(["-p", "/resume nope-nope"])
    no_crash(r)
    assert "not found" in r.output.lower()


@parallel
def test_sessions_list_shows_saved_session(axon_env: AxonEnv) -> None:
    """`/sessions` lists previously saved sessions."""
    axon_env.llm.enqueue([{"text": "x"}])
    sid = run_json(axon_env, "-n", "listed-one", "hello")["session_id"]
    r = axon_env.run(["-p", "/sessions"])
    no_crash(r)
    assert sid in r.output or "listed-one" in r.output, str(r)


@parallel
def test_tag_and_star_records(axon_env: AxonEnv) -> None:
    """`/tag` and `/star` write session_tag / session_star records."""
    axon_env.llm.enqueue([{"text": "x"}])
    sid = run_json(axon_env, "hello")["session_id"]
    no_crash(axon_env.run(["-p", "--continue", "/tag important"]))
    no_crash(axon_env.run(["-p", "--continue", "/star"]))
    kinds = [r["type"] for r in records(axon_env.sessions_dir() / f"{sid}.jsonl")]
    assert "session_tag" in kinds and "session_star" in kinds


@parallel
def test_history_lists_messages(axon_env: AxonEnv) -> None:
    """`/history` on a resumed session lists the stored messages."""
    axon_env.llm.enqueue([{"text": "HIST-ANSWER"}])
    run_json(axon_env, "HIST-QUESTION")
    r = axon_env.run(["-p", "--continue", "/history"])
    no_crash(r)
    assert "HIST-QUESTION" in r.output and "HIST-ANSWER" in r.output


# ─── REPL flows ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")
def test_repl_clear_then_resume_previous(axon_env: AxonEnv) -> None:
    """In the REPL: a turn, /clear, then /resume <old id> brings the old conversation back."""
    axon_env.llm.enqueue([{"text": "REPL-FIRST-ANSWER"}])
    with start_repl(axon_env) as s:
        repl_cmd(s, "first question", "REPL-FIRST-ANSWER", 20)
        sid = session_files(axon_env)[0].stem
        repl_cmd(s, "/clear", "New session")
        out = repl_cmd(s, f"/resume {sid}", r"REPL-FIRST-ANSWER|first question")
        assert "Traceback" not in out
        axon_env.llm.enqueue({"steps": [{"text": "SECOND"}], "expect": [text_in_request("REPL-FIRST-ANSWER")]})
        repl_cmd(s, "follow-up", "SECOND", 20)
        s.sendline("/exit")
        s.wait(10)
    assert not axon_env.llm.failures, axon_env.llm.failures


@pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")
def test_repl_rename_and_branch(axon_env: AxonEnv) -> None:
    """In the REPL: /rename then /branch create the expected records and files."""
    axon_env.llm.enqueue([{"text": "ok"}])
    with start_repl(axon_env) as s:
        repl_cmd(s, "hello", "ok", 20)
        repl_cmd(s, "/rename Repl Named", "Renamed")
        repl_cmd(s, "/branch side", "Branched")
        s.sendline("/exit")
        s.wait(10)
    names = [p.name for p in session_files(axon_env)]
    assert any(n.endswith("_branch_side.jsonl") for n in names), names
