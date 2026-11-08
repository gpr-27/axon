"""`axon --version` must stay fast: importing the package root must not pull in the agent or the provider SDKs."""
import subprocess
import sys


def _run(code: str) -> str:
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    return out.stdout.strip()


def test_package_root_import_is_light():
    out = _run("import sys, axon; print(axon.__version__, 'axon.agent' in sys.modules, 'anthropic' in sys.modules)")
    assert out.endswith("False False")


def test_public_names_still_resolve():
    out = _run("from axon import Agent, TurnResult, Settings, AxonError, ToolError, PermissionDenied; "
               "import axon; print(Agent.__name__, axon.Settings.__name__, 'Agent' in dir(axon))")
    assert out == "Agent Settings True"


def test_version_command_does_not_load_the_anthropic_sdk():
    out = _run("import sys; sys.argv=['axon','--version']\n"
               "from axon.cli import main\n"
               "try:\n    main()\nexcept SystemExit:\n    pass\n"
               "print('anthropic' in sys.modules, 'axon.agent.loop' in sys.modules)")
    assert out.splitlines()[-1] == "False False"

