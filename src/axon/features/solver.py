"""
Solver: detect project checks, run them, parse failures and drive the agent to fix them.

Commands
  /verify [names|all]          run detected checks (tests, lint, typecheck, build) -> results table
  /fix [command | error text]  Error Doctor: precise fix prompt (failures, file:line, snippets) -> agent
  /autofix [command] [--max N] verify -> fix -> re-verify loop with an iteration table

Checks are detected from project files (pyproject, package.json, go.mod, Cargo.toml, ...) and can be
overridden with settings ``verify_commands`` or ``<ws>/.axon/settings.json`` ``"verify": [...]``
(strings, or objects ``{"name", "command", "kind"}``). Nothing is written to the workspace.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from axon.features import FeatureCommand

KINDS = ("test", "lint", "typecheck", "build")
DEFAULT_TIMEOUT = 900.0
MAX_OUTPUT_CHARS = 200_000
MAX_FAILURES = 60
SNIPPET_RADIUS = 8

try:  # theme is optional (tests / headless use)
    from axon.ui import theme as _theme
except Exception:  # pragma: no cover
    _theme = None  # type: ignore[assignment]


def _c(name: str) -> str:
    return str(getattr(_theme, name, "")) if _theme is not None else ""


def _unicode() -> bool:
    try:
        return bool(_theme.supports_unicode()) if _theme is not None else True
    except Exception:
        return True


def _g(uni: str, ascii_: str) -> str:
    return uni if _unicode() else ascii_


def _out(text: str = "") -> None:
    try:
        if _theme is not None and hasattr(_theme, "safe_text"):
            text = _theme.safe_text(text)
    except Exception:
        pass
    try:
        print(text, flush=True)
    except UnicodeEncodeError:  # pragma: no cover - cp1252 consoles
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def _width(text: str) -> int:
    try:
        return int(_theme.display_width(text)) if _theme is not None else len(_strip_ansi(text))
    except Exception:
        return len(_strip_ansi(text))


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _width(text))
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def _table(headers: list[str], rows: list[list[str]], right: Iterable[int] = ()) -> list[str]:
    """Aligned, ANSI-aware plain table (no boxes)."""
    right = set(right)
    widths = [_width(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], _width(cell))
    muted, bold, rst = _c("MUTED"), _c("BOLD"), _c("RST")
    out = ["  " + "  ".join(f"{bold}{_pad(h, widths[i], 'right' if i in right else 'left')}{rst}"
                            for i, h in enumerate(headers))]
    out.append("  " + muted + "  ".join(_g("─", "-") * w for w in widths) + rst)
    for r in rows:
        out.append("  " + "  ".join(_pad(c, widths[i], "right" if i in right else "left") for i, c in enumerate(r)))
    return out


def fmt_duration(sec: float) -> str:
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec // 60)}m{int(sec % 60):02d}s"


def fmt_cost(cost: Any) -> str:
    try:
        v = float(cost)
    except Exception:
        return "-"
    return f"${v:.4f}"


# ─── Data model ───────────────────────────────────────────────────────────
@dataclass
class Check:
    name: str
    command: str | list[str]          # str: run through the platform shell; list: argv, no shell
    kind: str = "test"                # test | lint | typecheck | build | custom
    source: str = "detected"          # detected | settings | workspace | user

    @property
    def display(self) -> str:
        if isinstance(self.command, list):
            parts = list(self.command)
            if parts and os.path.isabs(parts[0]):
                parts[0] = Path(parts[0]).name
            return " ".join(parts)
        return self.command

    @property
    def argv(self) -> list[str] | str:
        return self.command


@dataclass(frozen=True)
class Failure:
    file: str
    line: int | None
    message: str
    kind: str = "error"
    col: int | None = None

    @property
    def location(self) -> str:
        if self.line is None:
            return self.file
        return f"{self.file}:{self.line}" + (f":{self.col}" if self.col else "")


@dataclass
class CheckResult:
    check: Check
    exit_code: int
    output: str
    duration: float
    failures: list[Failure] = field(default_factory=list)
    timed_out: bool = False
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.interrupted

    @property
    def failure_count(self) -> int:
        if self.ok:
            return 0
        return max(1, len(self.failures))


# ─── Detection ────────────────────────────────────────────────────────────
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _python_exe(ws: Path) -> str:
    for rel in ((".venv", "Scripts", "python.exe"), ("venv", "Scripts", "python.exe"),
                (".venv", "bin", "python"), ("venv", "bin", "python")):
        p = ws.joinpath(*rel)
        if p.is_file():
            return str(p.absolute())
    for name in ("python3", "python"):
        found = shutil.which(name)
        # Windows' WindowsApps\python*.exe are Microsoft Store stubs that only print "Python was not found".
        if found and "windowsapps" not in found.replace("/", "\\").lower().split("\\"):
            return found
    return sys.executable


def _tool_argv(ws: Path, py: str, tool: str, args: list[str]) -> list[str]:
    """Prefer the tool inside the project's venv, else on PATH, else `python -m tool`."""
    vdir = Path(py).parent
    for cand in (vdir / tool, vdir / f"{tool}.exe"):
        if cand.is_file():
            return [str(cand), *args]
    found = shutil.which(tool)
    if found:
        return [found, *args]
    return [py, "-m", tool, *args]


def _has_section(text: str, *names: str) -> bool:
    return any(re.search(rf"^\s*\[{re.escape(n)}[\].]", text, re.M) for n in names)


def _has_test_files(ws: Path) -> bool:
    for d in ("tests", "test"):
        p = ws / d
        if p.is_dir():
            try:
                for f in p.rglob("*.py"):
                    if f.name.startswith("test") or f.name.endswith("_test.py"):
                        return True
            except Exception:
                return False
    return False


