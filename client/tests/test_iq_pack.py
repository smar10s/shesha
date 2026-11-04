"""Client-side IQ pack/unpack matches the firmware wire format."""
import pytest

from shesha.protocol import (
    IQ_BITS_8, IQ_BITS_12, pack_iq, unpack_iq,
)


def test_unpack_12_roundtrip_matches_firmware_scale():
    pts = [complex(0.0, 0.0), complex(1.0, -1.0), complex(0.5, 0.25)]
    got = unpack_iq(pack_iq(pts, IQ_BITS_12), IQ_BITS_12)
    assert len(got) == len(pts)
    for a, b in zip(pts, got):
        assert abs(a - b) <= 1 / 2048.0


def test_unpack_8_roundtrip_matches_firmware_scale():
    pts = [complex(0.0, 0.0), complex(1.0, -1.0), complex(0.5, 0.25)]
    got = unpack_iq(pack_iq(pts, IQ_BITS_8), IQ_BITS_8)
    assert len(got) == len(pts)
    for a, b in zip(pts, got):
        assert abs(a - b) <= 1 / 128.0


def test_unpack_12_sign_extends():
    # I = -1 (0xFFF), Q = -2048 (0x800) -> normalized negatives.
    raw = bytes((0xFF, 0x0F, 0x80))
    (z,) = unpack_iq(raw, IQ_BITS_12)
    assert z.real == pytest.approx(-1 / 2048.0)
    assert z.imag == pytest.approx(-2048 / 2048.0)


def test_unpack_rejects_unknown_bits():
    with pytest.raises(ValueError):
        unpack_iq(b'\x00\x00', 10)


def test_pack_rejects_unknown_bits():
    with pytest.raises(ValueError):
        pack_iq([complex(0, 0)], 10)
