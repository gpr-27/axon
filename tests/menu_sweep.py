"""
The menu sweep: run EVERY registered command with no arguments and look at what it prints.

Not a test file (no ``test_`` prefix); tests/test_keyboard_menus_sweep.py uses it, and it can be run
by hand for a report:

    .venv/bin/python tests/menu_sweep.py            # text mode (no terminal)
    .venv/bin/python tests/menu_sweep.py --tty      # as in a terminal, every menu closed with Esc

A line is a "typed follow-up hint" when it tells the user to type another command: "/tools <name>",
"type /...", "run /...", "try /...", "· /word", "for parameters", "/word list|show|add|...".
In a terminal such lines must never be printed: the menu offers the same choice with the keys.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

HINT_PATTERNS = [
    re.compile(r"\b(type|run|use|try|see|with) `?/\w", re.I),   # type /x, run /x, use /x, try /x
    re.compile(r"for parameters", re.I),
    re.compile(r"\bUsage:\s*/", re.I),
]

# Lines of these commands quote slash commands as their CONTENT, not as advice to the reader.
CONTENT_ONLY = {
    "faq": "answers to questions; the text is the content",
    "release-notes": "the changelog text is the content",
    "transcript": "full-screen viewer: its frame is the menu",
}

# Lines that are DATA (the journal of commands that ran, the list of skill files), not advice.
DATA_LINES = {
    "activity": re.compile(r"^\d\d:\d\d:\d\d "),
    "clear": re.compile(r"^\| "),   # the welcome banner (key legend and tip) drawn in the new session
    "library": re.compile(r"^/[\w:-]+\s+(bundled|user|project|plugin|personal)\b"),
}

SIDE_QUESTION_TITLE = "Side Question (/btw)"

_known: set[str] = set()


def known_commands() -> set[str]:
    """Every command name and alias, plus skill names: what a line may tell the user to type."""
    if not _known:
        from axon.commands.registry import ALIASES, COMMANDS, feature_table, visible_commands
        _known.update(c.name for c in visible_commands())
        _known.update(COMMANDS)
        _known.update(ALIASES)
        _known.update(feature_table())
    return _known


def mentioned_commands(line: str) -> list[str]:
    """Slash commands named in a line ("/permissions", "/tools <name>"), ignoring paths."""
    found = []
    for match in re.finditer(r"(?<![\w/.~:-])/([a-z][\w-]*)(?![\w/.-]*/)", line):
        if match.group(1) in known_commands():
            found.append(match.group(1))
    return found


@dataclass
class Result:
    name: str
    output: str = ""
    menus_shown: int = 0
    error: str = ""
    hints: list[str] = field(default_factory=list)


def hint_lines(output: str, command: str = "") -> list[str]:
    """The lines of ``output`` that tell the user to type something."""
    found = []
    data = DATA_LINES.get(command)
    for line in ANSI.sub("", output).splitlines():
        if data is not None and data.search(line.strip()):
            continue
        if SIDE_QUESTION_TITLE in line:     # the /btw answer box is titled with its own command; that is a label, not advice
            continue
        if mentioned_commands(line) or any(p.search(line) for p in HINT_PATTERNS):
            found.append(line.strip())
    return found


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Keyboard:
    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return ""

    def read(self, *a: Any) -> str:
        return ""

    def fileno(self) -> int:
        raise OSError("no file descriptor")


class _TimedOut(Exception):
    pass


def make_agent(workspace: Path) -> Any:
    sys.path[:0] = [str(Path(__file__).parent)]
    from axon import cli
    from axon.config import Settings
    from fakes import FakeProvider
    settings = Settings(workspace=workspace, mode="bypass", api_key="test-key")
    return cli.build_agent(settings, provider=FakeProvider([]), connect_mcp=False)


def command_names() -> list[str]:
    from axon.commands.registry import visible_commands
    return sorted(c.name for c in visible_commands())


@contextlib.contextmanager
def _in_workspace(agent: Any):
    """Run from the agent's workspace, so a file a menu asks for (an export name answered with 'x') lands there,
    not in the repository the tests run from."""
    before = os.getcwd()
    os.chdir(agent.settings.workspace)
    try:
        yield
    finally:
        os.chdir(before)


def run_one(agent: Any, name: str, tty: bool, seconds: int = 20, line: str = "") -> Result:
    """Run ``/name`` once. With ``tty`` the terminal checks say "interactive", keys read as Esc
    (so every menu closes at once) and ``input()`` ends the input."""
    from axon import platform as plat
    from axon.commands.registry import dispatch_command
    from axon.ui import picker

    result = Result(name)
    out = _Terminal() if tty else io.StringIO()

    def timeout(*_a: Any) -> None:
        raise _TimedOut()

    def no_draw(self: Any, *a: Any, **k: Any) -> None:
        return None

    keys_read = [0]

    def esc(timeout=None, report_resize=True):
        keys_read[0] += 1
        return "esc"

    def no_input(*_a: Any) -> str:
        raise EOFError

    saved_alt_screen = plat.supports_alt_screen      # a Windows console without VT support prints instead of opening viewers
    saved = (sys.stdin, plat.read_key, plat.raw_terminal, plat.term_size, picker.LiveFrame.render, picker.LiveFrame.clear)
    menus_before = picker.menus_run()
    has_alarm = hasattr(signal, "SIGALRM")        # Windows has no SIGALRM: no per-command time limit there
    if has_alarm:
        old_alarm = signal.signal(signal.SIGALRM, timeout)
        signal.alarm(seconds)
    try:
        if tty:
            sys.stdin = _Keyboard()  # type: ignore[assignment]
            plat.read_key = esc  # type: ignore[assignment]
            plat.raw_terminal = lambda *a, **k: contextlib.nullcontext(True)  # type: ignore[assignment]
            plat.term_size = lambda: (100, 30)  # type: ignore[assignment]
            plat.supports_alt_screen = lambda: True  # type: ignore[assignment]
            picker.LiveFrame.render = no_draw  # type: ignore[method-assign]
            picker.LiveFrame.clear = no_draw  # type: ignore[method-assign]
        with contextlib.redirect_stdout(out), _patched_input(no_input), _patched_getpass(no_input), _no_browser(), _in_workspace(agent):
            dispatch_command(line or f"/{name}", agent)
    except BaseException as exc:  # a command must never break the sweep
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        from axon.commands.workspace_cmds import stop_loops
        stop_loops(agent)                       # a bare /loop starts one: what it queued must not show in the next command
        agent.message_queue.clear()
        if has_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_alarm)
        sys.stdin, plat.read_key, plat.raw_terminal, plat.term_size, picker.LiveFrame.render, picker.LiveFrame.clear = saved
        plat.supports_alt_screen = saved_alt_screen
        result.menus_shown = picker.menus_run() - menus_before + keys_read[0]
    result.output = out.getvalue()
    result.hints = [] if name in CONTENT_ONLY else hint_lines(result.output, name)
    return result


@contextlib.contextmanager
def _patched_input(fn: Any):
    import builtins
    saved = builtins.input
    builtins.input = fn
    try:
        yield
    finally:
        builtins.input = saved


@contextlib.contextmanager
def _no_browser():
    """A sweep must never open a browser window (/report --open)."""
    import webbrowser
    saved = webbrowser.open
    webbrowser.open = lambda *a, **k: True  # type: ignore[assignment]
    try:
        yield
    finally:
        webbrowser.open = saved


@contextlib.contextmanager
def _patched_getpass(fn: Any):
    import getpass
    saved = getpass.getpass
    getpass.getpass = fn
    try:
        yield
    finally:
        getpass.getpass = saved


def sweep(workspace: Path, tty: bool, names: list[str] | None = None) -> dict[str, Result]:
    agent = make_agent(workspace)
    return {name: run_one(agent, name, tty) for name in (names or command_names())}


def step_lines() -> list[tuple[str, str]]:
    """(command, typed line) for every entry of every "What next?" menu, questions answered with "x"."""
    from axon.commands.registry import visible_commands
    from axon.commands.usage_menu import REPORT_MENU, next_steps
    found = []
    for command in sorted(visible_commands(), key=lambda c: c.name):
        for step in next_steps(command) if command.name in REPORT_MENU or command.name in NEXT_OWN() else []:
            found.append((command.name, step.line.replace("{}", sample_answer(step.ask))))
    return found


def sample_answer(question: str) -> str:
    """What a user would type for a menu question: a number for numeric questions, else a word."""
    q = question.lower()
    typed = {"percent": "50", "n and the prompt": "3x run the tests", "interval and prompt": "5m check the build",
             "token limit": "2000", "seconds": "30", "template text": "greet hello", "subagent number": "1 hello",
             "template and name": "python demo"}
    for key, answer in typed.items():
        if key in q:
            return answer
    if any(word in q for word in ("turn", "number", "amount", "port", "days", "rounds", "events", "item")):
        return "3"
    return "x"


# Starting a server or a wizard cannot be run inside a sweep.
NOT_SWEPT = {"serve", "setup", "exit"}


def choice_lines() -> list[tuple[str, str]]:
    """(command, typed line) for every choice of every action and usage menu."""
    from axon.commands.registry import visible_commands
    from axon.commands.usage_menu import ACTION_MENU, USAGE_MENU, usage_choices
    found = []
    for command in sorted(visible_commands(), key=lambda c: c.name):
        if command.name in NOT_SWEPT or not (command.name in ACTION_MENU or command.name in USAGE_MENU):
            continue
        for choice in usage_choices(command.name, command.usage or ""):
            words = f"{choice.words} {sample_answer(choice.ask) if choice.ask else ''}".strip()
            found.append((command.name, f"/{command.name} {words}".strip()))
    return found


def NEXT_OWN() -> set[str]:
    from axon.commands.usage_menu import NEXT_STEPS
    return set(NEXT_STEPS)


def sweep_steps(workspace: Path) -> dict[str, Result]:
    """Run every "What next?" line as a terminal user would pick it; keyed by the typed line."""
    agent = make_agent(workspace)
    results = {}
    for _command, line in step_lines() + choice_lines():
        results.setdefault(line, run_one(agent, line.split()[0].lstrip("/"), True, line=line))
    return results


def report(results: dict[str, Result]) -> str:
    lines = []
    for name, r in results.items():
        for hint in r.hints:
            lines.append(f"/{name}: {hint}")
    return "\n".join(lines)


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    sys.path[:0] = [str(here.parent / "src"), str(here)]
    home = Path(tempfile.mkdtemp())
    os.environ["AXON_HOME"] = str(home)
    Path.home = classmethod(lambda cls: home)  # type: ignore[method-assign]
    work = Path(tempfile.mkdtemp())
    as_tty = "--tty" in sys.argv
    found = sweep(work, as_tty)
    print(report(found))
    print()
    print(f"{sum(1 for r in found.values() if r.hints)} of {len(found)} commands print a typed hint"
          + (" (terminal mode)" if as_tty else " (text mode)"))
    if as_tty:
        steps = sweep_steps(work)
        bad = {line: r for line, r in steps.items() if r.hints}
        for line, r in bad.items():
            for hint in r.hints:
                print(f"[step {line}] {hint}")
        print(f"{len(bad)} of {len(steps)} What-next lines print a typed hint")
        silent = sorted(n for n, r in found.items() if not r.menus_shown)
        print("no menu shown by:", ", ".join(silent))
