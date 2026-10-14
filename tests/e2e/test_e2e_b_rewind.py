"""E2E-B: checkpoints and /rewind through the real CLI.

Each scenario builds a two-prompt session in separate ``axon -p`` processes (prompt 1 creates
f.txt = v1, prompt 2 reads it and overwrites it with v2), then rewinds with
``axon -p --continue "/rewind ..."`` and checks the files on disk and the conversation that the
next ``--continue`` request sends to the model."""
from __future__ import annotations

import pytest

from harness import IS_WINDOWS, AxonEnv
from test_e2e_b_util import (
    has_tool_result, no_crash, project_dir, records, repl_cmd, run_json, start_repl, text_in_request,
    text_not_in_request, parallel, pbatch,  # noqa: F401 (pbatch is a fixture)
)

pytestmark = pytest.mark.e2e


def _two_prompts(env: AxonEnv) -> str:
    env.llm.enqueue([{"tool": "Write", "args": {"path": "f.txt", "content": "v1\n"}}],
                    {"steps": [{"text": "FIRST-DONE"}], "expect": [has_tool_result()]})
    sid = run_json(env, "PROMPT-ONE create f")["session_id"]
    env.llm.enqueue([{"tool": "Read", "args": {"path": "f.txt"}}],
                    [{"tool": "Write", "args": {"path": "f.txt", "content": "v2\n"}}],
                    [{"text": "SECOND-DONE"}])
    assert run_json(env, "--continue", "PROMPT-TWO update f")["session_id"] == sid
    assert (env.workspace / "f.txt").read_text(encoding="utf-8") == "v2\n"
    return sid


def _rewind(env: AxonEnv, arg: str) -> str:
    r = env.run(["-p", "--continue", f"/rewind {arg}"])
    no_crash(r)
    assert r.code == 0, str(r)
    return r.output


def _f(env: AxonEnv) -> str | None:
    p = env.workspace / "f.txt"
    return p.read_text(encoding="utf-8") if p.exists() else None


@parallel
def test_checkpoints_persisted_per_turn(axon_env: AxonEnv) -> None:
    """Each prompt that edits files persists one numbered snapshot with a manifest."""
    sid = _two_prompts(axon_env)
    cdir = project_dir(axon_env) / "checkpoints" / sid
    snaps = sorted(p for p in cdir.iterdir() if p.is_dir())
    assert len(snaps) == 2, list(cdir.rglob("*"))
    assert all((p / "manifest.json").is_file() for p in snaps)


@parallel
def test_rewind_points_lists_both_prompts(axon_env: AxonEnv) -> None:
    """/rewind points lists every prompt with its file-edit count."""
    _two_prompts(axon_env)
    out = _rewind(axon_env, "points")
    assert "PROMPT-ONE" in out and "PROMPT-TWO" in out and "1 file(s)" in out, out


@parallel
def test_rewind_code_only_restores_file_keeps_conversation(axon_env: AxonEnv) -> None:
    """/rewind to 2 code restores f.txt to v1; the conversation still contains prompt 2."""
    _two_prompts(axon_env)
    out = _rewind(axon_env, "to 2 code")
    assert "Restored f.txt" in out, out
    assert _f(axon_env) == "v1\n"
    axon_env.llm.enqueue({"steps": [{"text": "k"}], "expect": [text_in_request("PROMPT-TWO")]})
    run_json(axon_env, "--continue", "next")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_rewind_code_to_first_prompt_deletes_created_file(axon_env: AxonEnv) -> None:
    """/rewind to 1 code undoes both prompts: f.txt (created by prompt 1) is deleted."""
    _two_prompts(axon_env)
    out = _rewind(axon_env, "to 1 code")
    assert "Deleted newly created f.txt" in out, out
    assert _f(axon_env) is None


