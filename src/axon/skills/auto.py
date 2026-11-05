"""
Load skills by trigger: when ``automation.auto_skills`` is on, the skills that fit a prompt are added
to that prompt, so the model does not have to notice them in the skill list first.

A skill fits when its trigger phrases appear in the prompt, when :meth:`SkillManager.auto_match` finds
it (built-in intent words, ``/name``), or when enough of the words in its description appear in the
prompt. Files in the workspace that match the skill's ``paths`` count a little extra. At most
``automation.max_auto_skills`` skills are added, each skill once per session, each body cut to a limit.

Also here: the built-in prompt handler that :class:`axon.hooks.manager.HookManager` runs for every
prompt. It shows the waiting background notices (:mod:`axon.notices`) and adds the matching skills.
"""
from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from axon import notices
from axon.skills.manager import Skill, SkillManager, _manager_for

MIN_SCORE = 3
TRIGGER_SCORE = 5
MIN_WORD_LENGTH = 4
MAX_BODY_CHARS = 6000
MAX_FILES_SCANNED = 2000
MAX_SCAN_DEPTH = 3
FILE_CACHE_SECONDS = 30.0
SKIPPED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target"}
STOP_WORDS = frozenset(
    "this that with from have will your when what which into than then them they their there these those "
    "about also been being does each every just like make more most only other some such very want need "
    "should would could using used uses use when user users skill skills file files code".split())
QUOTED_RE = re.compile(r"[\"']([^\"']{3,60})[\"']")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

_file_cache: dict[str, tuple[float, list[str]]] = {}
_seen_in_session: dict[str, set[str]] = {}


def words_of(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text.lower()) if len(w) >= MIN_WORD_LENGTH and w not in STOP_WORDS}


def trigger_phrases(skill: Skill) -> list[str]:
    """Phrases from the ``triggers`` field plus the quoted phrases the skill creator writes into the
    description after "Use when the user mentions:"."""
    phrases = [t.lower() for t in skill.triggers]
    description = skill.description + " " + skill.when_to_use
    if "mentions:" in description:
        tail = description.split("mentions:", 1)[1]
        phrases += [q.lower() for q in QUOTED_RE.findall(tail)]
    return phrases


def description_words(skill: Skill) -> set[str]:
    return words_of(f"{skill.name.replace('-', ' ')} {skill.description} {skill.when_to_use}")


def mentions(prompt: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", prompt) is not None


def workspace_files(workspace: Path) -> list[str]:
    """Relative paths of files near the top of the workspace (cached for a short time)."""
    key = str(workspace)
    cached = _file_cache.get(key)
    if cached and time.monotonic() - cached[0] < FILE_CACHE_SECONDS:
        return cached[1]
    found: list[str] = []
    root_depth = len(Path(workspace).parts)
    for folder, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS]
        if len(Path(folder).parts) - root_depth >= MAX_SCAN_DEPTH:
            dirs[:] = []
        for name in names:
            found.append((Path(folder) / name).relative_to(workspace).as_posix())
        if len(found) >= MAX_FILES_SCANNED:
            break
    _file_cache[key] = (time.monotonic(), found)
    return found


def paths_match(skill: Skill, files: list[str]) -> bool:
    for pattern in skill.paths:
        for rel in files:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(Path(rel).name, pattern):
                return True
    return False


def score_skill(skill: Skill, prompt: str, prompt_words: set[str], files: list[str]) -> int:
    score = sum(TRIGGER_SCORE for phrase in trigger_phrases(skill) if mentions(prompt, phrase))
    overlap = len(description_words(skill) & prompt_words)
    score += overlap
    if skill.paths and overlap and paths_match(skill, files):
        score += 2
    return score


def relevant_skills(manager: SkillManager, prompt: str) -> list[Skill]:
    """Every matching skill, strongest first. User-only skills are never chosen."""
    if not prompt.strip():
        return []
    text = prompt.lower()
    prompt_words = words_of(text)
    files = workspace_files(manager.workspace)
    from_intents = {skill.name for skill in manager.auto_match(prompt)}
    scored = []
    for skill in manager.skills.values():
        if skill.disable_model_invocation:
            continue
        score = score_skill(skill, text, prompt_words, files)
        if skill.name in from_intents:
            score += TRIGGER_SCORE
        if score >= MIN_SCORE:
            scored.append((score, skill))
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [skill for _score, skill in scored]


def skill_block(manager: SkillManager, skill: Skill) -> str:
    body = manager.render(skill, "", allow_shell=None)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS].rstrip() + "\n[skill cut here; load the rest with the Skill tool]"
    return body


def context_for(manager: SkillManager, prompt: str, session_id: str, limit: int) -> tuple[str, list[str]]:
    """(text to add to the prompt, names of the skills in it). Skills already added in this session are
    left out."""
    seen = _seen_in_session.setdefault(session_id, set())
    fresh = [skill for skill in relevant_skills(manager, prompt) if skill.name not in seen]
    chosen = fresh[:limit]
    if not chosen:
        return "", []
    seen.update(skill.name for skill in chosen)
    blocks = [skill_block(manager, skill) for skill in chosen]
    names = [skill.name for skill in chosen]
    intro = f"Axon loaded these skills because they match the request: {', '.join(names)}. Follow them where relevant."
    return "<auto-loaded-skills>\n" + intro + "\n\n" + "\n\n".join(blocks) + "\n</auto-loaded-skills>", names


def prompt_handler(workspace: Path) -> Callable[[str, dict[str, Any]], dict[str, str]]:
    """The handler HookManager runs for each user prompt."""
    from axon.marketplace.automation import load_automation

    def handle(event: str, payload: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        waiting = notices.drain()
        if waiting:
            result["system_message"] = "\n".join(waiting)
        auto = load_automation(workspace)
        if not auto.on("auto_skills"):
            return result
        manager = _manager_for(workspace)
        text, names = context_for(manager, str(payload.get("prompt") or ""), str(payload.get("session_id") or ""),
                                  auto.max_auto_skills)
        if text:
            result["additional_context"] = text
            line = "Auto-loaded skills: " + ", ".join(names)
            result["system_message"] = "\n".join(filter(None, [result.get("system_message"), line]))
        return result

    return handle
