"""Memory file regressions: different memories never share a file, and a crash mid-write keeps the old file."""
from __future__ import annotations

import contextlib
from pathlib import Path

from axon.agent.memory import AutoMemory, MemoryStore, auto_memory_dir


def test_titles_with_the_same_first_words_get_separate_topic_files(workspace: Path) -> None:
    memory = AutoMemory(workspace)

    first, _ = memory.save("User preferences about code style in tests", "Use pytest fixtures.")
    second, _ = memory.save("User preferences about code style in docs", "Use short sentences.")

    assert first != second
    assert "pytest fixtures" in first.read_text(encoding="utf-8")
    assert "short sentences" in second.read_text(encoding="utf-8")
    index = (auto_memory_dir(workspace) / "MEMORY.md").read_text(encoding="utf-8")
    assert f"]({first.name})" in index and f"]({second.name})" in index


def test_saving_the_same_title_again_updates_its_file(workspace: Path) -> None:
    memory = AutoMemory(workspace)
    first, _ = memory.save("Deploy process", "old text")
    again, _ = memory.save("Deploy process", "new text")

    assert again == first
    assert "new text" in first.read_text(encoding="utf-8") and "old text" not in first.read_text(encoding="utf-8")
    assert (auto_memory_dir(workspace) / "MEMORY.md").read_text(encoding="utf-8").count("Deploy process") == 1


def crash_after_truncating(self: Path, data: str, *args, **kwargs) -> int:
    """What a power cut during an in-place write looks like: the file is cut to zero, then the write fails."""
    self.open("w").close()
    raise OSError("disk lost power")


def test_crash_while_saving_keeps_the_old_index(workspace: Path, monkeypatch) -> None:
    memory = AutoMemory(workspace)
    memory.save("Deploy process", "Use make deploy.")
    index = auto_memory_dir(workspace) / "MEMORY.md"
    before = index.read_text(encoding="utf-8")

    monkeypatch.setattr(Path, "write_text", crash_after_truncating)
    with contextlib.suppress(OSError):
        memory.save("Release notes", "Keep a changelog.")

    assert index.read_text(encoding="utf-8").startswith(before.rstrip())


def test_crash_while_rewriting_a_learned_memory_keeps_the_old_file(workspace: Path, monkeypatch) -> None:
    store = MemoryStore(workspace)
    item = store.learn("Always run the linter before committing.", custom_title="Linter rule")
    target = workspace / ".axon" / "memory" / f"{item.id}.md"

    monkeypatch.setattr(Path, "write_text", crash_after_truncating)
    with contextlib.suppress(OSError):
        store.learn("Always run the linter before committing.", custom_title="Linter rule")

    assert [i.content for i in store.list_all()] == ["Always run the linter before committing."]
    assert target.read_text(encoding="utf-8").startswith("---\n")
