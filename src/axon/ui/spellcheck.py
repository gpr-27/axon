"""
Spell checking for the prompt input: underlines words the user's own checker does not know.

Runs aspell, hunspell or ispell in pipe mode (``-a``, the ispell protocol) on a background thread, so typing
never waits for it. Axon has no word list of its own. Reads ``settings.spellcheck``, which a project cannot set:
``{"enabled": true, "checker": "hunspell", "language": "en_GB", "color": "yellow"}``.

Checking stops for the session after two failures in a row, or three answers slower than 15 seconds.
"""
from __future__ import annotations

import logging
import queue
import re
import shutil
import subprocess
import threading
from typing import Any

log = logging.getLogger("axon.spellcheck")

CHECKERS = ("aspell", "hunspell", "ispell")
SLOW_ANSWER_S = 15.0
MAX_FAILURES = 2
MAX_SLOW = 3

_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")
_CODELIKE_RE = re.compile(
    r"`[^`\n]*`?"                      # text in backticks
    r"|https?://\S+|\w+://\S+"         # URLs
    r"|(?:(?<=\s)|^)[/@~.][^\s]*"      # /commands, @mentions, paths
    r"|\S*[/\\]\S*"                    # anything with a path separator
    r"|(?:(?<=\s)|^)--?[\w-][^\s]*"    # flags
)
# Scripts without spaces between words: never sent to a checker.
_SKIP_SCRIPT_RE = re.compile("[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")


def candidate_words(text: str) -> list[tuple[int, int, str]]:
    """(start, end, word) of the words worth checking: nothing that looks like code, and not the word under
    construction (the text's last word when nothing follows it)."""
    masked = _CODELIKE_RE.sub(lambda m: " " * len(m.group(0)), text)
    out: list[tuple[int, int, str]] = []
    for m in _WORD_RE.finditer(masked):
        word = m.group(0)
        start, end = m.span()
        if end == len(text):
            continue                                       # still being typed
        if len(word) < 2 or _SKIP_SCRIPT_RE.search(word) or any(c.isupper() for c in word[1:]):
            continue
        if (start and (text[start - 1].isdigit() or text[start - 1] == "_")) or (text[end:end + 1].isdigit() or text[end:end + 1] == "_"):
            continue
        out.append((start, end, word))
    return out


def parse_config(raw: Any) -> dict[str, Any] | None:
    """The usable config, or None when spell checking is off."""
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    checker = raw.get("checker")
    language = raw.get("language")
    return {"checker": checker if checker in CHECKERS else "auto",
            "language": language if isinstance(language, str) and _LANGUAGE_RE.match(language) else "",
            "color": color_sgr(raw.get("color"))}


_NAMED = {"black": 30, "red": 31, "green": 32, "yellow": 33, "blue": 34, "magenta": 35, "cyan": 36, "white": 37, "gray": 90, "grey": 90}


