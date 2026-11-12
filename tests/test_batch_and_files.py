"""Batch jobs (native Message Batches via a mock transport, local worker pool) and Files API reuse. Fakes only."""
from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from axon.features import batch_core as bc
from axon.features.batch_cmd import run_command
from axon.features.batch_run import Runner
from axon.providers import files_api as fa


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AXON_FILES_KEEP", "1")
    return tmp_path


def settings(official=True):
    return SimpleNamespace(base_url="https://api.anthropic.com" if official else "https://example.test",
                           api_key=SecretStr("sk-test"), model="claude-sonnet-5-5", provider=None, max_tokens=8000,
                           workspace=Path.cwd())


def make_src(tmp_path: Path) -> Path:
    src = tmp_path / "proj"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("print('a')  # {not_a_var}\n")
    (src / "pkg" / "b-b.py").write_text("print('b')\n")
    return src


# ─── input ────────────────────────────────────────────────────────────────
def test_load_items_glob_template_and_ids(tmp_path):
    src = make_src(tmp_path)
    items = bc.load_items(["pkg/**/*.py"], "Review {path}:\n{content}", base=src)
    assert [i.custom_id for i in items] == ["pkg_a_py", "pkg_b-b_py"]
    assert "print('a')  # {not_a_var}" in items[0].prompt and items[0].prompt.startswith("Review pkg/a.py")
    assert all(bc.CUSTOM_ID_RE.match(i.custom_id) for i in items)


def test_custom_id_unique_and_capped():
    taken: set[str] = set()
    a = bc.safe_custom_id("x" * 200, taken)
    b = bc.safe_custom_id("x" * 200, taken)
    assert len(a) <= 64 and len(b) <= 64 and a != b


def test_jsonl_and_csv_rows(tmp_path):
    (tmp_path / "set.jsonl").write_text('{"custom_id": "q1", "topic": "cats"}\n{"topic": "dogs"}\n')
    items = bc.load_items(["set.jsonl"], "Write about {topic}", base=tmp_path)
    assert [(i.custom_id, i.prompt) for i in items] == [("q1", "Write about cats"), ("set-2", "Write about dogs")]
    (tmp_path / "set.csv").write_text("id,custom_id,q\n1,row_a,hi\n2,,yo\n")
    items = bc.load_items(["set.csv"], "{q}!", base=tmp_path)
    assert [i.prompt for i in items] == ["hi!", "yo!"] and items[0].custom_id == "row_a"


def test_build_requests_and_limits():
    reqs = bc.build_requests([bc.Item("a", "hi")], "claude-sonnet-5-5", max_tokens=500, system="be brief")
    assert reqs == [{"custom_id": "a", "params": {"model": "claude-sonnet-5-5", "max_tokens": 500,
                                                   "messages": [{"role": "user", "content": "hi"}], "system": "be brief"}}]
    assert bc.check_limits(reqs) > 0
    with pytest.raises(bc.BatchError):
        bc.check_limits([{"custom_id": str(i), "params": {}} for i in range(bc.MAX_REQUESTS + 1)])


# ─── native batches ───────────────────────────────────────────────────────
class FakeAnthropic:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status = "in_progress"
        self.lines: list[dict] = []
        self.created: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if req.method == "POST" and path == "/v1/messages/batches":
            body = json.loads(req.content)
            self.created.append(body)
            return httpx.Response(200, json=self._batch(f"msgbatch_{len(self.created)}", len(body["requests"])))
        if path.endswith("/results"):
            return httpx.Response(200, text="\n".join(json.dumps(x) for x in self.lines))
        if path.endswith("/cancel"):
            return httpx.Response(200, json={**self._batch("msgbatch_1", 3), "processing_status": "canceling"})
        if req.method == "GET":
            return httpx.Response(200, json=self._batch(path.rsplit("/", 1)[-1], 3))
        return httpx.Response(404, json={"error": {"message": "nope"}})

    def _batch(self, bid, n):
        done = self.status == "ended"
        counts = {"processing": 0 if done else n, "succeeded": 1 if done else 0, "errored": 1 if done else 0,
                  "canceled": 0, "expired": 1 if done else 0}
        return {"id": bid, "type": "message_batch", "processing_status": self.status, "request_counts": counts,
                "ended_at": "2026-11-12T00:00:00Z" if done else None, "expires_at": "2026-11-13T00:00:00Z", "results_url": None}


