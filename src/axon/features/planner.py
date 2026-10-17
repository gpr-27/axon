"""
Planner: PLAN -> APPROVE -> EXECUTE.

  /plan-run <task> [--yes]   (alias /solve)  ask the model for a step plan (no tools), review it
                             (approve / edit / delete / add / cancel), then run each step as an agent
                             turn with a live checklist, optional per-step verification and a summary.
  /plans                     list saved plans for this workspace
  /plan-resume [id]          continue the unfinished steps of a saved plan (latest unfinished by default)

Plans and results are saved to ``storage.project_dir(ws)/plans/<id>.json`` (never the workspace).
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from axon.features import FeatureCommand
from axon.features import solver
from axon.features.solver import _c, _g, _out, _table, fmt_cost, fmt_duration

PLAN_SYSTEM = """You are a senior software engineer planning work for an autonomous coding agent that can read,
search, edit files and run shell commands in the user's workspace. Produce a concise, ordered plan
that fully solves the task end to end.

Rules:
- 2 to 10 steps. Each step is one coherent unit of work the agent can complete in one turn.
- Start with investigation only if it is genuinely needed; end with verification.
- "verify" says how to confirm the step worked. When a shell command proves it, put ONLY that command
  in backticks, e.g. "`pytest -q tests/test_api.py`". Otherwise describe the check briefly, or "".
- Do not write code in the plan.

