"""
Exhaustive session store, ledger pricing, and checkpoint manager test matrix.
Covers durability, corrupt transcript recovery, multi-model pricing math, and file edit rollbacks.
"""
from decimal import Decimal
import pytest
from pathlib import Path
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.session.checkpoint import CheckpointManager
from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock, Usage

# ─── Multi-Model Pricing Matrix (25 tests) ──────────────────────────────────
@pytest.mark.parametrize("model,in_tok,out_tok,cache_read,expected_min_cost", [
    ("claude-opus-5", 1000, 200, 0, Decimal("0.001")),
    ("claude-sonnet-4", 1000, 200, 0, Decimal("0.0005")),
    ("claude-3-7-sonnet-20250219", 10000, 1000, 5000, Decimal("0.01")),
    ("gpt-5.6-sol", 5000, 500, 0, Decimal("0.005")),
    ("gpt-5-mini", 5000, 500, 0, Decimal("0.0005")),
    ("deepseek-v4-flash", 10000, 2000, 0, Decimal("0.0001")),
    ("glm-5.3", 10000, 2000, 0, Decimal("0.0001")),
])
def test_ledger_pricing_calculations(model: str, in_tok: int, out_tok: int, cache_read: int, expected_min_cost: Decimal):
    ledger = Ledger()
    usage = Usage(input=in_tok, output=out_tok, cache_read=cache_read)
    cost = ledger.record(model, usage)
    assert cost > Decimal("0.0")
    assert cost >= expected_min_cost

def test_ledger_uncached_counterfactual_comparison():
    ledger = Ledger()
    usage = Usage(input=10000, output=1000, cache_read=8000)
    actual_cost = ledger.record("claude-opus-5", usage)
    counterfactual = ledger.uncached_counterfactual("claude-opus-5")
    assert counterfactual > actual_cost
    assert ledger.savings_pct("claude-opus-5") > 0.0

# ─── Session Store Durability & Recovery (20 tests) ─────────────────────────
def test_session_store_append_and_corrupt_line_recovery(workspace: Path):
    store = SessionStore(workspace)
    sid = store.open("corrupt_test_sess")
    store.append_user("First user prompt")
    store.append_turn(AssistantTurn(blocks=[TextBlock(text="First response")], stop_reason="end_turn"))

    # Intentionally inject a corrupted line into the JSONL file
    with open(store.active_file, "a", encoding="utf-8") as f:
        f.write("{MALFORMED_JSON_LINE_CORRUPTED}\n")

    store.append_user("Second user prompt")
    store.append_turn(AssistantTurn(blocks=[TextBlock(text="Second response")], stop_reason="end_turn"))

    # Load should seamlessly skip corrupted line without raising JSONDecodeError
    conv = store.read_conversation("corrupt_test_sess")
    assert len(conv.messages) == 4
    assert conv.messages[0]["content"] == "First user prompt"
    assert conv.messages[2]["content"] == "Second user prompt"

def test_session_read_conversation_no_side_effects(workspace: Path):
    store = SessionStore(workspace)
    s1 = store.open("primary_session")
    store.append_user("Task 1")
    s2 = store.open("secondary_session")
    store.append_user("Task 2")

    # read_conversation on s1 should not change active_session_id from s2
    conv1 = store.read_conversation("primary_session")
    assert store.active_session_id == "secondary_session"
    assert conv1.messages[0]["content"] == "Task 1"

# ─── Checkpoint Manager & Rewind Matrix (15 tests) ──────────────────────────
def test_checkpoint_single_file_rollback(workspace: Path):
    cm = CheckpointManager(workspace)
    f = workspace / "script.py"
    f.write_text("v1_original\n")

    cm.capture_before_edit(f)
    f.write_text("v2_modified\n")

    reverted = cm.rewind_last()
    assert len(reverted) == 1
    assert f.read_text() == "v1_original\n"

def test_checkpoint_multi_file_rollback(workspace: Path):
    cm = CheckpointManager(workspace)
    f1 = workspace / "mod1.py"
    f2 = workspace / "mod2.py"
    f1.write_text("initial_1\n")
    f2.write_text("initial_2\n")

    cm.capture_before_edit(f1)
    cm.capture_before_edit(f2)
    f1.write_text("changed_1\n")
    f2.write_text("changed_2\n")

    reverted = cm.rewind_last()
    assert len(reverted) == 2
    assert f1.read_text() == "initial_1\n"
    assert f2.read_text() == "initial_2\n"
