"""
E2E-F: multi-step user scenarios through the real CLI (print mode and a real pty REPL).

Each test plays a realistic sequence (several turns, commands and keys) against the scripted
fake LLM server and asserts on what matters to the user: files in the workspace, what the model
was sent (recorded requests), what was shown, and what was persisted.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

from fake_llm import Recorded
from harness import AxonEnv
from test_e2e_b_util import has_tool_result, records, session_files, text_in_request, write_file
from test_e2e_d_helpers import PROMPT_READY, TRACEBACK, open_repl, posix_only

pytestmark = pytest.mark.e2e

ESC, CTRL_F = "\x1b", "\x06"
FAKE_MCP = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


def _ok(r) -> None:
    assert TRACEBACK not in r.output, str(r)
    assert r.code == 0, str(r)


def _flat(req: Recorded) -> str:
    return req.all_text()


def _bash(cmd: str, desc: str = "run") -> dict:
    return {"tool": "Bash", "args": {"command": cmd, "description": desc}}


# ─── 1. multi-step tool turn ───────────────────────────────────────────────

def test_multi_step_grep_read_edit_bash_fixes_bug(axon_env: AxonEnv) -> None:
    """One prompt, four tool rounds (Grep -> Read -> Edit -> Bash) and a final answer: the bug in the
    workspace is fixed on disk, every tool result reaches the model in the next request, and the JSON
    result counts the calls and LLM iterations."""
    write_file(axon_env.workspace / "calc.py", "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n")
    write_file(axon_env.workspace / "other.py", "print('unrelated')\n")
    py = sys.executable
    axon_env.llm.enqueue(
        [{"text": "Searching."}, {"tool": "Grep", "args": {"pattern": "def add", "output_mode": "files_with_matches"}}],
        {"steps": [{"tool": "Read", "args": {"path": "calc.py"}}], "expect": [has_tool_result("calc.py")]},
        {"steps": [{"tool": "Edit", "args": {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}}],
         "expect": [has_tool_result("return a - b")]},
        {"steps": [_bash(f"\"{py}\" -c \"import calc; print('SUM', calc.add(2, 3))\"", "verify")],
         "expect": [has_tool_result("calc.py")]},
        {"steps": [{"text": "Fixed: add() now returns a + b."}], "expect": [has_tool_result("SUM 5")]},
    )
    r = axon_env.run(["-p", "--output-format", "json", "fix the add bug"], timeout=60)
    _ok(r)
    assert not axon_env.llm.failures, axon_env.llm.failures
    data = r.json()
    assert data["is_error"] is False and data["result"].startswith("Fixed:")
    assert data["tool_calls"] == 4 and data["iterations"] == 5, data
    assert (axon_env.workspace / "calc.py").read_text().splitlines()[1] == "    return a + b"
    assert (axon_env.workspace / "other.py").read_text() == "print('unrelated')\n"
    # the Grep result named only the matching file
    grep_res = axon_env.llm.completion_requests()[1].tool_results()
    assert any("calc.py" in t and "other.py" not in t for t in grep_res), grep_res
    # persisted: every tool call and its result are in the session transcript
    sess = [p for p in session_files(axon_env) if p.stem == data["session_id"]][0]
    names = [u["name"] for rec in records(sess) if rec["type"] == "assistant_turn" for u in rec["data"].get("tool_uses") or []]
    assert names == ["Grep", "Read", "Edit", "Bash"], names


# ─── 2. permission flow in default mode ────────────────────────────────────

@posix_only
def test_permission_flow_once_deny_always(axon_env: AxonEnv) -> None:
    """--mode default in one REPL: 'allow once' runs the command but asks again next time; 'deny'
    blocks it and tells the model; 'always' (don't ask again) stops the prompts for that tool."""
    with open_repl(axon_env, ("--mode", "default")) as s:
        # allow once
        axon_env.llm.enqueue([_bash("echo one >> log.txt")], [{"text": "RUN-1"}])
        m = s.submit("first")
        s.expect(r"⚠ Allow Bash\?", timeout=15, since=m)
        s.drain(0.3)
        s.send("1")
        s.expect("RUN-1", timeout=15, since=m)
        s.ready(since=m)
        assert (axon_env.workspace / "log.txt").read_text() == "one\n"

        # the same command is asked again (allow once does not persist); deny it
        axon_env.llm.enqueue([_bash("echo one >> log.txt")], [{"text": "RUN-2"}])
        m = s.submit("second")
        s.expect(r"⚠ Allow Bash\?", timeout=15, since=m)
        s.drain(0.3)
        s.send("3")
        s.expect("Denied", timeout=8, since=m)
        s.expect("RUN-2", timeout=15, since=m)
        s.ready(since=m)
        assert (axon_env.workspace / "log.txt").read_text() == "one\n"
        denied = axon_env.llm.completion_requests()[-1].tool_results()
        assert denied and "declined" in denied[-1].lower(), denied

        # always: asked once more, then never again for Bash in this session
        axon_env.llm.enqueue([_bash("echo two >> log.txt")], [{"text": "RUN-3"}])
        m = s.submit("third")
        s.expect(r"⚠ Allow Bash\?", timeout=15, since=m)
        s.drain(0.3)
        s.send("2")
        s.expect("Allowed for this session", timeout=8, since=m)
        s.expect("RUN-3", timeout=15, since=m)
        s.ready(since=m)
        axon_env.llm.enqueue([_bash("echo three >> log.txt")], [{"text": "RUN-4"}])
        m = s.submit("fourth")
        s.expect(r"RUN-4|Allow Bash\?", timeout=15, since=m)
        assert "Allow Bash?" not in s.screen[m:]
        s.ready(since=m)
    assert (axon_env.workspace / "log.txt").read_text() == "one\ntwo\nthree\n"


# ─── 3. plan mode ──────────────────────────────────────────────────────────

def test_plan_mode_allows_reads_and_refuses_every_edit(axon_env: AxonEnv) -> None:
    """In plan mode a batch of Read + Edit + Write + mutating Bash: the Read succeeds, every mutation is
    denied with a plan-mode reason fed back to the model, and the workspace is untouched."""
    write_file(axon_env.workspace / "keep.txt", "original\n")
    axon_env.llm.enqueue(
        [{"tool": "Read", "args": {"path": "keep.txt"}}],
        [{"tool": "Edit", "args": {"path": "keep.txt", "old_string": "original", "new_string": "changed"}},
         {"tool": "Write", "args": {"path": "new.txt", "content": "x"}},
         _bash("rm keep.txt")],
        [{"text": "Here is the plan."}],
    )
    r = axon_env.run(["-p", "--mode", "plan", "--output-format", "stream-json", "plan the change"], timeout=60)
    _ok(r)
    ev = r.json_lines()
    perms = {e["name"] if "name" in e else e.get("tool"): e for e in ev if e["type"] == "permission"}
    assert perms["Read"]["outcome"] == "allow", perms
    for tool in ("Edit", "Write", "Bash"):
        assert perms[tool]["outcome"] == "deny" and "plan" in perms[tool]["reason"].lower(), perms[tool]
    assert (axon_env.workspace / "keep.txt").read_text() == "original\n"
    assert not (axon_env.workspace / "new.txt").exists()
    results = axon_env.llm.completion_requests()[2].tool_results()
    assert any("original" in t for t in results)               # the read went through
    assert sum("plan" in t.lower() for t in results) >= 3, results  # each refusal explains plan mode
    assert ev[-1]["type"] == "result" and ev[-1]["result"] == "Here is the plan."


# ─── 4. ! shell ────────────────────────────────────────────────────────────

@posix_only
def test_bang_shell_side_effect_model_reply_and_followup_context(axon_env: AxonEnv) -> None:
    """`!cmd` runs in the workspace (creating a file), its output is shown and the model replies to it;
    the shell exchange stays in the conversation for the next normal prompt."""
    axon_env.llm.enqueue([{"text": "The listing shows bang.txt."}], [{"text": "FOLLOW-UP-OK"}])
    with open_repl(axon_env) as s:
        out = s.command("!echo BANG-77 > bang.txt && cat bang.txt", r"✓ Done", timeout=20)
        assert re.search(r"^BANG-77$", out, re.M), out[-800:]
        assert "The listing shows bang.txt." in out
        s.ready()
        s.turn("and now?")
    assert (axon_env.workspace / "bang.txt").read_text().strip() == "BANG-77"
    reqs = axon_env.llm.completion_requests()
    assert len(reqs) == 2
    assert "BANG-77" in _flat(reqs[0])
    second = _flat(reqs[1])
    assert "BANG-77" in second and "The listing shows bang.txt." in second and "and now?" in second


# ─── 5. queued messages ────────────────────────────────────────────────────

@posix_only
def test_two_messages_queued_during_a_turn_run_in_order(axon_env: AxonEnv) -> None:
    """Two prompts typed while a slow answer streams are queued and then run one after the other,
    in order, each as its own turn with the previous answers in its history."""
    axon_env.llm.enqueue([{"text": "slow "}, {"delay": 2.5}, {"text": "SLOW-DONE"}], [{"text": "Q1-ANS"}],
                         [{"text": "Q2-ANS"}])
    with open_repl(axon_env) as s:
        m = s.submit("slow one")
        s.expect("responding", timeout=10, since=m)
        for msg in ("queued alpha", "queued beta"):
            for ch in msg:
                s.send(ch)
                time.sleep(0.02)
            time.sleep(0.25)
            s.send("\r")
            time.sleep(0.3)
        s.expect("Q2-ANS", timeout=30, since=m)
        s.ready(since=m)
    reqs = axon_env.llm.completion_requests()
    assert [q.last_user_text() for q in reqs] == ["slow one", "queued alpha", "queued beta"]
    assert "SLOW-DONE" in _flat(reqs[1]) and "Q1-ANS" in _flat(reqs[2])


# ─── 6. /btw ───────────────────────────────────────────────────────────────

@posix_only
def test_btw_side_question_uses_context_but_stays_out_of_history(axon_env: AxonEnv) -> None:
    """/btw after a turn answers in a side box using the session context (sent with a separate system
    prompt), and the side question/answer is not part of the next real request."""
    axon_env.llm.enqueue([{"text": "We built MAIN-TOPIC-42."}], [{"text": "SIDE-ANSWER-9"}], [{"text": "BACK"}])
    with open_repl(axon_env) as s:
        s.turn("build the thing")
        out = s.command("/btw what did we build?", "SIDE-ANSWER-9", timeout=15)
        assert "Side question" in out
        s.ready()
        s.turn("continue the work")
    reqs = axon_env.llm.completion_requests()
    side, nxt = reqs[1], reqs[2]
    assert "side question" in side.system_text.lower() and "MAIN-TOPIC-42" in _flat(side)
    assert not side.tool_names                       # a plain one-shot, no tools
    assert "what did we build?" not in _flat(nxt) and "SIDE-ANSWER-9" not in _flat(nxt)
    assert "MAIN-TOPIC-42" in _flat(nxt)


# ─── 7. interrupt / provider error recovery ────────────────────────────────

@posix_only
def test_esc_interrupt_then_continue_keeps_progress(axon_env: AxonEnv) -> None:
    """Esc interrupts a running tool; typing `continue` sends a request that still has the interrupted
    prompt in the history and the turn finishes."""
    axon_env.llm.enqueue([_bash("sleep 4; echo LATE > late.txt", "slow")], [{"text": "RESUMED-OK"}])
    with open_repl(axon_env) as s:
        m = s.submit("start the slow job")
        s.expect(r"Bash  sleep 4", timeout=10, since=m)
        started = time.monotonic()
        time.sleep(0.5)
        s.send(ESC)
        s.expect("Interrupt", timeout=5, since=m)
        s.ready(since=m, timeout=12)
        out = s.turn("continue")
        assert "RESUMED-OK" in out
    time.sleep(max(0.0, started + 5.0 - time.monotonic()))  # past the point the command would have finished
    assert not (axon_env.workspace / "late.txt").exists()     # the interrupted command was killed
    last = axon_env.llm.completion_requests()[-1]
    assert last.last_user_text().lower().startswith("continue")
    assert "start the slow job" in _flat(last)


@posix_only
def test_provider_500_mid_turn_then_retry_resumes_with_tool_results(axon_env: AxonEnv) -> None:
    """A tool round succeeds, then the provider fails with HTTP 500 (all retries); the REPL points at
    continue / /retry, keeps the tool's side effect, and /retry resumes with the tool result in history."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "half.txt", "content": "HALF\n"}}],
                         *[[{"error": 500, "retry_after": 0}]] * 4, [{"text": "RETRY-FINISHED"}])
    with open_repl(axon_env) as s:
        m = s.submit("two step job")
        s.expect(r"/retry", timeout=40, since=m)
        s.ready(since=m, timeout=20)
        assert (axon_env.workspace / "half.txt").read_text() == "HALF\n"
        out = s.command("/retry", "RETRY-FINISHED", timeout=30)
        assert "▸ /retry" in out
        s.ready()
        m2 = s.submit("/retry")
        s.expect("Nothing to retry", timeout=10, since=m2)
    reqs = axon_env.llm.completion_requests()
    assert len(reqs) == 6, [r.last_user_text() for r in reqs]
    last = reqs[-1]
    assert any("half.txt" in t or "HALF" in t for t in last.tool_results()) or "half.txt" in _flat(last)
    assert "two step job" in _flat(last)


@posix_only
def test_provider_500_mid_turn_then_continue_word(axon_env: AxonEnv) -> None:
    """After a mid-turn provider failure, typing `continue` (instead of /retry) resumes: the request
    carries the original prompt, the completed tool result and the new `continue` message."""
    axon_env.llm.enqueue([_bash("echo PART-ONE-OUT", "first half")],
                         *[[{"error": 500, "retry_after": 0}]] * 4, [{"text": "CONTINUED-FINISHED"}])
    with open_repl(axon_env) as s:
        m = s.submit("long job")
        s.expect(r"type 'continue' or /retry", timeout=40, since=m)
        s.ready(since=m, timeout=20)
        assert "CONTINUED-FINISHED" in s.turn("continue")
    last = axon_env.llm.completion_requests()[-1]
    assert last.last_user_text() == "continue"
    assert "long job" in _flat(last) and any("PART-ONE-OUT" in t for t in last.tool_results())


@posix_only
def test_provider_500_on_first_call_rolls_back_and_retry_resends(axon_env: AxonEnv) -> None:
    """When the very first call of a turn fails, the prompt is rolled back and /retry resends exactly
    the original prompt once (no duplicate user message)."""
    axon_env.llm.enqueue(*[[{"error": 500, "retry_after": 0}]] * 4, [{"text": "SECOND-TRY-OK"}])
    with open_repl(axon_env) as s:
        m = s.submit("original question")
        s.expect(r"/retry", timeout=40, since=m)
        s.ready(since=m, timeout=20)
        s.command("/retry", "SECOND-TRY-OK", timeout=30)
    reqs = axon_env.llm.completion_requests()
    last = reqs[-1]
    users = [m for m in last.messages if m.get("role") == "user"]
    assert len(users) == 1 and last.last_user_text() == "original question", last.messages


# ─── 8. /clear and turn numbering ──────────────────────────────────────────

@posix_only
def test_clear_resets_turn_numbering_and_history(axon_env: AxonEnv) -> None:
    """Turns are numbered 1, 2; after /clear the next turn is Turn 1 again and its request carries no
    earlier history; the old session file is kept."""
    axon_env.llm.enqueue([{"text": "A-ONE"}], [{"text": "A-TWO"}], [{"text": "A-THREE"}])
    with open_repl(axon_env) as s:
        assert re.search(r'Turn 1 · "first prompt"', s.turn("first prompt"))
        assert re.search(r'Turn 2 · "second prompt"', s.turn("second prompt"))
        s.command("/clear", "New session")
        s.ready()
        out = s.turn("after clear")
        assert re.search(r'Turn 1 · "after clear"', out), out[-1500:]
        assert "Turn 3" not in out
    last = axon_env.llm.completion_requests()[-1]
    assert "first prompt" not in _flat(last) and "A-TWO" not in _flat(last)
    assert len(session_files(axon_env)) == 2


# ─── 9. resume with replay ─────────────────────────────────────────────────

@posix_only
def test_resume_flag_replays_previous_session_and_continues_it(axon_env: AxonEnv) -> None:
    """`axon --resume <id>` opens the REPL with the previous turns replayed (marked restored), and the
    next prompt continues that conversation in the same session file."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "r.txt", "content": "r\n"}}],
                         [{"text": "PREVIOUS-ANSWER-55"}])
    r = axon_env.run(["-p", "--output-format", "json", "OLD-PROMPT-55"])
    _ok(r)
    sid = r.json()["session_id"]
    axon_env.llm.enqueue({"steps": [{"text": "CONTINUED"}], "expect": [text_in_request("PREVIOUS-ANSWER-55")]})
    with open_repl(axon_env, ("--resume", sid)) as s:
        scr = s.screen
        assert "(restored)" in scr and "OLD-PROMPT-55" in scr and "PREVIOUS-ANSWER-55" in scr, scr[-2500:]
        out = s.turn("new prompt")
        assert "CONTINUED" in out
    assert not axon_env.llm.failures, axon_env.llm.failures
    assert [p.stem for p in session_files(axon_env)] == [sid]


