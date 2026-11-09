"""Emoji shortcodes for the prompt: ``:heart:`` becomes the emoji, ``:hea`` offers suggestions."""
from __future__ import annotations

import re

EMOJI: dict[str, str] = {
    "+1": "👍", "-1": "👎", "100": "💯", "alarm_clock": "⏰", "angry": "😠", "apple": "🍎", "arrow_down": "⬇️",
    "arrow_left": "⬅️", "arrow_right": "➡️", "arrow_up": "⬆️", "art": "🎨", "baby": "👶", "balloon": "🎈",
    "bar_chart": "📊", "bear": "🐻", "beer": "🍺", "bell": "🔔", "bike": "🚲", "bird": "🐦", "birthday": "🎂",
    "blush": "😊", "bomb": "💣", "book": "📖", "books": "📚", "brain": "🧠", "bug": "🐛", "building_construction": "🏗️",
    "bulb": "💡", "cake": "🍰", "calendar": "📅", "camera": "📷", "car": "🚗", "cat": "🐱", "chart_with_upwards_trend": "📈",
    "check": "✔️", "white_check_mark": "✅", "clap": "👏", "clipboard": "📋", "clock": "🕐", "cloud": "☁️", "coffee": "☕",
    "computer": "💻", "confused": "😕", "construction": "🚧", "cool": "😎", "crab": "🦀", "cry": "😢", "crystal_ball": "🔮",
    "dart": "🎯", "disappointed": "😞", "dog": "🐶", "dollar": "💵", "door": "🚪", "eyes": "👀", "facepalm": "🤦",
    "file_folder": "📁", "fire": "🔥", "fish": "🐟", "flag": "🚩", "flashlight": "🔦", "flushed": "😳", "gear": "⚙️",
    "ghost": "👻", "gift": "🎁", "globe": "🌐", "green_heart": "💚", "grin": "😁", "grinning": "😀", "hammer": "🔨",
    "hammer_and_wrench": "🛠️", "hand": "✋", "heart": "❤️", "heart_eyes": "😍", "heavy_check_mark": "✔️", "hibiscus": "🌺",
    "hourglass": "⌛", "house": "🏠", "hugs": "🤗", "information_source": "ℹ️", "joy": "😂", "key": "🔑", "kiss": "😘",
    "ladybug": "🐞", "laughing": "😆", "link": "🔗", "lock": "🔒", "lollipop": "🍭", "mag": "🔍", "mailbox": "📫",
    "memo": "📝", "microscope": "🔬", "moneybag": "💰", "monkey": "🐒", "moon": "🌙", "muscle": "💪", "musical_note": "🎵",
    "nerd_face": "🤓", "no_entry": "⛔", "ok": "🆗", "ok_hand": "👌", "package": "📦", "paperclip": "📎", "party": "🥳",
    "pencil": "✏️", "penguin": "🐧", "pensive": "😔", "pin": "📌", "pizza": "🍕", "point_down": "👇", "point_left": "👈",
    "point_right": "👉", "point_up": "☝️", "pray": "🙏", "pushpin": "📌", "question": "❓", "rainbow": "🌈", "recycle": "♻️",
    "red_circle": "🔴", "relieved": "😌", "robot": "🤖", "rocket": "🚀", "rofl": "🤣", "rose": "🌹", "scream": "😱",
    "see_no_evil": "🙈", "seedling": "🌱", "shield": "🛡️", "shrug": "🤷", "skull": "💀", "sleeping": "😴", "smile": "😄",
    "smiley": "😃", "smirk": "😏", "snake": "🐍", "snowflake": "❄️", "sob": "😭", "sparkles": "✨", "star": "⭐",
    "star2": "🌟", "stop_sign": "🛑", "sun": "☀️", "sunglasses": "😎", "sweat": "😓", "sweat_smile": "😅", "tada": "🎉",
    "test_tube": "🧪", "thinking": "🤔", "thumbsdown": "👎", "thumbsup": "👍", "tired_face": "😫", "tools": "🛠️", "tree": "🌳",
    "trophy": "🏆", "turtle": "🐢", "umbrella": "☂️", "unamused": "😒", "unicorn": "🦄", "warning": "⚠️", "wave": "👋",
    "wink": "😉", "worried": "😟", "wrench": "🔧", "x": "❌", "yellow_heart": "💛", "yum": "😋", "zap": "⚡", "zzz": "💤",
}

# A shortcode starts the input or follows a space, so ':' inside a word or URL does nothing.
_COMPLETE_RE = re.compile(r"(?:^|(?<=\s)):([a-z0-9_+-]+):$")
_PARTIAL_RE = re.compile(r"(?:^|(?<=\s)):([a-z0-9_+-]{2,})$")


def replace_complete(before_cursor: str) -> tuple[int, str] | None:
    """(start of the shortcode, emoji) when the text before the cursor ends in a complete known shortcode."""
    m = _COMPLETE_RE.search(before_cursor)
    if m is None or m.group(1) not in EMOJI:
        return None
    return m.start(), EMOJI[m.group(1)]


def suggestions(before_cursor: str, limit: int = 8) -> tuple[int, list[tuple[str, str]]] | None:
    """(start of the shortcode, [(name, emoji), ...]) for a partly typed shortcode, else None."""
    m = _PARTIAL_RE.search(before_cursor)
    if m is None:
        return None
    q = m.group(1)
    hits = sorted((n for n in EMOJI if n.startswith(q)), key=lambda n: (len(n), n))
    hits += sorted(n for n in EMOJI if q in n and not n.startswith(q))
    return (m.start(), [(n, EMOJI[n]) for n in hits[:limit]]) if hits else None
