"""
Unit and Integration Tests for Axon Advanced Features:
- Live MCP Tool Bridge
- Multi-Language Code Graph, GoToDefinition & FindReferences
- Git Worktree Isolation
- Real-time Token Status Bar & Metrics
"""
from __future__ import annotations
from pathlib import Path
import pytest

from axon.agent.loop import Agent
from axon.agent.state import Conversation, FileState, TodoState
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from axon.tools.base import ToolContext
from axon.tools.code_graph import (
    MultiLanguageSymbolExtractor,
    GoToDefinitionTool,
    FindReferencesTool,
)
from axon.agent.worktree import WorktreeManager, WorktreeInfo
from axon.ui.statusbar import StatusBar, generate_sparkline, format_tokens
from axon.mcp.bridge import MCPServerConnection, MCPToolWrapper, MCPToolBridge


@pytest.fixture
def test_ctx(tmp_path: Path) -> ToolContext:
    settings = Settings(workspace=tmp_path, model="deepseek-v4-flash")
    return ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
        ledger=Ledger(),
    )


# ─── 1. Multi-Language Code Graph & Symbols Tests ─────────────────────────

def test_code_graph_python_extraction(tmp_path: Path):
    py_file = tmp_path / "sample.py"
    py_file.write_text("""
class UserAuthService:
    \"\"\"Service for handling user authentication.\"\"\"
    def login(self, username: str, password: str):
        pass

    async def verify_token(self, token: str):
        pass

def global_helper_function():
    pass
""")
    symbols = MultiLanguageSymbolExtractor.extract_symbols(py_file, "sample.py")
    names = [s.name for s in symbols]
    assert "UserAuthService" in names
    assert "login" in names
    assert "verify_token" in names
    assert "global_helper_function" in names


def test_code_graph_typescript_javascript_extraction(tmp_path: Path):
    ts_file = tmp_path / "service.ts"
    ts_file.write_text("""
export interface UserPayload {
    id: string;
    email: string;
}

export type AuthStatus = "authenticated" | "anonymous";

export class SessionClient {
    private token: string;

    public async authenticate(credentials: any): Promise<boolean> {
        return true;
    }
}

export const helperArrow = (x: number) => x * 2;
""")
    symbols = MultiLanguageSymbolExtractor.extract_symbols(ts_file, "service.ts")
    names = [s.name for s in symbols]
    assert "UserPayload" in names
    assert "AuthStatus" in names
    assert "SessionClient" in names
    assert "authenticate" in names
    assert "helperArrow" in names


def test_code_graph_go_and_rust_extraction(tmp_path: Path):
    go_file = tmp_path / "main.go"
    go_file.write_text("""
package main

type ServerConfig struct {
    Port int
}

func (s *ServerConfig) StartServer(ctx context.Context) error {
    return nil
}

func GlobalEntrypoint() {
}
""")
    symbols_go = MultiLanguageSymbolExtractor.extract_symbols(go_file, "main.go")
    names_go = [s.name for s in symbols_go]
    assert "ServerConfig" in names_go
    assert "StartServer" in names_go
    assert "GlobalEntrypoint" in names_go

    rs_file = tmp_path / "lib.rs"
    rs_file.write_text("""
pub struct DataProcessor;

impl DataProcessor {
    pub fn process_records(&self) -> bool {
        true
    }
}
""")
    symbols_rs = MultiLanguageSymbolExtractor.extract_symbols(rs_file, "lib.rs")
    names_rs = [s.name for s in symbols_rs]
    assert "DataProcessor" in names_rs
    assert "process_records" in names_rs


def test_goto_definition_tool(test_ctx: ToolContext):
    f = test_ctx.workspace / "core.py"
    f.write_text("""
class PaymentEngine:
    \"\"\"Handles credit card transactions.\"\"\"
    def charge(self, amount: int):
        pass
""")
    tool = GoToDefinitionTool()
    res = tool.run({"symbol": "PaymentEngine"}, test_ctx)
    assert "PaymentEngine" in res
    assert "core.py" in res
    assert "CLASS" in res


def test_find_references_tool(test_ctx: ToolContext):
    f1 = test_ctx.workspace / "engine.py"
    f1.write_text("class DatabasePool:\n    pass\n")

    f2 = test_ctx.workspace / "app.py"
    f2.write_text("from engine import DatabasePool\ndb = DatabasePool()\n")

    tool = FindReferencesTool()
    res = tool.run({"symbol": "DatabasePool"}, test_ctx)
    assert "engine.py" in res
    assert "app.py" in res
    assert "Found 3 reference(s)" in res


# ─── 2. Git Worktree Isolation Tests ──────────────────────────────────────

def test_worktree_manager_non_git_safe(tmp_path: Path):
    assert not WorktreeManager.is_git_repo(tmp_path)
    res = WorktreeManager.create_worktree(tmp_path, "task-1")
    assert res is None


# ─── 3. Real-Time Status Bar Tests ────────────────────────────────────────