# ─── 10. /compact ──────────────────────────────────────────────────────────

def test_compact_summarises_older_turns_and_persists_across_continue(axon_env: AxonEnv) -> None:
    """Three turns, then `/compact` in a --continue run: one summarisation call is made, older turns are
    replaced by the summary, and the next --continue request carries the summary instead of them."""
    for i, word in enumerate(("ALPHA", "BRAVO", "CHARLIE")):
        axon_env.llm.enqueue([{"text": f"answer-{word}"}])
        r = axon_env.run(["-p", *(["--continue"] if i else []), f"prompt-{word} please"])
        _ok(r)
    n = len(axon_env.llm.completion_requests())
    axon_env.llm.enqueue([{"text": "SUMMARY-OF-EARLIER-WORK"}])
    r = axon_env.run(["-p", "--continue", "/compact"])
    _ok(r)
    assert "Compacting" in r.output, r.output
    reqs = axon_env.llm.completion_requests()
    assert len(reqs) == n + 1                       # exactly one summarisation call
    assert "prompt-ALPHA" in _flat(reqs[-1])         # the summariser saw the old turns
    axon_env.llm.enqueue([{"text": "AFTER"}])
    _ok(axon_env.run(["-p", "--continue", "what next"]))
    last = _flat(axon_env.llm.completion_requests()[-1])
    assert "SUMMARY-OF-EARLIER-WORK" in last
    assert "prompt-ALPHA" not in last and "answer-ALPHA" not in last
    assert "prompt-CHARLIE" in last                  # the latest turn is kept verbatim


