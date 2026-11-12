"""Dependency-free QR Code encoder (byte mode, versions 1-10, ECC L/M/Q/H)."""

from __future__ import annotations

_ECC_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}
_ECC_PER_BLOCK = {
    "L": [7, 10, 15, 20, 26, 18, 20, 24, 30, 18],
    "M": [10, 16, 26, 18, 24, 16, 18, 22, 22, 26],
    "Q": [13, 22, 18, 26, 18, 24, 18, 22, 20, 24],
    "H": [17, 28, 22, 16, 22, 28, 26, 26, 24, 28],
}
_NUM_BLOCKS = {
    "L": [1, 1, 1, 1, 1, 2, 2, 2, 2, 4],
    "M": [1, 1, 1, 2, 2, 4, 4, 4, 5, 5],
    "Q": [1, 1, 2, 2, 4, 4, 6, 6, 8, 8],
    "H": [1, 1, 2, 4, 4, 4, 5, 6, 8, 8],
}
_TOTAL_CODEWORDS = [26, 44, 70, 100, 134, 172, 196, 242, 292, 346]
_ALIGN = [[], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]]
_MAX_VERSION = 10


def _gf_tables() -> tuple[list[int], list[int]]:
    exp, log, x = [0] * 512, [0] * 256, 1
    for i in range(255):
        exp[i], log[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _rs_remainder(data: list[int], n: int) -> list[int]:
    """Reed-Solomon parity (n bytes) of ``data`` over GF(256)."""
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            if c:
                nxt[j + 1] ^= _EXP[_LOG[c] + i]
        gen = nxt
    rem = [0] * n
    for b in data:
        f = b ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            lf = _LOG[f]
            for i in range(n):
                if gen[i + 1]:
                    rem[i] ^= _EXP[_LOG[gen[i + 1]] + lf]
    return rem


def _data_capacity(version: int, ecc: str) -> int:
    return (_TOTAL_CODEWORDS[version - 1]
            - _ECC_PER_BLOCK[ecc][version - 1] * _NUM_BLOCKS[ecc][version - 1])


def _encode_codewords(data: bytes, version: int, ecc: str) -> list[int]:
    """Build the final interleaved data + ECC codeword sequence."""
    cap = _data_capacity(version, ecc)
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))
    put(0, -len(bits) % 8)
    cws = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(cws) < cap:
        cws.append(pad)
        pad ^= 0xEC ^ 0x11

    nblocks = _NUM_BLOCKS[ecc][version - 1]
    necc = _ECC_PER_BLOCK[ecc][version - 1]
    total = _TOTAL_CODEWORDS[version - 1]
    short_len = total // nblocks - necc
    nshort = nblocks - total % nblocks
    blocks: list[list[int]] = []
    k = 0
    for i in range(nblocks):
        n = short_len + (0 if i < nshort else 1)
        blocks.append(cws[k:k + n])
        k += n
    eccs = [_rs_remainder(b, necc) for b in blocks]
    out: list[int] = []
    for i in range(short_len + 1):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(necc):
        out.extend(e[i] for e in eccs)
    return out


def _bch(value: int, poly: int, poly_bits: int, data_bits: int) -> int:
    rem = value << (poly_bits - 1)
    for i in range(data_bits + poly_bits - 2, poly_bits - 2, -1):
        if (rem >> i) & 1:
            rem ^= poly << (i - poly_bits + 1)
    return (value << (poly_bits - 1)) | rem


