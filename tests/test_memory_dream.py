"""/dream: consolidate notes from past sessions with a fake provider; dry run, apply, undo, bounds, one job."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon import storage  # noqa: E402
from axon.features import memory_dream as md  # noqa: E402
from axon.features import memory_files as mf  # noqa: E402
from axon.features import memory_history as mh  # noqa: E402
from axon.session.ledger import Ledger  # noqa: E402
from axon.session.store import SessionStore  # noqa: E402
from fakes import FakeProvider, scripted  # noqa: E402

ANSWER = """Here you go.
```
<note file="conventions.md" type="feedback" name="Conventions" description="how we code">
- use tabs
- run pytest before commit
</note>
<note file="new-fact.md" type="project" name="Deploy" description="deploys">
Deploys go through make ship.
</note>
<insight>Tests are always run before commits.</insight>
<dropped file="old-dup.md">duplicate of conventions</dropped>
```"""


@pytest.fixture(autouse=True)
def _home(isolate_user_home: Path) -> Path:
    return isolate_user_home


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    mf.write_memory(d, "project", name="Conventions", body="- use tabs", file="conventions.md")
    mf.write_memory(d, "project", name="Dup", body="- use tabs", file="old-dup.md")
    return d


def _session(ws: Path) -> None:
    st = SessionStore(ws)
    st.append_user("please remember my key sk-abcdefghijklmnopqrstuvwxyz123456 and that we always run pytest before commits")
    time.sleep(0.01)


def provider(text: str = ANSWER) -> FakeProvider:
    return FakeProvider(scripted(text))


def test_parse_answer_without_json_mode() -> None:
    notes, insights, dropped = md.parse_answer(ANSWER)
    assert [n.file for n in notes] == ["conventions.md", "new-fact.md"]
    assert insights == ["Tests are always run before commits."] and dropped[0]["file"] == "old-dup.md"
    assert md.parse_answer("no tags at all")[0] == []


def test_dry_run_changes_nothing_and_redacts_transcript(ws: Path) -> None:
    _session(ws)
    prov = provider()
    led = Ledger()
    p = md.dream(ws, prov, "any-model", ledger=led)
    sent = prov.requests[0]["messages"][0]["content"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in sent and "pytest before commits" in sent
    assert prov.requests[0]["model"] == "any-model" and prov.requests[0]["tools"] == []
    assert p.cost >= 0 and p.usage["output"] == 50
    assert {m.file for m in md.current_notes(ws)} == {"conventions.md", "old-dup.md"}      # untouched
    d = md.diff(ws, p)
    kinds = {x["file"]: x["kind"] for x in d}
    assert kinds == {"conventions.md": "changed", "new-fact.md": "added", "old-dup.md": "removed"}
    assert "3 " not in md.summary_line(d) and "1 added" in md.summary_line(d)


def test_apply_keeps_original_and_undo(ws: Path) -> None:
    p = md.dream(ws, provider(), "m")
    res = md.apply(ws, p)
    assert res["written"] == 2 and res["removed"] == 1
    assert {m.file for m in md.current_notes(ws)} == {"conventions.md", "new-fact.md"}
    orig = md.dreams_dir(ws) / p.id / "original"
    assert (orig / "old-dup.md").is_file()                                     # original kept
    assert mf.list_trash(ws)                                                   # and in the trash
    key = mh.note_key("project", "old-dup.md")
    assert mh.versions(ws, key)[-1].actor == f"dream {p.id}" and mh.versions(ws, key)[-1].action == "deleted"
    with pytest.raises(ValueError):
        md.apply(ws, md.load(ws, p.id))                                        # not twice
    md.undo(ws, md.load(ws, p.id))
    assert {m.file for m in md.current_notes(ws)} == {"conventions.md", "old-dup.md"}
    assert "run pytest" not in (mf.scope_dir(ws, "project") / "conventions.md").read_text()


def test_apply_refuses_when_notes_changed(ws: Path) -> None:
    p = md.dream(ws, provider(), "m")
    m = mf.find_memory(ws, "project:conventions")
    mf.replace_body(ws, m, "edited meanwhile")
    with pytest.raises(ValueError, match="changed since the dream read"):
        md.apply(ws, p)
    assert "edited meanwhile" in (mf.scope_dir(ws, "project") / "conventions.md").read_text()


def test_unusable_answer_changes_nothing(ws: Path) -> None:
    with pytest.raises(RuntimeError, match="no usable notes"):
        md.dream(ws, provider("I cannot help"), "m")
    assert md.list_proposals(ws) == []


def test_transcript_volume_is_bounded(ws: Path) -> None:
    st = SessionStore(ws)
    for i in range(200):
        st.append_user(f"message number {i} " + "x" * 300)
    text, n = md.read_transcripts(ws, max_chars=2000)
    assert n == 1 and len(text) <= 2000


def test_cancel_and_single_job(ws: Path) -> None:
    gate = threading.Event()

    class Slow(FakeProvider):
        def stream(self, **kw):
            for ev in super().stream(**kw):
                gate.wait(2)
                yield ev

    job = md.start(ws, Slow(scripted(ANSWER)), "m")
    with pytest.raises(RuntimeError, match="already running"):
        md.start(ws, provider(), "m")
    assert md.cancel()
    gate.set()
    job.thread.join(5)
    assert job.state == "cancelled" and md.list_proposals(ws) == []
    job2 = md.start(ws, provider(), "m")
    job2.thread.join(5)
    assert job2.state == "done" and job2.proposal is not None


def test_dream_command_dry_run_then_apply(ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from types import SimpleNamespace
    from axon.commands.memory_history_cmds import handle_dream
    from axon.ui.theme import strip_ansi
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=ws, model="some-model", small_model=None), provider=provider(),
                            ledger=Ledger(), session=None, reload_memory=lambda: None)
    handle_dream(agent, "")
    out = strip_ansi(capsys.readouterr().out)
    assert "1 added, 1 changed, 1 removed" in out and "Nothing changed yet" in out
    assert {m.file for m in md.current_notes(ws)} == {"conventions.md", "old-dup.md"}
    handle_dream(agent, "--apply")
    assert {m.file for m in md.current_notes(ws)} == {"conventions.md", "new-fact.md"}
