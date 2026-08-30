"""
Skills management for Axon: Discovery from ~/.axon/skills and .axon/skills,
YAML frontmatter parsing, dynamic context injection (!`cmd`), and on-demand execution.
"""
from __future__ import annotations
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    path: Path
    scope: str  # "personal", "project"

class SkillManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.skills: dict[str, Skill] = {}
        self.discover()

    def discover(self) -> dict[str, Skill]:
        """Discover skills from ~/.axon/skills and .axon/skills (project overrides personal)."""
        self.skills.clear()

        # 1. Personal skills: ~/.axon/skills/<name>/SKILL.md
        personal_root = Path.home() / ".axon" / "skills"
        if personal_root.exists() and personal_root.is_dir():
            for p in personal_root.glob("*/SKILL.md"):
                s = self._load_skill(p, scope="personal")
                if s:
                    self.skills[s.name] = s

        # 2. Project skills: .axon/skills/<name>/SKILL.md
        proj_root = self.workspace / ".axon" / "skills"
        if proj_root.exists() and proj_root.is_dir():
            for p in proj_root.glob("*/SKILL.md"):
                s = self._load_skill(p, scope="project")
                if s:
                    self.skills[s.name] = s

        # 3. Built-in bundled skills
        self._register_bundled_skills()

        return self.skills

    def _register_bundled_skills(self) -> None:
        """Register default bundled skills covering reviews, debugging, subagent fan-out, refactoring, security, and tests."""
        bundled = [
            Skill(
                name="code-review",
                description="Comprehensive multi-file code review analyzing logic errors, performance, and security.",
                instructions="Conduct a thorough code review. Inspect modified files, look for edge case failures, unhandled exceptions, type safety, and adherence to project architecture conventions.",
                path=Path("bundled://code-review"),
                scope="bundled",
            ),
            Skill(
                name="debug",
                description="Systematic root-cause debugging for errors, test failures, or crashes.",
                instructions="Debug the reported issue step by step: reproduce the error, inspect tracebacks, locate the failure root cause, and propose a minimal verified fix.",
                path=Path("bundled://debug"),
                scope="bundled",
            ),
            Skill(
                name="verify",
                description="Build and run tests to verify that changes work without regressions.",
                instructions="Run project test commands (e.g. pytest or npm test), check exit codes, and report exact validation results.",
                path=Path("bundled://verify"),
                scope="bundled",
            ),
            Skill(
                name="subagent-fanout",
                description="Deconstructs complex research/refactor tasks into parallel subagents and synthesizes findings.",
                instructions="Decompose the problem into 2-5 independent subtasks. Spawn isolated subagents using the `Task` tool for each subtask in parallel, then synthesize their conclusions into a cohesive executive report.",
                path=Path("bundled://subagent-fanout"),
                scope="bundled",
            ),
            Skill(
                name="refactor",
                description="Systematic architecture and code refactoring with design patterns and zero regressions.",
                instructions="Analyze code smells and technical debt. Plan semantic refactoring with clean abstractions, modularity, and strict typing while preserving all existing behaviors.",
                path=Path("bundled://refactor"),
                scope="bundled",
            ),
            Skill(
                name="security-audit",
                description="Static vulnerability audit for command injection, secret leakage, and insecure deserialization.",
                instructions="Scan the codebase for security vulnerabilities: look for shell injection in subprocess calls, unsanitized inputs, exposed API keys/tokens, and path traversal vectors.",
                path=Path("bundled://security-audit"),
                scope="bundled",
            ),
            Skill(
                name="test-gen",
                description="Automated unit and integration test generation with high branch coverage.",
                instructions="Analyze target functions, classes, and edge cases. Author clean pytest or unit tests with mock fixtures, parameterization, and boundary condition validation.",
                path=Path("bundled://test-gen"),
                scope="bundled",
            ),
            Skill(
                name="optimize",
                description="Algorithmic profiling, memory leak detection, and performance optimization.",
                instructions="Identify computational bottlenecks, O(N^2) complexity, unnecessary I/O or memory allocations, and propose cache or data structure optimizations.",
                path=Path("bundled://optimize"),
                scope="bundled",
            ),
            Skill(
                name="deep-research",
                description="Multi-round iterative deep research with sub-query planning, source crawling, and matrix synthesis.",
                instructions="Deconstruct the complex topic into sub-topics. Use `DeepResearch` and `WebSearch` to investigate each angle. Build a comprehensive report with executive summary, comparative analysis tables, and verified citations.",
                path=Path("bundled://deep-research"),
                scope="bundled",
            ),
            Skill(
                name="table-search",
                description="High-speed structured table scanning and matrix extraction across Markdown, CSV, and JSON data.",
                instructions="Use `TableSearch` to locate, filter, and extract structured comparative tables, CSV datasets, and JSON records matching given keywords and column patterns.",
                path=Path("bundled://table-search"),
                scope="bundled",
            ),
            Skill(
                name="docgen",
                description="Generates technical documentation, API specifications, and architecture walkthroughs.",
                instructions="Document modules, classes, and exported APIs. Generate markdown documentation with usage examples and clear structural overview.",
                path=Path("bundled://docgen"),
                scope="bundled",
            ),
            Skill(
                name="git-workflow",
                description="Conventional commits, automated changelog entries, and branch management.",
                instructions="Inspect recent changes via `Git(status)` and `Git(diff)`. Draft conventional commit messages (feat:, fix:, refactor:, test:) and update CHANGELOG.md.",
                path=Path("bundled://git-workflow"),
                scope="bundled",
            ),
            Skill(
                name="skill-creator",
                description="Interactive skill scaffolding wizard for creating custom project and personal skills.",
                instructions="Guide the user through creating a new custom skill. Prompt for name, description, dynamic context commands, and execution steps, then save the formatted SKILL.md.",
                path=Path("bundled://skill-creator"),
                scope="bundled",
            ),
            Skill(
                name="api-tester",
                description="Automated HTTP/REST endpoint probing, payload validation, and latency benchmarking.",
                instructions="Analyze API routes and schemas. Use `Http` to send GET/POST/PUT/DELETE requests, validate response codes, verify JSON schemas, and check status headers.",
                path=Path("bundled://api-tester"),
                scope="bundled",
            ),
            Skill(
                name="docker",
                description="Dockerfile, docker-compose, and container lifecycle analysis and optimization.",
                instructions="Analyze Dockerfiles and docker-compose.yml configurations. Check for multi-stage builds, minimal base images, secure non-root users, and volume caching.",
                path=Path("bundled://docker"),
                scope="bundled",
            ),
            Skill(
                name="db-migration",
                description="Database schema migration generator, index analyzer, and SQL query optimizer.",
                instructions="Inspect database schema definitions and SQL queries. Check for missing indexes, N+1 query bottlenecks, safe column additions, and rollback strategies.",
                path=Path("bundled://db-migration"),
                scope="bundled",
            ),
            Skill(
                name="frontend-ui",
                description="Modern responsive UI layout builder with semantic HTML, CSS styling, and accessibility.",
                instructions="Design and construct clean frontend components with responsive flex/grid layouts, theme color palettes, accessible ARIA attributes, and micro-interactions.",
                path=Path("bundled://frontend-ui"),
                scope="bundled",
            ),
            Skill(
                name="benchmark",
                description="Execution speed profiling, memory footprint analysis, and stress testing.",
                instructions="Run benchmark scripts with timing loops. Measure execution throughput, memory allocations, CPU utilization, and pinpoint performance regressions.",
                path=Path("bundled://benchmark"),
                scope="bundled",
            ),
        ]
        for b in bundled:
            if b.name not in self.skills:
                self.skills[b.name] = b

    def _load_skill(self, path: Path, scope: str) -> Skill | None:
        try:
            content = path.read_text(encoding="utf-8")
            name = path.parent.name
            desc = ""
            body = content

            # Parse YAML frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = parts[1]
                    body = parts[2].strip()
                    for line in fm.splitlines():
                        if line.startswith("description:"):
                            desc = line.split(":", 1)[1].strip()

            return Skill(name=name, description=desc, instructions=body, path=path, scope=scope)
        except Exception:
            return None

    def execute_skill(self, name: str) -> str:
        """Resolve skill, inject dynamic context (!`cmd`), and return ready instructions."""
        skill = self.skills.get(name)
        if not skill:
            raise KeyError(f"Skill '{name}' not found.")

        instructions = skill.instructions

        # Dynamic context injection: replace !`command` with command stdout
        def _replace_dyn(match: re.Match) -> str:
            cmd = match.group(1)
            try:
                out = subprocess.check_output(cmd, shell=True, cwd=self.workspace, text=True, timeout=10)
                return f"\n```\n{out.strip()}\n```\n"
            except Exception as e:
                return f"[Failed to execute `{cmd}`: {e}]"

        rendered = re.sub(r"!`([^`]+)`", _replace_dyn, instructions)
        return f"## Skill: /{skill.name}\n\n{rendered}"