def test_statusbar_sparkline_and_formatting():
    assert generate_sparkline([]) == ""
    spark = generate_sparkline([100, 200, 500, 1000, 800, 300])
    assert len(spark) == 6

    assert format_tokens(500) == "500"
    assert format_tokens(4500) == "4.5k"
    assert format_tokens(1_500_000) == "1.5M"

    bar = StatusBar.format_bar(
        model="claude-opus-5",
        effort="quantum",
        mode="bypass",
        total_tokens=15000,
        context_capacity=1_000_000,
        cost=0.0452,
    )
    assert "claude-opus-5" in bar
    assert "quantum" in bar
    assert "$0.0452" in bar


def test_statusbar_toggle():
    initial = StatusBar._enabled
    toggled = StatusBar.toggle()
    assert toggled != initial
    # Restore
    StatusBar.toggle()


# ─── 4. MCP Tool Bridge Tests ─────────────────────────────────────────────

def test_mcp_tool_wrapper_structure():
    conn = MCPServerConnection(
        name="test_server",
        command="echo",
        args=[],
        env={},
    )
    tool_spec = {
        "name": "search_db",
        "description": "Searches the database",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
    }
    wrapper = MCPToolWrapper("test_server", tool_spec, conn)
    assert wrapper.name == "mcp_test_server_search_db"
    assert "[MCP:test_server]" in wrapper.description
    assert wrapper.readonly is True
    assert wrapper.schema["type"] == "object"


def test_mcp_bridge_empty_status():
    bridge = MCPToolBridge()
    report = bridge.get_status_report()
    assert "No active MCP server connections" in report


# ─── 5. Semantic Search Tool Tests ────────────────────────────────────────

def test_semantic_search_tool(test_ctx: ToolContext):
    f = test_ctx.workspace / "auth.py"
    f.write_text("""
def validate_jwt_token(token: str):
    \"\"\"Validate JSON web token for user authentication.\"\"\"
    return True
""")
    from axon.tools.semantic_search import SemanticSearchTool
    tool = SemanticSearchTool()
    res = tool.run({"query": "user authentication token validation"}, test_ctx)
    assert "auth.py" in res
    assert "validate_jwt_token" in res


# ─── 6. Context Manager Rung 3 Compaction Tests ───────────────────────────

def test_context_manager_rung3_summarization():
    from axon.agent.context import ContextManager
    from axon.config import Settings
    from axon.agent.state import Conversation
    from axon.providers.base import AssistantTurn, TextBlock

    settings = Settings(turn_token_budget=100, compact_at=0.1)
    cm = ContextManager(settings)

    conv = Conversation()
    # Populate with 8 turns (16 messages) with long text
    for i in range(8):
        conv.append_user(f"Message number {i} discussing authentication, token rotation, and database scaling in depth." * 5)
        conv.append_assistant(AssistantTurn(blocks=[TextBlock(text=f"Response number {i} with architecture code." * 5)], stop_reason="end_turn"))

    assert len(conv.messages) == 16
    cm.prepare(conv, system=[], tools=[])
    # Should summarize older turns and keep latest turns
    assert len(conv.messages) < 16
    first_msg = conv.messages[0].get("content", "")
    assert "Prior Conversation Summary" in first_msg


# ─── 7. Clipboard and Auto-Skill Tests ────────────────────────────────────

def test_clipboard_copy_helper():
    from axon.ui.clipboard import copy_to_clipboard
    # Should not raise exception
    res = copy_to_clipboard("Test string for Axon clipboard")
    assert isinstance(res, bool)


def test_auto_skill_detection(tmp_path: Path):
    from axon.skills.manager import SkillManager
    sm = SkillManager(tmp_path)
    matched_review = sm.auto_match("Please review my code and check for bugs")
    assert any(s.name == "code-review" for s in matched_review)

    matched_debug = sm.auto_match("There is a fatal traceback error crash")
    assert any(s.name == "debug" for s in matched_debug)


# ─── 8. Session Encryption and Tagging Tests ──────────────────────────────

def test_session_encryption_and_decryption():
    from axon.session.crypto import encrypt_session_record, decrypt_session_record
    secret_text = '{"type": "user_message", "secret_key": "sk-123456789"}'
    passphrase = "my_secure_axon_passphrase"

    encrypted = encrypt_session_record(secret_text, passphrase)
    assert encrypted.startswith("ENC:v1:")
    assert "sk-123456789" not in encrypted

    decrypted = decrypt_session_record(encrypted, passphrase)
    assert decrypted == secret_text

    # Wrong passphrase test
    failed = decrypt_session_record(encrypted, "wrong_passphrase")
    assert "Failed" in failed or "Corrupt" in failed


def test_session_tags_and_starring(tmp_path: Path):
    store = SessionStore(workspace=tmp_path)
    store.append_user("Initial user prompt")

    tag = store.tag_session("frontend-refactor")
    assert tag == "frontend-refactor"

    starred = store.star_session()
    assert starred is True


# ─── 9. Fuzzy Matching Tests ──────────────────────────────────────────────

def test_fuzzy_scoring():
    from axon.ui.fuzzy_picker import _fuzzy_score
    score1 = _fuzzy_score("agent", "src/axon/agent/loop.py")
    score2 = _fuzzy_score("xyz123", "src/axon/agent/loop.py")
    assert score1 > score2
    assert score2 == 0.0



