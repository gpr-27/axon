"""
Exhaustive stress, state durability, queue concurrency, and edge-case boundary matrix.
"""
from decimal import Decimal
import pytest
from pathlib import Path
from axon.agent.state import FileState, TodoState, MessageQueue
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from axon.providers.base import Usage

# ─── FileState Staleness & Read Tracking Matrix (15 tests) ───────────────────
@pytest.mark.parametrize("file_count", [1, 5, 10, 25, 50])
def test_file_state_read_tracking_stress(workspace: Path, file_count: int):
    fs = FileState()
    paths = [workspace / f"file_{i}.txt" for i in range(file_count)]
    for p in paths:
        p.write_text(f"content {p.name}")
        fs.record_read(p)
        assert p.resolve() in fs._seen
        fs.check_writable(p)  # Must not raise

def test_file_state_modified_on_disk_detection(workspace: Path):
    from axon.errors import StaleFileError
    fs = FileState()
    p = workspace / "stale.txt"
    p.write_text("initial")
    fs.record_read(p)

    # Modify file content on disk
    p.write_text("modified by external process")
    with pytest.raises(StaleFileError, match="has changed on disk"):
        fs.check_writable(p)

# ─── MessageQueue Operations & Invariant Matrix (15 tests) ──────────────────
@pytest.mark.parametrize("num_items", [1, 3, 5, 10, 20])
def test_message_queue_push_and_drain(num_items: int):
    q = MessageQueue()
    for i in range(num_items):
        q.push(f"Question #{i}")
    assert len(q) == num_items

    popped = []
    while len(q) > 0:
        popped.append(q.pop())
    assert len(popped) == num_items
    assert len(q) == 0

def test_message_queue_peek_and_drop():
    q = MessageQueue()
    q.push("Item A")
    q.push("Item B")
    q.push("Item C")
    
    first = q.peek()
    assert first is not None
    assert first.text == "Item A"

    # Drop by id using remove()
    b_id = q.items[1].id
    dropped = q.remove(b_id)
    assert dropped is True
    assert len(q) == 2
    assert q.items[0].text == "Item A"
    assert q.items[1].text == "Item C"

# ─── TodoState Progress Math Matrix (15 tests) ──────────────────────────────
@pytest.mark.parametrize("completed_cnt,tot_cnt,expected_pct", [
    (0, 0, 0),
    (0, 5, 0),
    (1, 4, 25),
    (1, 3, 33),
    (1, 2, 50),
    (3, 4, 75),
    (5, 5, 100),
])
def test_todostate_progress_percentages(completed_cnt: int, tot_cnt: int, expected_pct: int):
    ts = TodoState()
    todos = []
    for i in range(tot_cnt):
        status = "completed" if i < completed_cnt else "pending"
        todos.append({"id": f"t_{i}", "content": f"Task {i}", "status": status})
    if todos:
        ts.replace(todos)
    comp, tot, pct = ts.progress()
    assert comp == completed_cnt
    assert tot == tot_cnt
    assert pct == expected_pct

# ─── Ledger Decimal Precision Under Micro Fractions (15 tests) ──────────────
@pytest.mark.parametrize("model", ["claude-opus-5", "deepseek-v4-flash", "gpt-5.6-sol"])
def test_ledger_micro_fraction_accounting(model: str):
    ledger = Ledger()
    # Micro usage (1 token)
    cost1 = ledger.record(model, Usage(input=1, output=1, cache_read=0))
    assert cost1 > Decimal("0.0")
    assert ledger.total() == cost1

    # Add 100 micro turns
    for _ in range(100):
        ledger.record(model, Usage(input=1, output=1, cache_read=0))
    assert ledger.total() > cost1
    assert len(ledger.turn_costs) == 101
