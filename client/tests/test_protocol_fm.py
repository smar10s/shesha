"""Tests for FM TX protocol encoding and DATA_FM parsing."""
import struct

from shesha.protocol import (
    MsgType,
    TxProtocol,
    TxFlags,
    RdsTxConfig,
    cmd_tx,
    cmd_tx_fm,
    cmd_set_rds,
    FRAME_HEADER,
    FM_HEADER_SIZE,
    RDS_WIRE_SIZE,
    PROTO_RDS_TX_SIZE,
    parse_fm,
)

# TX sub-payload: audio_rate, deviation, dac_rate, n_audio (16 B) then the
# fixed RDS transmit block, then float32 audio.
TX_HDR = 8 + 16
AUDIO_OFF = TX_HDR + PROTO_RDS_TX_SIZE


def test_cmd_tx_fm_basic():
    """FM TX encodes audio_rate, deviation, dac_rate, and float32 audio."""
    audio = [0.1, -0.2, 0.3, -0.4]
    frame = cmd_tx_fm(audio, audio_rate=44100, deviation=75000, dac_rate=500000)

    mtype, plen, _seq = FRAME_HEADER.unpack_from(frame, 0)
    assert mtype == MsgType.CMD_TX

    payload = frame[FRAME_HEADER.size:]
    assert len(payload) == plen

    proto, flags, repeat, interval_us = struct.unpack_from('<BBHI', payload, 0)
    assert proto == TxProtocol.FM
    assert flags == TxFlags.NONE
    assert repeat == 1
    assert interval_us == 0

    audio_rate, deviation, dac_rate, n_samples = struct.unpack_from('<IIII', payload, 8)
    assert audio_rate == 44100
    assert deviation == 75000
    assert dac_rate == 500000
    assert n_samples == 4

    for i in range(4):
        r = struct.unpack_from('<f', payload, AUDIO_OFF + i * 4)[0]
        assert abs(r - audio[i]) < 1e-7


def test_cmd_tx_fm_defaults():
    """Default parameters match broadcast FM conventions."""
    audio = [0.5]
    frame = cmd_tx_fm(audio)
    payload = frame[FRAME_HEADER.size:]
    audio_rate, deviation, dac_rate, n_samples = struct.unpack_from('<IIII', payload, 8)
    assert audio_rate == 44100
    assert deviation == 75000
    assert dac_rate == 2_100_000
    assert n_samples == 1

    r = struct.unpack_from('<f', payload, AUDIO_OFF)[0]
    assert abs(r - 0.5) < 1e-7


def test_cmd_tx_fm_empty():
    """Empty audio sends n_samples=0 with no float data."""
    frame = cmd_tx_fm([])
    payload = frame[FRAME_HEADER.size:]
    _, _, _, n_samples = struct.unpack_from('<IIII', payload, 8)
    assert n_samples == 0
    # Payload is exactly header (8) + fm_header (16) + rds block + 0 floats
    assert len(payload) == 8 + 16 + PROTO_RDS_TX_SIZE


