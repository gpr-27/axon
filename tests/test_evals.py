"""axon eval: task parsing, checks, scripted runs with a fake provider, grading, storage, compare, thresholds, safety."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from axon.evals import checks as ck
from axon.evals import store
from axon.evals.cli import run_command
from axon.evals.runner import RunConfig, scoped_approver
from axon.evals.tasks import TaskError, builtin_tasks, load_tasks, parse_task, parse_text
from axon.providers.base import AssistantTurn, TextBlock, Usage
from fakes import FakeProvider, scripted


@pytest.fixture(autouse=True)
def axon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axon_home"
    monkeypatch.setenv("AXON_HOME", str(home))
    return home


class ByPrompt(FakeProvider):
    """A fake provider whose script depends on the task prompt it receives (one provider per run)."""

    def __init__(self, scripts: dict[str, list[AssistantTurn]]) -> None:
        super().__init__([])
        self.scripts = scripts
        self.picked = False

    def stream(self, *, model, system, messages, tools, max_tokens, effort=None, thinking=True):
        if not self.picked:
            first = json.dumps(messages[0])
            for key, turns in self.scripts.items():
                if key in first:
                    self._turns = list(turns)
                    self.picked = True
        yield from super().stream(model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens,
                                  effort=effort, thinking=thinking)


def _hook(scripts, grader=None):
    def hook(cfg: RunConfig) -> None:
        cfg.provider_factory = lambda m, e: ByPrompt(scripts)
        if grader is not None:
            cfg.grader_factory = grader
    return hook


def _task_file(tmp_path: Path, tasks: list[dict], name: str = "t.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(tasks), encoding="utf-8")
    return p


CREATE = {"id": "mk", "prompt": "make hello zzz1", "checks": [{"type": "file_contains", "path": "hello.txt", "text": "hi"}]}


def _run(argv, scripts, capsys, grader=None):
    code = run_command(argv, out=print, cfg_hook=_hook(scripts, grader))
    return code, capsys.readouterr().out


# ─── parsing ────────────────────────────────────────────────────────────────

def test_builtin_suite_is_valid():
    tasks = builtin_tasks()
    assert 10 <= len(tasks) <= 15 and len({t.id for t in tasks}) == len(tasks)
    assert any(t.rubric for t in tasks) and all(t.builtin for t in tasks)


def test_parse_json_jsonl_toml():
    one = {"id": "a", "prompt": "p", "checks": [{"type": "file_exists", "path": "x"}], "tags": "t", "max_steps": 3}
    assert parse_text(json.dumps([one]), ".json")[0].tags == ["t"]
    assert parse_text(json.dumps({"tasks": [one]}), ".json")[0].max_steps == 3
    assert parse_text(json.dumps(one) + "\n", ".jsonl")[0].id == "a"
    toml = '[[tasks]]\nid = "a"\nprompt = "p"\n[[tasks.checks]]\ntype = "file_exists"\npath = "x"\n[tasks.setup.files]\n"d/y.txt" = "hi"\n'
    t = parse_text(toml, ".toml")[0]
    assert t.setup_files == {"d/y.txt": "hi"} and t.checks[0]["type"] == "file_exists"


@pytest.mark.parametrize("raw", [
    {"prompt": "p", "checks": [{"type": "file_exists", "path": "x"}]},
    {"id": "a", "prompt": "p"},
    {"id": "a", "prompt": "p", "checks": [{"type": "nope"}]},
    {"id": "a", "prompt": "p", "checks": [{"type": "file_exists", "path": "../x"}]},
    {"id": "a", "prompt": "p", "setup": {"files": {"/etc/x": "1"}}, "checks": [{"type": "file_exists", "path": "x"}]},
    {"id": "a", "prompt": "p", "checks": [{"type": "diff_only"}]},
])
def test_invalid_tasks_rejected(raw):
    with pytest.raises(TaskError):
        parse_task(raw)


def test_duplicate_ids_and_bad_extension(tmp_path):
    with pytest.raises(TaskError):
        parse_text(json.dumps([CREATE, CREATE]), ".json")
    with pytest.raises(TaskError):
        parse_text("id: a", ".yaml")
    with pytest.raises(TaskError):
        load_tasks(str(tmp_path / "missing"))


# ─── checks ─────────────────────────────────────────────────────────────────

def test_every_check_type(tmp_path):
    ws = tmp_path
    (ws / "a.txt").write_text("hello world\n")
    before = ck.snapshot(ws)
    (ws / "b.txt").write_text("new\n")

    def r(**c):
        return ck.run_check(c, ws, before=before, answer="The port is 8123.", tools=["Read", "Grep"])

    assert r(type="file_exists", path="a.txt").passed and not r(type="file_exists", path="zz").passed
    assert r(type="file_absent", path="zz").passed and not r(type="file_absent", path="a.txt").passed
    assert r(type="file_contains", path="a.txt", text="world").passed and not r(type="file_contains", path="a.txt", text="x").passed
    assert r(type="file_not_contains", path="a.txt", text="x").passed
    assert r(type="file_regex", path="a.txt", pattern=r"^hello\s+w").passed
    assert r(type="file_not_regex", path="a.txt", pattern="zzz").passed
    assert r(type="file_equals", path="a.txt", content="hello world").passed and not r(type="file_equals", path="a.txt", content="hello world", strict=True).passed
    cmd = r(type="command", cmd="{python} -c \"print('out-ok')\"", stdout_contains="out-ok")
    assert cmd.passed
    assert not r(type="command", cmd="{python} -c \"import sys; sys.exit(3)\"").passed
    assert r(type="command", cmd="{python} -c \"import sys; sys.exit(3)\"", exit_code=3).passed
    assert "timed out" in r(type="command", cmd="{python} -c \"import time; time.sleep(5)\"", timeout=0.3).detail
    assert not r(type="command", cmd="{python} -c \"print(1)\"", stdout_contains="2").passed
    assert r(type="tests_pass", cmd="{python} -c \"pass\"").passed
    d = r(type="diff_only", files=["a.txt"])
    assert not d.passed and "b.txt" in d.detail
    assert r(type="diff_only", files=["b.txt"]).passed
    assert r(type="answer_contains", text="8123").passed and r(type="answer_regex", pattern=r"port is \d+").passed
    assert r(type="tool_called", name="grep").passed and not r(type="tool_called", name="Write").passed
    assert not r(type="file_regex", path="a.txt", pattern="(").passed


def test_scoped_approver_blocks_paths_outside(tmp_path):
    ok = scoped_approver(tmp_path)
    assert ok("Write", {"file_path": "a.txt"}) and ok("Write", {"file_path": str(tmp_path / "d" / "a.txt")})
    assert not ok("Write", {"file_path": "../x"}) and not ok("Edit", {"file_path": "/etc/passwd"})


# ─── scripted runs ──────────────────────────────────────────────────────────

def test_scripted_success_stores_run(tmp_path, capsys, axon_home):
    f = _task_file(tmp_path, [CREATE])
    scripts = {"zzz1": scripted(("Write", {"file_path": "hello.txt", "content": "hi there\n"}), "done")}
    code, out = _run(["--tasks", str(f), "--json"], scripts, capsys)
    data = json.loads(out)
    assert code == 0 and data["passed"] == 1 and data["pass_rate"] == 1.0
    r = data["results"][0]
    assert r["status"] == "pass" and r["steps"] == 2 and r["tool_calls"] == 1 and r["cost"] >= 0
    d = Path(data["path"])
    assert (d / "results.jsonl").is_file() and (d / "summary.json").is_file()
    assert Path(r["transcript"]).is_file() and Path(r["transcript"]).parent == d / "transcripts"


def test_scripted_failure_names_failing_check_and_threshold(tmp_path, capsys):
    f = _task_file(tmp_path, [CREATE])
    scripts = {"zzz1": scripted("I did nothing")}
    code, out = _run(["--tasks", str(f), "--min-pass", "50"], scripts, capsys)
    assert code == 1
    assert "failed:" in out and "mk" in out and "file_contains:hello.txt" in out and "below --min-pass" in out
    code, _ = _run(["--tasks", str(f), "--min-pass", "0"], scripts, capsys)
    assert code == 0


def test_workspace_deleted_unless_keep_and_user_dir_untouched(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _task_file(tmp_path, [{**CREATE, "id": "k"}], "k.json")
    before = sorted(p.name for p in tmp_path.iterdir())
    scripts = {"zzz1": scripted(("Write", {"file_path": "hello.txt", "content": "hi"}), "ok")}
    _, out = _run(["--tasks", str(f), "--keep", "--json"], scripts, capsys)
    stored = store.load_run(Path(json.loads(out)["path"]))[1][0]
    kept = Path(stored["workspace"])
    assert (kept / "hello.txt").is_file() and tmp_path not in kept.parents
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "axon_home") == before     # nothing created in the cwd
    _, out = _run(["--tasks", str(f), "--json"], scripts, capsys)
    assert "workspace" not in store.load_run(Path(json.loads(out)["path"]))[1][0]


def test_write_outside_workspace_is_denied(tmp_path, capsys):
    outside = tmp_path / "outside.txt"
    t = {"id": "esc", "prompt": "escape zzz2", "checks": [{"type": "file_absent", "path": "nothing"}]}
    f = _task_file(tmp_path, [t])
    scripts = {"zzz2": scripted(("Write", {"file_path": str(outside), "content": "x"}), "done")}
    code, out = _run(["--tasks", str(f), "--json"], scripts, capsys)
    assert not outside.exists()
    assert json.loads(out)["results"][0]["status"] == "pass"


def test_rubric_grading_with_fake_grader(tmp_path, capsys):
    t = {"id": "rb", "prompt": "explain zzz3", "rubric": "mentions the cat", "checks": [{"type": "diff_only", "files": []}]}
    f = _task_file(tmp_path, [t])
    scripts = {"zzz3": scripted("It is about a dog.")}

    def grader(verdict: str):
        body = json.dumps({"per_criterion": [{"criterion": "mentions the cat", "met": verdict == "satisfied", "evidence": "e", "gap": "no cat"}],
                           "verdict": verdict, "summary": "s"})
        return lambda: FakeProvider([AssistantTurn(blocks=[TextBlock(text=body)], stop_reason="end_turn", usage=Usage(input=10, output=5))])

    code, out = _run(["--tasks", str(f), "--json"], scripts, capsys, grader("needs_revision"))
    r = json.loads(out)["results"][0]
    assert r["status"] == "fail" and r["failed_check"].startswith("rubric: mentions the cat")
    code, out = _run(["--tasks", str(f), "--json"], scripts, capsys, grader("satisfied"))
    assert json.loads(out)["results"][0]["status"] == "pass"


def test_rubric_skipped_when_exact_check_fails(tmp_path, capsys):
    t = {"id": "rb", "prompt": "explain zzz3", "rubric": "x", "checks": [{"type": "answer_contains", "text": "cat"}]}
    f = _task_file(tmp_path, [t])

    def boom():
        raise AssertionError("grader must not run")
    code, out = _run(["--tasks", str(f), "--json"], {"zzz3": scripted("dog")}, capsys, boom)
    assert json.loads(out)["results"][0]["failed_check"].startswith("answer_contains")


def test_repeat_models_efforts_matrix_and_table(tmp_path, capsys):
    f = _task_file(tmp_path, [CREATE])
    scripts = {"zzz1": scripted("nothing")}
    code, out = _run(["--tasks", str(f), "--models", "m-a,m-b", "--efforts", "low,high", "--repeat", "2", "--jobs", "3"], scripts, capsys)
    assert "8 run(s)" in out
    rows = [ln for ln in out.splitlines() if ln.startswith("m-")]
    assert len(rows) == 4 and all("0/2" in r for r in rows)
    assert out.count("mk") >= 1 and out.splitlines().count("failed:") == 1
    assert sum(1 for ln in out.splitlines() if ln.strip().startswith("mk")) == 1     # the failing task id is listed once


# ─── compare / list / show / rerun ──────────────────────────────────────────

def test_compare_list_show_rerun(tmp_path, capsys):
    tasks = [CREATE, {**CREATE, "id": "mk2", "prompt": "second zzz4"}]
    f = _task_file(tmp_path, tasks)
    good = scripted(("Write", {"file_path": "hello.txt", "content": "hi"}), "ok")
    _, out_a = _run(["--tasks", str(f), "--json"], {"zzz1": good, "zzz4": scripted("no")}, capsys)
    _, out_b = _run(["--tasks", str(f), "--json"], {"zzz1": scripted("no"), "zzz4": scripted(("Write", {"file_path": "hello.txt", "content": "hi"}), "ok")}, capsys)
    a, b = json.loads(out_a)["run_id"], json.loads(out_b)["run_id"]
    assert run_command(["compare", a, b], out=print) == 0
    text = capsys.readouterr().out
    assert "regressions:" in text and "improvements:" in text
    assert "mk  " in text.split("regressions:")[1].split("improvements:")[0] and "1/1 -> 0/1" in text
    capsys.readouterr()
    run_command(["list"], out=print)
    assert a in capsys.readouterr().out
    run_command(["show", a], out=print)
    assert "mk2" in capsys.readouterr().out
    assert run_command(["show", "nope"], out=print) == 2
    capsys.readouterr()
    code = run_command(["rerun-failed", a, "--json"], out=print, cfg_hook=_hook({"zzz1": good, "zzz4": good}))
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["runs"] == 1 and data["results"][0]["task"] == "mk2" and data["passed"] == 1


# ─── safety ─────────────────────────────────────────────────────────────────

def test_shell_checks_need_allow_commands(tmp_path, capsys):
    t = {"id": "sh", "prompt": "do zzz5", "checks": [{"type": "command", "cmd": "echo hi"}]}
    f = _task_file(tmp_path, [t])
    code, out = _run(["--tasks", str(f)], {"zzz5": scripted("x")}, capsys)
    assert code == 2 and "--allow-commands" in out and "sh" in out
    assert not (store.evals_root().exists() and any(store.evals_root().iterdir()))
    code, out = _run(["--tasks", str(f), "--allow-commands", "--json"], {"zzz5": scripted("x")}, capsys)
    assert code == 0 and json.loads(out)["passed"] == 1


def test_builtin_shell_tasks_exempt_and_selectable(capsys):
    code, out = _run(["--id", "fix_failing_test", "--json"], {"calc": scripted("I give up")}, capsys)
    assert code == 0 and json.loads(out)["results"][0]["status"] == "fail"


def test_secrets_redacted_in_stored_transcript(tmp_path, capsys):
    secret = "sk-ant-api03-" + "A" * 40
    f = _task_file(tmp_path, [CREATE])
    code, out = _run(["--tasks", str(f), "--json"], {"zzz1": scripted(f"my key is {secret}")}, capsys)
    tr = Path(json.loads(out)["results"][0]["transcript"]).read_text()
    assert secret not in tr


def test_usage_errors(capsys):
    assert run_command(["--bogus"], out=print) == 2
    assert run_command(["compare", "a"], out=print) == 2
    assert run_command(["--tag", "nonexistent-tag"], out=print) == 2


def test_sys_python_placeholder():
    assert sys.executable in ck.expand("{python} x")
