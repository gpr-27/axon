"""
Bundled prompt skills that mirror Claude Code's terminal bundled skills and are not built-in
commands: ``/batch``, ``/update-config``, ``/fewer-permission-prompts``, ``/run`` and
``/run-skill-generator``. (``/debug``, ``/verify``, ``/code-review``, ``/simplify``, ``/loop`` ...
exist elsewhere.) A user skill with the same name replaces a bundled one.

Cloud / desktop / account-only bundled skills (``/claude-in-chrome``, ``/design``, ``/slides``,
``/artifact-*``, ``/design-sync``, ``/claude-api``, ``/workflow-authoring``) are intentionally absent.
"""
from __future__ import annotations

from typing import Any


def bundled_extra() -> list[tuple[str, str, str, dict[str, Any]]]:
    """(name, description, instructions, extra Skill fields)."""
    return [
        (
            "batch",
            "Orchestrate a large change across the codebase in parallel: research, decompose into 5-30 independent units, plan, then one isolated subagent per unit.",
            "Orchestrate the large-scale change described in the arguments.\n\n"
            "1. Research: explore the codebase (Glob, Grep, Read) until you understand every place the change touches.\n"
            "2. Decompose the work into 5 to 30 independent units that can be done in parallel without editing the same files. "
            "If the arguments are missing or the change is small, say so and stop.\n"
            "3. Present the plan (a numbered list: unit, files, acceptance check) and WAIT for the user to approve it.\n"
            "4. Once approved, spawn one subagent per unit with the `Task` tool, all in the same message so they run in "
            "parallel. Each prompt must be self-contained: the goal, the exact files, the conventions to follow, how to "
            "run the tests for that unit, and to report a short summary of what changed.\n"
            "5. Collect the results, list any unit that failed or conflicted, re-run or fix those, run the full test suite, "
            "and finish with a table of units and their status.\n\n"
            "Never start step 4 before the user has approved the plan.",
            {"disable_model_invocation": True, "argument_hint": "<instruction>"},
        ),
        (
            "update-config",
            "Edit Axon / Claude Code settings.json (permissions, env vars, hooks, output style, model) from a plain-language request.",
            "The user wants a settings change: $ARGUMENTS\n\n"
            "Settings files, lowest to highest precedence: `~/.axon/settings.json` (user), `.axon/settings.json` "
            "(project, shareable), `.axon/settings.local.json` (local, git-ignored). The `.claude/` equivalents are read too.\n"
            "1. Read the target file first (create it as `{}` if missing) and keep every existing key.\n"
            "2. Pick the narrowest sensible scope; use local for personal preferences, project for team rules.\n"
            "3. Use the documented keys: `permissions.allow|ask|deny` (rules like `Bash(git status *)`), `env`, `hooks`, "
            "`model`, `outputStyle`, `enabledPlugins`, `skillOverrides`, `disableBundledSkills`. "
            "Automated behaviours (\"every time X, do Y\") belong in a hook, not in memory.\n"
            "4. Write valid JSON (Edit or Write), then read it back and tell the user exactly what changed and where.\n"
            "For theme / model pickers suggest /config or /model instead.",
            {"argument_hint": "[request]"},
        ),
        (
            "fewer-permission-prompts",
            "Scan session transcripts for common read-only Bash and MCP calls and add a prioritised allowlist to project settings.",
            "Reduce permission prompts for this project.\n\n"
            "1. Find recent sessions for this workspace (`~/.axon/projects/<project>/sessions/*.jsonl`, or use /history) and "
            "extract the `Bash` commands and MCP tool calls that were approved by the user.\n"
            "2. Count them, keep only read-only, side-effect free ones (git status/diff/log, ls, cat, grep, rg, test runners "
            "in dry/list mode, `--version`, MCP get/list/search tools). Never include rm, mv, curl|sh, git push, installs, or anything that writes.\n"
            "3. Generalise to prefix rules, e.g. `Bash(git diff *)`, `Bash(rg *)`, `mcp__server__list_*`, most frequent first.\n"
            "4. Show the proposed list with counts, ask the user to confirm, then merge it into `permissions.allow` of "
            "`.axon/settings.json` without removing existing entries, and report what was added.",
            {"disable_model_invocation": True},
        ),
        (
            "run",
            "Launch and drive this project's app to see a change working, not only passing tests.",
            "Run the project's app and exercise the change.\n\n"
            "1. Look for a project skill or script that already launches the app (README, Makefile, package.json scripts, "
            "pyproject scripts, docker-compose, `.axon/skills`). Prefer it.\n"
            "2. Otherwise start the app for its type: CLI (run it with representative arguments), server (start it in the "
            "background with Bash, wait for the port, hit it with curl), TUI (run it in a pty / tmux and send keys), "
            "browser app (start the dev server and drive it with the available browser tool), library (write and run a "
            "small script that imports it).\n"
            "3. Observe the actual behaviour related to $ARGUMENTS, capture the output or response, and compare it with what "
            "the change should do.\n"
            "4. Stop any background process you started and report what you ran and saw.",
            {"argument_hint": "[what to check]"},
        ),
        (
            "run-skill-generator",
            "Teach /run and /verify how to build, launch and drive this project's app by writing a per-project skill.",
            "Write a project skill that tells future sessions exactly how to build, launch and drive this app.\n\n"
            "1. Inspect the repository (README, build files, scripts, ports, env vars, seed data) and actually try to build "
            "and launch it from a clean state, fixing your notes as you hit problems.\n"
            "2. Create `.axon/skills/run-app/SKILL.md` with frontmatter (`name: run-app`, a `description` saying when to "
            "use it) and the verified steps: install, build, start, readiness check, how to drive it (commands, URLs, keys), "
            "how to tear it down, and known pitfalls.\n"
            "3. Run the skill's steps once more end to end to prove they work, then tell the user where the file is.",
            {"disable_model_invocation": True},
        ),
    ]
