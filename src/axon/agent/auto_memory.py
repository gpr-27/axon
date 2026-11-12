"""Automatic project memory: durable facts you state while working are saved to this project's auto memory.

How it works (and what it costs): at the start of each turn the user's own words are checked with a few regular
expressions for durable statements ("we use pytest, not unittest", "never push to main without asking", "remember
that ...", "run the tests with `make test`", "we decided to ...", "this project uses ..."). No model call is made,
so a turn pays no extra tokens, money or latency (well under a millisecond), and nothing here can block or fail a turn.

Facts land in four small topic files in the project's memory folder (preferences, conventions, commands,
project-facts) plus the MEMORY.md index, as one-line bullets. A fact that is already there (or says the same thing
shorter) is not added again; a fuller restatement replaces the shorter one; each file keeps at most MAX_BULLETS lines.
Text that looks like a secret is never saved, and long text, code blocks and questions are ignored.
What the agent itself discovers (how to run the tests, key paths) is written by the model with its file tools, as
the system prompt tells it to; ``saved_notice`` reports those writes the same way.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axon.features import memory_files as mf

MAX_BULLETS = 40
MAX_FACT_CHARS = 220
MIN_FACT_CHARS = 14
MAX_PER_TURN = 3
MAX_PROMPT_CHARS = 2000

# (file, note name, memory type)
TOPICS = {
    "preferences": ("preferences.md", "Preferences", "user"),
    "conventions": ("conventions.md", "Conventions and rules", "feedback"),
    "commands": ("commands.md", "Commands", "reference"),
    "facts": ("project-facts.md", "Project facts and decisions", "project"),
}


@dataclass(frozen=True)
class Fact:
    text: str
    topic: str


_LEAD = re.compile(r"^(?:(?:ok|okay|so|also|and|btw|by the way|hey)[, ]+)+", re.I)
_REMEMBER = re.compile(r"^(?:please\s+)?(?:remember|note|keep in mind|fyi|don'?t forget)(?:\s+that|\s+to)?\s*[:,-]?\s*(.+)$", re.I)
_RULE = re.compile(r"^(?:please\s+)?(?:always|never|don'?t ever|do not ever|from now on)\b", re.I)
_WE_RULE = re.compile(r"\b(?:we|i)\s+(?:always|never|only|prefer|follow|don'?t|do not)\b|\b(?:we|i)\s+(?:use|using|write|name)\b.{2,60}\b(?:not|instead of|rather than)\b", re.I)
_PREFER = re.compile(r"\bi\s+(?:really\s+|usually\s+)?prefer\s+\S", re.I)
_COMMAND = re.compile(
    r"\b(?:(?:test|tests|build|lint|format|typecheck|deploy|start|dev|run)\s+(?:command|script|step)\s+(?:is|are)\b"
    r"|(?:run|running)\s+(?:the\s+)?(?:tests?|build|lint|linter|app|server|dev server|formatter)\s+(?:with|using|via)\b"
    r"|(?:tests?|build|lint)\s+(?:is|are|runs?)\s+(?:run\s+)?(?:with|via|using)\b)", re.I)
_DECISION = re.compile(r"\b(?:we|i)(?:'ve|\s+have)?\s+(?:decided|chose|agreed|settled)\b|\b(?:we're|we are|we'll|we will)\s+(?:going with|switching to|moving to|migrating to|using)\b", re.I)
_FACT = re.compile(r"\b(?:our|this|the)\s+(?:project|repo|repository|codebase|app|backend|frontend|api|service|database|db|stack|monorepo)"
                   r"\s+(?:is|are|uses|use|runs on|lives in|is built with|is written in|has|deploys to|talks to)\b", re.I)
_SKIP_START = re.compile(r"^(?:can you|could you|would you|will you|how |what |why |where |when |who |is |are |does |do you|please (?:fix|add|make|create|write|run|show|explain|help|check|update|find))", re.I)
_CODEISH = re.compile(r"```|^\s{4,}\S|^\s*(?:def|class|import|from|function|const|let|var|\$)\s")


def _split(text: str) -> list[str]:
    parts: list[str] = []
    for chunk in re.split(r"\n+", text):
        parts += re.split(r"(?<=[.!?])\s+(?=[A-Z`])", chunk.strip())
    return [p.strip() for p in parts if p.strip()]


def _classify(s: str, explicit: bool) -> str | None:
    if _COMMAND.search(s):
        return "commands"
    if _PREFER.search(s):
        return "preferences"
    if _RULE.match(s) or _WE_RULE.search(s):
        return "conventions"
    if _DECISION.search(s) or _FACT.search(s):
        return "facts"
    if explicit:
        return "preferences" if re.match(r"(?i)i\b|my\b", s) else "conventions" if re.search(r"(?i)\b(?:always|never|use|don'?t)\b", s) else "facts"
    return None


def extract_facts(text: str) -> list[Fact]:
    """Durable statements in what the user typed (at most MAX_PER_TURN). Pure; no I/O."""
    if not text or text.lstrip().startswith(("/", "#", "!")) or len(text) > MAX_PROMPT_CHARS or _CODEISH.search(text):
        return []
    found: list[Fact] = []
    for sent in _split(text):
        s = _LEAD.sub("", sent).strip().rstrip(".").strip()
        if s.endswith("?") or len(s) < MIN_FACT_CHARS:
            continue
        explicit = False
        m = _REMEMBER.match(s)
        if m:
            s, explicit = m.group(1).strip().rstrip("."), True
        if not explicit and _SKIP_START.match(s):
            continue
        if len(s) > MAX_FACT_CHARS or len(s) < MIN_FACT_CHARS or mf.scan_secrets(s):
            continue
        topic = _classify(s, explicit)
        if topic is None:
            continue
        s = s[0].upper() + s[1:]
        if all(_key(s) != _key(f.text) for f in found):
            found.append(Fact(s, topic))
        if len(found) >= MAX_PER_TURN:
            break
    return found


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _bullets(body: str) -> tuple[list[str], list[str]]:
    """(head lines that are not bullets, bullet texts)."""
    head: list[str] = []
    bullets: list[str] = []
    for ln in body.splitlines():
        t = ln.strip()
        if t.startswith(("- ", "* ")):
            bullets.append(t[2:].strip())
        elif t or bullets:
            if bullets and t:
                bullets.append(t)        # continuation text stays with the list
        else:
            head.append(ln)
    return [h for h in head if h.strip()], bullets


def _merge(bullets: list[str], fact: str) -> tuple[list[str], bool]:
    """Add ``fact`` unless covered; replace a shorter version of the same statement. (new list, changed)"""
    k = _key(fact)
    out = list(bullets)
    for i, b in enumerate(out):
        kb = _key(b)
        if kb == k or k in kb:
            return out, False                   # already known
        if kb and kb in k:
            out[i] = fact                       # the new wording says more: update in place
            return out, True
    out.append(fact)
    return out[-MAX_BULLETS:], True


def save_facts(ws: Path, facts: list[Fact]) -> list[Fact]:
    """Write the facts into the project's topic files; returns the ones that changed something."""
    saved: list[Fact] = []
    by_topic: dict[str, list[Fact]] = {}
    for f in facts:
        by_topic.setdefault(f.topic, []).append(f)
    from axon.features import memory_history as mh
    for topic, items in by_topic.items():
        fname, title, typ = TOPICS[topic]
        path = mf.scope_dir(ws, "project") / fname
        for attempt in (0, 1):               # a hash conflict (edited elsewhere meanwhile): re-read once, then give up
            cur = mf.parse_memory(path, "project") if path.is_file() else None
            seen = mh.file_sha(path)
            head, bullets = _bullets(cur.body if cur else "")
            changed: list[Fact] = []
            for f in items:
                bullets, did = _merge(bullets, f.text)
                if did:
                    changed.append(f)
            if not changed:
                break
            body = ("\n".join(h for h in head if not h.startswith("# ")) + "\n\n" if any(not h.startswith("# ") for h in head) else "")
            body = f"# {title}\n\n" + body + "\n".join(f"- {b}" for b in bullets)
            try:
                with mh.writing_as("auto-capture"):
                    mf.write_memory(ws, "project", name=title, body=body, description=_describe(topic), type_=typ, file=fname,
                                    expected_sha=seen)
            except mh.Conflict:
                continue
            saved += changed
            break
    return saved


