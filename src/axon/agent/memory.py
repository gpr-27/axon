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
    created_at: float = field(default_factory=time.time)

class MemoryStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.memory_dir = workspace / ".axon" / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def learn(self, text: str, category: str = "conventions") -> MemoryItem:
        """Saves a learned rule or pattern to persistent disk memory."""
        first_line = text.strip().splitlines()[0] if text.strip() else "Learned rule"
        slug = re.sub(r"[^a-z0-9\-]", "", first_line.lower().replace(" ", "-"))[:40] or f"item-{int(time.time())}"
        
        item = MemoryItem(
            id=slug,
            title=first_line[:60],
            content=text.strip(),
            category=category,
            created_at=time.time(),
        )
        
        target = self.memory_dir / f"{slug}.md"
        file_content = f"""---
title: "{item.title}"
category: "{item.category}"
created_at: {item.created_at}
---

# {item.title}

{item.content}
"""
        target.write_text(file_content, encoding="utf-8")
        return item

    def list_all(self) -> list[MemoryItem]:
        """Lists all persistent memory items."""
        items: list[MemoryItem] = []
        for p in sorted(self.memory_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                txt = p.read_text(encoding="utf-8")
                lines = txt.splitlines()
                title = p.stem
                cat = "conventions"
                body = txt
                if txt.startswith("---"):
                    parts = txt.split("---", 2)
                    if len(parts) >= 3:
                        for l in parts[1].splitlines():
                            if l.startswith("title:"):
                                title = l.split(":", 1)[1].strip().strip('"').strip("'")
                            elif l.startswith("category:"):
                                cat = l.split(":", 1)[1].strip().strip('"').strip("'")
                        body = parts[2].strip()
                items.append(MemoryItem(id=p.stem, title=title, content=body, category=cat, created_at=p.stat().st_mtime))
            except Exception:
                pass
        return items

    def delete(self, item_id: str) -> bool:
        """Deletes a memory item."""
        target = self.memory_dir / f"{item_id}.md"
        if target.exists():
            target.unlink()
            return True
        return False

    def search(self, query: str) -> list[MemoryItem]:
        """Search memory items by keyword."""
        q = query.lower()
        return [item for item in self.list_all() if q in item.title.lower() or q in item.content.lower() or q in item.category.lower()]

    def clear(self) -> None:
        """Clear all memory items."""
        for p in self.memory_dir.glob("*.md"):
            try:
                p.unlink()
            except Exception:
                pass

