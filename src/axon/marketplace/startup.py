"""
Automatic work at startup, all of it opt-in (see :mod:`axon.marketplace.automation`).

``begin`` is called while the MCP servers are being connected. It returns the settings and, when
automation is on, starts one background thread for the project suggestions and the update check. The
thread never prints: it posts one-line notices (:mod:`axon.notices`) that appear with your next
prompt. Every failure is swallowed, so startup is never slowed down or broken by this.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from axon import notices
from axon.marketplace.automation import Automation, load_automation

logger = logging.getLogger(__name__)


def run_background_tasks(workspace: Path, auto: Automation) -> None:
    """The work of the background thread. Each task is separate so one failure does not stop the rest."""
    if auto.on("suggest"):
        run_task("connector suggestions", lambda: suggest_for_project(workspace, auto))
    if auto.on("check_updates"):
        run_task("update check", lambda: check_for_updates(workspace, auto))


def run_task(label: str, task: Callable[[], None]) -> None:
    try:
        task()
    except Exception as e:  # noqa: BLE001 - background work must never raise
        logger.debug("automation: %s failed: %s", label, e)


def suggest_for_project(workspace: Path, auto: Automation) -> None:
    from axon.marketplace import suggest
    suggest.run_once(workspace, auto.auto_install, notices.post)


def check_for_updates(workspace: Path, auto: Automation) -> None:
    from axon.marketplace import updates
    updates.run_if_due(workspace, auto.update_interval_days, auto.auto_update, notices.post)


def begin(workspace: Path) -> Automation:
    """Load the settings and start the background tasks when automation is on."""
    auto = load_automation(workspace)
    if auto.enabled and (auto.suggest or auto.check_updates):
        thread = threading.Thread(target=run_background_tasks, args=(workspace, auto),
                                  name="axon-automation", daemon=True)
        thread.start()
    return auto
