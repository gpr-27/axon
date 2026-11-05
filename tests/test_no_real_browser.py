"""The test suite must not be able to open a real browser."""
import os
import webbrowser


def test_webbrowser_calls_do_nothing():
    assert webbrowser.open("https://example.com") is False
    assert webbrowser.open_new_tab("https://example.com") is False


def test_subprocesses_get_a_harmless_browser_command():
    assert os.environ.get("BROWSER") == "true"


def test_desktop_notifications_are_switched_off_in_tests(monkeypatch):
    import subprocess

    from axon.ui import notify

    launched = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: launched.append(a))
    assert os.environ.get("AXON_NO_NOTIFY") == "1"
    assert notify.send_desktop_notification("Axon", "x") is False
    assert launched == []          # nothing was even started
