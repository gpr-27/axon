"""Done-when goals, checked client-side (the idea of Managed Agents "outcomes": a rubric, a separate grader, a revise loop).

``/goal <criteria>`` states what "done" means. While a goal is active, when the main agent ends a turn Axon runs a
GRADER: a fresh-context model call that sees only the criteria, the user's request, the agent's final answer and
objective evidence Axon gathers itself (git diff of the files changed this turn, the last command outputs, small
file contents). It must answer strict JSON. ``needs_revision`` hands the per-criterion gaps back to the main agent
inside the same run (same step limit, same budget caps); ``satisfied`` ends the goal. The goal applies to the main
agent only, never to subagents.

Everything here is best effort and never raises into the agent loop.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROUNDS = 3
MAX_ROUNDS = 10
GRADER_MAX_TOKENS = 1500
EVIDENCE_CHARS = 14_000          # the whole evidence block
DIFF_CHARS = 6_000
COMMAND_CHARS = 1_800
FILE_CHARS = 2_500
MAX_COMMANDS = 4
MAX_FILES = 6
RECORD = "goal"                  # session record type
FOLLOWUP_MARK = "[Goal check — not done yet]:"

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "Patch", "NotebookEdit")
SHELL_TOOLS = ("bash", "shell", "powershell")
PATH_KEYS = ("path", "file_path", "notebook_path")
# A goal that asks for an answer, not for work, is still graded on a turn that changed nothing.
_ANSWER_GOAL = re.compile(r"\b(answer|explain|explanation|describe|summari[sz]e|summary|report|list|tell|review|"
                          r"recommend|compare|analy[sz]e|identify|find out|why|what|how)\b", re.I)

GRADER_SYSTEM = (
    "You are a strict, independent grader. You did not write the work. Decide whether the DONE-WHEN criteria below "
    "are met, using only the final answer and the objective evidence supplied. Do not trust claims in the answer "
    "that the evidence does not back up; a claim with no evidence is not met. If a criterion cannot be checked from "
    "the evidence, judge it from the final answer and say so in its evidence field.\n\n"
    "Split the criteria into separate checkable items (a checklist is already split). Reply with ONE JSON object and "
    "nothing else, no prose and no code fence:\n"
    '{"per_criterion":[{"criterion":"...","met":true,"evidence":"one short line","gap":"what is missing, empty when met"}],'
    '"verdict":"satisfied|needs_revision|failed_unrecoverable","summary":"one short line"}\n'
    "verdict is satisfied only when every criterion is met. Use failed_unrecoverable only when the work cannot meet "
    "a criterion no matter what the agent does next (impossible, or it needs something only the user can give). "
    "Keep every string under 200 characters."
)


# ─── state kept on the agent ────────────────────────────────────────────────

def goal_text(agent: Any) -> str:
    g = getattr(agent, "goal", None) or getattr(getattr(agent, "settings", None), "goal", None)
    return str(g).strip() if g else ""


def goal_rounds(agent: Any) -> int:
    try:
        n = int(getattr(agent, "goal_rounds", DEFAULT_ROUNDS) or DEFAULT_ROUNDS)
    except (TypeError, ValueError):
        n = DEFAULT_ROUNDS
    return max(1, min(MAX_ROUNDS, n))


def set_rounds(agent: Any, n: int) -> int:
    agent.goal_rounds = max(1, min(MAX_ROUNDS, int(n)))
    persist(agent)
    return agent.goal_rounds


def set_goal(agent: Any, text: str) -> None:
    agent.goal = " ".join(str(text).split())[:2000] or None
    agent.goal_last = None
    persist(agent)


def clear_goal(agent: Any) -> None:
    agent.goal = None
    agent.goal_last = None
    persist(agent, cleared=True)


def persist(agent: Any, cleared: bool = False) -> None:
    """One session record per change, so /resume restores the goal, its round limit and the last verdict."""
    try:
        agent.session.append(RECORD, {"text": "" if cleared else goal_text(agent), "rounds": goal_rounds(agent),
                                      "last": None if cleared else getattr(agent, "goal_last", None),
                                      "cleared": bool(cleared or not goal_text(agent))})
    except Exception:
        pass


def restore_goal(agent: Any, session_id: str) -> str:
    """After /resume: the last goal record of the transcript becomes the active goal. Returns its text."""
    text, rounds, last, found = "", DEFAULT_ROUNDS, None, False
    try:
        from axon.session.store import iter_records
        path = agent.session._file(session_id) if hasattr(agent.session, "_file") else agent.session.session_dir / f"{session_id}.jsonl"
        for rec in iter_records(path):
            data = rec.get("data")
            if rec.get("type") == RECORD and isinstance(data, dict):
                found = True
                text = "" if data.get("cleared") else str(data.get("text") or "")
                rounds = int(data.get("rounds") or DEFAULT_ROUNDS)
                last = data.get("last") if isinstance(data.get("last"), dict) else None
    except Exception:
        return ""
    if not found:
        return goal_text(agent)           # an older transcript: keep what is set
    agent.goal = text or None
    agent.goal_rounds = max(1, min(MAX_ROUNDS, rounds))
    agent.goal_last = last if text else None
    try:
        if (text or None) != (getattr(agent.settings, "goal", None) or None):
            agent.settings = agent.settings.model_copy(update={"goal": text or None})
    except Exception:
        pass
    return text


# ─── /goal argument parsing ────────────────────────────────────────────────

def parse_goal_arg(arg: str) -> tuple[str, Any]:
    """('show', None) | ('clear', None) | ('budget', n or None when invalid) | ('set', text)."""
    a = " ".join(str(arg or "").split())
    if not a:
        return "show", None
    low = a.lower()
    if low in ("clear", "off", "none", "-", "reset"):
        return "clear", None
    m = re.match(r"budget(?:\s+|=)(\S+)$", low)
    if m or low == "budget":
        try:
            return "budget", int(m.group(1)) if m else None
        except (ValueError, AttributeError):
            return "budget", None
    return "set", a


# ─── evidence ───────────────────────────────────────────────────────────────

def _redact(text: str) -> str:
    try:
        from axon.features.memory_files import redact
        return redact(text)
    except Exception:
        return text


def _clip(text: str, limit: int, tail: bool = False) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return ("[...] " + text[-limit:]) if tail else (text[:limit] + " [...]")


def _turn_steps(agent: Any) -> list[Any]:
    tr = getattr(agent, "_trace", None)
    try:
        return list(tr.steps) if tr is not None else []
    except Exception:
        return []


def turn_activity(agent: Any) -> tuple[list[str], list[dict[str, Any]], bool]:
    """(files written this turn, shell calls this turn, whether the turn did any work) from the turn trace."""
    files: list[str] = []
    cmds: list[dict[str, Any]] = []
    for st in _turn_steps(agent):
        if getattr(st, "kind", "") != "tool":
            continue
        d = st.data or {}
        name = str(d.get("name") or "")
        inp = d.get("input") if isinstance(d.get("input"), dict) else {}
        if name in WRITE_TOOLS and not d.get("is_error"):
            p = next((str(inp.get(k)) for k in PATH_KEYS if inp.get(k)), "")
            if p and p not in files:
                files.append(p)
        elif name.lower() in SHELL_TOOLS:
            full = ""
            try:
                full = str((getattr(agent, "_full_outputs", None) or {}).get(d.get("id")) or "")
            except Exception:
                pass
            cmds.append({"command": str(inp.get("command") or inp.get("cmd") or ""), "error": bool(d.get("is_error")),
                         "output": full or str(d.get("result_preview") or "")})
    return files, cmds, bool(files or cmds)


def _git(workspace: Path, *args: str, timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(workspace), capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def gather_evidence(agent: Any) -> str:
    """Objective, bounded, redacted evidence about this turn: diff, command outputs, small written files."""
    files, cmds, _ = turn_activity(agent)
    ws = Path(getattr(agent.settings, "workspace", ".") or ".")
    parts: list[str] = []
    if files:
        rels: list[str] = []
        for p in files[:MAX_FILES * 2]:
            try:
                pp = Path(p)
                rels.append(str(pp.resolve().relative_to(ws.resolve())) if pp.is_absolute() else p)
            except Exception:
                rels.append(p)
        stat = _git(ws, "diff", "--stat", "--", *rels).strip()
        diff = _git(ws, "diff", "--unified=2", "--", *rels)
        parts.append("## Files written this turn\n" + "\n".join(f"- {r}" for r in rels))
        if stat:
            parts.append("## git diff --stat\n" + _clip(stat, 1_200))
        if diff.strip():
            parts.append("## git diff (bounded)\n" + _clip(diff, DIFF_CHARS))
        shown = 0
        for r in rels:
            if shown >= MAX_FILES:
                break
            fp = ws / r
            try:
                if fp.is_file() and fp.stat().st_size <= FILE_CHARS * 2:
                    parts.append(f"## File {r}\n" + _clip(fp.read_text(encoding="utf-8", errors="replace"), FILE_CHARS))
                    shown += 1
                elif fp.is_file():
                    parts.append(f"## File {r}\n(large file, {fp.stat().st_size:,} bytes, not included)")
            except Exception:
                continue
    for c in cmds[-MAX_COMMANDS:]:
        status = "failed" if c["error"] else "ran"
        parts.append(f"## Command ({status}): {_clip(c['command'], 300)}\n{_clip(c['output'], COMMAND_CHARS, tail=True)}")
    if not parts:
        parts.append("(no files were written and no commands were run this turn)")
    return _clip(_redact("\n\n".join(parts)), EVIDENCE_CHARS)


def build_request(goal: str, request: str, answer: str, evidence: str, redo: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    body = (f"# DONE-WHEN criteria\n{_redact(goal)}\n\n# The user's request\n{_clip(_redact(request), 3_000)}\n\n"
            f"# The agent's final answer\n{_clip(_redact(answer), 4_000)}\n\n# Objective evidence gathered by the harness\n{evidence}")
    if redo:
        body += "\n\nYour previous reply was not valid JSON. Reply again with ONLY the JSON object."
    return [{"type": "text", "text": GRADER_SYSTEM}], [{"role": "user", "content": body}]


# ─── verdict parsing ────────────────────────────────────────────────────────

VERDICTS = ("satisfied", "needs_revision", "failed_unrecoverable")


def parse_verdict(text: str) -> dict[str, Any] | None:
    """The grader's JSON, normalised; None when it is not usable. Tolerates a code fence or prose around the object."""
    raw = str(text or "").strip()
    if not raw:
        return None
    obj: Any = None
    for cand in (raw, re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)):
        try:
            obj = json.loads(cand)
            break
        except ValueError:
            obj = None
    if obj is None:
        i, j = raw.find("{"), raw.rfind("}")
        if 0 <= i < j:
            try:
                obj = json.loads(raw[i:j + 1])
            except ValueError:
                obj = None
    if not isinstance(obj, dict):
        return None
    rows = []
    for c in obj.get("per_criterion") or []:
        if isinstance(c, dict) and str(c.get("criterion") or "").strip():
            rows.append({"criterion": _clip(str(c["criterion"]).strip(), 240), "met": c.get("met") is True,
                         "evidence": _clip(str(c.get("evidence") or "").strip(), 240),
                         "gap": _clip(str(c.get("gap") or "").strip(), 240)})
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        if not rows:
            return None
        verdict = "satisfied" if all(r["met"] for r in rows) else "needs_revision"
    if verdict == "satisfied" and any(not r["met"] for r in rows):
        verdict = "needs_revision"           # never trust "satisfied" next to an unmet criterion
    if verdict == "needs_revision" and rows and all(r["met"] for r in rows):
        verdict = "satisfied"
    return {"per_criterion": rows, "verdict": verdict, "summary": _clip(str(obj.get("summary") or "").strip(), 240)}


