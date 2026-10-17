"""Subagent definitions, restricted registries, Task tool wiring and cost attribution."""
from __future__ import annotations
from pathlib import Path
from typing import Any

import pytest

from axon.agent.subagent import BUILTIN_AGENTS, load_agent_definitions, parse_agent_file, run_subagent
from axon.agent.trace import SubagentEvent
from axon.errors import ToolError
from axon.providers.base import AssistantTurn, TextBlock, Usage
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _names(tools: list[dict[str, Any]]) -> set[str]:
    return {t.get("name") or t.get("function", {}).get("name") for t in tools}


def test_builtin_agents_present(workspace: Path) -> None:
    names = {d.name for d in load_agent_definitions(workspace)}
    assert {"general-purpose", "explore", "plan", "code-reviewer", "test-runner"} <= names
    assert {d.name for d in BUILTIN_AGENTS if d.readonly} == {"explore", "plan"}


def test_custom_agents_from_all_dirs_with_precedence(workspace: Path, isolate_user_home: Path) -> None:
    (isolate_user_home / ".axon" / "agents").mkdir(parents=True)
    (isolate_user_home / ".axon" / "agents" / "auditor.md").write_text(
        "---\nname: auditor\ndescription: user version\n---\nUSER PROMPT", encoding="utf-8")
    (workspace / ".claude" / "agents").mkdir(parents=True)
    (workspace / ".claude" / "agents" / "docs.md").write_text(
        "---\nname: docs-writer\ndescription: Writes docs\ntools:\n  - Read\n  - Write\nmodel: inherit\n---\nWrite docs.", encoding="utf-8")
    (workspace / ".axon" / "agents").mkdir(parents=True)
    (workspace / ".axon" / "agents" / "auditor.md").write_text(
        "---\nname: auditor\ndescription: project version\ntools: Read, Grep, Bash\ndisallowedTools: Bash\nmodel: gpt-x\nmaxTurns: 7\n---\nPROJECT PROMPT",
        encoding="utf-8")
    defs = {d.name: d for d in load_agent_definitions(workspace)}
    a = defs["auditor"]
    assert a.description == "project version" and a.tools == ["Read", "Grep", "Bash"]
    assert a.disallowed_tools == ["Bash"] and a.model == "gpt-x" and a.max_turns == 7
    assert a.system_prompt == "PROJECT PROMPT" and a.source == "project"
    assert defs["docs-writer"].tools == ["Read", "Write"]


def test_parse_agent_without_frontmatter_uses_stem(tmp_path: Path) -> None:
    p = tmp_path / "helper.md"
    p.write_text("Just a prompt", encoding="utf-8")
    d = parse_agent_file(p, "project")
    assert d is not None and d.name == "helper" and d.tools is None and d.system_prompt == "Just a prompt"


def test_explore_gets_readonly_tools_and_no_task(workspace: Path) -> None:
    prov = FakeProvider(scripted("found it"))
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    out = run_subagent("find the config loader", parent, subagent_type="explore", description="find config")
    assert "found it" in out
    tool_names = _names(prov.requests[0]["tools"])
    assert "Read" in tool_names and "Task" not in tool_names
    assert not ({"Write", "Edit", "Bash"} & tool_names)
    assert "read-only exploration" in "".join(b["text"] for b in prov.requests[0]["system"])


def test_unknown_subagent_type_raises(workspace: Path) -> None:
    parent = make_agent(FakeProvider([]), workspace)
    with pytest.raises(ToolError):
        run_subagent("x", parent, subagent_type="does-not-exist")


def test_task_tool_records_cost_with_agent_source_and_emits_events(workspace: Path) -> None:
    events: list[Any] = []
    sub_turn = AssistantTurn(blocks=[TextBlock(text="sub done")], usage=Usage(input=1000, output=100))
    prov = FakeProvider(scripted(("Task", {"prompt": "review diff", "subagent_type": "code-reviewer", "description": "review"}))
                        + [sub_turn] + scripted("main done"))
    parent = make_agent(prov, workspace, events=events)
    res = parent.run_turn("please review")
    assert res.final_text == "main done"
    sources = [getattr(e, "source", None) or getattr(e, "tag", None) for e in parent.ledger.entries]
    assert "code-reviewer" in sources
    phases = [e.phase for e in events if isinstance(e, SubagentEvent)]
    assert phases == ["start", "end"]
    task = parent.subagents.all_tasks()[0]
    assert task.agent_type == "code-reviewer" and task.title == "review" and task.status == "completed"
    reviewer_tools = _names(prov.requests[1]["tools"])
    assert "Bash" in reviewer_tools and "Write" not in reviewer_tools