def native_lines():
    ok = {"type": "message", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "looks fine"}],
          "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000}}
    return [{"custom_id": "pkg_a_py", "result": {"type": "succeeded", "message": ok}},
            {"custom_id": "pkg_b-b_py", "result": {"type": "errored", "error": {"type": "invalid_request_error", "message": "bad"}}},
            {"custom_id": "extra", "result": {"type": "expired"}}]


def test_native_submit_status_results_retry_and_restart(tmp_path):
    fake = FakeAnthropic()
    out: list[str] = []
    st = settings()
    items = [bc.Item("pkg_a_py", "one"), bc.Item("pkg_b-b_py", "two"), bc.Item("extra", "three")]
    runner = Runner(st, transport=httpx.MockTransport(fake))
    plan = runner.plan(items, "claude-sonnet-5-5", max_tokens=300, system="")
    assert plan["backend"] == "anthropic" and plan["discount"] == Decimal("0.5")
    job = runner.submit(plan, "claude-sonnet-5-5", str(tmp_path / "out"))
    req = fake.calls[0]
    assert req.url.path == "/v1/messages/batches" and req.headers["x-api-key"] == "sk-test"
    assert "anthropic-beta" not in req.headers and req.headers["anthropic-version"]
    assert [r["custom_id"] for r in fake.created[0]["requests"]] == ["pkg_a_py", "pkg_b-b_py", "extra"]
    assert job["id"] == "msgbatch_1"

    # a restart: new Store and Runner on the same home; the id was persisted
    runner2 = Runner(st, transport=httpx.MockTransport(fake))
    assert runner2.list_jobs()[0]["id"] == "msgbatch_1"
    assert runner2.refresh("").get("status") == "in_progress"       # newest job, nothing downloaded yet

    fake.status, fake.lines = "ended", native_lines()
    job = runner2.refresh("msgbatch_1")
    assert job["status"] == "ended" and job["counts"] == {"processing": 0, "succeeded": 1, "errored": 1, "canceled": 0, "expired": 1}
    out_dir = Path(job["out_dir"])
    assert (out_dir / "pkg_a_py.md").read_text() == "looks fine"
    assert (out_dir / "pkg_b-b_py.error.json").exists() and (out_dir / "extra.error.json").exists()
    assert Decimal(job["cost"]) == Decimal("1.5")                   # (1M x $2 + 100k x $10) / MTok x 50%
    assert "50% batch rate" in bc.summary_line(job)

    n_calls = len(fake.calls)
    runner2.refresh("msgbatch_1")                                    # ended and downloaded: no more calls
    assert len(fake.calls) == n_calls

    assert run_command(st, "results msgbatch_1 --out " + str(tmp_path / "copy"), runner=runner2, emit=out.append) == 0
    assert (tmp_path / "copy" / "pkg_a_py.md").exists() and any("retry" in line for line in out)

    retry = runner2.retry("msgbatch_1")
    assert [r["custom_id"] for r in fake.created[1]["requests"]] == ["pkg_b-b_py", "extra"]
    assert retry["total"] == 2 and retry["note"] == "retry of msgbatch_1"


def test_native_cancel_and_list(tmp_path):
    fake = FakeAnthropic()
    runner = Runner(settings(), transport=httpx.MockTransport(fake))
    job = runner.submit(runner.plan([bc.Item("a", "x")], "claude-sonnet-5-5", max_tokens=100, system=""), "claude-sonnet-5-5")
    assert runner.cancel(job["id"])["status"] == "canceling"
    assert fake.calls[-1].url.path.endswith("/msgbatch_1/cancel")


def test_native_batch_refused_off_the_official_host():
    runner = Runner(settings(official=False))
    assert runner.backend() == "local"
    with pytest.raises(bc.BatchError, match="official Anthropic host"):
        runner.backend("anthropic")


def test_confirmation_above_threshold(tmp_path):
    fake = FakeAnthropic()
    st = settings()
    big = tmp_path / "big.jsonl"
    big.write_text(json.dumps({"prompt": "x" * 4_000_000}) + "\n")
    runner = Runner(st, transport=httpx.MockTransport(fake))
    out: list[str] = []
    cmd = f"--input {big} --model claude-opus-5-5 --max-tokens 1024 --backend anthropic"
    assert run_command(st, cmd, runner=runner, confirm=lambda q: False, emit=out.append, cwd=tmp_path) == 1
    assert fake.created == [] and any("Not submitted" in x for x in out)
    assert run_command(st, cmd + " --yes", runner=runner, confirm=lambda q: False, emit=out.append, cwd=tmp_path) == 0
    assert len(fake.created) == 1


