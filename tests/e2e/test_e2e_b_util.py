"""Shared helpers for the ``test_e2e_b_*`` end-to-end suites (no tests live here)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fake_llm import Recorded
from harness import AxonEnv, Result, _project_key

TRACEBACK = "Traceback (most recent call last)"
# _run_handler prints "✗ /name failed: ExcType: msg" when a command raises
CMD_FAILED_RE = re.compile(r"✗ /?[\w:#.-]+ (?:failed|cancelled):")


def no_crash(r: Result | str) -> None:
    """The output contains no Python traceback and no swallowed command exception."""
    text = r if isinstance(r, str) else r.output
    assert TRACEBACK not in text, str(r)
    assert "Error during" not in text, str(r)
    m = CMD_FAILED_RE.search(text)
    assert m is None, f"command raised: {m.group(0) if m else ''}\n{r}"


def project_dir(env: AxonEnv) -> Path:
    return env.axon_home / "projects" / _project_key(env.workspace)


def session_files(env: AxonEnv) -> list[Path]:
    d = env.sessions_dir()
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def records(path: Path) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def run_json(axon: AxonEnv, *args: str, **kw: Any) -> dict[str, Any]:
    r = axon.run(["-p", "--output-format", "json", *args], **kw)
    assert r.code == 0, str(r)
    return r.json()


def has_tool_result(substr: str = ""):
    def pred(req: Recorded):
        res = req.tool_results()
        if not res:
            return "no tool result in request"
        if substr and not any(substr in t for t in res):
            return f"no tool result containing {substr!r}: {res!r}"
        return True
    pred.__name__ = f"has_tool_result({substr!r})"
    return pred


def text_in_request(substr: str):
    def pred(req: Recorded):
        return substr in req.all_text() or f"{substr!r} not in request"
    pred.__name__ = f"text_in_request({substr!r})"
    return pred


def text_not_in_request(substr: str):
    def pred(req: Recorded):
        return substr not in req.all_text() or f"{substr!r} unexpectedly in request"
    pred.__name__ = f"text_not_in_request({substr!r})"
    return pred


def write_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def repl_cmd(s: Any, line: str, expect: str, timeout: float = 15.0) -> str:
    """Send a REPL line and wait for ``expect``; return the new screen text since the line was sent."""
    m = s.mark()
    s.sendline(line)
    s.expect(expect, timeout=timeout, since=m)
    s.drain(0.4)
    return s.screen[m:]


def start_repl(env: AxonEnv, args: tuple[str, ...] = (), **kw: Any) -> Any:
    s = env.pty(list(args), **kw)
    s.expect(r"(?i)(axon|❯|>)", timeout=20)
    s.drain(0.8)
    return s


# ─── parallel scenarios ─────────────────────────────────────────────────────
# Most e2e tests spend their time starting the axon subprocess. Tests decorated with
# ``@parallel`` are real tests (one pytest item each, same name/docstring/markers), but their
# bodies are executed concurrently by the module fixture ``pbatch`` - each in its own isolated
# home/AXON_HOME/workspace with its own FakeLLMServer - and the pytest item re-raises the
# scenario's own outcome (assertion error, skip, xfail). Only selected items are executed.
import os as _os
from concurrent.futures import ThreadPoolExecutor as _Pool

import pytest as _pytest

from fake_llm import FakeLLMServer as _FakeLLMServer
from harness import create_axon_env as _create_axon_env

_REGISTRY: dict[str, dict[str, tuple[Any, dict[str, Any]]]] = {}
MODEL_IDS = ("gpt-4o", "claude-sonnet-4-6", "fake-model")


def parallel(fn: Any = None, *, git: bool = False) -> Any:
    def deco(f: Any) -> Any:
        _REGISTRY.setdefault(f.__module__, {})[f.__name__] = (f, {"git": git})

        def wrapper(pbatch: "_Batch") -> None:
            pbatch.check(f.__name__)
        wrapper.__name__ = f.__name__
        wrapper.__qualname__ = f.__qualname__
        wrapper.__doc__ = f.__doc__
        wrapper.__module__ = f.__module__
        if hasattr(f, "pytestmark"):
            wrapper.pytestmark = list(f.pytestmark)
        return wrapper
    return deco(fn) if fn is not None else deco


class _Batch:
    def __init__(self, outcomes: dict[str, BaseException | None]) -> None:
        self.outcomes = outcomes

    def check(self, name: str) -> None:
        exc = self.outcomes.get(name, KeyError(f"scenario {name} was not executed"))
        if exc is not None:
            raise exc


def _run_scenario(fn: Any, opts: dict[str, Any], root: Path) -> BaseException | None:
    llm = _FakeLLMServer(model_ids=MODEL_IDS).start()
    try:
        env = _create_axon_env(root, llm, git=opts.get("git", False))
        fn(env)
        return None
    except BaseException as e:  # noqa: BLE001 - re-raised by the pytest item
        return e
    finally:
        llm.stop()


@_pytest.fixture(scope="module")
def pbatch(request: _pytest.FixtureRequest, tmp_path_factory: _pytest.TempPathFactory) -> _Batch:
    mod = request.module.__name__
    reg = _REGISTRY.get(mod, {})
    selected = [getattr(it, "originalname", it.name) for it in request.session.items
                if getattr(getattr(it, "module", None), "__name__", None) == mod]
    names = [n for n in dict.fromkeys(selected) if n in reg]
    base = tmp_path_factory.mktemp(mod.rsplit(".", 1)[-1])
    workers = max(4, min(10, _os.cpu_count() or 4))
    with _Pool(max_workers=workers) as ex:
        futs = {n: ex.submit(_run_scenario, reg[n][0], reg[n][1], base / n) for n in names}
        outcomes = {n: f.result() for n, f in futs.items()}
    return _Batch(outcomes)
