"""Tests for scripts/rds_quality.py.

The script lives outside the client package, so this test puts scripts/ on
sys.path the same way scripts/fm_rx_measure.py runs from the repo root.
"""
import array
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import rds_quality as rq  # noqa: E402


def _pack12(values):
    out = bytearray()
    for k in range(0, len(values), 2):
        ii = values[k] & 0xFFF
        qq = values[k + 1] & 0xFFF
        out += bytes([ii & 0xFF, ((ii >> 8) & 0xF) | ((qq & 0xF) << 4),
                      (qq >> 4) & 0xFF])
    return bytes(out)


def test_unpack12_roundtrip(tmp_path):
    vals = [100, -100, 2047, -2048, 0, 5, -1, 8]
    src = tmp_path / "x.f32q"
    src.write_bytes(_pack12(vals))
    dst = tmp_path / "x.f32"

    n = rq.unpack12(str(src), str(dst))

    assert n == 4
    out = array.array("f")
    with open(dst, "rb") as fh:
        out.fromfile(fh, len(vals))
    for i, v in enumerate(vals):
        assert abs(out[i] - v / 2048.0) < 1e-6