def test_subagent_without_approver_cannot_run_ask_tools(workspace: Path) -> None:
    prov = FakeProvider(scripted(("Write", {"path": "z.txt", "content": "x"}), "gave up"))
    parent = make_agent(prov, workspace, mode="default")
    out = run_subagent("write z", parent)
    assert "gave up" in out
    assert not (workspace / "z.txt").exists()


class _FlakyProvider(FakeProvider):
    """FakeProvider whose script may contain exceptions: those calls fail with a provider error."""

    def stream(self, **kw: Any):  # type: ignore[override]
        if self._turns and isinstance(self._turns[0], BaseException):
            self.requests.append({"messages": list(kw["messages"])})
            raise self._turns.pop(0)
        return super().stream(**kw)


def _rejected() -> Exception:
    from axon.errors import ProviderError
    e = ProviderError("AgentRouter rejected the request as invalid (HTTP 500). This is a rejection of the request itself.",
                      status=500)
    e.retryable = False  # type: ignore[attr-defined]  # as classify_error marks it: the provider already retried
    return e


def test_subagent_resumes_after_a_provider_error_and_keeps_its_work(workspace: Path, monkeypatch: Any) -> None:
    import axon.agent.subagent as sa
    monkeypatch.setattr(sa, "SUBAGENT_RESUME_DELAY_S", 0)
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    prov = _FlakyProvider(scripted(("Read", {"path": "a.py"})) + [_rejected()] + scripted("a.py sets x"))
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    out = run_subagent("inspect a.py", parent, subagent_type="explore", description="inspect a")
    assert "a.py sets x" in out and "failed" not in out
    task = parent.subagents.all_tasks()[0]
    assert task.status == "completed" and task.resumes == 1
    assert [e["kind"] for e in task.timeline if e["kind"] in ("tool", "resume")] == ["tool", "resume"]
    # The resumed call still carries the Read call and its result: nothing was redone.
    resumed = prov.requests[-1]["messages"]
    assert any(m.get("role") == "assistant" and "tool_use" in str(m.get("content")) for m in resumed)
    assert "continue" in str(resumed[-1].get("content"))


def test_subagent_that_keeps_failing_reports_its_progress(workspace: Path, monkeypatch: Any) -> None:
    import axon.agent.subagent as sa
    monkeypatch.setattr(sa, "SUBAGENT_RESUME_DELAY_S", 0)
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    prov = _FlakyProvider(scripted(("Read", {"path": "a.py"})) + [_rejected() for _ in range(sa.SUBAGENT_RESUMES + 1)])
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    out = run_subagent("inspect a.py", parent, subagent_type="explore", description="inspect a")
    first = out.splitlines()[0]
    assert first.startswith("[Subagent #1 (inspect a) failed after") and "rejected the request as invalid" in first
    assert f"Progress before the failure (resumed {sa.SUBAGENT_RESUMES}x before giving up):" in out
    assert "- Read a.py" in out
    task = parent.subagents.all_tasks()[0]
    assert task.status == "error" and task.resumes == sa.SUBAGENT_RESUMES
    assert task.timeline[-1]["kind"] == "error"


def test_system_prompt_warns_that_readonly_subagents_have_no_shell(workspace: Path) -> None:
    from axon.agent.prompt import subagents_block
    block = subagents_block(load_agent_definitions(workspace))
    explore = next(l for l in block.splitlines() if l.startswith("- **explore**"))
    assert "no Bash" in explore and "cannot run git" in explore
    general = next(l for l in block.splitlines() if l.startswith("- **general-purpose**"))
    assert "no Bash" not in general


def test_paths_in_a_subagent_prompt_are_not_attached_as_images(workspace: Path) -> None:
    (workspace / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    prov = FakeProvider(scripted("read it"))
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    run_subagent(f"Read {workspace / 'logo.png'} and describe it", parent, subagent_type="explore", description="x")
    first_user = next(m for m in prov.requests[0]["messages"] if m.get("role") == "user")
    assert isinstance(first_user["content"], str)            # no image blocks from a model-written prompt


def test_svg_is_not_treated_as_an_image() -> None:
    from axon.agent.images import IMAGE_EXTENSIONS
    assert ".svg" not in IMAGE_EXTENSIONS


def test_auto_compaction_is_saved_so_a_resume_replays_the_compacted_history(workspace: Path) -> None:
    agent = make_agent(FakeProvider([]), workspace)
    agent.session.open("sess_c")
    for i in range(8):
        agent.conversation.messages += [{"role": "user", "content": f"q{i} " + "x" * 400},
                                        {"role": "assistant", "content": f"a{i} " + "y" * 400}]
    assert agent.compact(trigger="auto", summary="SUMMARY OF EARLIER WORK")
    restored = agent.session.read_conversation("sess_c").messages
    assert restored == agent.conversation.messages and "SUMMARY OF EARLIER WORK" in str(restored)
