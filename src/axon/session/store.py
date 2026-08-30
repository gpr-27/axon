"""
Append-only JSONL session persistence and transcript recovery.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from axon.agent.state import Conversation
from axon.providers.base import AssistantTurn, ToolResultBlock, Usage
from axon.session.ledger import Ledger

@dataclass
class SessionMeta:
    session_id: str
    created_at: float
    model: str
    message_count: int
    path: Path
    first_prompt: str = "New Session"
    total_tokens: int = 0
    total_cost: float = 0.0

class SessionStore:
    def __init__(self, workspace: Path, session_dir: Path | None = None) -> None:
        self.workspace = workspace
        axon_pkg_root = Path(__file__).resolve().parents[3]
        pkg_axon_sessions = axon_pkg_root / ".axon" / "sessions"

        if session_dir is not None:
            self.session_dir = session_dir
        elif str(workspace).startswith(("/tmp", "/var/folders", "/private/var")):
            self.session_dir = workspace.parent / ".global_axon" / "sessions"
        elif pkg_axon_sessions.exists() or axon_pkg_root.exists():
            self.session_dir = pkg_axon_sessions
        else:
            self.session_dir = Path.home() / ".axon" / "sessions"

        self.session_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.active_session_id: str = f"session_{date_str}"
        self.active_file: Path = self.session_dir / f"{self.active_session_id}.jsonl"

    def open(self, session_id: str | None = None) -> str:
        if session_id:
            self.active_session_id = session_id
        else:
            from datetime import datetime
            date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_id = f"session_{date_str}"
            target_id = base_id
            counter = 1
            while (self.session_dir / f"{target_id}.jsonl").exists() or target_id == getattr(self, "active_session_id", None):
                target_id = f"{base_id}_{counter}"
                counter += 1
            self.active_session_id = target_id
        self.active_file = self.session_dir / f"{self.active_session_id}.jsonl"
        return self.active_session_id

    def append(self, record_type: str, data: Any) -> None:
        """Append one JSON line + fsync for crash durability (ADR-007)."""
        entry = {
            "type": record_type,
            "timestamp": time.time(),
            "data": data if isinstance(data, (dict, list, str, int, float, bool)) else str(data),
        }
        with open(self.active_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def append_user(self, text: str) -> None:
        self.append("user_message", {"role": "user", "content": text})

    def append_turn(self, turn: AssistantTurn) -> None:
        turn_data = {
            "stop_reason": turn.stop_reason,
            "text": turn.text,
            "thinking": turn.thinking,
            "tool_uses": [{"id": tu.id, "name": tu.name, "input": tu.input} for tu in turn.tool_uses],
            "native": turn.native,
            "usage": {
                "input": turn.usage.input,
                "output": turn.usage.output,
                "cache_read": turn.usage.cache_read,
                "cache_write": turn.usage.cache_write,
                "reasoning": turn.usage.reasoning,
            } if turn.usage else None,
        }
        self.append("assistant_turn", turn_data)

    def append_results(self, results: list[ToolResultBlock]) -> None:
        res_data = [
            {"tool_use_id": r.tool_use_id, "content": r.content, "is_error": r.is_error}
            for r in results
        ]
        self.append("tool_results", res_data)

    def read_conversation(self, session_id: str) -> Conversation:
        """Reconstruct Conversation from append-only transcript without changing active session."""
        target = self.session_dir / f"{session_id}.jsonl"
        if not target.exists():
            raise FileNotFoundError(f"Session transcript not found: {target}")

        messages: list[dict[str, Any]] = []
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    t = entry.get("type")
                    data = entry.get("data", {})
                    if t == "user_message":
                        messages.append(data)
                    elif t == "assistant_turn":
                        if "native" in data and data["native"] is not None:
                            if isinstance(data["native"], dict):
                                native_msg = dict(data["native"])
                                if native_msg.get("content") is None:
                                    native_msg["content"] = ""
                                messages.append(native_msg)
                            else:
                                messages.append({"role": "assistant", "content": str(data["native"])})
                        else:
                            blocks = []
                            for tu in data.get("tool_uses", []):
                                blocks.append({"type": "tool_use", "id": tu["id"], "name": tu["name"], "input": tu["input"]})
                            if data.get("text"):
                                blocks.append({"type": "text", "text": data["text"]})
                            messages.append({"role": "assistant", "content": blocks or data.get("text", "")})
                    elif t == "tool_results":
                        content = [
                            {
                                "type": "tool_result",
                                "tool_use_id": r["tool_use_id"],
                                "content": r["content"],
                                **({"is_error": True} if r.get("is_error") else {})
                            }
                            for r in data
                        ]
                        messages.append({"role": "user", "content": content})
                except Exception:
                    continue

        return Conversation(messages)

    def load(self, session_id: str) -> Conversation:
        """Reconstruct Conversation from append-only transcript and set active session."""
        conv = self.read_conversation(session_id)
        self.open(session_id)
        return conv

    def load_ledger(self, session_id: str, model: str) -> Ledger:
        """Reconstruct Ledger for a specific session from its JSONL entries."""
        target = self.session_dir / f"{session_id}.jsonl"
        ledger = Ledger()
        if not target.exists():
            return ledger

        conv_messages: list[dict[str, Any]] = []
        try:
            with open(target, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        t = entry.get("type")
                        data = entry.get("data", {})
                        if t == "user_message":
                            conv_messages.append(data)
                        elif t == "assistant_turn":
                            usage_dict = data.get("usage")
                            if usage_dict and isinstance(usage_dict, dict):
                                u = Usage(
                                    input=int(usage_dict.get("input", 0)),
                                    output=int(usage_dict.get("output", 0)),
                                    cache_read=int(usage_dict.get("cache_read", 0)),
                                    cache_write=int(usage_dict.get("cache_write", 0)),
                                    reasoning=int(usage_dict.get("reasoning", 0)),
                                )
                                ledger.record(model, u)
                            else:
                                # Estimate usage for legacy sessions
                                txt_len = len(data.get("text", "")) + len(data.get("thinking", ""))
                                from axon.agent.state import estimate_content_tokens
                                est_in = max(150, sum(estimate_content_tokens(m.get("content", "")) for m in conv_messages))
                                est_out = max(25, int(txt_len / 3.7))
                                u = Usage(input=est_in, output=est_out)
                                ledger.record(model, u)
                            conv_messages.append({"role": "assistant", "content": data.get("text", "")})
                    except Exception:
                        pass
        except Exception:
            pass
        return ledger

    def load_workspace_ledger(self, model: str) -> Ledger:
        """Reconstruct cumulative ledger across all historical workspace sessions (incorporating subagent costs into normal chats)."""
        total_ledger = Ledger()
        all_files = sorted(self.session_dir.glob("*.jsonl"))
        main_files = [f for f in all_files if "_sub_" not in f.stem]
        total_ledger.chat_count = len(main_files)

        for f in main_files:
            main_stem = f.stem
            s_ledger = self.load_ledger(main_stem, model)
            total_ledger.total_input_tokens += s_ledger.total_input_tokens
            total_ledger.total_output_tokens += s_ledger.total_output_tokens
            total_ledger.total_cache_read_tokens += s_ledger.total_cache_read_tokens
            total_ledger.total_cache_write_tokens += s_ledger.total_cache_write_tokens
            total_ledger.total_reasoning_tokens += s_ledger.total_reasoning_tokens
            total_ledger.total_cost += s_ledger.total_cost
            total_ledger.turn_costs.extend(s_ledger.turn_costs)

            # Include any subagents belonging to this main chat
            for sub_file in sorted(self.session_dir.glob(f"{main_stem}_sub_*.jsonl")):
                sub_l = self.load_ledger(sub_file.stem, model)
                total_ledger.total_input_tokens += sub_l.total_input_tokens
                total_ledger.total_output_tokens += sub_l.total_output_tokens
                total_ledger.total_cache_read_tokens += sub_l.total_cache_read_tokens
                total_ledger.total_cache_write_tokens += sub_l.total_cache_write_tokens
                total_ledger.total_reasoning_tokens += sub_l.total_reasoning_tokens
                total_ledger.total_cost += sub_l.total_cost
                total_ledger.turn_costs.extend(sub_l.turn_costs)

        return total_ledger

    def list_recent(self, limit: int | None = None) -> list[SessionMeta]:
        all_files = sorted(self.session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        # Exclude sub-agent sessions (_sub_*) — only visible via /subagents
        files = [f for f in all_files if "_sub_" not in f.stem]
        items: list[SessionMeta] = []
        target_files = files[:limit] if limit is not None else files
        for f in target_files:
            sid = f.stem
            mtime = f.stat().st_mtime
            count = 0
            first_prompt = "New Session"
            total_tokens = 0
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as s_file:
                    for l in s_file:
                        if not l.strip():
                            continue
                        count += 1
                        try:
                            entry = json.loads(l)
                            t = entry.get("type")
                            if first_prompt == "New Session" and t == "user_message":
                                txt = entry.get("data", {}).get("content", "")
                                if txt and isinstance(txt, str):
                                    first_prompt = txt.splitlines()[0].strip()[:48]
                                elif isinstance(txt, list):
                                    for blk in txt:
                                        if isinstance(blk, dict) and blk.get("text"):
                                            first_prompt = blk["text"].splitlines()[0].strip()[:48]
                                            break
                            elif t == "assistant_turn":
                                u = entry.get("data", {}).get("usage")
                                if u and isinstance(u, dict):
                                    total_tokens += int(u.get("input", 0)) + int(u.get("output", 0))
                        except Exception:
                            pass
            except Exception:
                pass
            items.append(
                SessionMeta(
                    session_id=sid,
                    created_at=mtime,
                    model="unknown",
                    message_count=count,
                    path=f,
                    first_prompt=first_prompt,
                    total_tokens=total_tokens,
                )
            )
        return items

    def latest(self) -> str | None:
        files = sorted(self.session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0].stem if files else None
