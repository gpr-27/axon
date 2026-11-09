#!/usr/bin/env python3
"""Build, check and publish axon-terminal to PyPI, then tag and push the release.

    python scripts/release.py --dry-run          # every check and the build, nothing uploaded, tagged or pushed
    PYPI_TOKEN=pypi-... python scripts/release.py   # the real release
    python scripts/release.py --testpypi         # upload to test.pypi.org instead (token from TEST_PYPI_TOKEN)

The version comes from ``src/axon/__init__.py``. Bump it, the CHANGELOG heading (``## <version>``) and the install
pins in README.md and docs/windows.md first (see the ``release: x.y.z`` commits), and commit. The script then:

  1. checks you are on main with no uncommitted changes to tracked files, the version is new on PyPI and has no tag,
     the CHANGELOG has a heading for it and the README pins match;
  2. runs ruff and the docs check (``--full-tests`` runs the whole test suite too, about 25 minutes);
  3. builds the sdist and wheel, runs ``twine check --strict``, installs the wheel in a clean virtual environment
     and runs ``axon --version``;
  4. uploads to PyPI, tags ``v<version>`` and pushes main and the tag (the tag starts .github/workflows/release.yml,
     which attaches the files to a GitHub Release).

The token is read from the environment (PYPI_TOKEN, or TWINE_PASSWORD) or asked for with a hidden prompt. It is never
taken from a command-line argument, never written to a file and never printed. Create one at
https://pypi.org/manage/account/token/ (scope it to the axon-terminal project) and revoke it when you are done.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "axon-terminal"
DIST = ROOT / "dist"


def say(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def die(msg: str) -> "None":
    print(f"\nerror: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    print("   $ " + " ".join(cmd), flush=True)
    done = subprocess.run(cmd, cwd=str(cwd), env=env, text=True)
    if check and done.returncode != 0:
        die(f"command failed ({done.returncode}): {' '.join(cmd)}")
    return done


def out(cmd: list[str]) -> str:
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True).stdout.strip()


def version() -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', (ROOT / "src" / "axon" / "__init__.py").read_text(encoding="utf-8"), re.M)
    if not m:
        die("could not read __version__ from src/axon/__init__.py")
    return m.group(1)


def on_pypi(index: str, ver: str) -> bool:
    url = f"https://{index}/pypi/{PROJECT}/{ver}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status == 200 and bool(json.load(r).get("info"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        die(f"could not ask {index} about {ver}: HTTP {e.code}")
    except OSError as e:
        die(f"could not reach {index}: {e}")
    return False


def preflight(ver: str, index: str, allow_branch: bool) -> None:
    say(f"Checking the repository for {PROJECT} {ver}")
    branch = out(["git", "branch", "--show-current"])
    if branch != "main" and not allow_branch:
        die(f"on branch '{branch}', not main (pass --allow-branch to release from here)")
    dirty = [ln for ln in out(["git", "status", "--porcelain", "--untracked-files=no"]).splitlines() if ln.strip()]
    if dirty:
        die("uncommitted changes to tracked files:\n  " + "\n  ".join(dirty) + "\ncommit them first")
    if out(["git", "tag", "--list", f"v{ver}"]):
        die(f"tag v{ver} already exists")
    log = (ROOT / "src" / "axon" / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(ver)}\s*$", log, re.M):
        die(f"src/axon/CHANGELOG.md has no '## {ver}' heading")
    for name in ("README.md", "docs/windows.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        pins = set(re.findall(rf"{PROJECT}==([0-9][^\s`]*)", text))
        if pins and pins != {ver}:
            die(f"{name} pins {sorted(pins)}, expected {ver}")
    if on_pypi(index, ver):
        die(f"{PROJECT} {ver} is already on {index}: bump the version")
    print("   ok")


def checks(full: bool) -> None:
    say("Lint and docs")
    py = sys.executable
    ruff = shutil.which("ruff")
    run([ruff, "check", "src", "tests"] if ruff else [py, "-m", "ruff", "check", "src", "tests"])
    run([py, "scripts/gen_docs.py", "--check"])
    if full:
        say("Full test suite (about 25 minutes)")
        run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"])


def build(ver: str) -> list[Path]:
    say("Building")
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob(f"axon_terminal-{ver}*"):
        old.unlink()
    if shutil.which("uv"):
        run(["uv", "build", "--out-dir", str(DIST)])
    else:
        run([sys.executable, "-m", "build", "--outdir", str(DIST)])
    files = sorted(DIST.glob(f"axon_terminal-{ver}*"))
    if len(files) < 2 or not any(f.suffix == ".whl" for f in files):
        die(f"expected a wheel and an sdist for {ver} in dist/, found: {[f.name for f in files]}")
    return files


def twine_cmd() -> list[str]:
    """The first way of running twine that works here: this Python's module, uvx, then a twine on PATH."""
    candidates = [[sys.executable, "-m", "twine"]]
    if shutil.which("uvx"):
        candidates.append([shutil.which("uvx"), "twine"])
    if shutil.which("twine"):
        candidates.append([shutil.which("twine")])
    for cmd in candidates:
        try:
            if subprocess.run([*cmd, "--version"], capture_output=True, timeout=120).returncode == 0:
                return cmd
        except (OSError, subprocess.SubprocessError):
            continue
    die("twine does not run here: install it (pip install twine) or install uv (then uvx twine is used)")
    return []


