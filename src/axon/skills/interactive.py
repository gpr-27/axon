"""
/skills: list, show, create, edit, delete, install (from GitHub) and reload skills.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from axon.storage import axon_home as _axon_home
from axon.skills.manager import SkillManager
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
    print(f"\n  {DIM}/skills show|create|install|edit|delete <name>{RST}\n")


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
