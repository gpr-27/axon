"""Interface-language data shared by the app and the agent: the language list, the catalogs and the
"reply in this language" line. No web or terminal imports.

Catalogs live in ``features/i18n/<code>.json`` as flat ``{"key": "text"}`` maps. English (en.json) is the
source: every key exists there, and a missing key in another language falls back to English.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

I18N_DIR = Path(__file__).parent / "i18n"
# (code, English name, name in its own language)
LANGS: tuple[tuple[str, str, str], ...] = (("en", "English", "English"), ("te", "Telugu", "తెలుగు"), ("hi", "Hindi", "हिन्दी"))
CODES = tuple(c for c, _e, _n in LANGS)
_cache: dict[str, dict[str, str]] = {}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def catalog(code: str) -> dict[str, str]:
    """The key -> text map for one language. A missing or corrupt file gives an empty map, never an error."""
    if code not in CODES:
        return {}
    if code not in _cache:
        try:
            raw = json.loads((I18N_DIR / f"{code}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _cache[code] = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw, dict) else {}
    return _cache[code]


def placeholders(text: str) -> list[str]:
    return sorted(_PLACEHOLDER.findall(text))


def reply_line(code: Any) -> str:
    """One line for the system prompt, or "" when the preference is off or the code is unknown."""
    code = str(code or "").strip().lower()
    if code not in CODES:
        return ""
    english, native = next((e, n) for c, e, n in LANGS if c == code)
    name = english if code == "en" else f"{english} ({native})"
    return (f"Write your replies to the user in {name}. Keep code, file paths, commands, identifiers and technical "
            f"terms exactly as they are.")