def invalid_verdict(note: str = "") -> dict[str, Any]:
    return {"per_criterion": [], "verdict": "needs_revision", "invalid": True,
            "summary": note or "The grader did not return valid JSON, so nothing was verified."}


def unmet(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in verdict.get("per_criterion") or [] if not c.get("met")]


def followup_text(verdict: dict[str, Any], rnd: int, rounds: int) -> str:
    gaps = unmet(verdict)
    if gaps:
        lines = [f"- {c['criterion']}: {c.get('gap') or 'not shown to be met'}" for c in gaps]
    else:
        lines = ["- The grader could not verify the goal. Re-check every criterion yourself and show the evidence "
                 "(run the check, quote the output)."]
    return (f"{FOLLOWUP_MARK}\nThe independent grader says these criteria are not met yet (round {rnd} of {rounds}):\n"
            + "\n".join(lines) + "\nFix them, then finish with a short answer. Do not claim success without evidence.")


# ─── the grader call ────────────────────────────────────────────────────────

@dataclass
class GradeResult:
    verdict: dict[str, Any]
    model: str = ""
    usage: Any = None
    cost: float = 0.0
    duration_s: float = 0.0
    interrupted: bool = False


def grader_model(agent: Any) -> str:
    s = agent.settings
    return str(getattr(s, "small_model", None) or s.model)


