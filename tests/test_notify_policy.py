"""Desktop notification policy: only events that need the user reach the desktop."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from axon.ui import notify


@pytest.fixture
def policy(monkeypatch):
    """An interactive session outside pytest/CI, with the user in another app."""
    monkeypatch.setattr(notify, "_blocked_by_env", lambda: False)   # pytest itself would block notifications
    monkeypatch.setattr(notify.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(notify.sys.stdout, "isatty", lambda: True, raising=False)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "notify_if_unfocused", lambda title, msg: sent.append((title, msg)) or True)
    notify._last_sent.clear()
    notify.configure(SimpleNamespace(notifications="important", notify_after_seconds=30))
    yield sent
    notify._last_sent.clear()
    notify.configure(SimpleNamespace())


def test_quick_turns_do_not_notify_but_long_and_failed_ones_do(policy):
    assert not notify.notify_event("turn_complete", "done", elapsed=4.0)
    assert notify.notify_event("turn_complete", "done", elapsed=45.0)
    assert notify.notify_event("turn_failed", "stopped", elapsed=1.0)
    assert [m for _, m in policy] == ["done", "stopped"]


def test_waiting_for_the_user_always_notifies(policy):
    assert notify.notify_event("permission_prompt", "Axon needs your permission to use Bash")
    assert notify.notify_event("question", "Axon is asking you: which approach?")
    assert notify.notify_event("background_task", "Background turn bg1 completed")


def test_same_kind_is_not_repeated_within_seconds(policy):
    assert notify.notify_event("permission_prompt", "one")
    assert not notify.notify_event("permission_prompt", "two")
    assert len(policy) == 1


def test_modes_all_and_off(policy):
    notify.configure(SimpleNamespace(notifications="all", notify_after_seconds=30))
    assert notify.notify_event("turn_complete", "quick", elapsed=1.0)
    notify.configure(SimpleNamespace(notifications="off", notify_after_seconds=30))
    assert not notify.notify_event("permission_prompt", "x")


def test_tests_ci_and_scripts_never_notify(policy, monkeypatch):
    monkeypatch.undo()                                   # real environment: we are under pytest
    monkeypatch.setattr(notify, "notify_if_unfocused", lambda title, msg: policy.append((title, msg)) or True)
    assert not notify.notify_event("turn_failed", "x")
    monkeypatch.setattr(notify, "_blocked_by_env", lambda: False)
    monkeypatch.setattr(notify.sys.stdout, "isatty", lambda: False, raising=False)   # print mode / piped
    assert not notify.notify_event("turn_failed", "x")
    assert policy == []


def test_settings_have_safe_defaults():
    from axon.config import Settings
    s = Settings()
    assert s.notifications == "important" and s.notify_after_seconds == 30.0