def verify(files: list[Path], ver: str) -> None:
    say("Checking the package")
    run([*twine_cmd(), "check", "--strict", *map(str, files)])
    say("Installing the wheel in a clean environment")
    wheel = next(f for f in files if f.suffix == ".whl")
    with tempfile.TemporaryDirectory(prefix="axon-release-") as tmp:
        venv = Path(tmp) / "venv"
        run([sys.executable, "-m", "venv", str(venv)])
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        run([str(bindir / "python"), "-m", "pip", "install", "--quiet", str(wheel)])
        shown = subprocess.run([str(bindir / "axon"), "--version"], capture_output=True, text=True)
        print("   " + (shown.stdout or shown.stderr).strip())
        if ver not in (shown.stdout + shown.stderr):
            die(f"the installed wheel reports '{(shown.stdout + shown.stderr).strip()}', expected {ver}")
        run([str(bindir / "axon"), "--help"], check=True)


def token(testpypi: bool) -> str:
    names = ("TEST_PYPI_TOKEN",) if testpypi else ("PYPI_TOKEN", "TWINE_PASSWORD")
    for name in names:
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    if not sys.stdin.isatty():
        die(f"no token: set {names[0]} in the environment")
    return getpass.getpass(f"{'TestPyPI' if testpypi else 'PyPI'} API token (hidden): ").strip()


def upload(files: list[Path], testpypi: bool) -> None:
    secret = token(testpypi)
    if not secret.startswith("pypi-"):
        die("that does not look like a PyPI API token (it starts with 'pypi-')")
    say("Uploading to " + ("test.pypi.org" if testpypi else "pypi.org"))
    env = {**os.environ, "TWINE_USERNAME": "__token__", "TWINE_PASSWORD": secret, "TWINE_NON_INTERACTIVE": "1"}
    cmd = [*twine_cmd(), "upload", "--non-interactive"]
    if testpypi:
        cmd += ["--repository-url", "https://test.pypi.org/legacy/"]
    run([*cmd, *map(str, files)], env=env)


def publish_git(ver: str) -> None:
    say(f"Tagging v{ver} and pushing")
    run(["git", "tag", "-a", f"v{ver}", "-m", f"release: {ver}"])
    run(["git", "push", "origin", "HEAD", f"v{ver}"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="run every check and the build, but upload, tag and push nothing")
    ap.add_argument("--testpypi", action="store_true", help="upload to test.pypi.org (no tag, no push)")
    ap.add_argument("--full-tests", action="store_true", help="also run the whole test suite (about 25 minutes)")
    ap.add_argument("--no-tag", action="store_true", help="upload, but do not tag or push")
    ap.add_argument("--allow-branch", action="store_true", help="allow a release from a branch other than main")
    args = ap.parse_args()

    os.chdir(ROOT)
    ver = version()
    index = "test.pypi.org" if args.testpypi else "pypi.org"
    preflight(ver, index, args.allow_branch)
    checks(args.full_tests)
    files = build(ver)
    verify(files, ver)
    if args.dry_run:
        say(f"Dry run finished: {PROJECT} {ver} is ready. Nothing was uploaded, tagged or pushed.")
        return
    upload(files, args.testpypi)
    if not args.testpypi and not args.no_tag:
        publish_git(ver)
    say(f"Done: https://{index}/project/{PROJECT}/{ver}/")
    print("   On your machine:  uv tool upgrade axon-terminal   (or: uv tool install --force " + f"{PROJECT}=={ver})")


if __name__ == "__main__":
    main()