def _python_checks(ws: Path) -> list[Check]:
    pyproject = _read(ws / "pyproject.toml")
    setup_cfg = _read(ws / "setup.cfg")
    tox = _read(ws / "tox.ini")
    if not (pyproject or setup_cfg or (ws / "setup.py").exists() or (ws / "requirements.txt").exists()
            or _has_test_files(ws) or (ws / "pytest.ini").exists()):
        return []
    py = _python_exe(ws)
    checks: list[Check] = []
    reqs = _read(ws / "requirements-dev.txt") + _read(ws / "requirements.txt")
    pytest_cfg = ((ws / "pytest.ini").exists() or (ws / "conftest.py").exists()
                  or _has_section(pyproject, "tool.pytest") or _has_section(setup_cfg, "tool:pytest")
                  or _has_section(tox, "pytest") or "pytest" in pyproject or "pytest" in reqs
                  or (ws / "tests" / "conftest.py").exists())
    if pytest_cfg:
        checks.append(Check("pytest", [py, "-m", "pytest", "-q", "--no-header", "-rfE"], "test"))
    elif _has_test_files(ws):
        start = "tests" if (ws / "tests").is_dir() else "test"
        checks.append(Check("unittest", [py, "-m", "unittest", "discover", "-s", start, "-t", "."], "test"))
    if (ws / "ruff.toml").exists() or (ws / ".ruff.toml").exists() or _has_section(pyproject, "tool.ruff"):
        checks.append(Check("ruff", _tool_argv(ws, py, "ruff", ["check", "."]), "lint"))
    elif (ws / ".flake8").exists() or _has_section(setup_cfg, "flake8") or _has_section(tox, "flake8"):
        checks.append(Check("flake8", _tool_argv(ws, py, "flake8", ["."]), "lint"))
    if (ws / "mypy.ini").exists() or (ws / ".mypy.ini").exists() or _has_section(pyproject, "tool.mypy") \
            or _has_section(setup_cfg, "mypy"):
        checks.append(Check("mypy", _tool_argv(ws, py, "mypy", ["."]), "typecheck"))
    return checks