Respond with STRICT JSON only, no prose, no code fences:
{"goal": "<one sentence>", "steps": [{"title": "<imperative, short>", "detail": "<what and where>", "verify": "<how to confirm>"}]}"""

STATUS_GLYPHS = {
    "pending": ("☐", "[ ]"), "running": ("◐", "[~]"), "done": ("☑", "[x]"),
    "failed": ("✗", "[!]"), "skipped": ("⊘", "[-]"),
}
_FAILED_STOPS = {"error", "blocked", "interrupted", "budget_exceeded", "token_budget", "max_iterations"}


# ─── Model ────────────────────────────────────────────────────────────────
@dataclass
class PlanStep:
    title: str
    detail: str = ""
    verify: str = ""
    status: str = "pending"          # pending | running | done | failed | skipped
    tools: list[str] = field(default_factory=list)
    cost: float = 0.0
    duration: float = 0.0
    attempts: int = 0
    summary: str = ""
    error: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlanStep:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        step = cls(**known)
        step.title = str(step.title or "").strip() or "Untitled step"
        step.tools = [str(t) for t in (step.tools or [])]
        return step


@dataclass
class Plan:
    id: str
    task: str
    goal: str
    steps: list[PlanStep]
    created: float = field(default_factory=time.time)
    updated: float = 0.0
    status: str = "draft"            # draft | running | done | failed | aborted | cancelled
    workspace: str = ""
    planning_cost: float = 0.0
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plan:
        return cls(
            id=str(d.get("id", "")), task=str(d.get("task", "")), goal=str(d.get("goal", "")),
            steps=[PlanStep.from_dict(s) for s in d.get("steps") or [] if isinstance(s, dict)],
            created=float(d.get("created") or 0.0), updated=float(d.get("updated") or 0.0),
            status=str(d.get("status") or "draft"), workspace=str(d.get("workspace") or ""),
            planning_cost=float(d.get("planning_cost") or 0.0), model=str(d.get("model") or ""),
        )

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.status in ("done", "skipped"))

    @property
    def unfinished(self) -> bool:
        return any(s.status not in ("done", "skipped") for s in self.steps)


# ─── Parsing the model's plan ─────────────────────────────────────────────
def _balanced_objects(text: str) -> list[str]:
    """Every top-level {...} substring (string-aware brace matching)."""
    out: list[str] = []
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:i + 1])
    return out


def _steps_from(data: Any) -> tuple[str, list[PlanStep]] | None:
    if isinstance(data, list):
        data = {"steps": data}
    if not isinstance(data, dict):
        return None
    raw = data.get("steps") or data.get("plan") or []
    steps: list[PlanStep] = []
    for s in raw if isinstance(raw, list) else []:
        if isinstance(s, str) and s.strip():
            steps.append(PlanStep(title=s.strip()))
        elif isinstance(s, dict):
            title = str(s.get("title") or s.get("name") or s.get("step") or "").strip()
            detail = str(s.get("detail") or s.get("description") or s.get("details") or "").strip()
            if not title and detail:
                title, detail = detail[:80], detail
            if title:
                steps.append(PlanStep(title=title, detail=detail, verify=str(s.get("verify") or s.get("check") or "").strip()))
    if not steps:
        return None
    return str(data.get("goal") or data.get("objective") or "").strip(), steps


def extract_plan_json(text: str) -> tuple[str, list[PlanStep]] | None:
    """Parse {"goal","steps":[...]} from raw model text (handles code fences and trailing prose)."""
    if not text:
        return None
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json|JSON)?\s*\n?(.*?)```", text, re.S):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    candidates.extend(sorted(_balanced_objects(text), key=len, reverse=True))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:
            # tolerate trailing commas
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", cand))
            except Exception:
                continue
        got = _steps_from(data)
        if got:
            return got
    return None


_NUM_RE = re.compile(r"^\s*(?:(?:step\s*)?\d+[.):]|[-*•]|#{1,4}\s*(?:step\s*)?\d+[.):]?)\s+(?P<body>.+)$", re.I)


def parse_numbered_plan(text: str) -> tuple[str, list[PlanStep]] | None:
    """Fallback: '1. Title - detail' / '- Title' lists, with indented 'Verify:' lines attached."""
    goal = ""
    steps: list[PlanStep] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        gm = re.match(r"^\s*\**goal\**\s*[:\-]\s*(.+)$", line, re.I)
        if gm and not steps:
            goal = gm.group(1).strip().strip("*")
            continue
        vm = re.match(r"^\s+[-*]?\s*\**verify\**\s*[:\-]\s*(.+)$", line, re.I)
        if vm and steps:
            steps[-1].verify = vm.group(1).strip()
            continue
        m = _NUM_RE.match(line)
        indented = raw.startswith(("  ", "\t"))
        if m and (not indented or not steps):
            body = m.group("body").strip().replace("**", "")
            title, detail = body, ""
            split = re.split(r"\s+[-\u2013\u2014]\s+|:\s+", body, maxsplit=1)
            if len(split) == 2 and split[0].strip():
                title, detail = split[0], split[1]
            steps.append(PlanStep(title=title.strip(), detail=detail.strip()))
        elif steps and indented:
            extra = line.strip().lstrip("-* ").strip()
            steps[-1].detail = (steps[-1].detail + " " + extra).strip()
    return (goal, steps) if steps else None


def parse_plan_text(text: str) -> tuple[str, list[PlanStep]] | None:
    return extract_plan_json(text) or parse_numbered_plan(text)


# ─── Asking the model ─────────────────────────────────────────────────────
def _workspace(agent: Any) -> Path:
    return solver._workspace(agent)


def _workspace_overview(ws: Path, limit: int = 60) -> str:
    try:
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in ws.iterdir()
                       if not p.name.startswith(".") or p.name in (".github",))
    except Exception:
        return ""
    names = [n for n in names if n not in ("node_modules/", "__pycache__/", "venv/", "dist/", "build/", "target/")]
    more = f" (+{len(names) - limit} more)" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


def _record_usage(agent: Any, model: str, usage: Any) -> None:
    try:
        from axon.agent.state import record_usage
        record_usage(getattr(agent, "ledger", None), model, usage, source="planner")
    except Exception:
        ledger = getattr(agent, "ledger", None)
        try:
            if ledger is not None and usage is not None:
                ledger.record(model, usage)
        except Exception:
            pass


def _provider_lock(provider: Any) -> Any:
    try:
        from axon.agent.loop import _provider_lock as lock_for
        return lock_for(provider)
    except Exception:
        import contextlib
        return contextlib.nullcontext()


def request_plan_text(agent: Any, task: str) -> tuple[str, Any]:
    """One tool-less model call. Returns (text, usage)."""
    provider = getattr(agent, "provider", None)
    settings = getattr(agent, "settings", None)
    if provider is None or not hasattr(provider, "stream"):
        raise RuntimeError("no model provider is configured")
    ws = _workspace(agent)
    model = str(getattr(settings, "model", "") or "")
    overview = _workspace_overview(ws)
    user = f"Task: {task}\n\nWorkspace: {ws}" + (f"\nTop-level entries: {overview}" if overview else "")
    with _provider_lock(provider):
        stream = provider.stream(
            model=model,
            system=[{"type": "text", "text": PLAN_SYSTEM}],
            messages=[{"role": "user", "content": user}],
            tools=[],
            max_tokens=4096,
            effort=getattr(settings, "effort", None),
            thinking=False,
        )
        chunks: list[str] = []
        for ev in stream:
            t = getattr(ev, "text", None)
            if isinstance(t, str) and type(ev).__name__ == "TextDelta":
                chunks.append(t)
        turn = provider.finalize()
    text = getattr(turn, "text", "") or "".join(chunks)
    usage = getattr(turn, "usage", None)
    _record_usage(agent, getattr(turn, "model", None) or model, usage)
    return text, usage


def create_plan(agent: Any, task: str) -> Plan:
    before = solver.ledger_total(agent)
    text, _ = request_plan_text(agent, task)
    parsed = parse_plan_text(text)
    if not parsed:
        raise ValueError("the model did not return a usable plan:\n" + text.strip()[:800])
    goal, steps = parsed
    cost = solver.cost_delta(before, agent)
    return Plan(id=new_plan_id(), task=task, goal=goal or task, steps=steps[:20],
                workspace=str(_workspace(agent)), planning_cost=float(cost or 0),
                model=str(getattr(getattr(agent, "settings", None), "model", "") or ""))


# ─── Storage ──────────────────────────────────────────────────────────────
def new_plan_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def plans_dir(ws: Path) -> Path:
    from axon import storage
    d = storage.project_dir(ws) / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_plan(ws: Path, plan: Plan) -> Path:
    plan.updated = time.time()
    path = plans_dir(ws) / f"{plan.id}.json"
    try:
        old_status = json.loads(path.read_text(encoding="utf-8")).get("status") if path.exists() else None
    except (OSError, ValueError):
        old_status = None
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    if old_status != plan.status:
        from axon.session.library import record_run
        record_run("plan", {"id": plan.id, "action": "created" if old_status is None else plan.status,
                            "status": plan.status, "task": plan.task, "goal": plan.goal, "path": str(path),
                            "steps": [s.title for s in plan.steps]},
                   workspace=ws)
    return path


def list_plans(ws: Path) -> list[Plan]:
    out: list[Plan] = []
    for p in sorted(plans_dir(ws).glob("*.json"), reverse=True):
        try:
            out.append(Plan.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def load_plan(ws: Path, plan_id: str) -> Plan | None:
    plans = list_plans(ws)
    for p in plans:
        if p.id == plan_id:
            return p
    matches = [p for p in plans if p.id.startswith(plan_id)]
    return matches[0] if len(matches) == 1 else None


# ─── Rendering ────────────────────────────────────────────────────────────
def _glyph(status: str) -> str:
    uni, asc = STATUS_GLYPHS.get(status, STATUS_GLYPHS["pending"])
    color = {"done": "SUCCESS", "failed": "ERROR", "running": "INFO", "skipped": "MUTED"}.get(status, "MUTED")
    return f"{_c(color)}{_g(uni, asc)}{_c('RST')}"


def plan_lines(plan: Plan, checklist: bool = False) -> list[str]:
    lines = [f"  {_c('PRIMARY')}{_c('BOLD')}Goal{_c('RST')}  {plan.goal}"]
    for i, s in enumerate(plan.steps, 1):
        lead = f"{_glyph(s.status)} " if checklist else ""
        lines.append(f"  {lead}{_c('BOLD')}{i:>2}. {s.title}{_c('RST')}")
        if s.detail:
            lines.append(f"      {_c('MUTED')}{s.detail[:300]}{_c('RST')}")
        if s.verify:
            lines.append(f"      {_c('ACCENT')}verify:{_c('RST')} {_c('MUTED')}{s.verify[:200]}{_c('RST')}")
    return lines


def print_checklist(plan: Plan) -> None:
    _out()
    for i, s in enumerate(plan.steps, 1):
        extra = ""
        if s.status in ("done", "failed") and s.duration:
            extra = f"  {_c('MUTED')}{fmt_duration(s.duration)} · {fmt_cost(s.cost)}{_c('RST')}"
        _out(f"  {_glyph(s.status)} {i:>2}. {s.title}{extra}")
    _out()


def print_summary(plan: Plan) -> None:
    rows = []
    for i, s in enumerate(plan.steps, 1):
        tools = ", ".join(_compact_tools(s.tools)) or "-"
        rows.append([str(i), s.title[:48], f"{_glyph(s.status)} {s.status}", tools[:40],
                     fmt_cost(s.cost), fmt_duration(s.duration)])
    _out()
    for line in _table(["#", "step", "status", "tools", "cost", "time"], rows, right=(4, 5)):
        _out(line)
    total_cost = sum(s.cost for s in plan.steps) + plan.planning_cost
    total_time = sum(s.duration for s in plan.steps)
    color = "SUCCESS" if plan.status == "done" else "ERROR" if plan.status in ("failed", "aborted") else "WARNING"
    _out(f"\n  {_c(color)}{_c('BOLD')}Plan {plan.status}{_c('RST')} · {plan.done_count}/{len(plan.steps)} steps · "
         f"{fmt_duration(total_time)} · {fmt_cost(total_cost)} (incl. planning {fmt_cost(plan.planning_cost)})"
         f"  {_c('MUTED')}id {plan.id}{_c('RST')}")
    if plan.unfinished and plan.status != "cancelled":
        _out(f"  {_c('MUTED')}Resume with /plan-resume {plan.id}{_c('RST')}")


def _compact_tools(tools: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    for t in tools:
        counts[t] = counts.get(t, 0) + 1
    return [f"{k}×{v}" if v > 1 else k for k, v in counts.items()]


# ─── Review (approve / edit / delete / add / cancel) ──────────────────────
def _interactive() -> bool:
    try:
        from axon import platform as plat
        if not plat.is_interactive():
            return False
    except Exception:
        pass
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def apply_edit(plan: Plan, action: str, index: int | None = None, text: str = "") -> bool:
    """Pure plan mutation used by the review UI. Returns True when the plan changed."""
    if action == "delete" and index is not None and 0 <= index < len(plan.steps):
        del plan.steps[index]
        return True
    if action == "edit" and index is not None and 0 <= index < len(plan.steps) and text.strip():
        title, _, detail = text.partition(" -- ")
        plan.steps[index].title = title.strip()
        if detail.strip():
            plan.steps[index].detail = detail.strip()
        return True
    if action == "add" and text.strip():
        title, _, detail = text.partition(" -- ")
        step = PlanStep(title=title.strip(), detail=detail.strip())
        pos = len(plan.steps) if index is None else max(0, min(index, len(plan.steps)))
        plan.steps.insert(pos, step)
        return True
    return False


def review_plan(plan: Plan) -> bool:
    """Interactive approval. Returns True to execute, False to cancel."""
    try:
        from axon.ui.picker import SelectItem, Selector
    except Exception:
        return False
    actions = [("approve", "Approve & run", "execute every step"),
               ("edit", "Edit a step", "rewrite title (use ' -- ' to set detail)"),
               ("delete", "Delete a step", ""),
               ("add", "Add a step", "append, or insert at a position"),
               ("cancel", "Cancel", "keep the plan saved as draft")]
    items = [SelectItem(label=label, value=key, hint=hint) for key, label, hint in actions]

    def ask_index(sel: Any, verb: str) -> int | None:
        raw = sel.prompt_line(f"Step number to {verb} (1-{len(plan.steps)}): ")
        if raw is None or not raw.isdigit():
            return None
        n = int(raw) - 1
        return n if 0 <= n < len(plan.steps) else None

    def on_key(sel: Any, key: str) -> bool:
        if key != "enter" or sel.current() is None:
            return False
        what = sel.current().value
        if what in ("approve", "cancel"):
            if what == "approve" and not plan.steps:
                sel.flash("The plan has no steps.", _c("ERROR"))
                return True
            sel.finish(what)
            return True
        if what == "delete":
            idx = ask_index(sel, "delete")
            if idx is not None and sel.confirm(f"Delete step {idx + 1}?"):
                apply_edit(plan, "delete", idx)
                sel.flash(f"Deleted step {idx + 1}.")
        elif what == "edit":
            idx = ask_index(sel, "edit")
            if idx is not None:
                text = sel.prompt_line("New title: ", default=plan.steps[idx].title)
                if text and apply_edit(plan, "edit", idx, text):
                    sel.flash(f"Updated step {idx + 1}.")
        elif what == "add":
            text = sel.prompt_line("New step (title -- detail): ")
            if text:
                pos = sel.prompt_line(f"Insert at position (1-{len(plan.steps) + 1}, enter = end): ", allow_empty=True)
                index = int(pos) - 1 if pos and pos.isdigit() else None
                apply_edit(plan, "add", index, text)
                sel.flash("Step added.")
        return True

    sel = Selector(items, title="Review plan", subtitle=f"{len(plan.steps)} steps",
                   extra_lines=lambda s: [""] + plan_lines(plan) + [""], on_key=on_key,
                   footer="↑↓ move · enter choose · esc cancel")
    result = sel.run()
    if hasattr(result, "value"):
        result = result.value
    return result == "approve"


# ─── Execution ────────────────────────────────────────────────────────────
def verify_command(verify: str) -> str | None:
    """Shell command named by a step's verify hint ("`pytest -q`", "run npm test"), else None."""
    v = (verify or "").strip()
    if not v:
        return None
    m = re.search(r"`([^`]+)`", v)
    if m:
        cmd = m.group(1).strip()
        return cmd if cmd and solver.looks_like_command(cmd) else None
    v = re.sub(r"^(?:run|execute)\s+", "", v, flags=re.I).rstrip(".")
    return v if solver.looks_like_command(v) else None


