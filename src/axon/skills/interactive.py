"""
Interactive /skills management studio: create, inspect, edit, list, and delete custom skills.
"""
from __future__ import annotations
import os
import subprocess
from pathlib import Path
from axon.skills.manager import Skill, SkillManager
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, MINT, RST, ROSE, SLATE, TEAL, UNDER, WHITE,
)

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

def handle_skills_command(skills_mgr: SkillManager, workspace: Path, arg: str) -> None:
    parts = arg.strip().split(maxsplit=2)
    sub = parts[0].lower() if parts else "list"

    if sub in ("list", ""):
        skills = sorted(skills_mgr.skills.values(), key=lambda x: (x.scope, x.name))
        print(f"\n{GOLD}{BOLD}=== Active Skills Studio ({len(skills)} Available) ==={RST}")
        
        # Group by scope
        grouped: dict[str, list[Skill]] = {"project": [], "personal": [], "bundled": []}
        for s in skills:
            grouped.setdefault(s.scope, []).append(s)

        for scope_name, s_list in grouped.items():
            if not s_list:
                continue
            if scope_name == "project":
                print(f"\n  {MINT}{BOLD}📁 Project Skills (.axon/skills/){RST}")
            elif scope_name == "personal":
                print(f"\n  {CYAN}{BOLD}👤 Personal Skills (~/.axon/skills/){RST}")
            else:
                print(f"\n  {TEAL}{BOLD}⚡ Bundled Skills{RST}")

            for s in s_list:
                print(f"    {BOLD}/{s.name:<18}{RST} {SLATE}{s.description}{RST}")

        print(f"\n  {DIM}Commands:{RST} {GOLD}/skill create <name>{RST} · {CYAN}/skill edit <name>{RST} · {MINT}/skill show <name>{RST} · {ROSE}/skill delete <name>{RST}\n")

    elif sub in ("create", "new", "creator"):
        if len(parts) < 2:
            print(f"\n{GOLD}{BOLD}=== Interactive Skill Creator Wizard ==={RST}")
            print("  Usage: /skill create <name> [template_type]")
            print("\n  Available Templates:")
            for k, (title_t, _) in TEMPLATES.items():
                print(f"    {CYAN}{k:<12}{RST} - {title_t}")
            print(f"\n  Example: {WHITE}/skill create deploy-service workflow{RST}\n")
            return

        name = parts[1].lstrip("/")
        template_key = parts[2].lower() if len(parts) > 2 else "workflow"
        if template_key not in TEMPLATES:
            template_key = "workflow"

        _, tmpl_text = TEMPLATES[template_key]
        title = name.replace("-", " ").replace("_", " ").title()
        skill_content = tmpl_text.format(name=name, title=title)

        target_dir = workspace / ".axon" / "skills" / name
        target_dir.mkdir(parents=True, exist_ok=True)
        skill_file = target_dir / "SKILL.md"

        if skill_file.exists():
            print(f"\n  {SLATE}Skill already exists at {skill_file}{RST}\n")
            return

        skill_file.write_text(skill_content, encoding="utf-8")
        skills_mgr.discover()

        print(f"\n  {MINT}✓ Successfully created skill {BOLD}/{name}{RST}{MINT}!{RST}")
        print(f"  {SLATE}Location:{RST} {WHITE}{skill_file}{RST}")
        print(f"  {DIM}Template: {template_key} · Invoke anytime via /{name} or edit with /skill edit {name}{RST}\n")

    elif sub in ("edit", "modify"):
        if len(parts) < 2:
            print(f"\n  {GOLD}Usage:{RST} /skill edit <name>\n")
            return
        name = parts[1].lstrip("/")
        skill = skills_mgr.skills.get(name)
        if not skill:
            print(f"\n  {ROSE}Skill '{name}' not found in catalog.{RST}\n")
            return
        if skill.scope == "bundled":
            # Clone bundled skill to project folder for editing
            target_dir = workspace / ".axon" / "skills" / name
            target_dir.mkdir(parents=True, exist_ok=True)
            skill_file = target_dir / "SKILL.md"
            if not skill_file.exists():
                template = f"""---
description: {skill.description}
---

# {name.title()} Skill

## Step-by-Step Instructions
{skill.instructions}
"""
                skill_file.write_text(template, encoding="utf-8")
        else:
            skill_file = skill.path

        editor = os.environ.get("EDITOR") or "nano"
        print(f"\n  {TEAL}Opening {skill_file} in {editor}...{RST}")
        subprocess.call([editor, str(skill_file)])
        skills_mgr.discover()
        print(f"  {MINT}✓ Reloaded skill /{name}.{RST}\n")

    elif sub in ("show", "info", "inspect"):
        if len(parts) < 2:
            print(f"\n  {GOLD}Usage:{RST} /skill show <name>\n")
            return
        name = parts[1].lstrip("/")
        skill = skills_mgr.skills.get(name)
        if not skill:
            print(f"\n  {ROSE}Skill '{name}' not found.{RST}\n")
            return
        scope_badge = f"{MINT}[{skill.scope}]{RST}"
        print(f"\n{GOLD}{BOLD}=== Skill: /{skill.name} {scope_badge} ==={RST}")
        print(f"  {SLATE}Path:{RST} {WHITE}{skill.path}{RST}")
        print(f"  {SLATE}Description:{RST} {WHITE}{skill.description}{RST}\n")
        print(f"{WHITE}{skill.instructions}{RST}\n")

    elif sub in ("delete", "rm", "remove"):
        if len(parts) < 2:
            print(f"\n  {GOLD}Usage:{RST} /skill delete <name>\n")
            return
        name = parts[1].lstrip("/")
        skill = skills_mgr.skills.get(name)
        if not skill:
            print(f"\n  {ROSE}Skill '{name}' not found.{RST}\n")
            return
        if skill.scope == "bundled":
            print(f"\n  {ROSE}Cannot delete bundled skill '{name}'.{RST}\n")
            return
        try:
            if skill.path.exists():
                skill.path.unlink()
                if skill.path.parent.exists() and not list(skill.path.parent.iterdir()):
                    skill.path.parent.rmdir()
            skills_mgr.discover()
            print(f"\n  {TEAL}✓ Removed skill /{name}.{RST}\n")
        except Exception as e:
            print(f"\n  ❌ Failed to delete skill: {e}\n")

    elif sub in ("import", "install", "add"):
        if len(parts) < 2:
            print(f"\n  {GOLD}Usage:{RST} /skill import <github_repo_or_url>\n")
            print(f"  {DIM}Example: /skill import anthropics/anthropic-quickstarts/skills/rag{RST}\n")
            return
        repo_target = parts[1]
        from axon.skills.importer import import_skill_from_url
        print(f"\n  {TEAL}⬇ Fetching remote skill bundle from {repo_target}...{RST}")
        try:
            imported = import_skill_from_url(repo_target, workspace)
            skills_mgr.discover()
            print(f"  {MINT}✓ Successfully installed skill {BOLD}/{imported.name}{RST}{MINT}!{RST}")
            print(f"  {SLATE}Description:{RST} {WHITE}{imported.description}{RST}")
            print(f"  {SLATE}Saved to:{RST} {WHITE}{imported.target_path}{RST}\n")
        except Exception as e:
            print(f"  {ROSE}❌ Failed to import skill: {e}{RST}\n")

    else:
        print(f"\n  {GOLD}Skills Studio Commands:{RST}")
        print("    /skills                        - Browse catalog of active skills")
        print("    /skill create <name> [tmpl]    - Scaffold a new skill (workflow, tester, reviewer, api)")
        print("    /skill import <github_url>     - Import SKILL.md directly from GitHub or skills.sh")
        print("    /skill edit <name>             - Open skill in $EDITOR")
        print("    /skill show <name>             - Inspect skill instructions and dynamic directives")
        print("    /skill delete <name>           - Remove custom skill\n")