def test_cmd_tx_fm_large():
    """Large audio buffer encodes correctly."""
    n = 10000
    audio = [float(i % 256 - 128) / 128.0 for i in range(n)]
    frame = cmd_tx_fm(audio, audio_rate=48000, deviation=50000, dac_rate=2000000)

    payload = frame[FRAME_HEADER.size:]
    audio_rate, deviation, dac_rate, n_samples = struct.unpack_from('<IIII', payload, 8)
    assert audio_rate == 48000
    assert deviation == 50000
    assert dac_rate == 2000000
    assert n_samples == n

    assert len(payload) == 8 + 16 + PROTO_RDS_TX_SIZE + n * 4

    for idx in [0, n // 2, n - 1]:
        r = struct.unpack_from('<f', payload, AUDIO_OFF + idx * 4)[0]
        assert abs(r - audio[idx]) < 1e-7


def test_cmd_tx_fm_roundtrip_via_cmd_tx():
    """Verify cmd_tx_fm uses cmd_tx with protocol=FM and correct sub-payload."""
    audio = [0.75, -0.25]
    frame = cmd_tx_fm(audio, audio_rate=22050, deviation=40000, dac_rate=250000)

    # Rebuild manually via cmd_tx and compare
    sub = struct.pack('<IIII', 22050, 40000, 250000, 2)
    sub += b'\x00' * PROTO_RDS_TX_SIZE
    sub += struct.pack('<2f', 0.75, -0.25)
    expected = cmd_tx(TxProtocol.FM, TxFlags.NONE, 1, 0, sub)

    assert frame == expected


# ─── RDS transmit block ───────────────────────────────────────────────


def test_rds_tx_block_layout():
    rds = RdsTxConfig(enabled=True, tp=True, pi=0x8203, pty=10,
                      ps='TESTFM  ', rt='SHESHA RDS')
    frame = cmd_tx_fm([0.5], rds=rds)
    payload = frame[FRAME_HEADER.size:]
    b = payload[TX_HDR:TX_HDR + PROTO_RDS_TX_SIZE]
    assert len(b) == 76
    assert b[0] == 0x03               # enabled | tp
    assert struct.unpack_from('<H', b, 1)[0] == 0x8203
    assert b[3] == 10
    assert b[4:12] == b'TESTFM  '
    assert b[12:76] == b'SHESHA RDS'.ljust(64, b' ')


def test_cmd_tx_fm_stream_start_carries_rds():
    from shesha.protocol import cmd_tx_fm_stream_start

    rds = RdsTxConfig(enabled=True, pi=0x1111, pty=5, ps='ABCDEFGH',
                      rt='hi')
    frame = cmd_tx_fm_stream_start([0.1, 0.2], rds=rds)
    payload = frame[FRAME_HEADER.size:]
    proto, flags, _, _ = struct.unpack_from('<BBHI', payload, 0)
    assert proto == TxProtocol.FM and flags == TxFlags.STREAM
    b = payload[TX_HDR:TX_HDR + PROTO_RDS_TX_SIZE]
    assert b[0] == 0x01
    assert struct.unpack_from('<H', b, 1)[0] == 0x1111
    assert b[4:12] == b'ABCDEFGH'


def test_cmd_set_rds_frame():
    rds = RdsTxConfig(enabled=True, tp=False, pi=0x8203, pty=10,
                      ps='TESTFM  ', rt='x')
    frame = cmd_set_rds(rds)
    mtype, plen, _seq = FRAME_HEADER.unpack_from(frame, 0)
    assert mtype == MsgType.CMD_SET_RDS
    payload = frame[FRAME_HEADER.size:]
    assert len(payload) == plen == PROTO_RDS_TX_SIZE
    assert payload[0] == 0x01
    assert struct.unpack_from('<H', payload, 1)[0] == 0x8203


# ─── DATA_FM / RDS state layout ──────────────────────────────────────
# Mirrors firmware/tests/test_protocol_fm.c. The offsets are pinned on
# both ends because a disagreement parses as plausible garbage.


def _build_fm_payload(*, rds: bytes, audio: list[float]) -> bytes:
    hdr = struct.pack('<dII', 1.5, 43750, len(audio))
    return hdr + rds + struct.pack(f'<{len(audio)}f', *audio)


def test_rds_wire_size_constants():
    assert FM_HEADER_SIZE == 16
    assert RDS_WIRE_SIZE == 190


def test_parse_fm_reads_rds_state():
    m = bytearray(RDS_WIRE_SIZE)
    m[0] = 0xFF          # synced, pi_valid, ps, rt, ptyn, ct, tp, ta
    m[1] = 0x2B          # ms, rt_ab, di=5
    struct.pack_into('<H', m, 2, 0x8203)
    m[4] = 10
    m[5:13] = b'TESTFM  '
    m[13] = 0xFF
    m[14] = 11
    m[15:26] = b'Hello world'
    struct.pack_into('<Q', m, 79, 0x7FF)
    m[87] = 0xFF
    m[88:96] = b'ROCK ON '
    m[96] = 2
    m[97] = 126
    m[98] = 92
    struct.pack_into('<I', m, 122, 53142)
    struct.pack_into('<H', m, 126, 754)
    m[128] = 3
    m[129] = 1
    struct.pack_into('<H', m, 130, 0x4BD7)
    m[132] = 3
    m[133:136] = bytes([0x12, 0x34, 0x56])
    struct.pack_into('<I', m, 174, 7)
    struct.pack_into('<I', m, 178, 28)
    struct.pack_into('<I', m, 182, 1)
    struct.pack_into('<I', m, 186, 2)

    payload = _build_fm_payload(rds=bytes(m), audio=[0.1, -0.2, 0.3, -0.4])
    frame = parse_fm(payload)

    assert frame.timestamp == 1.5
    assert frame.audio_rate_hz == 43750
    for got, want in zip(frame.audio, [0.1, -0.2, 0.3, -0.4]):
        assert abs(got - want) < 1e-6
    rds = frame.rds
    assert rds is not None
    assert rds.synced and rds.pi_valid
    assert rds.pi == 0x8203 and rds.pi_text == '8203'
    assert rds.pty == 10
    assert rds.tp and rds.ta and rds.ms
    assert rds.di == 5
    assert rds.ps == 'TESTFM  ' and rds.ps_complete
    assert rds.rt == 'Hello world' + ' ' * 53
    assert rds.rt_len == 11 and rds.rt_valid and rds.rt_ab
    assert rds.ptyn == 'ROCK ON ' and rds.ptyn_complete
    assert rds.af == [126, 92]
    assert rds.ct_mjd == 53142 and rds.ct_minutes == 754
    assert rds.ct_offset_half_hours == 3 and rds.ct_valid
    assert rds.oda == [(0x4BD7, 3, bytes([0x12, 0x34, 0x56]))]
    assert (rds.groups, rds.blocks, rds.block_errors, rds.sync_losses) == (
        7, 28, 1, 2)


def test_parse_fm_audio_starts_after_rds_block():
    rds = bytes(RDS_WIRE_SIZE)
    payload = _build_fm_payload(rds=rds, audio=[1.5, -2.5])
    frame = parse_fm(payload)
    for got, want in zip(frame.audio, [1.5, -2.5]):
        assert abs(got - want) < 1e-6
    assert frame.rds is not None
    assert frame.rds.pi_valid is False
    assert frame.rds.ps == ' ' * 8


def test_rds_text_is_restricted_to_ascii():
    rds = RdsTxConfig(enabled=True, pi=0x8203, pty=10,
                      ps='H\u00c9LLO', rt='Caf\u00e9 \u2014 snow \u2603')
    frame = cmd_tx_fm([0.5], rds=rds)
    payload = frame[FRAME_HEADER.size:]
    b = payload[TX_HDR:TX_HDR + PROTO_RDS_TX_SIZE]
    assert b[4:12] == b'H?LLO   '
    assert b[12:76] == 'Caf? ? snow ?'.ljust(64, ' ').encode('ascii')
