"""ANSI terminal output to safe HTML for the dashboard (colour classes only; everything else is escaped)."""
from __future__ import annotations

import html
import re

_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_OTHER_CSI = re.compile(r"\x1b\[[0-9;?<=>]*[ -/]*[@-ln-~]")      # every CSI except SGR ('m')
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_ESC_REST = re.compile(r"\x1b[()#%][ -~]?|\x1b[=>78DEHMNOZc]|\x1b")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x9b]")
_NAMES = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]


def _c256(n: int) -> str:
    if n < 16:
        return ""
    if n >= 232:
        v = 8 + (n - 232) * 10
        return f"#{v:02x}{v:02x}{v:02x}"
    n -= 16
    steps = [0, 95, 135, 175, 215, 255]
    return "#%02x%02x%02x" % (steps[n // 36], steps[(n // 6) % 6], steps[n % 6])


class _Style:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.fg = self.bg = ""          # "c<N>" class suffix or "#rrggbb"
        self.bold = self.dim = self.italic = self.under = self.strike = self.rev = False

    def apply(self, params: str) -> None:
        nums = [int(p) if p.isdigit() else 0 for p in re.split(r"[;:]", params)] if params else [0]
        i = 0
        while i < len(nums):
            n = nums[i]
            if n == 0:
                self.reset()
            elif n == 1:
                self.bold = True
            elif n == 2:
                self.dim = True
            elif n == 3:
                self.italic = True
            elif n == 4:
                self.under = True
            elif n == 7:
                self.rev = True
            elif n == 9:
                self.strike = True
            elif n in (22,):
                self.bold = self.dim = False
            elif n == 23:
                self.italic = False
            elif n == 24:
                self.under = False
            elif n == 27:
                self.rev = False
            elif n == 29:
                self.strike = False
            elif 30 <= n <= 37:
                self.fg = f"c{n - 30}"
            elif 90 <= n <= 97:
                self.fg = f"c{n - 90 + 8}"
            elif 40 <= n <= 47:
                self.bg = f"c{n - 40}"
            elif 100 <= n <= 107:
                self.bg = f"c{n - 100 + 8}"
            elif n == 39:
                self.fg = ""
            elif n == 49:
                self.bg = ""
            elif n in (38, 48):
                target = "fg" if n == 38 else "bg"
                if i + 2 < len(nums) and nums[i + 1] == 5:
                    idx = nums[i + 2] & 255
                    setattr(self, target, f"c{idx}" if idx < 16 else _c256(idx))
                    i += 2
                elif i + 4 < len(nums) and nums[i + 1] == 2:
                    r, g, b = (max(0, min(255, v)) for v in nums[i + 2:i + 5])
                    setattr(self, target, f"#{r:02x}{g:02x}{b:02x}")
                    i += 4
            i += 1

    def open_tag(self) -> str:
        cls, style = [], []
        for val, kind in ((self.fg, "fg"), (self.bg, "bg")):
            if not val:
                continue
            if val.startswith("#"):
                style.append(f"{'color' if kind == 'fg' else 'background'}:{val}")
            else:
                cls.append(f"a{'f' if kind == 'fg' else 'b'}{val[1:]}")
        for flag, name in ((self.bold, "ab"), (self.dim, "ad"), (self.italic, "ai"), (self.under, "au"),
                           (self.strike, "as"), (self.rev, "ar")):
            if flag:
                cls.append(name)
        if not cls and not style:
            return ""
        attrs = f' class="{" ".join(cls)}"' if cls else ""
        if style:
            attrs += f' style="{";".join(style)}"'
        return f"<span{attrs}>"


def _settle_carriage_returns(text: str) -> str:
    """Progress bars rewrite a line with \\r: keep what the line finally showed."""
    if "\r" not in text:
        return text
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            parts = [p for p in line.split("\r") if p != ""]
            line = parts[-1] if parts else ""
        out.append(line)
    return "\n".join(out)


def strip_ansi(text: str) -> str:
    text = _OSC.sub("", text)
    text = _SGR.sub("", text)
    text = _OTHER_CSI.sub("", text)
    return _CTRL.sub("", _ESC_REST.sub("", text))


def ansi_to_html(text: str) -> str:
    """Escaped HTML of *text*; SGR colours/attributes become spans with theme classes (a-f0..a-f15, ab, ad ...)."""
    text = _settle_carriage_returns((text or "").replace("\r\n", "\n"))
    text = _OSC.sub("", text)
    text = _OTHER_CSI.sub("", text)
    out: list[str] = []
    style = _Style()
    open_tag = ""
    pos = 0

    def emit(chunk: str) -> None:
        nonlocal open_tag
        chunk = _CTRL.sub("", _ESC_REST.sub("", chunk))
        if not chunk:
            return
        tag = style.open_tag()
        if tag != open_tag:
            if open_tag:
                out.append("</span>")
            if tag:
                out.append(tag)
            open_tag = tag
        out.append(html.escape(chunk, quote=True))

    for m in _SGR.finditer(text):
        emit(text[pos:m.start()])
        style.apply(m.group(1))
        pos = m.end()
    emit(text[pos:])
    if open_tag:
        out.append("</span>")
    return "".join(out)