def step_prompt(plan: Plan, index: int, retry_note: str = "") -> str:
    step = plan.steps[index]
    done = [f"{i}. {s.title} - {s.status}" + (f": {s.summary[:200]}" if s.summary else "")
            for i, s in enumerate(plan.steps[:index], 1) if s.status in ("done", "skipped", "failed")]
    later = [f"{i}. {s.title}" for i, s in enumerate(plan.steps[index + 1:], index + 2)]
    parts = [f"You are executing an approved plan, one step at a time.\n\nGOAL: {plan.goal}",
             f"ORIGINAL TASK: {plan.task}" if plan.task and plan.task != plan.goal else ""]
    if done:
        parts.append("COMPLETED STEPS:\n" + "\n".join(done))
    parts.append(f"CURRENT STEP {index + 1}/{len(plan.steps)}: {step.title}" + (f"\n{step.detail}" if step.detail else ""))
    if step.verify:
        parts.append(f"VERIFY: {step.verify} - check this before you finish the step.")
    if later:
        parts.append("LATER STEPS (do NOT do these now):\n" + "\n".join(later))
    if retry_note:
        parts.append("PREVIOUS ATTEMPT FAILED:\n" + retry_note)
    parts.append("Do only the current step, then reply with a one-paragraph summary of what you changed.")
    return "\n\n".join(p for p in parts if p)