# ─── local worker pool ────────────────────────────────────────────────────
def fake_call(state):
    def call(params):
        text = params["messages"][0]["content"]
        state["n"] = state.get("n", 0) + 1
        if text == "rate" and not state.get("limited"):
            state["limited"] = True
            raise RuntimeError("HTTP 429 rate limit")
        if text == "boom":
            raise RuntimeError("400 invalid")
        return {"model": params["model"], "content": [{"type": "text", "text": f"re: {text}"}],
                "usage": {"input_tokens": 1000, "output_tokens": 100}}
    return call


def test_local_pool_backoff_errors_retry_and_resume(tmp_path):
    state: dict = {}
    sleeps: list[float] = []
    st = settings(official=False)
    runner = Runner(st, call=fake_call(state), workers=2, sleep=sleeps.append)
    items = [bc.Item("ok", "fine"), bc.Item("slow", "rate"), bc.Item("bad", "boom")]
    plan = runner.plan(items, "claude-sonnet-5-5", max_tokens=100, system="")
    assert plan["backend"] == "local" and plan["discount"] == 1
    job = runner.submit(plan, "claude-sonnet-5-5", str(tmp_path / "res"), start_pool=False)
    from axon.features import batch_run
    pool = runner._pool(job)
    pool.run(plan["requests"])
    job = runner.store.load(job["id"])
    assert job["status"] == "ended" and job["counts"]["succeeded"] == 2 and job["counts"]["errored"] == 1
    assert sleeps and (tmp_path / "res" / "slow.md").read_text() == "re: rate"
    assert json.loads((tmp_path / "res" / "bad.error.json").read_text())["result"]["error"]["message"] == "400 invalid"
    assert Decimal(job["cost"]) > 0 and "50%" not in bc.summary_line(job)        # normal price, no batch rate
    assert (tmp_path / "res" / "summary.json").exists()

    state["fix"] = True
    retry = runner.retry(job["id"])
    assert retry["total"] == 1 and list(retry["items"]) == ["bad"]
    batch_run._POOLS[retry["id"]].thread.join(5)

    # resume: a job interrupted with pending items
    job2 = runner.submit(plan, "claude-sonnet-5-5", str(tmp_path / "res2"), start_pool=False)
    batch_run._POOLS.pop(job2["id"], None)
    assert "resume" in runner.refresh(job2["id"])["note"]
    runner.resume(job2["id"])
    batch_run._POOLS[job2["id"]].thread.join(5)
    assert runner.store.load(job2["id"])["counts"]["succeeded"] == 2


def test_local_pool_cancel(tmp_path):
    runner = Runner(settings(official=False), call=fake_call({}), workers=1, sleep=lambda s: None)
    plan = runner.plan([bc.Item("a", "x"), bc.Item("b", "y")], "claude-sonnet-5-5", max_tokens=100, system="")
    job = runner.submit(plan, "claude-sonnet-5-5", str(tmp_path / "r"), start_pool=False)
    pool = runner._pool(job)
    pool.cancel_flag.set()
    pool.run(plan["requests"])
    job = runner.store.load(job["id"])
    assert job["status"] == "canceled" and job["counts"]["canceled"] == 2


# ─── Files API ────────────────────────────────────────────────────────────
class FakeFiles:
    def __init__(self):
        self.uploads = 0
        self.deleted: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            assert b"expires_in_seconds" in req.content and b"filename=" in req.content
            assert "anthropic-beta" not in req.headers
            self.uploads += 1
            return httpx.Response(200, json={"id": f"file_{self.uploads}", "type": "file"})
        if req.method == "DELETE":
            self.deleted.append(req.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"id": "x", "type": "file_deleted"})
        return httpx.Response(404)