class _Grid:
    """Module grid with a mask of reserved (function pattern) cells."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.m[y][x] = dark
        self.fn[y][x] = True

    def draw_function_patterns(self) -> None:
        s = self.size
        for i in range(s):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        d = max(abs(dx), abs(dy))
                        self.set(x, y, d not in (2, 4))
        pos = _ALIGN[self.version - 1]
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0, "L")  # reserve; redrawn once the mask is known
        if self.version >= 7:
            bits = _bch(self.version, 0x1F25, 13, 6)
            for i in range(18):
                bit = bool((bits >> i) & 1)
                a, b = s - 11 + i % 3, i // 3
                self.set(a, b, bit)
                self.set(b, a, bit)

    def draw_format(self, mask: int, ecc: str) -> None:
        s = self.size
        bits = _bch((_ECC_BITS[ecc] << 3) | mask, 0x537, 11, 5) ^ 0x5412
        bit = lambda i: bool((bits >> i) & 1)  # noqa: E731
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, s - 15 + i, bit(i))
        self.set(8, s - 8, True)

    def place_data(self, cws: list[int]) -> None:
        bits = [(c >> (7 - i)) & 1 for c in cws for i in range(8)]
        s, k, right = self.size, 0, self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    y = s - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fn[y][x] and k < len(bits):
                        self.m[y][x] = bool(bits[k])
                        k += 1
            right -= 2


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


_FINDER_LIKE = bytes((1, 0, 1, 1, 1, 0, 1))


def _finder_like(row: list[bool]) -> int:
    """N3 score: 1:1:3:1:1 patterns with 4 light modules on a side (edges count as light)."""
    seq = bytes([0] * 4 + [int(v) for v in row] + [0] * 4)
    score, idx = 0, seq.find(_FINDER_LIKE)
    while idx != -1:
        end = idx + 7
        if not any(seq[idx - 4:idx]) or not any(seq[end:end + 4]):
            score += 40
            idx = seq.find(_FINDER_LIKE, end)
        else:
            idx = seq.find(_FINDER_LIKE, idx + 4)
    return score


def _penalty(m: list[list[bool]]) -> int:
    """Standard QR mask penalty (rules N1-N4)."""
    n = len(m)
    score = 0
    for grid in (m, [list(c) for c in zip(*m)]):
        for row in grid:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                else:
                    run = 1
            score += _finder_like(row)
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    return score + 10 * (abs(dark * 100 - n * n * 50) // (n * n * 5))


def _build(text: str, ecc: str, mask: int | None = None) -> tuple[list[list[bool]], int, int]:
    if ecc not in _ECC_BITS:
        raise ValueError(f"unknown ECC level: {ecc!r}")
    data = text.encode("utf-8")
    for version in range(1, _MAX_VERSION + 1):
        # 4 mode bits + length field + 8 bits per byte must fit in capacity.
        if 4 + (8 if version < 10 else 16) + 8 * len(data) <= _data_capacity(version, ecc) * 8:
            break
    else:
        raise ValueError("text too long for QR version 10")
    grid = _Grid(version)
    grid.draw_function_patterns()
    grid.place_data(_encode_codewords(data, version, ecc))
    best: tuple[int, int, list[list[bool]]] | None = None
    for mk in ([mask] if mask is not None else range(8)):
        grid2 = _Grid(version)
        grid2.m = [r[:] for r in grid.m]
        grid2.fn = grid.fn
        for y in range(grid.size):
            for x in range(grid.size):
                if not grid.fn[y][x] and _MASKS[mk](x, y):
                    grid2.m[y][x] = not grid2.m[y][x]
        grid2.fn = [r[:] for r in grid.fn]
        grid2.draw_format(mk, ecc)
        score = _penalty(grid2.m)
        if best is None or score < best[0]:
            best = (score, mk, grid2.m)
    assert best is not None
    return best[2], version, best[1]


def qr_matrix(text: str, ecc: str = "M") -> list[list[bool]]:
    """Return the QR module matrix (True = dark, no quiet zone)."""
    return _build(text, ecc.upper())[0]


def qr_terminal(text: str, ecc: str = "M", indent: str = "  ") -> str:
    """Render as half-block text: black modules on a white ANSI background."""
    m = qr_matrix(text, ecc)
    n = len(m) + 4

    def dark(x: int, y: int) -> bool:
        return 2 <= x < n - 2 and 2 <= y < n - 2 and m[y - 2][x - 2]

    chars = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    lines = []
    for y in range(0, n, 2):
        row = "".join(chars[(dark(x, y), dark(x, y + 1))] for x in range(n))
        lines.append(f"{indent}\033[47;30m{row}\033[0m")
    return "\n".join(lines)


def qr_svg(text: str, ecc: str = "M", border: int = 4) -> str:
    """Return a compact standalone SVG of the QR code."""
    m = qr_matrix(text, ecc)
    size = len(m) + 2 * border
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                parts.append(f"M{start + border} {y + border}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges"><rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path d="{"".join(parts)}" fill="#000"/></svg>'
    )