def _tools_from_trace(agent: Any, n_before: int) -> list[str]:
    traces = getattr(agent, "traces", None)
    if not isinstance(traces, list):
        return []
    names: list[str] = []
    for tr in traces[n_before:]:
        for st in getattr(tr, "steps", []) or []:
            if getattr(st, "kind", "") == "tool":
                data = getattr(st, "data", {}) or {}
                name = data.get("name") or data.get("tool")
                if name:
                    names.append(str(name))
    return names


ChooseFn = Callable[[PlanStep, int], str]


def _default_choose(step: PlanStep, index: int) -> str:
    if not _interactive():
        return "abort"
    try:
        from axon.ui.picker import SelectItem, Selector
        items = [SelectItem("Retry", "retry", hint="re-run the step with the failure details"),
                 SelectItem("Skip", "skip", hint="mark skipped and continue"),
                 SelectItem("Abort", "abort", hint="stop; resume later with /plan-resume")]
        res = Selector(items, title=f"Step {index + 1} failed", subtitle=step.error[:80]).run()
        return getattr(res, "value", None) or "abort"
    except Exception:
        return "abort"


def run_step(agent: Any, plan: Plan, index: int, ws: Path, retry_note: str = "") -> None:
    step = plan.steps[index]
    step.status = "running"
    step.attempts += 1
    step.error = ""
    n_traces = len(getattr(agent, "traces", []) or []) if isinstance(getattr(agent, "traces", None), list) else 0
    cost0 = solver.ledger_total(agent)
    t0 = time.time()
    try:
        result = solver._run_turn(agent, step_prompt(plan, index, retry_note))
    finally:
        step.duration += time.time() - t0
        delta = solver.cost_delta(cost0, agent)
        step.cost += float(delta or 0)
        step.tools.extend(_tools_from_trace(agent, n_traces))
    if result is None:
        step.status, step.error = "failed", "agent could not run the step"
        return
    step.summary = str(getattr(result, "final_text", "") or "").strip()[:600]
    if not step.tools and getattr(result, "tool_calls_count", 0):
        step.tools.extend(["tool"] * int(getattr(result, "tool_calls_count", 0)))
    stop = str(getattr(result, "stop_reason", ""))
    if getattr(result, "error", None) or stop in _FAILED_STOPS:
        step.status = "failed"
        step.error = str(getattr(result, "error", None) or f"turn stopped: {stop}")
        return
    cmd = verify_command(step.verify)
    if cmd:
        _out(f"  {_c('INFO')}{_g('◐', '*')}{_c('RST')} verifying step {index + 1}: {_c('MUTED')}{cmd}{_c('RST')}")
        res = solver.run_check(solver.Check(cmd, cmd, "custom", "plan"), ws, solver._timeout(agent))
        step.duration += res.duration
        if res.interrupted:
            raise KeyboardInterrupt
        if not res.ok:
            locs = "; ".join(f"{f.location} {f.message[:100]}" for f in res.failures[:5])
            step.status = "failed"
            step.error = f"verify `{cmd}` failed (exit {res.exit_code})" + (f": {locs}" if locs else "")
            step.summary += "\n\n" + solver.build_fix_prompt(ws, cmd, res.output, res.failures)[:4000]
            return
        _out(f"  {_c('SUCCESS')}{_g('✓', 'OK')} verify passed{_c('RST')} {_c('MUTED')}{fmt_duration(res.duration)}{_c('RST')}")
    step.status = "done"


