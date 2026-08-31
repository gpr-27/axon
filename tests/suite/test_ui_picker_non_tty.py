"""
Unit test for pick() function in non-tty and default environments.
"""
from __future__ import annotations
from axon.ui.picker import pick

def test_pick_empty():
    assert pick([]) is None

def test_pick_non_tty():
    opts = ["Option A", "Option B", "Option C"]
    # In non-tty, pick should return current or first option
    assert pick(opts) == "Option A"
    assert pick(opts, current="Option B") == "Option B"
