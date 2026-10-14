"""Insight commands: /debug /skill-doctor /import, and the `/doctor prompt-audit` checks."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axon.commands.common import (
    estimate_tokens, fmt_tokens, header, hint, info, section, table, usage, ws_of,
)
from axon.commands.registry import CommandResult, command
from axon.storage import axon_home as _axon_home
from axon.ui.theme import CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE

CAT = "Agents, Skills & Tools"


def _done() -> CommandResult:
    return CommandResult(handled=True)


# ─── /debug ────────────────────────────────────────────────────────────────

def debug_prompt(agent: Any, description: str) -> str:
    """Prompt asking the model to diagnose the last turn from its recorded trace (and any log files)."""
    from axon.commands.agent_cmds import _traces, explain_trace
    parts = ["Help me debug Axon's behaviour in this session."]
    if description:
        parts.append(f"What went wrong, in my words: {description}")
    traces = _traces(agent)
    if traces:
        t = traces[-1]
        parts.append("Trace of the last turn (recorded by Axon):\n" + explain_trace(t))
        problems = []
        for st in t.steps:
            d = st.data if isinstance(st.data, dict) else {}
            if d.get("is_error") or d.get("error") or st.kind in ("retry", "budget"):
                problems.append(f"- {st.kind} {d.get('name', '')}: {str(d.get('error') or d.get('result_preview') or d)[:400]}")
        if problems:
            parts.append("Errors and retries:\n" + "\n".join(problems[:12]))
    try:
        from axon import storage
        logs = sorted(storage.logs_dir().glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        if logs:
            parts.append("Recent Axon log files you may read: " + ", ".join(str(p) for p in logs))
    except Exception:
        pass
    parts.append("Explain the most likely cause, point to evidence in the trace, and suggest the fix "
                 "(a setting, a permission rule, a prompt change or a code change). Do not change files unless I ask.")
    return "\n\n".join(parts)


@command("debug", category=CAT, description="Turn on debug tracing and have the model diagnose the last turn",
         usage="/debug [what went wrong]")
def handle_debug(agent: Any, arg: str) -> CommandResult:
    from axon.commands.model_cmds import handle_trace
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        handle_trace(agent, "debug")
    print(f"\n  {TEAL}▸ Debug{RST} {SLATE}trace level set to debug · diagnosing the last turn{RST}\n")
    agent.run_turn(debug_prompt(agent, arg.strip()))
    return _done()


# ─── /skill-doctor ─────────────────────────────────────────────────────────

def skill_usage_counts(agent: Any) -> dict[str, int]:
    """How often each skill was loaded this session (Skill tool calls and /<skill> runs)."""
    counts: dict[str, int] = {}
    for t in getattr(agent, "traces", None) or []:
        prompt = str(getattr(t, "prompt", "") or "")
        m = re.match(r"## Skill: /([\w.:-]+)", prompt)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        for st in getattr(t, "steps", []) or []:
            d = st.data if isinstance(st.data, dict) else {}
            if st.kind == "tool" and d.get("name") == "Skill":
                name = str((d.get("input") or {}).get("skill", "")).lstrip("/")
                if name:
                    counts[name] = counts.get(name, 0) + 1
    return counts


def skill_report(agent: Any) -> list[dict[str, Any]]:
    from axon.skills.manager import skills_prompt_section
    mgr = getattr(agent, "skills", None)
    skills = dict(getattr(mgr, "skills", {}) or {})
    uses = skill_usage_counts(agent)
    rows = []
    for name, sk in sorted(skills.items()):
        listing = skills_prompt_section([sk]).splitlines()[-1] if not getattr(sk, "disable_model_invocation", False) else ""
        issues = []
        desc = (sk.description or "").strip()
        if not desc:
            issues.append("no description (the model cannot tell when to use it)")
        elif len(desc) > 240:
            issues.append("description over 240 chars is truncated in the listing")
        body_tok = estimate_tokens(sk.instructions or "")
        if body_tok > 5000:
            issues.append("body over 5k tokens: split it or move detail into referenced files")
        if not (sk.instructions or "").strip():
            issues.append("empty body")
        rows.append({"name": name, "scope": getattr(sk, "scope", ""), "listing_tokens": estimate_tokens(listing) if listing else 0,
                     "body_tokens": body_tok, "uses": uses.get(name, 0), "issues": issues,
                     "model_invocable": not getattr(sk, "disable_model_invocation", False)})
    return rows


@command("skill-doctor", category=CAT, description="Token cost, usage and problems of every skill",
         aliases=("skills-doctor",))
def handle_skill_doctor(agent: Any, arg: str) -> CommandResult:
    rows = skill_report(agent)
    if not rows:
        info("No skills installed. /skills create <name> makes one.")
        return _done()
    always = sum(r["listing_tokens"] for r in rows)
    header("Skill doctor", f"{len(rows)} skills · ~{fmt_tokens(always)} tokens in every prompt for the listing")
    table([[f"{WHITE}{r['name']}{RST}", f"{SLATE}{r['scope']}{RST}", fmt_tokens(r["listing_tokens"]) if r["model_invocable"] else f"{DARK_SLATE}user-only{RST}",
            fmt_tokens(r["body_tokens"]), str(r["uses"]), f"{GOLD}{len(r['issues'])}{RST}" if r["issues"] else f"{MINT}ok{RST}"]
           for r in rows], ["Skill", "Scope", "Listing", "Body", "Uses", "Issues"])
    flagged = [r for r in rows if r["issues"]]
    if flagged:
        section("Issues", GOLD)
        for r in flagged:
            for i in r["issues"]:
                print(f"    {GOLD}!{RST} {WHITE}{r['name']}{RST}: {SLATE}{i}{RST}")
    unused = [r["name"] for r in rows if not r["uses"] and r["scope"] != "bundled"]
    if unused:
        hint(f"\n    Not used this session: {', '.join(unused[:12])}")
    print()
    return _done()


# ─── /doctor prompt-audit ──────────────────────────────────────────────────

_DIRECTIVE = re.compile(r"\b(always|never|do not|don't|must not|must|should not|shouldn't|avoid|prefer)\s+(.{4,80})", re.I)
_NEG = {"never", "do not", "don't", "must not", "should not", "shouldn't", "avoid"}
_PATH_REF = re.compile(r"`((?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]{1,6})`")


@dataclass
class AuditFinding:
    kind: str       # conflict | duplicate | stale-path | size
    message: str
    files: tuple[str, ...]


def _instruction_sources(agent: Any) -> list[tuple[str, str]]:
    """(label, text) for instruction files (+ imports), rules, skills and agent definitions."""
    ws = ws_of(agent)
    out: list[tuple[str, str]] = []
    try:
        from axon.agent.memory import discover_instruction_files

        def walk(n: Any) -> None:
            out.append((str(n.path), n.content))
            for imp in getattr(n, "imports", []) or []:
                walk(imp)
        for f in discover_instruction_files(ws):
            walk(f)
    except Exception:
        pass
    for sk in (getattr(getattr(agent, "skills", None), "skills", {}) or {}).values():
        if getattr(sk, "scope", "") != "bundled":
            out.append((f"skill:{sk.name}", sk.instructions or ""))
    try:
        from axon.agent.subagent import load_agent_definitions
        for d in load_agent_definitions(ws):
            if str(d.source) != "builtin":
                out.append((f"agent:{d.name}", d.system_prompt or ""))
    except Exception:
        pass
    return out


def _norm_obj(text: str) -> str:
    t = re.sub(r"[`*_\"'()\[\]]", "", text.lower())
    t = re.split(r"[.;:!?]| because | unless | when | if ", t)[0]
    return " ".join(t.split())


def prompt_audit(agent: Any, sources: list[tuple[str, str]] | None = None) -> list[AuditFinding]:
    """Conflicting directives, duplicated instructions, stale file references and oversized files
    across AXON.md/CLAUDE.md/AGENTS.md (with imports), rules, skills and agents."""
    ws = ws_of(agent)
    sources = _instruction_sources(agent) if sources is None else sources
    findings: list[AuditFinding] = []
    polar: dict[str, dict[bool, set[str]]] = {}
    lines_at: dict[str, set[str]] = {}
    for label, text in sources:
        if estimate_tokens(text) > 5000:
            findings.append(AuditFinding("size", f"~{fmt_tokens(estimate_tokens(text))} tokens: sent often, consider trimming", (label,)))
        in_code = False
        for raw in text.splitlines():
            if raw.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            line = raw.strip().lstrip("-*0123456789. ").strip()
            if len(line) >= 25:
                lines_at.setdefault(" ".join(line.lower().split()), set()).add(label)
            for m in _DIRECTIVE.finditer(line):
                obj = _norm_obj(m.group(2))
                if len(obj) < 4:
                    continue
                neg = m.group(1).lower() in _NEG
                polar.setdefault(obj, {True: set(), False: set()})[neg].add(label)
            for pm in _PATH_REF.finditer(raw):
                ref = pm.group(1)
                if not (ws / ref).exists() and not Path(ref).expanduser().is_absolute():
                    findings.append(AuditFinding("stale-path", f"references `{ref}`, which does not exist in the workspace", (label,)))
    for obj, sides in polar.items():
        if sides[True] and sides[False]:
            findings.append(AuditFinding("conflict", f"both requires and forbids \"{obj}\"",
                                         tuple(sorted(sides[True] | sides[False]))))
    for line, labels in lines_at.items():
        if len(labels) > 1:
            findings.append(AuditFinding("duplicate", f"same instruction in {len(labels)} places: \"{line[:70]}\"", tuple(sorted(labels))))
    order = {"conflict": 0, "stale-path": 1, "duplicate": 2, "size": 3}
    return sorted(findings, key=lambda f: order.get(f.kind, 9))


def render_prompt_audit(agent: Any) -> None:
    findings = prompt_audit(agent)
    n_src = len(_instruction_sources(agent))
    header("Prompt audit", f"{n_src} instruction source(s) · {len(findings)} finding(s)")
    if not findings:
        print(f"    {MINT}✓{RST} {SLATE}No conflicts, duplicates or stale references found.{RST}\n")
        return
    colors = {"conflict": ROSE, "stale-path": GOLD, "duplicate": CYAN, "size": SLATE}
    for f in findings[:60]:
        print(f"    {colors.get(f.kind, WHITE)}{f.kind:<10}{RST} {WHITE}{f.message}{RST}")
        print(f"    {' ' * 10} {DARK_SLATE}{', '.join(f.files)}{RST}")
    print()


# ─── /import ───────────────────────────────────────────────────────────────

@dataclass
class ImportItem:
    source: Path
    target: Path | None       # file to write (None for MCP servers)
    content: str = ""
    mcp: tuple[str, dict[str, Any], str] | None = None   # (name, raw spec, scope)

    def describe(self) -> str:
        if self.mcp:
            return f"MCP server {self.mcp[0]} ({self.mcp[2]}) from {self.source}"
        return f"{self.source} → {self.target}"


def _mdc_to_rule(text: str, source: Path) -> str:
    try:
        from axon.agent.memory import split_frontmatter
        meta, body = split_frontmatter(text)
    except Exception:
        meta, body = {}, text
    globs = meta.get("globs") if isinstance(meta, dict) else None
    if isinstance(globs, str):
        globs = [g.strip() for g in globs.split(",") if g.strip()]
    head = f"<!-- imported from {source} -->\n"
    if globs and not str(meta.get("alwaysApply", "")).lower() == "true":
        return "---\npaths:\n" + "".join(f"  - \"{g}\"\n" for g in globs) + "---\n" + head + body.strip() + "\n"
    return head + body.strip() + "\n"


def _mcp_from_json(path: Path, scope: str) -> list[ImportItem]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return [ImportItem(path, None, mcp=(str(n), dict(spec), scope)) for n, spec in (servers or {}).items() if isinstance(spec, dict)]


def import_plan(workspace: Path, source: str) -> list[ImportItem]:
    """What `/import <source>` would create. Sources: cursor, gemini, codex, all."""
    ws = Path(workspace)
    home = Path.home()
    rules = ws / ".axon" / "rules"
    items: list[ImportItem] = []
    if source in ("cursor", "all"):
        cr = ws / ".cursorrules"
        if cr.is_file():
            items.append(ImportItem(cr, rules / "cursor.md", f"<!-- imported from {cr} -->\n" + cr.read_text(encoding="utf-8", errors="replace").strip() + "\n"))
        rd = ws / ".cursor" / "rules"
        if rd.is_dir():
            for f in sorted(rd.glob("**/*.mdc")) + sorted(rd.glob("**/*.md")):
                items.append(ImportItem(f, rules / f"cursor-{f.stem}.md", _mdc_to_rule(f.read_text(encoding="utf-8", errors="replace"), f)))
        mj = ws / ".cursor" / "mcp.json"
        if mj.is_file():
            items += _mcp_from_json(mj, "project")
    if source in ("gemini", "all"):
        gm = ws / "GEMINI.md"
        if gm.is_file():
            items.append(ImportItem(gm, rules / "gemini.md", f"<!-- imported from {gm} -->\n" + gm.read_text(encoding="utf-8", errors="replace").strip() + "\n"))
        ug = home / ".gemini" / "GEMINI.md"
        if ug.is_file():
            items.append(ImportItem(ug, _axon_home() / "rules" / "gemini.md", f"<!-- imported from {ug} -->\n" + ug.read_text(encoding="utf-8", errors="replace").strip() + "\n"))
        gs = home / ".gemini" / "settings.json"
        if gs.is_file():
            items += _mcp_from_json(gs, "user")
        ps = ws / ".gemini" / "settings.json"
        if ps.is_file():
            items += _mcp_from_json(ps, "project")
    if source in ("codex", "all"):
        for name in ("AGENTS.md", "instructions.md"):
            cf = home / ".codex" / name
            if cf.is_file():
                items.append(ImportItem(cf, _axon_home() / "rules" / f"codex-{cf.stem.lower()}.md",
                                        f"<!-- imported from {cf} -->\n" + cf.read_text(encoding="utf-8", errors="replace").strip() + "\n"))
        ct = home / ".codex" / "config.toml"
        if ct.is_file():
            try:
                try:
                    import tomllib  # type: ignore[import-not-found]
                except ModuleNotFoundError:
                    import tomli as tomllib  # type: ignore[no-redef]
                data = tomllib.loads(ct.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            for n, spec in (data.get("mcp_servers") or {}).items():
                if isinstance(spec, dict) and spec.get("command"):
                    raw = {"command": spec["command"], "args": list(spec.get("args") or []), "env": dict(spec.get("env") or {})}
                    items.append(ImportItem(ct, None, mcp=(str(n), raw, "user")))
    return items


def apply_import(workspace: Path, items: list[ImportItem]) -> list[str]:
    """Write the planned files (never overwriting) and register MCP servers (never replacing). Returns notes."""
    from axon.session.store import atomic_write_text
    notes: list[str] = []
    existing: dict[str, Any] = {}
    cfg = None
    if any(i.mcp for i in items):
        try:
            from axon.mcp.config import MCPConfig
            cfg = MCPConfig(Path(workspace))
            existing = cfg.load_specs()
        except Exception as e:
            notes.append(f"MCP import unavailable: {e}")
    for it in items:
        if it.mcp:
            name, raw, scope = it.mcp
            if cfg is None:
                continue
            if name in existing:
                notes.append(f"skipped MCP server {name}: already configured")
                continue
            try:
                path = cfg.add_raw(name, raw, scope)
                notes.append(f"added MCP server {name} → {path}")
            except Exception as e:
                notes.append(f"skipped MCP server {name}: {e}")
            continue
        assert it.target is not None
        if it.target.exists():
            notes.append(f"skipped {it.target}: already exists")
            continue
        atomic_write_text(it.target, it.content)
        notes.append(f"wrote {it.target}")
    return notes


IMPORT_USAGE = """/import <cursor|gemini|codex|all> [--dry-run]
  cursor: .cursorrules, .cursor/rules/*.mdc (globs become path-scoped rules), .cursor/mcp.json
  gemini: GEMINI.md, ~/.gemini/GEMINI.md, .gemini/settings.json and ~/.gemini/settings.json MCP servers
  codex:  ~/.codex/AGENTS.md / instructions.md, ~/.codex/config.toml [mcp_servers]"""


@command("import", category="Memory & Project", description="Import rules and MCP servers from Cursor, Gemini CLI or Codex",
         usage=IMPORT_USAGE)
def handle_import(agent: Any, arg: str) -> CommandResult:
    words = arg.lower().split()
    dry = "--dry-run" in words or "-n" in words
    src = next((w for w in words if w in ("cursor", "gemini", "codex", "all")), "")
    if not src:
        usage(IMPORT_USAGE)
        return _done()
    ws = ws_of(agent)
    items = import_plan(ws, src)
    if not items:
        info(f"Nothing to import from {src} (no matching files found).")
        return _done()
    header("Import" + (" (dry run)" if dry else ""), f"{src} · {len(items)} item(s)")
    for it in items:
        print(f"    {CYAN}•{RST} {WHITE}{it.describe()}{RST}")
    print()
    if dry:
        hint("    Run again without --dry-run to write these files (existing files are never overwritten).")
        print()
        return _done()
    for note in apply_import(ws, items):
        color = MINT if note.startswith(("wrote", "added")) else GOLD
        print(f"    {color}{note}{RST}")
    print()
    if hasattr(agent, "reload_memory"):
        try:
            agent.reload_memory()
        except Exception:
            pass
    return _done()