def execute_plan(agent: Any, plan: Plan, ws: Path, choose: ChooseFn | None = None, max_retries: int = 2) -> Plan:
    choose = choose or _default_choose
    plan.status = "running"
    save_plan(ws, plan)
    try:
        for i, step in enumerate(plan.steps):
            if step.status in ("done", "skipped"):
                continue
            step.status = "running"
            print_checklist(plan)
            _out(f"  {_c('ACCENT')}{_c('BOLD')}Step {i + 1}/{len(plan.steps)}{_c('RST')} · {step.title}")
            retry_note = ""
            attempts0 = step.attempts  # attempts persist across /plan-resume; the retry limit is per run
            while True:
                run_step(agent, plan, i, ws, retry_note)
                save_plan(ws, plan)
                if step.status == "done":
                    break
                _out(f"  {_c('ERROR')}{_g('✗', 'X')} step {i + 1} failed{_c('RST')}: {step.error[:300]}")
                decision = choose(step, i)
                if decision == "retry":
                    if step.attempts - attempts0 <= max_retries:
                        retry_note = step.error + ("\n\n" + step.summary[-3000:] if step.summary else "")
                        continue
                    _out(f"  {_c('WARNING')}Retry limit ({max_retries}) reached for step {i + 1}.{_c('RST')}")
                if decision == "skip":
                    step.status = "skipped"
                    save_plan(ws, plan)
                    break
                plan.status = "aborted"
                save_plan(ws, plan)
                print_checklist(plan)
                print_summary(plan)
                return plan
    except KeyboardInterrupt:
        for s in plan.steps:
            if s.status == "running":
                s.status = "pending"
                s.error = "interrupted"
        plan.status = "aborted"
        save_plan(ws, plan)
        _out(f"\n  {_c('WARNING')}Stopped by Ctrl+C.{_c('RST')}")
        print_summary(plan)
        return plan
    plan.status = "failed" if any(s.status == "failed" for s in plan.steps) else "done"
    save_plan(ws, plan)
    print_checklist(plan)
    print_summary(plan)
    return plan


