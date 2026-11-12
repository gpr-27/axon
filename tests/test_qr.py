"""Tests for the stdlib QR encoder."""

import math
import random
import string

import pytest

from axon.features.qr import _build, qr_matrix, qr_svg, qr_terminal

_ALPHABET = string.ascii_letters + string.digits + "-_"
_rng = random.Random(7)
SAMPLES = [
    "hello",
    "http://192.168.1.23:8788/?t=" + "".join(_rng.choice(_ALPHABET) for _ in range(32)),
    "".join(_rng.choice(_ALPHABET + " ") for _ in range(120)),
    "héllo wörld ✓",
]


def test_square_and_size_formula() -> None:
    for text in SAMPLES:
        m = qr_matrix(text)
        assert len(m) % 4 == 1 and all(len(r) == len(m) for r in m)
        assert (len(m) - 17) // 4 in range(1, 11)


def test_finder_patterns() -> None:
    m = qr_matrix("hello")
    n = len(m)
    for ox, oy in ((0, 0), (n - 7, 0), (0, n - 7)):
        for dy in range(7):
            for dx in range(7):
                ring = max(abs(dx - 3), abs(dy - 3))
                assert m[oy + dy][ox + dx] == (ring != 2)


def test_oversized_raises() -> None:
    with pytest.raises(ValueError):
        qr_matrix("x" * 400)
    with pytest.raises(ValueError):
        qr_matrix("x", ecc="Z")


def test_svg_and_terminal() -> None:
    assert qr_svg("hello").startswith("<svg")
    n = len(qr_matrix("hello"))
    assert len(qr_terminal("hello").split("\n")) == math.ceil((n + 4) / 2)


@pytest.mark.parametrize("ecc", ["L", "M", "Q", "H"])
@pytest.mark.parametrize("text", SAMPLES)
def test_matches_segno(text: str, ecc: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compare with segno using our chosen mask.

    segno 1.6 appends a stray zero byte after the terminator (harmless to
    decoders but non-canonical) and scores masks before drawing format info,
    so its padding is patched and the mask is forced to ours.
    """
    segno = pytest.importorskip("segno")
    import segno.encoder as enc

    monkeypatch.setattr(enc, "write_padding_bits",
                        lambda buff, version, length: buff.extend([0] * (-len(buff) % 8)))
    try:
        mine, version, mask = _build(text, ecc)
    except ValueError:
        pytest.skip("does not fit version 10")
    qr = segno.make(text, error=ecc.lower(), boost_error=False, micro=False, mask=mask)
    assert qr.version == version
    assert mine == [[bool(v) for v in row] for row in qr.matrix]