@parallel
def test_rewind_conversation_only_keeps_files(axon_env: AxonEnv) -> None:
    """/rewind to 2 conversation drops prompt 2 from history but leaves f.txt = v2."""
    _two_prompts(axon_env)
    out = _rewind(axon_env, "to 2 conversation")
    assert "removed from the conversation" in out, out
    assert _f(axon_env) == "v2\n"
    axon_env.llm.enqueue({"steps": [{"text": "k"}],
                          "expect": [text_in_request("PROMPT-ONE"), text_not_in_request("PROMPT-TWO")]})
    run_json(axon_env, "--continue", "next")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_rewind_code_and_conversation(axon_env: AxonEnv) -> None:
    """/rewind to 2 (default mode) restores f.txt to v1 and drops prompt 2 from history."""
    _two_prompts(axon_env)
    _rewind(axon_env, "to 2")
    assert _f(axon_env) == "v1\n"
    axon_env.llm.enqueue({"steps": [{"text": "k"}],
                          "expect": [text_in_request("PROMPT-ONE"), text_not_in_request("PROMPT-TWO")]})
    run_json(axon_env, "--continue", "next")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_rewind_to_first_prompt_empties_conversation(axon_env: AxonEnv) -> None:
    """/rewind to 1 code_and_conversation leaves no prior messages and no f.txt."""
    _two_prompts(axon_env)
    _rewind(axon_env, "to 1 code_and_conversation")
    assert _f(axon_env) is None
    axon_env.llm.enqueue({"steps": [{"text": "k"}], "expect": [text_not_in_request("PROMPT-ONE")]})
    run_json(axon_env, "--continue", "fresh start")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_rewind_summarize_from_here(axon_env: AxonEnv) -> None:
    """/rewind to 2 summarize_from_here replaces prompt 2 onward with an LLM summary; files stay."""
    _two_prompts(axon_env)
    axon_env.llm.enqueue([{"text": "SUMMARY-OF-TAIL: updated f.txt to v2"}])
    out = _rewind(axon_env, "to 2 summarize_from_here")
    assert "SUMMARY-OF-TAIL" in out, out
    assert _f(axon_env) == "v2\n"
    axon_env.llm.enqueue({"steps": [{"text": "k"}],
                          "expect": [text_in_request("SUMMARY-OF-TAIL"), text_not_in_request("PROMPT-TWO update")]})
    run_json(axon_env, "--continue", "next")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_rewind_record_written_to_transcript(axon_env: AxonEnv) -> None:
    """A conversation rewind is persisted as a `rewind` record with mode and prompt index."""
    sid = _two_prompts(axon_env)
    _rewind(axon_env, "to 2 conversation")
    recs = [r for r in records(axon_env.sessions_dir() / f"{sid}.jsonl") if r["type"] == "rewind"]
    assert recs and recs[-1]["data"]["mode"] == "conversation" and recs[-1]["data"]["prompt_index"] == 2


@parallel
def test_rewind_invalid_index(axon_env: AxonEnv) -> None:
    """/rewind to 9 reports there is no such prompt and changes nothing."""
    _two_prompts(axon_env)
    out = _rewind(axon_env, "to 9")
    assert "no prompt #9" in out
    assert _f(axon_env) == "v2\n"


@parallel
def test_rewind_invalid_mode(axon_env: AxonEnv) -> None:
    """/rewind to 1 bogus reports an unknown mode and changes nothing."""
    _two_prompts(axon_env)
    out = _rewind(axon_env, "to 1 bogus")
    assert "unknown rewind mode" in out
    assert _f(axon_env) == "v2\n"


@parallel
def test_rewind_last_n_turns_of_edits(axon_env: AxonEnv) -> None:
    """/rewind 1 undoes the newest snapshot only (f.txt back to v1)."""
    _two_prompts(axon_env)
    out = _rewind(axon_env, "1")
    assert "Rewound 1 turn" in out
    assert _f(axon_env) == "v1\n"
    _rewind(axon_env, "1")
    assert _f(axon_env) is None


@parallel
def test_undo_alias(axon_env: AxonEnv) -> None:
    """/undo is an alias of /rewind."""
    _two_prompts(axon_env)
    no_crash(axon_env.run(["-p", "--continue", "/undo 1"]))
    assert _f(axon_env) == "v1\n"


@parallel
def test_rewind_with_no_edits(axon_env: AxonEnv) -> None:
    """/rewind 1 in a session without edits says there is nothing to rewind."""
    axon_env.llm.enqueue([{"text": "no edits"}])
    run_json(axon_env, "hi")
    assert "No file modifications to rewind" in _rewind(axon_env, "1")


@parallel
def test_diff_since_checkpoint(axon_env: AxonEnv) -> None:
    """/diff T1 shows the file edits made since checkpoint 1."""
    _two_prompts(axon_env)
    r = axon_env.run(["-p", "--continue", "/diff T1"])
    no_crash(r)
    assert "f.txt" in r.output, str(r)


@parallel
def test_edit_tool_checkpoint_and_rewind(axon_env: AxonEnv) -> None:
    """An Edit tool call on an existing file is checkpointed and /rewind 1 restores the original."""
    (axon_env.workspace / "e.py").write_text("x = 1\n", encoding="utf-8")
    axon_env.llm.enqueue([{"tool": "Read", "args": {"path": "e.py"}}],
                         [{"tool": "Edit", "args": {"path": "e.py", "old_string": "x = 1", "new_string": "x = 2"}}],
                         [{"text": "edited"}])
    run_json(axon_env, "edit e.py")
    assert (axon_env.workspace / "e.py").read_text(encoding="utf-8") == "x = 2\n"
    _rewind(axon_env, "1")
    assert (axon_env.workspace / "e.py").read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")
def test_repl_rewind_to_code_mode(axon_env: AxonEnv) -> None:
    """In one REPL session: two prompts, then /rewind to 2 code restores the file in-process."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "f.txt", "content": "v1\n"}}], [{"text": "R-ONE"}],
                         [{"tool": "Read", "args": {"path": "f.txt"}}],
                         [{"tool": "Write", "args": {"path": "f.txt", "content": "v2\n"}}], [{"text": "R-TWO"}])
    with start_repl(axon_env) as s:
        repl_cmd(s, "first", "R-ONE", 20)
        repl_cmd(s, "second", "R-TWO", 20)
        assert _f(axon_env) == "v2\n"
        repl_cmd(s, "/rewind to 2 code", "Restored f.txt")
        assert _f(axon_env) == "v1\n"
        s.sendline("/exit")
        s.wait(10)
