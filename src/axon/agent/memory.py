"""
Persistent Memory & Knowledge Item Store (adapted from Odysseus memory services).
Persists learned conventions, debugging patterns, and user preferences into .axon/memory/<slug>.md.
"""
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class MemoryItem:
    id: str
    title: str
    content: str
    category: str  # "conventions", "architecture", "debugging", "preferences"
    scope: str = "project"  # "global", "project"
    created_at: float = field(default_factory=time.time)

def _generate_clean_slug(text: str) -> str:
    """Generate a readable, semantic slug without chopping words."""
    cleaned = text.strip()
    prefixes = [
        "this is the", "this is a", "please remember that", "always remember to",
        "i want to", "we want to", "remember that", "the project is", "note that",
    ]
    lowered = cleaned.lower()
    for p in prefixes:
        if lowered.startswith(p):
            cleaned = cleaned[len(p):].strip(" ,:.-")
            break

    words = re.findall(r"[a-z0-9]+", cleaned.lower())
    if not words:
        words = ["learned", "rule", str(int(time.time()))]

    stopwords = {"a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of", "with", "that", "this", "it", "is"}
    key_words = [w for w in words if w not in stopwords] or words
    slug_words = key_words[:5]
    slug = "-".join(slug_words)[:40].rstrip("-")
    return slug or f"rule-{int(time.time())}"

def _generate_clean_title(text: str) -> str:
    """Extract a clean title up to first punctuation or word boundary."""
    first_sentence = re.split(r"[.\n!?]", text.strip())[0].strip()
    if len(first_sentence) <= 70:
        return first_sentence
    truncated = first_sentence[:65].rsplit(" ", 1)[0]
    return truncated + "..."

class MemoryStore:
    def __init__(self, workspace: Path, global_dir: Path | None = None) -> None:
        self.workspace = workspace
        self.project_memory_dir = workspace / ".axon" / "memory"

        # In test environments (tmp/var), isolate global directory to prevent test pollution
        if global_dir is not None:
            self.global_memory_dir = global_dir
        elif str(workspace).startswith(("/tmp", "/var/folders", "/private/var")):
            self.global_memory_dir = workspace.parent / ".global_axon" / "memory"
        else:
            self.global_memory_dir = Path.home() / ".axon" / "memory"

        self.global_memory_dir.mkdir(parents=True, exist_ok=True)

    def learn(
        self,
        text: str,
        category: str = "conventions",
        scope: str = "project",
        custom_title: str | None = None,
    ) -> MemoryItem:
        """Saves a learned rule or pattern to persistent disk memory (project or global)."""
        slug = _generate_clean_slug(custom_title or text)
        title = (custom_title.strip() if custom_title else _generate_clean_title(text))

        item = MemoryItem(
            id=slug,
            title=title,
            content=text.strip(),
            category=category,
            scope=scope,
            created_at=time.time(),
        )

        target_dir = self.global_memory_dir if scope == "global" else self.project_memory_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{slug}.md"
        file_content = f"""---
title: "{item.title}"
category: "{item.category}"
scope: "{item.scope}"
created_at: {item.created_at}
---

# {item.title}

{item.content}
"""
        target.write_text(file_content, encoding="utf-8")
        return item

    def list_all(self) -> list[MemoryItem]:
        """Lists all persistent memory items from both global and project.
        Project items override global items with matching ID."""
        by_id: dict[str, MemoryItem] = {}

        # 1. Load global memories first
        if self.global_memory_dir.exists():
            for p in sorted(self.global_memory_dir.glob("*.md"), key=lambda f: f.stat().st_mtime):
                item = self._load_file(p, scope="global")
                if item:
                    by_id[item.id] = item

        # 2. Load project memories (override global if duplicate id and different folder)
        if self.project_memory_dir != self.global_memory_dir and self.project_memory_dir.exists():
            for p in sorted(self.project_memory_dir.glob("*.md"), key=lambda f: f.stat().st_mtime):
                item = self._load_file(p, scope="project")
                if item:
                    by_id[item.id] = item

        return sorted(by_id.values(), key=lambda it: it.created_at, reverse=True)

    def _load_file(self, p: Path, scope: str = "project") -> MemoryItem | None:
        try:
            txt = p.read_text(encoding="utf-8")
            title = p.stem
            cat = "conventions"
            sc = scope
            body = txt
            if txt.startswith("---"):
                parts = txt.split("---", 2)
                if len(parts) >= 3:
                    for l in parts[1].splitlines():
                        if l.startswith("title:"):
                            title = l.split(":", 1)[1].strip().strip('"').strip("'")
                        elif l.startswith("category:"):
                            cat = l.split(":", 1)[1].strip().strip('"').strip("'")
                        elif l.startswith("scope:"):
                            sc = l.split(":", 1)[1].strip().strip('"').strip("'")
                    body = parts[2].strip()
            return MemoryItem(id=p.stem, title=title, content=body, category=cat, scope=sc, created_at=p.stat().st_mtime)
        except Exception:
            return None

    def delete(self, item_id: str) -> bool:
        """Deletes a memory item from project or global store."""
        proj_target = self.project_memory_dir / f"{item_id}.md"
        if proj_target.exists():
            proj_target.unlink()
            return True
        glob_target = self.global_memory_dir / f"{item_id}.md"
        if glob_target.exists():
            glob_target.unlink()
            return True
        return False

    def search(self, query: str) -> list[MemoryItem]:
        """Search memory items by keyword across both global and project memories."""
        q = query.lower()
        return [item for item in self.list_all() if q in item.title.lower() or q in item.content.lower() or q in item.category.lower()]

    def clear(self) -> None:
        """Clear all project memory items."""
        for p in self.project_memory_dir.glob("*.md"):
            try:
                p.unlink()
            except Exception:
                pass

def distill_and_learn(
    provider: Any,
    text: str,
    workspace: Path,
    scope: str = "project",
) -> MemoryItem:
    """Use fast LLM call to extract and format clean structured memory from conversational user input."""
    prompt = f"""You are a memory distillation module for an AI coding assistant.
Given the following user statement, extract the core rule, preference, or fact into a clean structured format.

Input:
\"\"\"{text}\"\"\"

Generate a JSON object with:
- "title": A concise descriptive title (under 50 characters, capitalize properly, NO conversational words like "Create a memory" or "Save this").
- "category": One of "preferences", "conventions", "architecture", "debugging".
- "content": Clean, direct statement or markdown bullet points of the rule or fact (omit conversational phrases).

Respond ONLY with valid JSON:
{{"title": "...", "category": "...", "content": "..."}}"""

    title = ""
    category = "conventions"
    content = text.strip()

    if provider is not None:
        try:
            model_name = getattr(getattr(provider, "settings", None), "model", "deepseek-v4-flash")
            stream = provider.stream(
                model=model_name,
                system=[{"type": "text", "text": "You are a strict JSON generator. Output only valid JSON."}],
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                max_tokens=500,
                effort="low",
            )
            for _ in stream:
                pass
            turn = provider.finalize()
            raw = turn.text.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
            data = json.loads(raw)
            if isinstance(data, dict):
                title = data.get("title", "").strip()
                category = data.get("category", "conventions")
                content = data.get("content", "").strip() or content
        except Exception:
            pass

    store = MemoryStore(workspace)
    return store.learn(
        text=content or text,
        category=category or "conventions",
        scope=scope,
        custom_title=title or None,
    )


