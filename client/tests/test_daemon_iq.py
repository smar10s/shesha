"""The TUI daemon decodes DATA_IQ frames into iq dicts."""
import struct

from shesha.tui.daemon import SheshaDaemon, MsgType


def _iq_payload(ts, freq, rate, bits, samples):
    return struct.pack('<dfIBBH', ts, freq, rate, bits, 0, len(samples) // (2 if bits == 8 else 3)) + samples


def test_dispatch_decodes_data_iq():
    daemon = SheshaDaemon.__new__(SheshaDaemon)  # skip __init__/hardware
    frames: list[dict] = []
    payload = _iq_payload(2.5, 915.0, 2_100_000, 8, b'\x7f\x80')
    daemon._dispatch(MsgType.DATA_IQ, payload, frames)
    assert len(frames) == 1
    f = frames[0]
    assert f['type'] == 'iq'
    assert f['ts'] == 2.5
    assert f['bits'] == 8
    assert f['n_samples'] == 1
    assert f['samples'] == b'\x7f\x80'