# ─── 11. /rewind ───────────────────────────────────────────────────────────

@posix_only
def test_rewind_restores_edited_and_deletes_created_file(axon_env: AxonEnv) -> None:
    """A turn edits an existing file and creates a new one; `/rewind to 1 code` in the REPL restores the
    edit and removes the new file, while the conversation is kept for the next turn."""
    write_file(axon_env.workspace / "main.py", "VALUE = 1\n")
    axon_env.llm.enqueue(
        [{"tool": "Read", "args": {"path": "main.py"}}],
        [{"tool": "Edit", "args": {"path": "main.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}},
         {"tool": "Write", "args": {"path": "added.py", "content": "NEW = True\n"}}],
        [{"text": "EDITED"}])
    with open_repl(axon_env) as s:
        s.turn("change things")
        assert (axon_env.workspace / "main.py").read_text() == "VALUE = 2\n"
        assert (axon_env.workspace / "added.py").exists()
        out = s.command("/rewind to 1 code", r"Restored", timeout=10)
        assert "main.py" in out, out[-1500:]
        s.ready()
        assert (axon_env.workspace / "main.py").read_text() == "VALUE = 1\n"
        assert not (axon_env.workspace / "added.py").exists()
        axon_env.llm.enqueue([{"text": "OK"}])
        s.turn("next")
    assert "EDITED" in _flat(axon_env.llm.completion_requests()[-1])  # conversation kept (code-only rewind)


# ─── 12. MCP tool call ─────────────────────────────────────────────────────

@posix_only
def test_mcp_tool_call_in_repl_with_approval(axon_env: AxonEnv) -> None:
    """A project .mcp.json stdio server's tool is called by the model in the REPL (default mode asks
    first); the server result is shown and fed back to the model."""
    log = axon_env.root / "mcp.log"
    write_file(axon_env.workspace / ".mcp.json", json.dumps({"mcpServers": {"srv": {
        "command": sys.executable, "args": [FAKE_MCP, "--name", "srv", "--log", str(log)]}}}))
    axon_env.llm.enqueue([{"tool": "mcp__srv__echo", "args": {"text": "via-repl-31"}}],
                         {"steps": [{"text": "MCP-REPL-DONE"}], "expect": [has_tool_result("ECHO:via-repl-31")]})
    with open_repl(axon_env, ("--mode", "default")) as s:
        m = s.submit("echo through mcp")
        s.expect(r"Allow .*echo|Allow mcp__srv__echo", timeout=15, since=m)
        s.drain(0.3)
        s.send("1")
        s.expect("MCP-REPL-DONE", timeout=20, since=m)
        out = s.screen[m:]
        assert "echo" in out and "ECHO:via-repl-31" in out, out[-2000:]
    assert not axon_env.llm.failures, axon_env.llm.failures
    methods = [json.loads(ln).get("method") for ln in log.read_text().splitlines() if ln.strip()]
    assert "tools/call" in methods


# ─── 13. subagent ──────────────────────────────────────────────────────────

def test_task_subagent_does_work_and_returns_its_report(axon_env: AxonEnv) -> None:
    """The main agent delegates via Task; the subagent runs its own tool loop (writing a file) with its
    own system prompt, and its final report comes back to the main agent as the Task result."""
    axon_env.llm.enqueue(
        [{"tool": "Task", "args": {"description": "write notes", "subagent_type": "general-purpose",
                                   "prompt": "SUBTASK: create notes.md with the word delegated"}}],
        {"steps": [{"tool": "Write", "args": {"path": "notes.md", "content": "delegated\n"}}],
         "expect": [text_in_request("SUBTASK: create notes.md")]},
        {"steps": [{"text": "SUB-REPORT: notes.md written"}], "expect": [has_tool_result()]},
        {"steps": [{"text": "MAIN-DONE"}], "expect": [has_tool_result("SUB-REPORT")]},
    )
    r = axon_env.run(["-p", "--output-format", "json", "delegate it"], timeout=60)
    _ok(r)
    assert not axon_env.llm.failures, axon_env.llm.failures
    assert r.json()["result"] == "MAIN-DONE"
    assert (axon_env.workspace / "notes.md").read_text() == "delegated\n"
    reqs = axon_env.llm.completion_requests()
    main_sys, sub_sys = reqs[0].system_text, reqs[1].system_text
    assert main_sys != sub_sys
    assert "Task" in reqs[0].tool_names and "Task" not in reqs[1].tool_names  # no recursive delegation


# ─── 14. print mode output formats ─────────────────────────────────────────

def test_print_json_and_stream_json_agree_and_continue(axon_env: AxonEnv) -> None:
    """json prints exactly one JSON object; stream-json prints only JSON lines ending in the result
    event; both report the same session when continued, and the history carries over."""
    axon_env.llm.enqueue([_bash("echo PJ"), ], [{"text": "JSON-ANSWER"}])
    r1 = axon_env.run(["-p", "--output-format", "json", "first"])
    _ok(r1)
    d = json.loads(r1.stdout)                      # the whole stdout is one JSON document
    for k in ("result", "session_id", "is_error", "cost_usd", "tool_calls", "iterations"):
        assert k in d, (k, d)
    assert d["result"] == "JSON-ANSWER" and d["tool_calls"] == 1 and d["is_error"] is False

    axon_env.llm.enqueue({"steps": [{"text": "STREAM-ANSWER"}], "expect": [text_in_request("JSON-ANSWER")]})
    r2 = axon_env.run(["-p", "--continue", "--output-format", "stream-json", "second"])
    _ok(r2)
    lines = [ln for ln in r2.stdout.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]      # every stdout line is JSON
    assert events[-1]["type"] == "result" and events[-1]["result"] == "STREAM-ANSWER"
    assert events[-1]["session_id"] == d["session_id"]
    assert "".join(e.get("text", "") for e in events if e["type"] == "text_delta") == "STREAM-ANSWER"
    assert not axon_env.llm.failures, axon_env.llm.failures


@pytest.mark.parametrize("fmt", ["json", "stream-json"])
def test_output_format_errors_stay_machine_readable(axon_env: AxonEnv, fmt: str) -> None:
    """A provider auth error in json / stream-json: exit 1, stdout is still valid JSON whose result is
    flagged is_error with a friendly message, and there is no traceback."""
    axon_env.llm.enqueue([{"error": 401, "message": "bad key here"}])
    r = axon_env.run(["-p", "--output-format", fmt, "hi"], timeout=40)
    assert TRACEBACK not in r.output
    assert r.code == 1, str(r)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines] if fmt == "stream-json" else [json.loads(r.stdout)]
    res = events[-1]
    assert res["type"] == "result" and res["is_error"] is True and res["stop_reason"] == "error", res
    assert "401" in res["result"] and "bad key here" in res["result"]


def test_output_format_json_max_turns_and_usage_errors(axon_env: AxonEnv) -> None:
    """--max-turns exhaustion is reported in the JSON result; an invalid flag value fails before any
    request with a non-zero exit and a message on stderr (stdout stays empty)."""
    axon_env.llm.enqueue([_bash("echo a")], [_bash("echo b")], [{"text": "never"}])
    r = axon_env.run(["-p", "--output-format", "json", "--max-turns", "1", "loop"])
    assert TRACEBACK not in r.output
    d = json.loads(r.stdout)
    assert d["stop_reason"] in ("max_turns", "max_iterations") or "max" in str(d["stop_reason"]), d
    assert d["result"] != "never"

    before = len(axon_env.llm.completion_requests())
    r2 = axon_env.run(["-p", "--output-format", "json", "--mode", "nonsense", "hi"])
    assert r2.code != 0 and TRACEBACK not in r2.output
    assert r2.stdout.strip() == "" or json.loads(r2.stdout)["is_error"] is True, str(r2)
    assert "nonsense" in r2.output
    assert len(axon_env.llm.completion_requests()) == before


# ─── 15. transcript viewer ─────────────────────────────────────────────────

@posix_only
def test_transcript_ctrl_f_shows_all_turns_search_and_closes_with_ctrl_f(axon_env: AxonEnv) -> None:
    """Two turns, then Ctrl+F opens the full-screen transcript listing both; `/` search jumps to a
    match; Ctrl+F (the opening key) closes it and the prompt works; /transcript reopens it."""
    axon_env.llm.enqueue([{"text": "FIRST-REPLY-T"}], [_bash("echo TOOLOUT-T")], [{"text": "SECOND-REPLY-T"}])
    with open_repl(axon_env, ("--mode", "bypass")) as s:
        s.turn("transcript one")
        s.turn("transcript two")
        raw0 = len(s._raw)
        s.send(CTRL_F)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            s.drain(0.15)
            view = s.raw_since(raw0)
            if "transcript one" in view and "transcript two" in view:
                break
        view = s.raw_since(raw0)
        assert "\x1b[?1049h" in view, repr(view[:300])
        assert "transcript one" in view and "transcript two" in view and "Bash" in view
        raw1 = len(s._raw)
        s.send("/")
        time.sleep(0.2)
        s.send("TOOLOUT")
        time.sleep(0.2)
        s.send("\r")
        time.sleep(0.6)
        s.drain(0.3)
        assert "no match" not in s.raw_since(raw1)
        m = s.mark()
        s.send(CTRL_F)
        s.expect(PROMPT_READY, timeout=10, since=m)
        assert "\x1b[?1049l" in s.raw_since(raw1)
        # reopen with the command, close with Esc
        raw2 = len(s._raw)
        s.submit("/transcript")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and "\x1b[?1049h" not in s.raw_since(raw2):
            s.drain(0.15)
        assert "\x1b[?1049h" in s.raw_since(raw2)
        time.sleep(0.3)
        m = s.mark()
        s.send(ESC)
        s.expect(PROMPT_READY, timeout=10, since=m)
        s.drain(0.3)
        assert "\x1b[?1049l" in s.raw_since(raw2)
        axon_env.llm.enqueue([{"text": "AFTER-VIEW"}])
        assert "AFTER-VIEW" in s.turn("after view")
        assert TRACEBACK not in s.screen
