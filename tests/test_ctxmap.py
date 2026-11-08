"""/ctxmap: what fills the context window now, and dropping a tool result while keeping it on disk."""
from __future__ import annotations

from pathlib import Path

from axon.features.ctxmap import ctxmap_data, drop_result
from axon.features.insights import pin_add
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _agent(workspace: Path):
    (workspace / "big.txt").write_text("alpha beta gamma\n" * 400, encoding="utf-8")
    (workspace / "small.txt").write_text("tiny\n", encoding="utf-8")
    prov = FakeProvider(scripted(("Read", {"path": str(workspace / "big.txt")}), ("Read", {"path": str(workspace / "small.txt")}), "done"))
    agent = make_agent(prov, workspace)
    agent.run_turn("read both")
    return agent


def test_map_lists_every_kind_with_tokens_and_share(workspace: Path) -> None:
    agent = _agent(workspace)
    d = ctxmap_data(agent)
    kinds = {i["kind"] for i in d["items"]}
    assert {"system", "tools", "message", "tool_result"} <= kinds
    assert d["total"] == sum(i["tokens"] for i in d["items"]) > 0
    assert abs(sum(i["share"] for i in d["items"]) - 1.0) < 0.01
    results = [i for i in d["items"] if i["kind"] == "tool_result"]
    assert len(results) == 2 and all(i["droppable"] for i in results)
    big = max(results, key=lambda i: i["tokens"])
    assert "big.txt" in big["label"]


def test_drop_replaces_the_result_and_keeps_the_text_on_disk(workspace: Path) -> None:
    agent = _agent(workspace)
    before = ctxmap_data(agent)
    big = max((i for i in before["items"] if i["kind"] == "tool_result"), key=lambda i: i["tokens"])
    res = drop_result(agent, big["id"])
    assert res["ok"], res
    saved = Path(res["path"])
    assert saved.is_file() and "alpha beta gamma" in saved.read_text(encoding="utf-8")
    after = ctxmap_data(agent)
    assert after["total"] < before["total"]
    assert not next(i for i in after["items"] if i["id"] == big["id"])["droppable"]
    assert not any("alpha beta gamma" in str(m.get("content")) for m in agent.conversation.messages)


def test_a_pinned_result_is_never_dropped(workspace: Path) -> None:
    agent = _agent(workspace)
    big = max((i for i in ctxmap_data(agent)["items"] if i["kind"] == "tool_result"), key=lambda i: i["tokens"])
    assert pin_add(agent, big["id"])["ok"]
    item = next(i for i in ctxmap_data(agent)["items"] if i["id"] == big["id"])
    assert item["pinned"] and not item["droppable"]
    res = drop_result(agent, big["id"])
    assert not res["ok"] and "pinned" in res["message"]


def test_unknown_id(workspace: Path) -> None:
    agent = _agent(workspace)
    assert not drop_result(agent, "nope")["ok"]


def test_a_result_smaller_than_its_stub_is_kept(workspace: Path) -> None:
    """Dropping a 20-token result swapped in a 130-token note and made the context bigger."""
    agent = _agent(workspace)
    small = min((i for i in ctxmap_data(agent)["items"] if i["kind"] == "tool_result"), key=lambda i: i["tokens"])
    before = ctxmap_data(agent)["total"]
    res = drop_result(agent, small["id"])
    assert not res["ok"] and "kept" in res["message"]
    assert ctxmap_data(agent)["total"] == before