# ─── Commands ─────────────────────────────────────────────────────────────
def _split_yes(arg: str) -> tuple[str, bool]:
    yes = bool(re.search(r"(?:^|\s)(?:--yes|-y)(?:\s|$)", arg))
    return re.sub(r"(?:^|\s)(?:--yes|-y)(?=\s|$)", " ", arg).strip(), yes


def cmd_plan_run(agent: Any, arg: str) -> None:
    task, yes = _split_yes(arg)
    if not task:
        _out(f"  {_c('MUTED')}Usage: /plan-run <task> [--yes]   e.g. /solve add input validation to the signup API{_c('RST')}")
        return
    ws = _workspace(agent)
    _out(f"\n  {_c('PRIMARY')}{_c('BOLD')}Planning{_c('RST')} · {_c('MUTED')}one model call, no tools{_c('RST')}")
    t0 = time.time()
    try:
        plan = create_plan(agent, task)
    except KeyboardInterrupt:
        _out(f"  {_c('WARNING')}Planning stopped.{_c('RST')}")
        return
    except Exception as e:
        _out(f"  {_c('ERROR')}Could not create a plan: {e}{_c('RST')}")
        return
    _out(f"  {_c('MUTED')}{len(plan.steps)} steps · {fmt_duration(time.time() - t0)} · {fmt_cost(plan.planning_cost)}{_c('RST')}\n")
    save_plan(ws, plan)
    for line in plan_lines(plan):
        _out(line)
    _out()
    if not yes:
        if not _interactive():
            _out(f"  {_c('WARNING')}Not a terminal: plan saved as draft {plan.id}. "
                 f"Re-run with --yes to execute, or /plan-resume {plan.id}.{_c('RST')}")
            return
        if not review_plan(plan):
            plan.status = "cancelled"
            save_plan(ws, plan)
            _out(f"  {_c('MUTED')}Cancelled. Plan saved as {plan.id} (/plan-resume {plan.id} to run it later).{_c('RST')}")
            return
    if not plan.steps:
        _out(f"  {_c('WARNING')}The plan has no steps.{_c('RST')}")
        return
    execute_plan(agent, plan, ws)