def _js_checks(ws: Path) -> list[Check]:
    pkg_path = ws / "package.json"
    if not pkg_path.is_file():
        return []
    try:
        pkg = json.loads(_read(pkg_path) or "{}")
    except Exception:
        pkg = {}
    scripts = pkg.get("scripts") or {}
    if not isinstance(scripts, dict):
        scripts = {}
    if (ws / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (ws / "yarn.lock").exists():
        pm = "yarn"
    elif (ws / "bun.lockb").exists() or (ws / "bun.lock").exists():
        pm = "bun"
    else:
        pm = "npm"
    run = (lambda s: f"{pm} run {s}") if pm in ("npm", "bun") else (lambda s: f"{pm} {s}")
    checks: list[Check] = []
    test = str(scripts.get("test", ""))
    if test and "no test specified" not in test:
        checks.append(Check(f"{pm} test", f"{pm} test" if pm != "bun" else "bun run test", "test"))
    for key in ("lint",):
        if key in scripts:
            checks.append(Check(f"{pm} {key}", run(key), "lint"))
    tc = next((k for k in ("typecheck", "type-check", "tsc", "check-types", "types") if k in scripts), None)
    if tc:
        checks.append(Check(f"{pm} {tc}", run(tc), "typecheck"))
    elif (ws / "tsconfig.json").exists():
        checks.append(Check("tsc", "npx --no-install tsc --noEmit -p .", "typecheck"))
    if "build" in scripts:
        checks.append(Check(f"{pm} build", run("build"), "build"))
    return checks


def _make_has_target(ws: Path, target: str) -> bool:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        if re.search(rf"^{re.escape(target)}\s*:", _read(ws / name), re.M):
            return True
    return False


def _other_checks(ws: Path) -> list[Check]:
    checks: list[Check] = []
    if (ws / "go.mod").exists():
        checks.append(Check("go test", "go test ./...", "test"))
        checks.append(Check("go vet", "go vet ./...", "lint"))
    if (ws / "Cargo.toml").exists():
        checks.append(Check("cargo test", "cargo test", "test"))
        if shutil.which("cargo-clippy"):
            checks.append(Check("cargo clippy", "cargo clippy --all-targets", "lint"))
    try:
        dotnet = any(ws.glob("*.sln")) or any(ws.glob("*.csproj")) or any(ws.glob("*/*.csproj"))
    except Exception:
        dotnet = False
    if dotnet:
        checks.append(Check("dotnet test", "dotnet test", "test"))
    if (ws / "pom.xml").exists():
        mvn = "./mvnw" if (ws / "mvnw").exists() and os.name != "nt" else ("mvnw.cmd" if (ws / "mvnw.cmd").exists() and os.name == "nt" else "mvn")
        checks.append(Check("mvn test", f"{mvn} -q test", "test"))
    elif (ws / "build.gradle").exists() or (ws / "build.gradle.kts").exists():
        if os.name == "nt" and (ws / "gradlew.bat").exists():
            gradle = "gradlew.bat"
        elif (ws / "gradlew").exists() and os.name != "nt":
            gradle = "./gradlew"
        else:
            gradle = "gradle"
        checks.append(Check("gradle test", f"{gradle} test", "test"))
    if _make_has_target(ws, "test") and not checks:
        checks.append(Check("make test", "make test", "test"))
    return checks


def _coerce_override(item: Any, idx: int, source: str) -> Check | None:
    if isinstance(item, str) and item.strip():
        cmd = item.strip()
        return Check(cmd.split()[0] if len(cmd) > 40 else cmd, cmd, _guess_kind(cmd), source)
    if isinstance(item, dict):
        cmd = item.get("command") or item.get("cmd") or item.get("run")
        if isinstance(cmd, (str, list)) and cmd:
            name = str(item.get("name") or (cmd if isinstance(cmd, str) else " ".join(cmd)) or f"check{idx}")
            kind = str(item.get("kind") or _guess_kind(cmd if isinstance(cmd, str) else " ".join(cmd)))
            return Check(name, cmd if isinstance(cmd, str) else [str(c) for c in cmd], kind, source)
    return None


def _guess_kind(cmd: str) -> str:
    low = cmd.lower()
    if any(k in low for k in ("lint", "ruff", "flake8", "clippy", "eslint", "vet")):
        return "lint"
    if any(k in low for k in ("mypy", "tsc", "pyright", "typecheck", "type-check")):
        return "typecheck"
    if "build" in low or "compile" in low:
        return "build"
    return "test"


def override_checks(ws: Path, settings: Any = None) -> list[Check] | None:
    """Configured checks (settings.verify_commands, then <ws>/.axon/settings.json "verify")."""
    items = getattr(settings, "verify_commands", None) if settings is not None else None
    source = "settings"
    if not items:
        data: Any = None
        try:
            data = json.loads(_read(ws / ".axon" / "settings.json") or "{}")
        except Exception:
            data = None
        items = data.get("verify") if isinstance(data, dict) else None
        source = "workspace"
    if isinstance(items, (str, dict)):
        items = [items]
    if not isinstance(items, list) or not items:
        return None
    checks = [c for i, it in enumerate(items) if (c := _coerce_override(it, i, source)) is not None]
    return checks or None


def detect_checks(ws: Path | str, settings: Any = None) -> list[Check]:
    ws = Path(ws)
    configured = override_checks(ws, settings)
    if configured:
        return configured
    checks = _python_checks(ws) + _js_checks(ws) + _other_checks(ws)
    seen: set[str] = set()
    out: list[Check] = []
    for c in checks:
        if c.name not in seen:
            seen.add(c.name)
            out.append(c)
    return out


# ─── Running ──────────────────────────────────────────────────────────────
def _popen_args(check: Check) -> tuple[list[str] | str, dict[str, Any]]:
    kwargs: dict[str, Any] = {}
    try:
        from axon import platform as plat
        kwargs.update(plat.new_process_group_kwargs())
        if isinstance(check.command, list):
            return check.command, kwargs
        return plat.shell_command_argv(check.command), kwargs
    except Exception:
        if isinstance(check.command, list):
            return check.command, kwargs
        if os.name == "nt":
            return check.command, {**kwargs, "shell": True}
        return ["/bin/sh" if os.path.exists("/bin/sh") else "sh", "-c", check.command], kwargs


def _decode(data: bytes) -> str:
    try:
        from axon.platform import decode_output
        return decode_output(data)
    except Exception:
        return (data or b"").decode("utf-8", errors="replace")


def _kill(proc: subprocess.Popen[Any]) -> None:
    try:
        from axon.platform import kill_process_tree
        kill_process_tree(proc)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_check(check: Check, ws: Path | str, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Run one check in the workspace, capturing combined output. Ctrl+C kills the process tree
    and returns an interrupted result (it is re-raised by callers that need to stop)."""
    ws = Path(ws)
    argv, kwargs = _popen_args(check)
    env = dict(os.environ)
    env.update({"CI": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1",
                "FORCE_COLOR": "0", "TERM": env.get("TERM", "dumb")})
    started = time.time()
    try:
        proc = subprocess.Popen(argv, cwd=str(ws), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, **kwargs)
    except (FileNotFoundError, PermissionError, OSError) as e:
        msg = f"could not start `{check.display}`: {e}"
        return CheckResult(check, 127, msg, time.time() - started, [Failure("", None, msg, "start")])
    timed_out = interrupted = False
    try:
        data, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        try:
            data, _ = proc.communicate(timeout=5)
        except Exception:
            data = b""
    except KeyboardInterrupt:
        interrupted = True
        _kill(proc)
        data = b""
        try:
            data, _ = proc.communicate(timeout=2)
        except BaseException:
            pass
    output = _strip_ansi(_decode(data or b"")).replace("\r\n", "\n")
    if len(output) > MAX_OUTPUT_CHARS:
        output = "[... output truncated ...]\n" + output[-MAX_OUTPUT_CHARS:]
    if timed_out:
        output += f"\n[timed out after {fmt_duration(timeout)}]"
    if interrupted:
        output += "\n[interrupted]"
    code = proc.returncode if proc.returncode is not None else -1
    res = CheckResult(check, code, output, time.time() - started, timed_out=timed_out, interrupted=interrupted)
    if not res.ok:
        res.failures = parse_failures(output, ws)
    return res


# ─── Failure parsing ──────────────────────────────────────────────────────
_CODE_EXT = (
    "py", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "vue", "svelte", "go", "rs", "cs",
    "java", "kt", "kts", "scala", "c", "h", "cc", "cpp", "hpp", "cxx", "m", "mm", "swift", "rb", "php",
    "ex", "exs", "erl", "hs", "lua", "dart", "zig", "toml", "json", "yaml", "yml", "sh", "ps1", "sql",
    "css", "scss", "html", "fs", "vb", "gradle", "xml", "proto", "ipynb", "r", "jl", "nim", "pl",
)
_PATH = r"(?P<file>(?:[A-Za-z]:)?[^\s:()\[\]\"'<>|,]*[^\s:()\[\]\"'<>|,.]\.(?:" + "|".join(_CODE_EXT) + r"))"
_RE_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<fn>.+))?')
_RE_PYTEST_FAILED = re.compile(r"^(?P<what>FAILED|ERROR)\s+(?P<file>[^\s:]+?\.py)(?:::(?P<test>\S+))?(?:\s+-\s+(?P<msg>.*))?$")
_RE_PAREN = re.compile(_PATH + r"\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s*(?P<msg>.+)$")
_RE_TSC_NEW = re.compile(r"^" + _PATH + r":(?P<line>\d+):(?P<col>\d+)\s+-\s+(?P<msg>(?:error|warning).+)$")
_RE_JS_FRAME = re.compile(r"^\s*at (?:.*? \()?(?:file://)?" + _PATH + r":(?P<line>\d+):(?P<col>\d+)\)?\s*$")
_RE_RUST_ARROW = re.compile(r"^\s*-->\s+" + _PATH + r":(?P<line>\d+):(?P<col>\d+)")
_RE_RUST_PANIC = re.compile(r"panicked at (?:'[^']*', )?" + _PATH + r":(?P<line>\d+):(?P<col>\d+)")
_RE_MAVEN = re.compile(r"^\[(?:ERROR|WARNING)\]\s+" + _PATH + r":\[(?P<line>\d+),(?P<col>\d+)\]\s*(?P<msg>.*)$")
_RE_GENERIC = re.compile(r"(?:^|\s)" + _PATH + r":(?P<line>\d+)(?::(?P<col>\d+))?(?::|\s+-\s+|\s)\s*(?P<msg>.*)$")
_RE_ERRLINE = re.compile(r"^\s*(?:Uncaught\s+)?(?:[A-Za-z_][\w.]*)?(?:Error|Exception|Failure|AssertionError|panic)\b[:!]?.*")
_RE_PY_EXC = re.compile(r"^(?:[A-Za-z_][\w.]*)(?:Error|Exception|Exit|Interrupt|Warning|Failed)\b.*|^[A-Za-z_][\w.]*: .+")
_RE_RUST_ERR = re.compile(r"^(?P<lvl>error|warning)(?:\[[A-Z0-9]+\])?:\s*(?P<msg>.+)$")
_LIB_MARKERS = ("site-packages", "dist-packages", "node_modules", "/lib/python", "\\lib\\python",
                "<frozen", "/rustc/", ".cargo/registry", "/usr/lib/", "internal/")


def normalize_path(path: str, ws: Path | None = None) -> str:
    p = path.strip().strip("\"'")
    if p.startswith("file://"):
        p = p[7:]
    p = p.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if ws is not None:
        wsn = str(ws).replace("\\", "/").rstrip("/")
        for base in {wsn, str(Path(ws).resolve()).replace("\\", "/").rstrip("/")}:
            if base and (p.lower().startswith(base.lower() + "/") if os.name == "nt" or re.match(r"^[A-Za-z]:", p) else p.startswith(base + "/")):
                return p[len(base) + 1:]
    return p


def _is_lib(path: str) -> bool:
    low = path.replace("\\", "/").lower()
    return any(m.replace("\\", "/").lower() in low for m in _LIB_MARKERS)


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except Exception:
        return None


def parse_failures(output: str, ws: Path | str | None = None) -> list[Failure]:
    """Extract file:line failures from tool output in many ecosystems (order of appearance)."""
    wsp = Path(ws) if ws is not None else None
    lines = _strip_ansi(output or "").replace("\r\n", "\n").split("\n")
    found: list[Failure] = []
    seen: set[tuple[str, int | None]] = set()
    consumed: set[int] = set()

    def add(file: str, line: Any, msg: str, kind: str, col: Any = None) -> None:
        f = normalize_path(file, wsp)
        ln = _int(line)
        key = (f.lower() if os.name == "nt" else f, ln)
        msg = (msg or "").strip()
        if not f or key in seen:
            # Enrich a previously message-less failure at the same location.
            if f and msg:
                for i, prev in enumerate(found):
                    if (prev.file, prev.line) == (f, ln) and not prev.message:
                        found[i] = Failure(prev.file, prev.line, msg[:400], prev.kind, prev.col)
            return
        seen.add(key)
        found.append(Failure(f, ln, msg[:400], kind, _int(col)))

    # Python tracebacks -> deepest project frame + exception line.
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("Traceback (most recent call last)"):
            frames: list[tuple[str, str, int]] = []
            j = i + 1
            while j < len(lines):
                m = _RE_PY_FRAME.match(lines[j])
                if m:
                    frames.append((m.group("file"), m.group("line"), j))
                    consumed.add(j)
                    j += 1
                    continue
                if lines[j].startswith((" ", "\t")) or not lines[j].strip():
                    j += 1
                    continue
                break
            exc = lines[j].strip() if j < len(lines) else ""
            if frames:
                proj = [f for f in frames if not _is_lib(f[0])] or frames
                file, line, _ = proj[-1]
                add(file, line, exc, "python")
            i = j + 1
            continue
        i += 1

    rust_msg = ""
    last_err = ""
    for idx, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip() or idx in consumed:
            continue
        m = _RE_PYTEST_FAILED.match(line.strip())
        if m:
            msg = m.group("msg") or (f"{m.group('what').lower()} in {m.group('test')}" if m.group("test") else m.group("what"))
            if m.group("test"):
                msg = f"{m.group('test')}: {msg}" if m.group("msg") else msg
            f = normalize_path(m.group("file"), wsp)
            existing = [x for x in found if x.file == f]
            if existing:
                if msg and all(msg not in x.message for x in existing):
                    k = found.index(existing[-1])
                    x = existing[-1]
                    found[k] = Failure(x.file, x.line, (x.message + " | " + msg if x.message else msg)[:400], x.kind, x.col)
            else:
                add(f, None, msg, "pytest")
            continue
        mr = _RE_RUST_ERR.match(line)
        if mr:
            rust_msg = mr.group("msg")
            last_err = line.strip()
            continue
        m = _RE_RUST_ARROW.match(line)
        if m:
            add(m.group("file"), m.group("line"), rust_msg or last_err, "rust", m.group("col"))
            rust_msg = ""
            continue
        m = _RE_RUST_PANIC.search(line)
        if m:
            nxt = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            add(m.group("file"), m.group("line"), nxt or "panicked", "rust", m.group("col"))
            continue
        m = _RE_MAVEN.match(line.strip())
        if m:
            add(m.group("file"), m.group("line"), m.group("msg"), "java", m.group("col"))
            continue
        m = _RE_PAREN.search(line)
        if m:
            fname = m.group("file")
            add(fname, m.group("line"), m.group("msg"), "csharp" if fname.endswith(".cs") else "tsc", m.group("col"))
            continue
        m = _RE_TSC_NEW.match(line.strip())
        if m:
            add(m.group("file"), m.group("line"), m.group("msg"), "tsc", m.group("col"))
            continue
        m = _RE_JS_FRAME.match(line)
        if m:
            if not _is_lib(m.group("file")):
                add(m.group("file"), m.group("line"), last_err, "js", m.group("col"))
            continue
        if _RE_ERRLINE.match(line) and ":" in line and not _RE_GENERIC.search(line):
            last_err = line.strip()
            continue
        m = _RE_GENERIC.search(line)
        if m and not _is_lib(m.group("file")):
            msg = m.group("msg").strip()
            fname = m.group("file")
            kind = "go" if fname.endswith(".go") else ("java" if fname.endswith((".java", ".kt")) else "generic")
            if kind == "generic" and re.match(r"^\d+\s+(?:passed|failed)\b", msg):
                continue
            # pytest's "tests/x.py:12: AssertionError" lines attach to an existing FAILED entry.
            add(fname, m.group("line"), msg, kind, m.group("col"))
        if len(found) >= MAX_FAILURES:
            break
    # Merge message-less pytest FAILED entries with a located entry for the same file.
    merged: list[Failure] = []
    for f in found:
        if f.line is None:
            located = [x for x in found if x.file == f.file and x.line is not None]
            if located:
                k = found.index(located[0])
                x = located[0]
                if f.message and f.message not in x.message:
                    found[k] = Failure(x.file, x.line, (f.message + (" | " + x.message if x.message else ""))[:400], x.kind, x.col)
                continue
        merged.append(f)
    # Rebuild from `found` (which may have been enriched) preserving order and dropping merged entries.
    keep = {(m.file, m.line) for m in merged}
    return [f for f in found if (f.file, f.line) in keep][:MAX_FAILURES]


# ─── Snippets & prompts ───────────────────────────────────────────────────
def resolve_file(ws: Path, file: str) -> Path | None:
    if not file:
        return None
    p = Path(file)
    cand = p if p.is_absolute() else ws / p
    try:
        if cand.is_file():
            return cand
    except Exception:
        return None
    return None


def snippet(ws: Path | str, failure: Failure, radius: int = SNIPPET_RADIUS) -> str:
    """Numbered source lines around the failure (marked with '>'); '' if unreadable."""
    if failure.line is None:
        return ""
    path = resolve_file(Path(ws), failure.file)
    if path is None:
        return ""
    try:
        if path.stat().st_size > 5_000_000:
            return ""
        src = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    lo = max(1, failure.line - radius)
    hi = min(len(src), failure.line + radius)
    width = len(str(hi))
    return "\n".join(f"{'>' if n == failure.line else ' '} {n:>{width}} | {src[n - 1]}" for n in range(lo, hi + 1))


def _tail(text: str, max_lines: int = 80, max_chars: int = 8000) -> str:
    rows = text.strip().split("\n")
    if len(rows) > max_lines:
        rows = [f"[... {len(rows) - max_lines} earlier lines omitted ...]"] + rows[-max_lines:]
    out = "\n".join(rows)
    return out[-max_chars:]


def build_fix_prompt(ws: Path | str, command: str, output: str, failures: list[Failure],
                     previous: str = "", max_snippets: int = 6) -> str:
    ws = Path(ws)
    parts = ["Fix the following failure(s). Find the root cause, make the minimal correct change, "
             "then re-run the command to confirm it passes. Do not weaken, skip or delete tests to make them pass."]
    if command:
        parts.append(f"\n## Command\n`{command}`")
    if failures:
        parts.append(f"\n## Failures ({len(failures)})")
        for n, f in enumerate(failures[:20], 1):
            parts.append(f"{n}. {f.location or '(no location)'} [{f.kind}] {f.message}".rstrip())
        shown = 0
        snippets: list[str] = []
        done: set[str] = set()
        for f in failures:
            if shown >= max_snippets:
                break
            if f.location in done:
                continue
            s = snippet(ws, f)
            if s:
                done.add(f.location)
                shown += 1
                snippets.append(f"### {f.location}\n```\n{s}\n```")
        if snippets:
            parts.append("\n## Source around the failures\n" + "\n\n".join(snippets))
    if output.strip():
        parts.append(f"\n## Output (tail)\n```\n{_tail(output)}\n```")
    if previous:
        parts.append(f"\n## Previous attempt\n{previous}")
    return "\n".join(parts)


# ─── Agent helpers (duck-typed) ───────────────────────────────────────────
def _workspace(agent: Any) -> Path:
    ws = getattr(getattr(agent, "settings", None), "workspace", None)
    return Path(ws) if ws else Path.cwd()


def ledger_total(agent: Any) -> Decimal | None:
    ledger = getattr(agent, "ledger", None)
    if ledger is None:
        return None
    try:
        return Decimal(str(ledger.total()))
    except Exception:
        try:
            return Decimal(str(getattr(ledger, "total_cost")))
        except Exception:
            return None


def cost_delta(before: Decimal | None, agent: Any) -> Decimal | None:
    after = ledger_total(agent)
    if before is None or after is None:
        return None
    return after - before


_STATE: dict[int, dict[str, Any]] = {}


def _state(agent: Any) -> dict[str, Any]:
    st = getattr(agent, "_axon_solver_state", None)
    if isinstance(st, dict):
        return st
    st = {}
    try:
        setattr(agent, "_axon_solver_state", st)
        return st
    except Exception:
        return _STATE.setdefault(id(agent), st)


def last_bash_error(agent: Any) -> tuple[str, str] | None:
    """(command, output) of the most recent failed Bash tool call in agent.conversation."""
    conv = getattr(agent, "conversation", None)
    messages = getattr(conv, "messages", None) or []
    calls: dict[str, tuple[str, str]] = {}
    last: tuple[str, str] | None = None
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if m.get("role") == "assistant":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        calls[str(b.get("id"))] = (str(b.get("name", "")), str(inp.get("command", "")) if isinstance(inp, dict) else "")
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                calls[str(tc.get("id"))] = (str(fn.get("name", "")), str(args.get("command", "")) if isinstance(args, dict) else "")
        elif m.get("role") == "tool":
            name, cmd = calls.get(str(m.get("tool_call_id")), ("", ""))
            text = content if isinstance(content, str) else json.dumps(content)
            if name.lower() in ("bash", "shell", "powershell") and _looks_failed(text, None):
                last = (cmd, text)
        elif m.get("role") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    name, cmd = calls.get(str(b.get("tool_use_id")), ("", ""))
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
                    if name.lower() in ("bash", "shell", "powershell") and _looks_failed(str(c), b.get("is_error")):
                        last = (cmd, str(c))
    return last


def _looks_failed(text: str, is_error: Any) -> bool:
    if is_error:
        return True
    m = re.search(r"(?:exit code|exit status|exited with(?: code)?)[:\s]+(-?\d+)", text, re.I)
    if m:
        return m.group(1) != "0"
    return bool(re.search(r"Traceback \(most recent call last\)|\bFAILED\b|error(?:\[\w+\])?:", text))


_KNOWN_EXES = {
    "pytest", "python", "python3", "py", "npm", "pnpm", "yarn", "npx", "bun", "deno", "node", "go", "cargo",
    "dotnet", "mvn", "gradle", "make", "ruff", "mypy", "tsc", "flake8", "eslint", "jest", "vitest", "uv",
    "poetry", "tox", "nox", "pyright", "rustc", "gcc", "g++", "clang", "javac", "java", "cmake", "ctest",
    "swift", "bundle", "rake", "rspec", "php", "composer", "mix", "elixir", "pwsh", "powershell", "bash", "sh",
}


def looks_like_command(text: str) -> bool:
    s = text.strip()
    if not s or "\n" in s:
        return False
    if re.search(r"Traceback|Error:|error\[|\.\w+:\d+", s):
        return False
    if s.startswith("& "):  # PowerShell call operator
        s = s[2:].lstrip()
    q = re.match(r'^(["\'])(.+?)\1', s)
    first = q.group(2) if q else s.split()[0].strip("\"'")
    if first.startswith(("./", ".\\")) or first in ("mvnw", "gradlew", "gradlew.bat", "mvnw.cmd"):
        return True
    base = Path(first).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base in _KNOWN_EXES or (shutil.which(first) is not None and len(s.split()) <= 12)


def _run_turn(agent: Any, prompt: str) -> Any:
    fn = getattr(agent, "run_turn", None)
    if fn is None:
        _out(f"  {_c('ERROR')}This agent cannot run turns.{_c('RST')}")
        return None
    return fn(prompt)


def _turn_failed(result: Any) -> bool:
    if result is None:
        return True
    if getattr(result, "error", None):
        return True
    return str(getattr(result, "stop_reason", "")) in ("error", "blocked", "interrupted", "budget_exceeded",
                                                       "token_budget", "max_iterations")


# ─── Rendering ────────────────────────────────────────────────────────────
def _link(ws: Path, f: Failure) -> str:
    label = f.location or "(no location)"
    path = resolve_file(ws, f.file)
    if path is None or _theme is None or not hasattr(_theme, "hyperlink"):
        return label
    try:
        return _theme.hyperlink(path.resolve().as_uri(), label)
    except Exception:
        return label


def print_results(results: list[CheckResult], ws: Path, top: int = 8) -> None:
    ok_g, bad_g = _g("✓", "OK"), _g("✗", "X")
    rows = []
    for r in results:
        mark = f"{_c('SUCCESS')}{ok_g}{_c('RST')}" if r.ok else f"{_c('ERROR')}{bad_g}{_c('RST')}"
        status = "pass" if r.ok else ("timeout" if r.timed_out else ("stopped" if r.interrupted else f"exit {r.exit_code}"))
        rows.append([mark, r.check.name, r.check.kind, status, fmt_duration(r.duration),
                     "0" if r.ok else str(r.failure_count), f"{_c('MUTED')}{r.check.display}{_c('RST')}"])
    for line in _table(["", "check", "kind", "status", "time", "fails", "command"], rows, right=(4, 5)):
        _out(line)
    failed = [r for r in results if not r.ok]
    passed = len(results) - len(failed)
    _out()
    if not failed:
        _out(f"  {_c('SUCCESS')}{_c('BOLD')}{ok_g} All {len(results)} checks passed{_c('RST')}"
             f"  {_c('MUTED')}{fmt_duration(sum(r.duration for r in results))}{_c('RST')}")
        return
    _out(f"  {_c('ERROR')}{_c('BOLD')}{bad_g} {len(failed)} failing{_c('RST')} · {passed} passing"
         f"  {_c('MUTED')}{fmt_duration(sum(r.duration for r in results))}{_c('RST')}")
    for r in failed:
        _out(f"\n  {_c('BOLD')}{r.check.name}{_c('RST')}")
        if r.failures:
            for f in r.failures[:top]:
                _out(f"    {_c('LINK')}{_link(ws, f)}{_c('RST')}  {f.message[:160]}")
            if len(r.failures) > top:
                _out(f"    {_c('MUTED')}… {len(r.failures) - top} more{_c('RST')}")
        else:
            for line in _tail(r.output, max_lines=6).split("\n"):
                _out(f"    {_c('MUTED')}{line[:200]}{_c('RST')}")
    _out(f"\n  {_c('MUTED')}Next: /fix to send these to the agent, or /autofix to loop until green.{_c('RST')}")


def run_checks(checks: list[Check], ws: Path, timeout: float = DEFAULT_TIMEOUT,
               on_start: Callable[[Check], None] | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    for c in checks:
        if on_start:
            on_start(c)
        r = run_check(c, ws, timeout)
        results.append(r)
        if r.interrupted:
            raise KeyboardInterrupt
    return results


def _announce(c: Check) -> None:
    _out(f"  {_c('INFO')}{_g('◐', '*')}{_c('RST')} running {_c('BOLD')}{c.name}{_c('RST')}  "
         f"{_c('MUTED')}{c.display}{_c('RST')}")


def _select(checks: list[Check], arg: str) -> list[Check]:
    names = [a for a in re.split(r"[\s,]+", arg.strip()) if a]
    if not names or names == ["all"]:
        return checks
    picked = [c for c in checks if any(n.lower() in (c.name.lower(), c.kind.lower()) or n.lower() in c.name.lower()
                                       for n in names)]
    return picked


def _timeout(agent: Any) -> float:
    try:
        return float(getattr(getattr(agent, "settings", None), "verify_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except Exception:
        return DEFAULT_TIMEOUT


def _no_checks_hint(ws: Path) -> None:
    _out(f"  {_c('WARNING')}No checks detected in {ws}.{_c('RST')}")
    _out(f"  {_c('MUTED')}Add them in .axon/settings.json, e.g. "
         '{"verify": ["pytest -q", {"name": "lint", "command": "ruff check .", "kind": "lint"}]}'
         f", or pass a command: /fix <command>, /autofix <command>.{_c('RST')}")


# ─── /verify ──────────────────────────────────────────────────────────────
def cmd_verify(agent: Any, arg: str) -> bool | None:
    """Run checks; returns False when a check failed (so `axon -p /verify` exits non-zero)."""
    ws = _workspace(agent)
    checks = detect_checks(ws, getattr(agent, "settings", None))
    if not checks:
        _no_checks_hint(ws)
        return
    selected = _select(checks, arg)
    if not selected:
        _out(f"  {_c('WARNING')}No check matches {arg!r}. Available: {', '.join(c.name for c in checks)}{_c('RST')}")
        return
    _out(f"\n  {_c('PRIMARY')}{_c('BOLD')}Verify{_c('RST')} · {len(selected)} check(s) in {_c('MUTED')}{ws}{_c('RST')}")
    try:
        results = run_checks(selected, ws, _timeout(agent), _announce)
    except KeyboardInterrupt:
        _out(f"  {_c('WARNING')}Stopped.{_c('RST')}")
        return
    _out()
    print_results(results, ws)
    _state(agent)["last_verify"] = results
    return all(r.ok for r in results)


# ─── /fix ─────────────────────────────────────────────────────────────────
def gather_fix_target(agent: Any, arg: str) -> tuple[str, str, list[Failure]] | None:
    """Resolve (command, output, failures) for /fix from the argument or session history."""
    ws = _workspace(agent)
    arg = arg.strip()
    if arg and looks_like_command(arg):
        _announce(Check(arg, arg, "custom", "user"))
        r = run_check(Check(arg, arg, "custom", "user"), ws, _timeout(agent))
        if r.interrupted:
            raise KeyboardInterrupt
        if r.ok:
            _out(f"  {_c('SUCCESS')}{_g('✓', 'OK')} `{arg}` passes ({fmt_duration(r.duration)}) - nothing to fix.{_c('RST')}")
            return None
        return arg, r.output, r.failures
    if arg:
        return "", arg, parse_failures(arg, ws)
    last = _state(agent).get("last_verify") or []
    failing = [r for r in last if not r.ok]
    if failing:
        # Consumed: once handed to the agent these results are stale (the files have changed), so a
        # second /fix must not resend them; /verify records fresh ones.
        _state(agent).pop("last_verify", None)
        output = "\n\n".join(f"$ {r.check.display}\n{_tail(r.output, 60)}" for r in failing)
        fails = [f for r in failing for f in r.failures]
        return " && ".join(r.check.display for r in failing), output, fails
    bash = last_bash_error(agent)
    if bash:
        cmd, out = bash
        return cmd, out, parse_failures(out, ws)
    return None


def cmd_fix(agent: Any, arg: str) -> None:
    ws = _workspace(agent)
    try:
        target = gather_fix_target(agent, arg)
    except KeyboardInterrupt:
        _out(f"  {_c('WARNING')}Stopped.{_c('RST')}")
        return
    if target is None:
        if not arg.strip():
            _out(f"  {_c('MUTED')}Nothing to fix: no failing /verify result or failed Bash command in this session.\n"
                 f"  Usage: /fix <command>  or  /fix <pasted error text>{_c('RST')}")
        return
    command, output, failures = target
    _out(f"\n  {_c('PRIMARY')}{_c('BOLD')}Error Doctor{_c('RST')} · {len(failures)} located failure(s)"
         + (f" from `{command}`" if command else ""))
    for f in failures[:8]:
        _out(f"    {_c('LINK')}{_link(ws, f)}{_c('RST')}  {f.message[:140]}")
    _out(f"  {_c('MUTED')}→ handing to the agent with source snippets{_c('RST')}\n")
    _run_turn(agent, build_fix_prompt(ws, command, output, failures))


# ─── /autofix ─────────────────────────────────────────────────────────────
def _parse_autofix_args(arg: str) -> tuple[str, int]:
    max_n = 3
    m = re.search(r"--max(?:=|\s+)(\d+)", arg)
    if m:
        max_n = max(1, min(20, int(m.group(1))))
        arg = (arg[: m.start()] + arg[m.end():]).strip()
    return arg.strip(), max_n


def _count(results: list[CheckResult]) -> int:
    return sum(r.failure_count for r in results)


def _diff_failures(before: list[Failure], after: list[Failure]) -> str:
    b = {f.location + f.message[:60] for f in before}
    a = {f.location + f.message[:60] for f in after}
    fixed, new = len(b - a), [f for f in after if f.location + f.message[:60] not in b]
    lines = [f"Fixed {fixed}, still failing {len(a & b)}, new {len(new)}."]
    for f in new[:5]:
        lines.append(f"- new: {f.location} {f.message[:160]}")
    return "\n".join(lines)


def _git_stat(ws: Path) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    try:  # also works when the workspace is a subdirectory of the repository
        res = subprocess.run([git, "diff", "--stat"], cwd=str(ws), capture_output=True, timeout=10,
                             stdin=subprocess.DEVNULL)
        return _decode(res.stdout).strip()[-2000:] if res.returncode == 0 else ""
    except Exception:
        return ""


def autofix(agent: Any, checks: list[Check], ws: Path, max_attempts: int = 3,
            timeout: float = DEFAULT_TIMEOUT, sink: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Verify-and-fix loop. Returns iteration rows (attempt, before, after, cost, time, status).
    Rows are appended to `sink` as they complete, so a Ctrl+C still leaves a partial table."""
    rows: list[dict[str, Any]] = sink if sink is not None else []
    results = run_checks(checks, ws, timeout, _announce)
    _state(agent)["last_verify"] = results
    before = _count(results)
    if before == 0:
        return rows
    stall = 0
    prev_note = ""
    for attempt in range(1, max_attempts + 1):
        failing = [r for r in results if not r.ok]
        failures = [f for r in failing for f in r.failures]
        output = "\n\n".join(f"$ {r.check.display}\n{_tail(r.output, 60)}" for r in failing)
        command = " && ".join(r.check.display for r in failing)
        _out(f"\n  {_c('ACCENT')}{_c('BOLD')}Attempt {attempt}/{max_attempts}{_c('RST')} · {before} failure(s) → agent")
        cost0 = ledger_total(agent)
        t0 = time.time()
        tr = _run_turn(agent, build_fix_prompt(ws, command, output, failures, previous=prev_note))
        cost = cost_delta(cost0, agent)
        if _turn_failed(tr):
            rows.append({"attempt": attempt, "before": before, "after": None, "cost": cost,
                         "time": time.time() - t0, "status": "agent stopped"})
            break
        results = run_checks(checks, ws, timeout, _announce)
        _state(agent)["last_verify"] = results  # /fix afterwards works on what is failing now
        after = _count(results)
        new_failures = [f for r in results if not r.ok for f in r.failures]
        rows.append({"attempt": attempt, "before": before, "after": after, "cost": cost,
                     "time": time.time() - t0, "status": "pass" if after == 0 else "fail"})
        if after == 0:
            break
        stall = stall + 1 if after >= before else 0
        if stall >= 2:
            rows[-1]["status"] = "no progress"
            break
        stat = _git_stat(ws)
        prev_note = (f"Attempt {attempt} changed failures {before} → {after}. {_diff_failures(failures, new_failures)}"
                     + (f"\nWorking tree changes so far (git diff --stat):\n```\n{stat}\n```" if stat else "")
                     + ("\nThat approach did not reduce failures; try a different one." if after >= before else ""))
        before = after
    return rows


def print_iterations(rows: list[dict[str, Any]]) -> None:
    arrow = _g("→", "->")
    table = []
    for r in rows:
        after = "-" if r["after"] is None else str(r["after"])
        st = r["status"]
        color = _c("SUCCESS") if st == "pass" else _c("ERROR") if st in ("fail", "no progress", "agent stopped") else ""
        table.append([str(r["attempt"]), f"{r['before']} {arrow} {after}",
                      fmt_cost(r["cost"]) if r["cost"] is not None else "-", fmt_duration(r["time"]),
                      f"{color}{st}{_c('RST')}"])
    for line in _table(["#", "failures", "cost", "time", "result"], table, right=(2, 3)):
        _out(line)


def cmd_autofix(agent: Any, arg: str) -> None:
    ws = _workspace(agent)
    command, max_n = _parse_autofix_args(arg)
    if command:
        checks = [Check(command, command, "custom", "user")]
    else:
        checks = detect_checks(ws, getattr(agent, "settings", None))
        if not checks:
            _no_checks_hint(ws)
            return
    _out(f"\n  {_c('PRIMARY')}{_c('BOLD')}Autofix{_c('RST')} · up to {max_n} attempt(s) · "
         f"{', '.join(c.name for c in checks)}")
    rows: list[dict[str, Any]] = []
    started = time.time()
    total0 = ledger_total(agent)
    interrupted = False
    try:
        rows = autofix(agent, checks, ws, max_n, _timeout(agent), sink=rows)
    except KeyboardInterrupt:
        interrupted = True
        _out(f"\n  {_c('WARNING')}Stopped by Ctrl+C.{_c('RST')}")
    _out()
    if not rows:
        if not interrupted:
            _out(f"  {_c('SUCCESS')}{_g('✓', 'OK')} Already passing - nothing to fix.{_c('RST')}")
        return
    print_iterations(rows)
    final = rows[-1]
    total = cost_delta(total0, agent)
    summary = f"{fmt_duration(time.time() - started)}" + (f" · {fmt_cost(total)}" if total is not None else "")
    if final["status"] == "pass":
        _out(f"\n  {_c('SUCCESS')}{_c('BOLD')}{_g('✓', 'OK')} Green after {final['attempt']} attempt(s){_c('RST')} · {summary}")
    else:
        why = {"no progress": "failures stopped decreasing", "agent stopped": "the agent turn did not complete"}.get(
            final["status"], f"reached --max {max_n}")
        _out(f"\n  {_c('ERROR')}{_c('BOLD')}{_g('✗', 'X')} Still failing ({why}){_c('RST')} · {summary}"
             f"\n  {_c('MUTED')}Run /verify to see the remaining failures.{_c('RST')}")


COMMANDS = [
    FeatureCommand(name="/verify", handler=cmd_verify, category="Solve",
                   description="Run the project's tests/lint/typecheck and show failures with file:line",
                   usage="/verify [check names|all]"),
    FeatureCommand(name="/fix", handler=cmd_fix, category="Solve",
                   description="Error Doctor: run a command or parse pasted errors and have the agent fix them",
                   usage="/fix [command | pasted error]"),
    FeatureCommand(name="/autofix", handler=cmd_autofix, category="Solve",
                   description="Verify-and-fix loop until checks pass (default 3 attempts)",
                   usage="/autofix [command] [--max N]"),
]

__all__ = [
    "Check", "CheckResult", "Failure", "COMMANDS", "detect_checks", "run_check", "run_checks",
    "parse_failures", "snippet", "build_fix_prompt", "autofix", "looks_like_command", "last_bash_error",
    "ledger_total", "cost_delta", "fmt_cost", "fmt_duration", "normalize_path",
]