def _describe(topic: str) -> str:
    return {"preferences": "How the user likes to work", "conventions": "Rules and conventions to follow in this project",
            "commands": "How to build, test, lint and run this project", "facts": "Facts and decisions about this project"}[topic]


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "auto_memory", True)) and os.environ.get("AXON_DISABLE_AUTO_MEMORY", "") not in ("1", "true")


def capture(ws: Path, text: str, settings: Any = None) -> list[Fact]:
    """The whole step: extract, save, return what was saved. Never raises."""
    try:
        if settings is not None and not enabled(settings):
            return []
        facts = extract_facts(text)
        return save_facts(Path(ws), facts) if facts else []
    except Exception:
        return []


def saved_notice(facts: list[Fact]) -> str:
    """The one line shown for a capture: 'memory saved: <fact>' (+ how many more)."""
    if not facts:
        return ""
    first = facts[0].text
    return f"{first[:100]}" + (f" (+{len(facts) - 1} more)" if len(facts) > 1 else "")


STARTUP_CHARS = 6000


def startup_topics(ws: Path) -> list[tuple[Path, str]]:
    """The saved fact files (bullets only), in a fixed order, to put in the system prompt; at most STARTUP_CHARS in all."""
    out: list[tuple[Path, str]] = []
    used = 0
    for fname, _title, _typ in TOPICS.values():
        path = mf.scope_dir(ws, "project") / fname
        mem = mf.parse_memory(path, "project") if path.is_file() else None
        if mem is None:
            continue
        _head, bullets = _bullets(mem.body)
        if not bullets:
            continue
        text = f"{mem.name}:\n" + "\n".join(f"- {b}" for b in bullets)
        if used + len(text) > STARTUP_CHARS:
            break
        used += len(text)
        out.append((path, text))
    return out