def cmd_plans(agent: Any, arg: str) -> None:
    ws = _workspace(agent)
    plans = list_plans(ws)
    if not plans:
        _out(f"  {_c('MUTED')}No saved plans for this workspace. Start one with /plan-run <task>.{_c('RST')}")
        return
    rows = []
    for p in plans[: int(arg) if arg.strip().isdigit() else 20]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.created or 0))
        cost = sum(s.cost for s in p.steps) + p.planning_cost
        rows.append([p.id, when, p.status, f"{p.done_count}/{len(p.steps)}", fmt_cost(cost), (p.goal or p.task)[:60]])
    _out()
    for line in _table(["id", "created", "status", "steps", "cost", "goal"], rows, right=(3, 4)):
        _out(line)
    _out(f"\n  {_c('MUTED')}Stored in {plans_dir(ws)} · /plan-resume <id> continues unfinished steps{_c('RST')}")


def cmd_plan_resume(agent: Any, arg: str) -> None:
    ws = _workspace(agent)
    plan_id, _ = _split_yes(arg)
    if plan_id:
        plan = load_plan(ws, plan_id)
        if plan is None:
            _out(f"  {_c('ERROR')}No plan {plan_id!r}. See /plans.{_c('RST')}")
            return
    else:
        plan = next((p for p in list_plans(ws) if p.unfinished), None)
        if plan is None:
            _out(f"  {_c('MUTED')}No unfinished plans. See /plans.{_c('RST')}")
            return
    if not plan.unfinished:
        _out(f"  {_c('SUCCESS')}Plan {plan.id} is already complete.{_c('RST')}")
        print_summary(plan)
        return
    for s in plan.steps:
        if s.status in ("failed", "running"):
            s.status = "pending"
    _out(f"\n  {_c('PRIMARY')}{_c('BOLD')}Resuming{_c('RST')} {plan.id} · {plan.done_count}/{len(plan.steps)} done")
    for line in plan_lines(plan, checklist=True):
        _out(line)
    execute_plan(agent, plan, ws)


COMMANDS = [
    FeatureCommand(name="/plan-run", aliases=("/solve",), handler=cmd_plan_run, category="Solve",
                   description="Plan -> approve -> execute: model drafts steps, you review, agent runs each with a checklist",
                   usage="/plan-run <task> [--yes]"),
    FeatureCommand(name="/plans", handler=cmd_plans, category="Solve",
                   description="List saved plans for this workspace", usage="/plans [n]"),
    FeatureCommand(name="/plan-resume", handler=cmd_plan_resume, category="Solve",
                   description="Continue the unfinished steps of a saved plan", usage="/plan-resume [id]"),
]

__all__ = [
    "Plan", "PlanStep", "COMMANDS", "PLAN_SYSTEM", "extract_plan_json", "parse_numbered_plan", "parse_plan_text",
    "create_plan", "request_plan_text", "execute_plan", "run_step", "step_prompt", "verify_command", "apply_edit",
    "save_plan", "load_plan", "list_plans", "plans_dir",
]
