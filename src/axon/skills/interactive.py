"""
/skills: list, show, create, edit, delete, install (from GitHub) and reload skills.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from axon.storage import axon_home as _axon_home
from axon.skills.manager import Skill, SkillManager
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, DIM, GOLD, MINT, RST, SLATE, TEAL, WHITE

TEMPLATES = {
    "workflow": (
        "General Workflow",
        """---
name: {name}
description: Custom automated workflow for {name}
---

# {title} Skill

## Objective
Execute the standard {name} process with precision and validation.

## Dynamic Context
!`git status --short`

## Step-by-Step Instructions
1. Review the relevant files and configurations.
2. Execute the required modifications or actions.
3. Validate output and report a structured summary.
""",
    ),
    "tester": (
        "Automated Testing & QA",
        """---
name: {name}
description: Automated test execution and QA validation for {name}
---

# {title} QA Skill

## Objective
Run unit tests, verify edge cases, and inspect test coverage.

## Dynamic Context
!`pytest --tb=short`

## Step-by-Step Instructions
1. Identify target modules requiring test coverage.
2. Generate parametrized test functions with mock fixtures.
3. Run test runner, verify 100% pass rate, and document coverage.
""",
    ),
    "reviewer": (
        "Code Review & Security Audit",
        """---
name: {name}
description: Static code review and security audit for {name}
---

# {title} Reviewer Skill

## Objective
Review modified code for logic errors, type safety, and OWASP vulnerabilities.

## Dynamic Context
!`git diff HEAD~1`

## Step-by-Step Instructions
1. Inspect git diff for security vulnerabilities or performance antipatterns.
2. Check error handling, input sanitization, and resource cleanup.
3. Provide actionable suggestions in a clear markdown table.
""",
    ),
    "api": (
        "API Integration & Probing",
        """---
name: {name}
description: API integration and schema verification for {name}
---

# {title} API Skill

## Objective
Probe HTTP endpoints, validate JSON payloads, and verify response contracts.

## Dynamic Context
!`curl -s http://localhost:8000/health || echo 'Service offline'`

