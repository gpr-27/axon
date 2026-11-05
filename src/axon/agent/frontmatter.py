"""
A small YAML-subset reader for agent-definition frontmatter (no PyYAML dependency).

Supports what Claude Code agent files use: ``key: value`` scalars, quoted strings, inline lists
``[a, b]`` and flow maps ``{a: 1}``, block lists (``- item``, ``- key: value`` maps), nested block maps
(``hooks:``, ``mcpServers:``), ``|`` / ``>`` block scalars and ``#`` comments. ``true``/``false`` become
bools; every other scalar stays a string. Raises ``ValueError`` on input it cannot read, so a broken
file is reported instead of half-loaded.
"""
from __future__ import annotations

import json
from typing import Any


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """(frontmatter text or None, body). The opening ``---`` must be the first line."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def _strip_comment(s: str) -> str:
    quote = ""
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.rstrip()


def _split_top(s: str, sep: str = ",") -> list[str]:
    """Split on *sep* outside quotes and brackets."""
    out: list[str] = []
    depth, quote, cur = 0, "", []
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in "[{(":
            depth += 1
            cur.append(ch)
        elif ch in "]})":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def _scalar(raw: str) -> Any:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    return s


def _inline(raw: str) -> Any:
    s = raw.strip()
    if s.startswith("[") and s.endswith("]"):
        return [_inline(p) for p in _split_top(s[1:-1])]
    if s.startswith("{") and s.endswith("}"):
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        out: dict[str, Any] = {}
        for part in _split_top(s[1:-1]):
            k, sep, v = part.partition(":")
            if not sep:
                raise ValueError(f"bad flow map entry: {part!r}")
            out[str(_scalar(k))] = _inline(v)
        return out
    return _scalar(s)


def _key_value(text: str) -> tuple[str, str] | None:
    """('key', 'rest') for ``key: rest`` (the colon outside quotes, followed by space or end)."""
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
            return text[:i].strip().strip("'\""), text[i + 1:].strip()
    return None


def parse_yaml_subset(text: str) -> dict[str, Any]:
    raw_lines = text.splitlines()
    lines: list[tuple[int, str]] = []          # (indent, content) with blanks and comments removed
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i].expandtabs(2)
        i += 1
        content = _strip_comment(line.strip())
        if not content:
            continue
        indent = len(line) - len(line.lstrip(" "))
        kv = _key_value(content)
        if kv and kv[1] in ("|", ">", "|-", ">-", "|+", ">+"):
            fold = kv[1].startswith(">")
            block: list[str] = []
            while i < len(raw_lines):
                nxt = raw_lines[i].expandtabs(2)
                if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= indent:
                    break
                block.append(nxt.strip())
                i += 1
            joined = (" " if fold else "\n").join(block).strip()
            lines.append((indent, f"{kv[0]}: {json.dumps(joined)}"))
            continue
        lines.append((indent, content))
    value, pos = _block(lines, 0, lines[0][0] if lines else 0)
    if pos < len(lines):
        raise ValueError(f"unexpected indentation near: {lines[pos][1]!r}")
    if not isinstance(value, dict):
        raise ValueError("frontmatter must be a mapping of fields")
    return value


def _value(rest: str) -> Any:
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        try:
            return json.loads(rest)
        except ValueError:
            return rest[1:-1]
    return _inline(rest)


def _block(lines: list[tuple[int, str]], pos: int, indent: int) -> tuple[Any, int]:
    if pos >= len(lines):
        return {}, pos
    if lines[pos][1] == "-" or lines[pos][1].startswith("- "):
        return _list(lines, pos, indent)
    return _map(lines, pos, indent)


def _map(lines: list[tuple[int, str]], pos: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while pos < len(lines):
        ind, content = lines[pos]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError(f"unexpected indentation near: {content!r}")
        if content == "-" or content.startswith("- "):
            break
        kv = _key_value(content)
        if kv is None:
            raise ValueError(f"expected 'key: value', got: {content!r}")
        key, rest = kv
        pos += 1
        if rest:
            out[key] = _value(rest)
            continue
        if pos < len(lines) and (lines[pos][0] > indent or
                                 (lines[pos][0] == indent and (lines[pos][1] == "-" or lines[pos][1].startswith("- ")))):
            out[key], pos = _block(lines, pos, lines[pos][0])
        else:
            out[key] = None
    return out, pos


def _list(lines: list[tuple[int, str]], pos: int, indent: int) -> tuple[list[Any], int]:
    out: list[Any] = []
    while pos < len(lines):
        ind, content = lines[pos]
        if ind != indent or not (content == "-" or content.startswith("- ")):
            if ind > indent:
                raise ValueError(f"unexpected indentation near: {content!r}")
            break
        rest = content[1:].lstrip()
        offset = len(content) - len(rest)
        if not rest:
            pos += 1
            if pos < len(lines) and lines[pos][0] > indent:
                item, pos = _block(lines, pos, lines[pos][0])
            else:
                item = None
            out.append(item)
        elif _key_value(rest) and not rest.startswith(("[", "{", '"', "'")):
            lines[pos] = (indent + offset, rest)       # "- key: v" starts a map whose keys sit after the dash
            item, pos = _map(lines, pos, indent + offset)
            out.append(item)
        else:
            out.append(_value(rest))
            pos += 1
    return out, pos
