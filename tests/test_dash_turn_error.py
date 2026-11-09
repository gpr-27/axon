"""A turn that raises must reach the page as an error event, not vanish silently."""
from __future__ import annotations

import inspect

from axon.features import dash_runtime


def test_turn_loop_reports_crashes_to_the_page() -> None:
    src = inspect.getsource(dash_runtime.Runtime._turn_loop)
    assert 'self.hub.emit("error"' in src
    assert "except Exception:\n                    pass" not in src
