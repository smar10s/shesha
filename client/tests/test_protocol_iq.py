"""Tests for the IQ data wire format."""
import struct

import pytest

from shesha.protocol import (
    FRAME_HEADER,
    MsgType,
    DaemonMode,
    SUBSCRIBE_IQ,
    IqfFrame,
    parse_iq,
    cmd_set_iq_bits,
    cmd_iq_tx_stream_data,
    cmd_iq_tx_stream_end,
    parse_status,
    parse_tx_stats,
)

IQ_HEADER = struct.Struct('<dfIBBH')  # independent of the client's own


def _iq_payload(bits=12, n=4) -> bytes:
    head = IQ_HEADER.pack(1.5, 915.0, 2_000_000, bits, 0, n)
    bps = 3 if bits == 12 else 2
    return head + bytes(range(n * bps))


def test_daemon_mode_iq_value():
    assert DaemonMode.IQ == 2


def test_subscribe_iq_bit():
    assert SUBSCRIBE_IQ == (1 << 3)


def test_cmd_set_iq_bits_encoding():
    frame = cmd_set_iq_bits(8)
    mtype, plen, _seq = FRAME_HEADER.unpack_from(frame, 0)
    assert mtype == MsgType.CMD_SET_IQ_BITS
    assert plen == 1
    assert frame[FRAME_HEADER.size:] == b'\x08'


def test_cmd_iq_tx_stream_data_encoding():
    data = b'\x01\x02\x03\x04\x05\x06'
    frame = cmd_iq_tx_stream_data(data)
    mtype, plen, _seq = FRAME_HEADER.unpack_from(frame, 0)
    assert mtype == MsgType.CMD_IQ_TX_STREAM_DATA
    payload = frame[FRAME_HEADER.size:]
    assert struct.unpack_from('<I', payload, 0)[0] == 6
    assert payload[4:] == data
    assert plen == 4 + len(data)


def test_cmd_iq_tx_stream_end_encoding():
    frame = cmd_iq_tx_stream_end()
    mtype, plen, _seq = FRAME_HEADER.unpack_from(frame, 0)
    assert mtype == MsgType.CMD_IQ_TX_STREAM_END
    assert plen == 0


def test_parse_iq_12bit():
    payload = _iq_payload(bits=12, n=4)
    f = parse_iq(payload)
    assert isinstance(f, IqfFrame)
    assert f.timestamp == 1.5
    assert f.freq_mhz == 915.0
    assert f.sample_rate_hz == 2_000_000
    assert f.bits == 12
    assert f.n_samples == 4
    assert f.samples == payload[20:]


def test_parse_iq_8bit():
    payload = _iq_payload(bits=8, n=3)
    f = parse_iq(payload)
    assert f.bits == 8
    assert f.n_samples == 3
    assert f.samples == payload[20:]


def test_parse_iq_unknown_bits_raises():
    with pytest.raises(ValueError):
        parse_iq(_iq_payload(bits=10, n=1))


def test_parse_iq_truncated_raises():
    payload = _iq_payload(bits=12, n=8)
    with pytest.raises(ValueError):
        parse_iq(payload[:-3])


def test_status_91_bytes_with_iq_bits_and_coverage():
    # Reconstruct the daemon's byte layout independently.
    payload = struct.pack('<QIIIfBBBBfBBBHIBBBHBIIIIIIBBQQBB',
                          5_180_000_000, 2_000_000, 2_000_000, 1024,
                          50.0, 0, 1, 10, 2, -10.0, 0, 3, 0, 1, 0, 0, 1, 12,
                          276, 2,
                          200_000, 8_000_000, 0,
                          2_100_000, 4_000_000, 0, 1, 0, 0, 0, 1, 0)
    st = parse_status(payload)
    assert st.daemon_mode == DaemonMode.IQ
    assert st.iq_bits == 12
    assert st.sample_rate_hz == 2_000_000
    assert st.psd_coverage == 0.276
    assert st.workers == 2
    assert st.accum == 1
    assert st.fpga_running is False
    assert st.radio.sr_is_user_input
    assert st.radio.allows_sr(4_000_000)


def test_tx_stats_flags_at_fixed_offsets_92_and_93():
    # Build the payload by hand: these offsets are the wire contract, not
    # something to share with the parser's struct format.
    payload = bytearray(96)
    payload[92] = 1
    payload[93] = 1
    ts = parse_tx_stats(bytes(payload))
    assert ts.iq_active is True
    assert ts.dma_start_failed is True

    payload[92] = 0
    ts = parse_tx_stats(bytes(payload))
    assert ts.iq_active is False
    assert ts.dma_start_failed is True


@pytest.mark.parametrize('failed,expected', [(0, False), (1, True)])
def test_tx_stats_96_bytes_with_iq_fields(failed, expected):
    payload = struct.pack('<5QIi3I4I4B3I4B',
                          100, 100, 5000, 100, 0, 2_000_000, 3000, 3000, 3000,
                          0, 100, 262144, 44100, 0, 1, 0, 1, 0,
                          0, 7, 13, 1, failed, 0, 0)
    ts = parse_tx_stats(payload)
    assert ts.iq_active is True
    assert ts.iq_ring_fill == 0
    assert ts.iq_push_rejects == 7
    assert ts.iq_starved == 13
    assert ts.dma_start_failed is expected
