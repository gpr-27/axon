"""/search: full-text search over this project's transcripts, streaming, skipping encrypted ones."""
from __future__ import annotations

import json
from pathlib import Path

from axon import storage
from axon.features.session_search import search_sessions


def _session(ws: Path, sid: str, records: list[dict], encrypted: bool = False) -> Path:
    d = storage.session_dir(ws, sid)
    p = d / "session.jsonl"
    lines = [json.dumps(r) for r in records]
    if encrypted:
        lines = ["ENC:v1:" + "A" * 40 for _ in lines]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _u(text): return {"type": "user_message", "data": {"role": "user", "content": text}}
def _a(text): return {"type": "assistant_turn", "data": {"text": text}}


def test_hits_carry_session_turn_and_snippet(workspace: Path):
    _session(workspace, "s1", [{"type": "session_meta", "data": {}}, _u("first question"), _a("ok"), _u("how do I fix the Frobnicator bug?"), _a("Frobnicator needs a lock")])
    res = search_sessions(workspace, "frobnicator")
    assert [(h["session"], h["turn"]) for h in res["hits"]] == [("s1", 2), ("s1", 2)]
    assert "Frobnicator" in res["hits"][0]["snippet"] and len(res["hits"][0]["snippet"]) <= 160


def test_other_projects_only_with_all(workspace: Path, tmp_path: Path):
    other = tmp_path / "other-ws"
    other.mkdir()
    _session(other, "o1", [_u("zanzibar in another project")])
    _session(workspace, "s1", [_u("nothing here")])
    assert search_sessions(workspace, "zanzibar")["hits"] == []
    res = search_sessions(workspace, "zanzibar", all_projects=True)
    assert [h["session"] for h in res["hits"]] == ["o1"]


def test_encrypted_sessions_are_skipped_with_a_note(workspace: Path):
    _session(workspace, "enc1", [_u("secret zanzibar")], encrypted=True)
    res = search_sessions(workspace, "zanzibar")
    assert res["hits"] == [] and [e["session"] for e in res["skipped_encrypted"]] == ["enc1"]


def test_streams_a_huge_session_and_honours_the_limit(workspace: Path):
    recs = [_u(f"filler line number {i} " + "x" * 200) for i in range(5000)] + [_u("needle at the end")]
    _session(workspace, "big", recs)
    res = search_sessions(workspace, "needle")
    assert len(res["hits"]) == 1 and res["hits"][0]["turn"] == 5001
    res = search_sessions(workspace, "filler", limit=5)
    assert len(res["hits"]) == 5 and res["truncated"]


def test_case_insensitive_and_empty_query(workspace: Path):
    _session(workspace, "s1", [_u("Hello World")])
    assert len(search_sessions(workspace, "WORLD")["hits"]) == 1
    assert search_sessions(workspace, "  ")["hits"] == []
