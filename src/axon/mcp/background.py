"""
Automatic backgrounding of long MCP tool calls.

A call from the main conversation that is still running after two minutes moves to a background task instead of
blocking the session: the model gets the task id at once and the result arrives as a task notification with the
next prompt. ``/tasks`` lists these tasks with the latest progress the server reported and can stop them.

``CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`` changes the threshold (0 = never). ``CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1``
turns it off too. Subagent calls never move, and neither do calls in a non-interactive run unless
``CLAUDE_AUTO_BACKGROUND_TASKS=1``. A call waiting on an open elicitation dialog does not move until it closes.
The per-call limits (wall clock, idle) keep applying in the background. Tasks do not survive exiting the session.
"""
from __future__ import annotations

import itertools
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from axon.errors import ToolError
from axon.mcp import output as mcp_output

DEFAULT_THRESHOLD_MS = 0   # off; AXON_MCP_AUTO_BACKGROUND_MS (or CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS) turns it on
_ids = itertools.count(1)
_tasks: dict[str, "McpBackgroundTask"] = {}
_lock = threading.Lock()


@dataclass
class McpBackgroundTask:
    id: str
    server: str
    tool: str
    connection: Any
    thread_id: int = 0
    started: float = field(default_factory=time.time)
    status: str = "running"                # running | completed | failed | stopped
    result: str = ""
    deliver: Callable[["McpBackgroundTask"], None] | None = None

    def latest_progress(self) -> str:
        client = getattr(self.connection, "client", None)
        return client.progress_of_thread(self.thread_id) if client is not None else ""


def threshold_ms() -> int:
    return mcp_output.env_int("CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS", "AXON_MCP_AUTO_BACKGROUND_MS",
                              default=DEFAULT_THRESHOLD_MS)


def _flag(*names: str) -> bool:
    return any(os.environ.get(n, "").strip().lower() in ("1", "true", "yes") for n in names)


def eligible(ctx: Any) -> bool:
    """May a call made through ``ctx`` move to the background?"""
    if threshold_ms() <= 0 or _flag("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "AXON_DISABLE_BACKGROUND_TASKS"):
        return False
    agent = getattr(ctx, "agent", None)
    if agent is None or getattr(agent, "parent_agent", None) is not None:
        return False
    from axon.mcp import state
    return state.is_interactive() or _flag("CLAUDE_AUTO_BACKGROUND_TASKS", "AXON_AUTO_BACKGROUND_TASKS")


def tasks() -> list[McpBackgroundTask]:
    with _lock:
        return list(_tasks.values())


def get(task_id: str) -> McpBackgroundTask | None:
    with _lock:
        return _tasks.get(task_id)


def stop(task_id: str) -> McpBackgroundTask | None:
    """Stop following a task and ask the server to cancel it (the server may still finish the work)."""
    task = get(task_id)
    if task is None or task.status != "running":
        return task
    task.status = "stopped"
    client = getattr(task.connection, "client", None)
    if client is not None:
        client.cancel_thread_call(task.thread_id, "stopped by the user")
    if task.deliver is not None:
        task.deliver(task)
    return task


def _notify(agent: Any, task: McpBackgroundTask) -> None:
    from axon import notices
    label = f"{task.server}/{task.tool}"
    if task.status == "stopped":
        body = f"MCP tool call {label} (task {task.id}) was stopped. No result is coming; the server may still " \
               "have done some of the work."
    else:
        body = f"MCP tool call {label} (task {task.id}) {task.status}:\n{task.result}"
    pending = getattr(agent, "_pending_context", None)
    if isinstance(pending, list):
        pending.append(f"<task-notification>\n{body}\n</task-notification>")
    notices.post(f"MCP task {task.id} ({label}) {task.status}")


def run_call(connection: Any, tool: str, call: Callable[[], str], ctx: Any) -> str:
    """Run ``call`` and return its text, or the background-task message if it outlasts the threshold."""
    if not eligible(ctx):
        return call()
    from axon.mcp import elicit
    box: dict[str, Any] = {}
    done = threading.Event()
    task = McpBackgroundTask(id=f"mcp_{next(_ids)}", server=connection.name, tool=tool, connection=connection)
    agent = getattr(ctx, "agent", None)

    def work() -> None:
        try:
            box["text"] = call()
        except BaseException as e:  # noqa: BLE001 - handed to whichever thread is still waiting
            box["error"] = e
        done.set()
        if task.deliver is not None and task.status == "running":
            error = box.get("error")
            task.status = "failed" if error is not None else "completed"
            task.result = str(error) if error is not None else str(box.get("text", ""))
            task.deliver(task)

    worker = threading.Thread(target=work, daemon=True, name=f"mcp-call-{connection.name}")
    worker.start()
    task.thread_id = worker.ident or 0
    deadline = time.monotonic() + threshold_ms() / 1000.0
    try:
        while not done.wait(0.05):
            if time.monotonic() < deadline or elicit.dialog_open():
                continue
            task.deliver = lambda t: _notify(agent, t)
            with _lock:
                _tasks[task.id] = task
            if done.is_set():  # finished in the instant between the check and registering: answer in place
                task.deliver = None
                with _lock:
                    _tasks.pop(task.id, None)
                break
            return (f"This MCP call is still running, so it moved to background task {task.id}. Keep working: the "
                    f"result arrives as a task notification. It can be stopped from /tasks.")
    except KeyboardInterrupt:
        client = getattr(connection, "client", None)
        if client is not None:
            client.cancel_thread_call(task.thread_id, "interrupted by the user")
        raise
    error = box.get("error")
    if error is not None:
        raise error if isinstance(error, (ToolError, KeyboardInterrupt)) else ToolError(str(error))
    return str(box.get("text", ""))
