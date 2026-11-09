"""Session-scoped scheduling tools: CronCreate, CronList, CronDelete and ScheduleWakeup (self-paced /loop).

The jobs are /loop jobs (``axon.commands.workspace_cmds.LoopJob``): due prompts go onto ``agent.message_queue`` and
``LOOP_WAKE`` makes the idle prompt run them, so they fire only between turns and only while the terminal is open.

A prompt written here reaches the model as plain text, never as a typed line: no slash command, ``!`` shell, ``#``
memory or ``@file`` handling. Creating a job needs no prompt (like Task), because every tool call the prompt leads to
is permission-checked when it runs, under the mode in force then.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext, int_arg

MAX_SEARCH_DAYS = 366 * 9        # the longest gap a valid expression can have: 29 February across a skipped leap year


def cron_disabled() -> bool:
    """AXON_DISABLE_CRON=1: no scheduling tools, no /loop, and jobs already scheduled stop firing."""
    return os.environ.get("AXON_DISABLE_CRON", "").strip().lower() in ("1", "true", "yes", "on")


class CronError(ValueError):
    pass


_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))


def _parse_field(text: str, name: str, lo: int, hi: int) -> frozenset[int]:
    out: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", part)
        if not m:
            raise CronError(f"Bad {name} field '{part}': use *, 5, 1-5, */15 or a comma list (names and L, W, ? are not supported).")
        step = int(m.group(2)) if m.group(2) else 1
        if step < 1:
            raise CronError(f"Bad {name} field '{part}': the step must be at least 1.")
        if m.group(1) == "*":
            first, last = lo, hi
        else:
            ends = m.group(1).split("-")
            first = int(ends[0])
            last = int(ends[-1]) if len(ends) > 1 or m.group(2) is None else hi
            if first < lo or last > hi or first > last:
                raise CronError(f"Bad {name} field '{part}': values run from {lo} to {hi}.")
        out.update(range(first, last + 1, step))
    return frozenset(out)


class CronSpec:
    """A standard 5-field cron expression read in local time. Day of month and day of week, when both are
    restricted, match if either matches (vixie-cron)."""

    def __init__(self, text: str) -> None:
        self.text = " ".join(str(text or "").split())
        parts = self.text.split(" ")
        if len(parts) != 5:
            raise CronError(f"'{self.text}' is not a cron expression: it needs 5 fields (minute hour day-of-month month day-of-week).")
        fields = [_parse_field(p, *f) for p, f in zip(parts, _FIELDS)]
        self.minutes, self.hours, self.doms, self.months = fields[:4]
        self.dows = frozenset(0 if d == 7 else d for d in fields[4])    # 0 and 7 are both Sunday
        self.any_dom, self.any_dow = parts[2].startswith("*"), parts[4].startswith("*")
        if self.next_after(datetime(2024, 1, 1).timestamp()) is None:
            raise CronError(f"'{self.text}' never fires: no such date exists.")

    def _day_matches(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom, dow = d.day in self.doms, (d.weekday() + 1) % 7 in self.dows
        if self.any_dom or self.any_dow:
            return dom and dow
        return dom or dow

    def next_after(self, ts: float) -> float | None:
        """The first matching minute strictly after *ts* (epoch seconds), or None when there is none."""
        start = datetime.fromtimestamp(ts).replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.replace(hour=0, minute=0)
        for _ in range(MAX_SEARCH_DAYS):
            if self._day_matches(day):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        at = day.replace(hour=hour, minute=minute)
                        if at >= start and at.timestamp() > ts:
                            return at.timestamp()
            day += timedelta(days=1)
        return None


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


def _cmds() -> Any:
    from axon.commands import workspace_cmds
    return workspace_cmds


def _main_agent(ctx: ToolContext, tool: str) -> Any:
    agent = ctx.agent
    if agent is None or getattr(agent, "is_subagent", False):
        raise ToolError(f"{tool} works only in the main conversation.")
    return agent


class CronCreateTool(Tool):
    name: ClassVar[str] = "CronCreate"
    description: ClassVar[str] = (
        "Schedule a prompt to run later in this session. `cron` is a standard 5-field expression in local time "
        "(minute hour day-of-month month day-of-week; supports *, 5, 1-5, */15 and lists; 0 or 7 is Sunday). "
        "recurring=true (the default) repeats, and expires 7 days after creation after one final run; recurring=false "
        "fires once at the next match and deletes itself. Jobs fire only between turns, only while this terminal "
        "session is open, and never catch up on missed times; resuming the session restores them. "
        "At most 50 per session. The prompt reaches you as plain "
        "text when it fires: slash commands, ! and @file in it are not executed. Returns the job's 8-character id."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "cron": {"type": "string", "description": "5-field cron expression in local time, e.g. '*/5 * * * *'"},
            "prompt": {"type": "string", "description": "What to do each time it fires"},
            "recurring": {"type": "boolean", "description": "Repeat (true, the default) or fire once (false)"},
        },
        "required": ["cron", "prompt"],
        "additionalProperties": False,
    }
    # Not read-only (plan mode refuses it). Like Task it asks nothing itself: what the prompt later does is checked then.
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        agent = _main_agent(ctx, self.name)
        if not getattr(agent, "fires_scheduled_tasks", False):
            raise ToolError("Scheduled prompts fire only in an open terminal session, and this one is not. "
                            "/schedule runs tasks while Axon is open, without a session.")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("CronCreate requires 'prompt'.")
        recurring = args.get("recurring", True)
        if not isinstance(recurring, bool):
            raise ToolError("Argument 'recurring' must be true or false.")
        try:
            spec = CronSpec(str(args.get("cron") or ""))
            job = _cmds().start_cron_job(agent, spec, prompt, recurring=recurring)
        except (CronError, _cmds().LoopLimitError) as e:
            raise ToolError(str(e)) from e
        kind = "recurring, expires in 7 days" if recurring else "fires once"
        return f"Scheduled {job.uid}: {spec.text} ({kind}), next run {_stamp(job.next_at)}. Local time; it fires between turns."


class CronListTool(Tool):
    name: ClassVar[str] = "CronList"
    description: ClassVar[str] = ("List this session's scheduled tasks, including /loop jobs: id, schedule, next run and prompt.")
    schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "additionalProperties": False}
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        agent = _main_agent(ctx, self.name)
        jobs = _cmds().active_jobs(agent)
        if not jobs:
            return "No scheduled tasks."
        return "\n".join(f"{j.uid}  {_cmds().job_schedule(j)}  next run {_cmds().job_next(j)}  {_one_line(_cmds().job_prompt(j))}" for j in jobs)


class CronDeleteTool(Tool):
    name: ClassVar[str] = "CronDelete"
    description: ClassVar[str] = "Cancel a scheduled task of this session by its id (see CronList). Also cancels /loop jobs."
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "The task's 8-character id"}},
        "required": ["id"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        agent = _main_agent(ctx, self.name)
        uid = str(args.get("id") or "").strip()
        if not uid or not any(j.uid == uid for j in _cmds().active_jobs(agent)):
            raise ToolError(f"No scheduled task with id '{uid}'. CronList shows them.")
        _cmds().stop_loops(agent, uid)
        _cmds().save_cron_jobs(agent)
        return f"Cancelled {uid}."


class ScheduleWakeupTool(Tool):
    name: ClassVar[str] = "ScheduleWakeup"
    description: ClassVar[str] = (
        "End one iteration of a self-paced /loop: pick when the next one runs, from 60 to 3600 seconds out, based on "
        "what you saw (short while something is about to finish, long when nothing is pending), with a one-line reason "
        "the user sees. Pass stop=true instead when the task is complete. Only for a running self-paced /loop."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "delay_seconds": {"type": "integer", "description": "Seconds until the next iteration; values outside 60 to 3600 are moved to the nearest limit"},
            "reason": {"type": "string", "description": "One line: why this delay"},
            "stop": {"type": "boolean", "description": "End the loop and cancel the pending wakeup"},
        },
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False      # plan mode allows it by name: it only times a loop the user started
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        agent = _main_agent(ctx, self.name)
        cmds = _cmds()
        job = cmds.self_paced_job(agent)
        if job is None:
            raise ToolError("No self-paced /loop is running. CronCreate schedules a prompt for later.")
        if args.get("stop") is True:
            cmds.stop_loops(agent, job.uid)
            return "Loop stopped."
        if args.get("delay_seconds") in (None, ""):
            raise ToolError("ScheduleWakeup requires 'delay_seconds', or stop=true to end the loop.")
        delay = int_arg(args, "delay_seconds", cmds.SELF_PACED_MIN_S, lo=cmds.SELF_PACED_MIN_S, hi=cmds.SELF_PACED_MAX_S)
        cmds.schedule_wakeup(job, delay, str(args.get("reason") or "").strip())
        return f"Next iteration in {cmds.fmt_wait(delay)}."


def _one_line(text: str, limit: int = 120) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[:limit - 3] + "..."


__all__ = ["CronCreateTool", "CronDeleteTool", "CronError", "CronListTool", "CronSpec", "ScheduleWakeupTool", "cron_disabled"]