def _grader_provider(agent: Any, model: str) -> Any:
    override = getattr(agent, "goal_provider", None)
    if override is not None:
        return override
    if model != agent.settings.model:
        try:
            from axon.agent.loop import make_provider
            return make_provider(model, agent.settings, role="goal")
        except Exception:
            pass
    return agent.provider


def _ask(agent: Any, prov: Any, model: str, system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> tuple[str, Any, float]:
    from axon.agent.loop import _as_float, _provider_lock
    from axon.agent.state import record_usage
    with _provider_lock(prov):
        for _ in prov.stream(model=model, system=system, messages=messages, tools=[], max_tokens=GRADER_MAX_TOKENS,
                             effort="reflex", thinking=False):
            pass
        turn = prov.finalize()
    cost = 0.0
    usage = getattr(turn, "usage", None)
    if usage is not None:
        cost = _as_float(record_usage(agent.ledger, model, usage, source="goal"))
        agent._log_usage(model, usage, cost, "goal")
    return str(getattr(turn, "text", "") or ""), usage, cost


def grade(agent: Any, goal: str, request: str, answer: str) -> GradeResult:
    """One grader call (a second one when the JSON is invalid). Never raises; Esc gives ``interrupted``."""
    model = grader_model(agent)
    t0 = time.time()
    total_in = total_out = 0
    usage: Any = None
    cost = 0.0
    verdict: dict[str, Any] | None = None
    try:
        evidence = gather_evidence(agent)
        prov = _grader_provider(agent, model)
        for attempt in (0, 1):
            system, messages = build_request(goal, request, answer, evidence, redo=bool(attempt))
            text, u, c = _ask(agent, prov, model, system, messages)
            cost += c
            if u is not None:
                total_in += int(getattr(u, "input", 0) or 0)
                total_out += int(getattr(u, "output", 0) or 0)
                usage = u if usage is None else _sum_usage(usage, u)
            verdict = parse_verdict(text)
            if verdict is not None:
                break
        if verdict is None:
            verdict = invalid_verdict()
    except KeyboardInterrupt:
        return GradeResult(invalid_verdict("The goal check was interrupted."), model, usage, cost, time.time() - t0, True)
    except Exception as e:                      # a grader outage must never fail the turn
        verdict = invalid_verdict(f"The grader call failed ({type(e).__name__}).")
    return GradeResult(verdict, model, usage, cost, time.time() - t0)


def _sum_usage(a: Any, b: Any) -> Any:
    try:
        from axon.providers.base import Usage
        return Usage(input=int(a.input or 0) + int(b.input or 0), output=int(a.output or 0) + int(b.output or 0),
                     cache_read=int(getattr(a, "cache_read", 0) or 0) + int(getattr(b, "cache_read", 0) or 0),
                     cache_write=int(getattr(a, "cache_write", 0) or 0) + int(getattr(b, "cache_write", 0) or 0))
    except Exception:
        return b


# ─── the loop hook ──────────────────────────────────────────────────────────

@dataclass
class GoalDecision:
    follow_up: str = ""          # non-empty: inject as a user turn and continue the same run
    stop: bool = False           # True: end the turn with ``interrupted``
    graded: bool = False


def _emit(agent: Any, **fields: Any) -> None:
    from axon.agent.trace import GoalCheckEvent
    agent._emit(GoalCheckEvent(**fields))


def should_grade(agent: Any, goal: str) -> tuple[bool, str]:
    """(grade this turn?, reason when not). Plan mode and read-only turns are skipped unless the goal asks for an answer."""
    _, _, worked = turn_activity(agent)
    if getattr(agent.settings, "mode", "") == "plan" and not _ANSWER_GOAL.search(goal):
        return False, "plan mode, nothing to verify"
    if not worked and not _ANSWER_GOAL.search(goal):
        return False, "read-only turn, nothing to verify"
    return True, ""


def check_after_answer(agent: Any, goal: str, request: str, answer: str, round_no: int, iterations: int, limit: int) -> GoalDecision:
    """Called when the main agent ended its turn with an answer. Round numbers count grader calls in this turn."""
    rounds = goal_rounds(agent)
    ok, why = should_grade(agent, goal)
    if not ok:
        _emit(agent, goal=goal, iteration=iterations, phase="skipped", round=round_no, max_rounds=rounds, note=why)
        return GoalDecision()
    rnd = round_no + 1
    _emit(agent, goal=goal, iteration=iterations, phase="start", round=rnd, max_rounds=rounds)
    res = grade(agent, goal, request, answer)
    v = res.verdict
    last = {"verdict": v["verdict"], "round": rnd, "max_rounds": rounds, "summary": v.get("summary", ""),
            "per_criterion": v.get("per_criterion", []), "at": time.time()}
    base = dict(goal=goal, iteration=iterations, phase="verdict", round=rnd, max_rounds=rounds, verdict=v["verdict"],
                criteria=tuple(v.get("per_criterion") or ()), summary=v.get("summary", ""), model=res.model,
                usage=res.usage, cost=res.cost, duration_s=res.duration_s)
    if res.interrupted:
        _emit(agent, **{**base, "verdict": "interrupted", "note": "goal check cancelled"})
        return GoalDecision(stop=True, graded=True)
    agent.goal_last = last
    if v["verdict"] == "satisfied":
        _emit(agent, **{**base, "note": "goal met"})
        clear_goal(agent)
        try:
            from axon.commands.common import update_settings
            update_settings(agent, goal=None)
        except Exception:
            pass
        return GoalDecision(graded=True)
    if v["verdict"] == "failed_unrecoverable":
        _emit(agent, **{**base, "note": "stopped: the grader says this cannot be met as it stands"})
        persist(agent)
        return GoalDecision(graded=True)
    if rnd >= rounds:
        _emit(agent, **{**base, "note": f"round limit reached, {len(unmet(v)) or 'some'} criteria still unmet"})
        persist(agent)
        return GoalDecision(graded=True)
    if iterations >= limit:
        _emit(agent, **{**base, "note": "step limit reached, not revising"})
        persist(agent)
        return GoalDecision(graded=True)
    _emit(agent, **base)
    persist(agent)
    return GoalDecision(follow_up=followup_text(v, rnd, rounds), graded=True)


# ─── text for /goal and the status views ────────────────────────────────────

def describe(agent: Any) -> list[str]:
    """Plain lines for ``/goal`` with no arguments."""
    g = goal_text(agent)
    if not g:
        return ["No goal set."]
    out = [f"Goal: {g}", f"Revise rounds: up to {goal_rounds(agent)} (default {DEFAULT_ROUNDS}, max {MAX_ROUNDS})"]
    last = getattr(agent, "goal_last", None)
    if isinstance(last, dict):
        out.append(f"Last check: {last.get('verdict', '?').replace('_', ' ')} (round {last.get('round')}/{last.get('max_rounds')})"
                   + (f" - {last['summary']}" if last.get("summary") else ""))
        for c in last.get("per_criterion") or []:
            out.append(f"  {'ok' if c.get('met') else 'no'}  {c.get('criterion')}" + ("" if c.get("met") else f" - {c.get('gap')}"))
    else:
        out.append("Not checked yet: the grader runs when the agent finishes a turn that did work.")
    return out