def color_sgr(value: Any) -> str:
    """SGR parameters of an underline colour: a name, #rrggbb, #rgb, rgb(r,g,b), ansi256(n), ansi:<name>. '' = theme's."""
    if not isinstance(value, str):
        return ""
    v = value.strip().lower()
    if v.startswith("ansi:"):
        v = v[5:]
    if v in _NAMED:
        return str(_NAMED[v])
    m = re.fullmatch(r"#([0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        return f"38;2;{int(h[:2], 16)};{int(h[2:4], 16)};{int(h[4:], 16)}"
    m = re.fullmatch(r"#([0-9a-f]{3})", v)
    if m:
        r, g, b = (int(c * 2, 16) for c in m.group(1))
        return f"38;2;{r};{g};{b}"
    m = re.fullmatch(r"rgb\((\d{1,3}),\s*(\d{1,3}),\s*(\d{1,3})\)", v)
    if m and all(int(x) < 256 for x in m.groups()):
        return "38;2;" + ";".join(m.groups())
    m = re.fullmatch(r"ansi256\((\d{1,3})\)", v)
    if m and int(m.group(1)) < 256:
        return f"38;5;{m.group(1)}"
    return ""


def find_checker(cfg: dict[str, Any]) -> str | None:
    if cfg.get("checker") in CHECKERS:          # no fallback from a checker the user named
        return shutil.which(cfg["checker"])
    for name in CHECKERS:
        path = shutil.which(name)
        if path:
            return path
    log.debug("[spellcheck] none of %s found", ", ".join(CHECKERS))
    return None


class SpellChecker:
    """Answers :meth:`misspelled` from a cache at once and asks the checker about new words in the background."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.known: dict[str, bool] = {}          # word -> misspelled
        self.dead = False
        self.failures = 0
        self.slow = 0
        self.version = 0                          # bumps when answers arrive, so the prompt redraws
        self._todo: queue.Queue[str] = queue.Queue()
        self._asked: set[str] = set()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    def misspelled(self, word: str) -> bool:
        hit = self.known.get(word)
        if hit is None and not self.dead and word not in self._asked:
            self._asked.add(word)
            self._todo.put(word)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="axon-spellcheck", daemon=True)
                self._thread.start()
        return bool(hit)

    def _start(self) -> bool:
        path = find_checker(self.cfg)
        if path is None:
            self.dead = True
            return False
        argv = [path, "-a"]
        if self.cfg.get("language"):
            argv += ["-d", self.cfg["language"]]
        log.debug("[spellcheck] starting %s", " ".join(argv))
        try:
            self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          text=True, encoding="utf-8", errors="replace", bufsize=1)
            banner = self._proc.stdout.readline() if self._proc.stdout else ""
        except OSError as e:
            log.debug("[spellcheck] could not start: %s", e)
            return self._failed()
        if not banner:
            return self._failed()
        return True

    def _failed(self) -> bool:
        self.failures += 1
        self._proc = None
        if self.failures >= MAX_FAILURES:
            log.debug("[spellcheck] stopped after %d failures", self.failures)
            self.dead = True
        return False

    def _ask(self, word: str) -> bool | None:
        """True / False = misspelled or not, None = no answer (the checker died or was too slow)."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None
        answer: list[bool] = []

        def read() -> None:
            first = proc.stdout.readline()         # type: ignore[union-attr]
            if not first:
                return
            line = first
            while line.strip():                    # one line per word, then a blank line
                line = proc.stdout.readline()      # type: ignore[union-attr]
            answer.append(first[:1] in ("&", "#", "?"))

        try:
            proc.stdin.write("^" + word + "\n")     # ^ keeps a word from being read as a pipe-mode command
            proc.stdin.flush()
        except (OSError, ValueError):
            return None
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(SLOW_ANSWER_S)
        if reader.is_alive():
            self.slow += 1
            log.debug("[spellcheck] no answer within %.0fs (%d)", SLOW_ANSWER_S, self.slow)
            if self.slow >= MAX_SLOW:
                self.dead = True
            proc.kill()
            self._proc = None
            return None
        return answer[0] if answer else None

    def _run(self) -> None:
        while not self.dead:
            word = self._todo.get()
            if self._proc is None and not self._start():
                if self.dead:
                    return
                self._asked.discard(word)
                continue
            result = self._ask(word)
            if result is None:
                if self._proc is not None:
                    self._failed()
                continue
            self.failures = 0
            self.known[word] = result
            self.version += 1


_active: SpellChecker | None = None
_active_raw: Any = None


def checker_for(settings: Any) -> SpellChecker | None:
    """The session's checker, or None when spell checking is off."""
    global _active, _active_raw
    raw = getattr(settings, "spellcheck", None)
    if raw != _active_raw or (_active is None and raw):
        _active_raw = raw
        cfg = parse_config(raw)
        _active = SpellChecker(cfg) if cfg is not None else None
    return _active


def underline_row(styled: str, plain: str, checker: SpellChecker, base: str = "") -> str:
    """*styled* (the row with its colours) with the misspelled words of *plain* underlined."""
    words = {w for _s, _e, w in candidate_words(plain) if checker.misspelled(w)}
    if not words:
        return styled
    color = checker.cfg.get("color") or "31"
    on, off = f"\033[4;{color}m", "\033[24;39m"
    pattern = re.compile(r"(?<![\w\x1b\[;])(?:" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")(?![\w])")
    return pattern.sub(lambda m: f"{on}{m.group(0)}{off}{base}", styled)