def pdf_msg(payload: bytes, mime="application/pdf"):
    return [{"role": "user", "content": [{"type": "document", "cache_control": {"type": "ephemeral"},
                                         "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(payload).decode()}},
                                        {"type": "text", "text": "summarize"}]}]


def sess(official=True, fake=None):
    base = "https://api.anthropic.com" if official else "https://example.test"
    return fa.FileSession(base, "sk-test", transport=httpx.MockTransport(fake or FakeFiles()), minimum=1000)


def test_upload_once_then_reuse_by_id():
    fake = FakeFiles()
    s = sess(fake=fake)
    msgs = pdf_msg(b"%PDF-" + b"x" * 5000)
    first = s.substitute(msgs)
    assert first[0]["content"][0]["source"] == {"type": "file", "file_id": "file_1"}
    assert first[0]["content"][0]["cache_control"] == {"type": "ephemeral"} and first[0]["content"][1]["text"] == "summarize"
    assert msgs[0]["content"][0]["source"]["type"] == "base64"                 # the input is untouched
    for _ in range(3):
        assert s.substitute(msgs)[0]["content"][0]["source"]["file_id"] == "file_1"
    assert fake.uploads == 1
    s2 = sess(fake=fake)                                                          # a new process reads the index
    assert s2.substitute(msgs)[0]["content"][0]["source"]["file_id"] == "file_1" and fake.uploads == 1


def test_expired_or_missing_file_is_uploaded_again():
    fake = FakeFiles()
    s = sess(fake=fake)
    msgs = pdf_msg(b"%PDF-" + b"y" * 5000)
    sent = s.substitute(msgs)
    data = s.index.load()
    for e in data.values():
        e["expires_at"] = 1.0                                                     # long expired
    s.index.save(data)
    assert s.substitute(msgs)[0]["content"][0]["source"]["file_id"] == "file_2"
    # a 404 from the API: forget the id, upload again
    assert fa.is_missing_file_error(404, "file not found: file_2")
    assert s.forget_ids(s.substitute(msgs)) == 1
    assert s.substitute(msgs)[0]["content"][0]["source"]["file_id"] == "file_3" and fake.uploads == 3
    assert sent  # first substitution had worked


def test_small_unsupported_and_failed_uploads_stay_inline():
    s = sess()
    small = pdf_msg(b"tiny")
    assert s.substitute(small) is small
    odd = pdf_msg(b"z" * 5000, mime="application/zip")
    assert s.substitute(odd)[0]["content"][0]["source"]["type"] == "base64"

    def broken(req):
        return httpx.Response(500)
    s2 = fa.FileSession("https://api.anthropic.com", "sk-test", transport=httpx.MockTransport(broken), minimum=1000)
    assert s2.substitute(pdf_msg(b"%PDF-" + b"q" * 5000))[0]["content"][0]["source"]["type"] == "base64"


def test_other_providers_keep_inline_and_cache_locally_once():
    fake = FakeFiles()
    s = sess(official=False, fake=fake)
    msgs = pdf_msg(b"%PDF-" + b"w" * 5000)
    for _ in range(2):
        assert s.substitute(msgs)[0]["content"][0]["source"]["type"] == "base64"
    data = s.index.load()
    (entry,) = data.values()
    assert fake.uploads == 0 and entry["uses"] == 2 and entry["local_copy"] and s.index.blob(entry["sha"]).exists()


def test_files_command_list_delete_clear():
    from axon.features.files_cmd import run_files
    fake = FakeFiles()
    st = settings()
    s = fa.FileSession("https://api.anthropic.com", "sk-test", transport=httpx.MockTransport(fake), minimum=1000)
    s.substitute(pdf_msg(b"%PDF-" + b"a" * 5000))
    s.substitute(pdf_msg(b"%PDF-" + b"b" * 5000))
    out: list[str] = []
    run_files(st, "list", transport=httpx.MockTransport(fake), emit=out.append)
    assert len(out) == 2 and all("file id file_" in x and fa.NOTE in x for x in out)
    out.clear()
    assert run_files(st, "delete file_1", transport=httpx.MockTransport(fake), emit=out.append) == 0
    assert fake.deleted == ["file_1"]
    run_files(st, "clear", transport=httpx.MockTransport(fake), emit=out.append)
    assert fake.deleted == ["file_1", "file_2"] and s.index.load() == {}


def test_session_end_deletes_uploads(monkeypatch):
    fake = FakeFiles()
    s = sess(fake=fake)
    s.substitute(pdf_msg(b"%PDF-" + b"c" * 5000))
    assert s.delete_uploaded(only_mine=True) == 1 and fake.deleted == ["file_1"]