## Step-by-Step Instructions
1. Check endpoint availability and latency.
2. Send test payloads across positive and negative scenarios.
3. Assert status codes, headers, and response schemas.
""",
    ),
}


_SCOPE_TITLES = {
    "project": (MINT, "Project skills (.axon/skills, .claude/skills)"),
    "personal": (CYAN, "Personal skills (~/.axon/skills)"),
    "plugin": (GOLD, "Plugin skills"),
    "bundled": (TEAL, "Built-in skills"),
}

USAGE = """  /skills                              list skills
  /skills show <name>                  instructions and allowed tools
  /skills create <name> [template] [--global]   templates: workflow, tester, reviewer, api
  /skills install <github-url|owner/repo/path> [--global] [--force]
  /skills edit <name>                  open in $EDITOR (built-ins are copied to the project first)
  /skills delete <name>                remove a project/personal skill
  /skills reload                       rescan skill folders
  /skills enable|disable <name> [--project]    switch a skill on or off (saved; the model stops seeing it)
  /skills test <name>                  check that the skill file is valid
  /skills scope <name> user|project    move a skill to another scope
  /skills status                       every connector, skill and plugin: enabled, connected, errors
  Run a skill with /<name> [arguments] or /skill:<name> [arguments]."""


def _print_list(mgr: SkillManager) -> None:
    skills = sorted(mgr.skills.values(), key=lambda s: s.name)
    print(f"\n  {GOLD}{BOLD}Skills{RST}  {SLATE}{len(skills)} available · run with /<name> [args]{RST}")
    print(f"  {DARK_SLATE}{'─' * 60}{RST}")
    try:
        from axon.commands.registry import resolve_builtin
    except Exception:
        def resolve_builtin(_n: str) -> None:  # type: ignore[misc]
            return None
    for scope in ("project", "personal", "plugin", "bundled"):
        group = [s for s in skills if s.scope == scope]
        if not group:
            continue
        color, title = _SCOPE_TITLES[scope]
        print(f"\n  {color}{BOLD}{title}{RST}")
        for s in group:
            shadow = f" {GOLD}(use /skill:{s.name}){RST}" if resolve_builtin(s.name) else ""
            tools = f" {DARK_SLATE}[{', '.join(s.allowed_tools)}]{RST}" if s.allowed_tools else ""
            from axon.commands.common import clip
            print(f"    {WHITE}/{s.name:<20}{RST} {SLATE}{clip(s.description or '', 64)}{RST}{tools}{shadow}")
    problems = list(getattr(mgr, "problems", []) or [])
    if problems:
        print(f"\n  {GOLD}{BOLD}Problems{RST}")
        for p in problems:
            print(f"    {SLATE}{p}{RST}")
    off = sorted(mgr.disabled)
    if off:
        print(f"\n  {SLATE}{BOLD}Disabled{RST}  {SLATE}{', '.join(off)}  (turn on: /skills enable <name>){RST}")
    print(f"\n  {DIM}/skills show|create|install|edit|delete|enable|disable <name>{RST}\n")


def _run_control(skills_mgr: SkillManager, workspace: Path, sub: str, words: list[str], flags: set[str]) -> None:
    """enable / disable / test / scope / status share their code with /marketplace."""
    from axon.marketplace.cli import run_command
    tokens = [sub, *(w.lstrip("/") for w in words), "--tab", "skills"]
    if "--project" in flags:
        tokens += ["--scope", "project"]
    run_command(tokens, workspace)
    skills_mgr.discover()


def find_skill(skills_mgr: SkillManager, name: str) -> Skill | None:
    """A skill by name from every source the manager reads (enabled or switched off)."""
    skill = skills_mgr.get(name)
    if skill is not None:
        return skill
    for key, off in getattr(skills_mgr, "disabled", {}).items():
        if key.lower() == name.lower():
            return off
    return None


def test_skill(skills_mgr: SkillManager, name: str) -> bool:
    """Validate one skill wherever it lives (personal, ~/.claude, project, plugin, bundled):
    print its file, size, description, triggers and any problems, then OK or the problems."""
    from axon.commands.common import err, ok
    skill = find_skill(skills_mgr, name)
    if skill is None:
        err(f"Skill '{name}' not found. /skills lists the ones Axon can see.")
        return False
    print(f"\n  {GOLD}{BOLD}Testing skill /{skill.name}{RST}  {SLATE}{skill.scope}{RST}")
    problems: list[str] = []
    path = Path(skill.path)
    if skill.scope == "bundled" or not path.is_file():
        print(f"    {SLATE}file{RST}         built into Axon" if skill.scope == "bundled" else f"    {SLATE}file{RST}         {path} (missing)")
        if skill.scope != "bundled":
            problems.append(f"{path} does not exist")
    else:
        from axon.marketplace.skill_tools import validate_skill_file
        print(f"    {SLATE}file{RST}         {path}")
        print(f"    {SLATE}size{RST}         {path.stat().st_size:,} bytes")
        problems += validate_skill_file(path)
    print(f"    {SLATE}description{RST}  {skill.description[:100] or '(none)'}")
    triggers = ", ".join(skill.triggers) if skill.triggers else "(none: the description decides)"
    print(f"    {SLATE}triggers{RST}     {triggers}")
    if not skill.description.strip():
        problems.append("no description, so the model cannot tell when to use it")
    if problems:
        err(f"Skill '{skill.name}' has {len(problems)} problem(s):")
        for problem in problems:
            print(f"      - {problem}")
        print()
        return False
    ok(f"OK: skill '{skill.name}' is valid.")
    print()
    return True


def handle_skills_command(skills_mgr: SkillManager, workspace: Path, arg: str) -> None:
    words = arg.split()
    flags = {w for w in words if w.startswith("--")}
    parts = [w for w in words if not w.startswith("--")]
    sub = parts[0].lower() if parts else "list"
    name = parts[1].lstrip("/") if len(parts) > 1 else ""
    from axon.commands.common import confirm, err, info, is_tty, ok, open_in_editor, usage

    if sub in ("list", "ls"):
        _print_list(skills_mgr)
        return

    if sub == "reload":
        skills_mgr.discover()
        ok(f"Reloaded {len(skills_mgr.skills)} skills")
        return

    if sub == "test":
        if not name:
            usage("/skills test <name>")
            return
        test_skill(skills_mgr, name)
        return

    if sub in ("enable", "disable") and name:
        # every skill Axon can see (also ~/.claude/skills), not only the marketplace folders
        from axon.marketplace import control
        result = control.set_skill_enabled(workspace, name, sub == "enable",
                                           "project" if "--project" in flags else "user")
        (ok if result.ok else err)(result.message)
        skills_mgr.discover()
        return

    if sub in ("enable", "disable", "scope", "status"):
        _run_control(skills_mgr, workspace, sub, parts[1:], flags)
        return

    if sub in ("show", "info", "inspect"):
        skill = skills_mgr.get(name) if name else None
        if skill is None:
            err(f"Skill '{name}' not found.")
            return
        print(f"\n  {GOLD}{BOLD}/{skill.name}{RST}  {SLATE}{skill.scope} · {skill.path}{RST}")
        print(f"  {WHITE}{skill.description}{RST}")
        if skill.allowed_tools:
            print(f"  {SLATE}Allowed tools:{RST} {', '.join(skill.allowed_tools)}")
        if skill.argument_hint:
            print(f"  {SLATE}Arguments:{RST} {skill.argument_hint}")
        if skill.model:
            print(f"  {SLATE}Model:{RST} {skill.model}")
        print(f"  {DARK_SLATE}{'─' * 60}{RST}")
        for ln in skill.instructions.splitlines():
            print(f"  {ln}")
        print()
        return

    if sub in ("create", "new"):
        if not name:
            usage("/skills create <name> [workflow|tester|reviewer|api] [--global]")
            return
        from axon.skills.importer import sanitize_skill_name
        clean = sanitize_skill_name(name)
        if not clean:
            err("Skill names may contain letters, digits, - and _.")
            return
        tmpl = parts[2].lower() if len(parts) > 2 and parts[2].lower() in TEMPLATES else "workflow"
        root = (_axon_home() / "skills") if "--global" in flags else (workspace / ".axon" / "skills")
        skill_file = root / clean / "SKILL.md"
        if skill_file.exists():
            err(f"Skill already exists: {skill_file}")
            return
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        title = clean.replace("-", " ").replace("_", " ").title()
        skill_file.write_text(TEMPLATES[tmpl][1].format(name=clean, title=title), encoding="utf-8")
        from axon.session.library import record_saved
        record_saved(skill_file, action="created", source="/skills create", workspace=workspace)
        skills_mgr.discover()
        ok(f"Created skill {BOLD}/{clean}{RST} {SLATE}({tmpl}) → {skill_file}{RST}")
        return

    if sub in ("edit", "modify"):
        skill = skills_mgr.get(name) if name else None
        if skill is None:
            err(f"Skill '{name}' not found.")
            return
        path = skill.path
        if skill.scope == "bundled":
            path = workspace / ".axon" / "skills" / skill.name / "SKILL.md"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n{skill.instructions}\n", encoding="utf-8")
                info(f"Copied the built-in skill to {path} (it now overrides the built-in).")
        if is_tty():
            open_in_editor(path)
            from axon.session.library import record_saved
            record_saved(path, action="edited", source="/skills edit", workspace=workspace)
        else:
            info(f"Edit {path} in your editor.")
        skills_mgr.discover()
        return

    if sub in ("delete", "rm", "remove"):
        skill = skills_mgr.get(name) if name else None
        if skill is None:
            err(f"Skill '{name}' not found.")
            return
        if skill.scope in ("bundled", "plugin"):
            err(f"'{name}' is a {skill.scope} skill and cannot be deleted here.")
            return
        if not confirm(f"Delete {skill.path.parent}?", default=not is_tty()):
            info("Cancelled.")
            return
        try:
            from axon.session.library import record_saved
            record_saved(skill.path, action="deleted", source="/skills delete", workspace=workspace)  # keep a last copy
        except Exception:
            pass
        try:
            roots = {r.resolve() for r, _ in skills_mgr.skill_roots()}
            folder = skill.path.parent.resolve()
            if skill.path.name == "SKILL.md" and folder.parent in roots:
                shutil.rmtree(folder)  # a skill folder directly under a skills root
            else:
                skill.path.unlink()
        except OSError as e:
            err(f"Failed to delete: {e}")
            return
        skills_mgr.discover()
        ok(f"Deleted skill /{skill.name}")
        return

    if sub in ("install", "import", "add"):
        if not name:
            usage("/skills install <github-url | owner/repo/path> [--global] [--force]")
            return
        from axon.skills.importer import import_skill_from_url
        root = (_axon_home() / "skills") if "--global" in flags else None
        print(f"\n  {TEAL}⬇ Fetching {parts[1]}…{RST}")
        try:
            imported = import_skill_from_url(parts[1], workspace, target_root=root, overwrite="--force" in flags)
        except Exception as e:
            err(f"Install failed: {e}")
            return
        skills_mgr.discover()
        ok(f"Installed {BOLD}/{imported.name}{RST} → {imported.target_path}")
        print(f"  {GOLD}!{RST} {SLATE}Review it before use; its !`commands` only run after a permission check.{RST}\n")
        return

    print(f"\n  {GOLD}{BOLD}Skills commands{RST}\n{USAGE}\n")
