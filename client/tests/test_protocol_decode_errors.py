"""Malformed data frames are rejected before they can be unpacked."""
import struct

import pytest

from shesha.protocol import (
    FM_HEADER_SIZE,
    MAX_FFT_BINS,
    MAX_FM_SAMPLES,
    RDS_WIRE_SIZE,
    FrameDecodeError,
    parse_fm,
    parse_iq,
    parse_psd,
    parse_wifi,
)


def _psd_payload(n_bins: int = 4, *, declared: int | None = None) -> bytes:
    count = n_bins if declared is None else declared
    header = struct.pack('<dfffI', 1.0, 2437.0, -100.0, -10.0, count)
    return header + struct.pack(f'<{n_bins}f', *([-50.0] * n_bins))


def _wifi_header(psdu_len: int, *, fclass: int = 3) -> bytes:
    return struct.pack('<dBBBHBfHQ', 1.0, 0x0B, 6, 1, 100, fclass, 50.0,
                       psdu_len, 5_180_000_000)


def _wifi_meta(*, flags: int = 0, ssid: bytes = b'',
               ssid_len: int | None = None) -> bytes:
    length = len(ssid) if ssid_len is None else ssid_len
    return (bytes([flags]) + b'\x00' * 18 + bytes([length]) + ssid)


def _fm_payload(n_samples: int = 2, *, audio_samples: int | None = None
                ) -> bytes:
    count = n_samples if audio_samples is None else audio_samples
    audio = struct.pack(f'<{count}f', *([0.0] * count))
    return (struct.pack('<dII', 1.5, 43750, n_samples)
            + bytes(RDS_WIRE_SIZE) + audio)


def _iq_header(n: int = 4, *, bits: int = 8) -> bytes:
    return struct.pack('<dfIBBH', 1.5, 915.0, 2_000_000, bits, 0, n)


def test_parse_psd_rejects_short_header():
    with pytest.raises(FrameDecodeError, match="psd"):
        parse_psd(_psd_payload()[:10])


def test_parse_psd_rejects_oversized_bin_count():
    with pytest.raises(FrameDecodeError, match="bin"):
        parse_psd(_psd_payload(0, declared=MAX_FFT_BINS + 1))


def test_parse_psd_rejects_truncated_bins():
    with pytest.raises(FrameDecodeError, match="bin"):
        parse_psd(_psd_payload(4)[:-8])


def test_parse_psd_accepts_a_valid_frame():
    frame = parse_psd(_psd_payload(4))
    assert frame.bins == [-50.0] * 4


def test_parse_wifi_rejects_short_header():
    with pytest.raises(FrameDecodeError, match="wifi"):
        parse_wifi(_wifi_header(0)[:20])


def test_parse_wifi_rejects_psdu_past_payload():
    payload = _wifi_header(10) + b'\x00' * 4

    with pytest.raises(FrameDecodeError, match="psdu"):
        parse_wifi(payload)


def test_parse_wifi_rejects_truncated_meta_block():
    payload = _wifi_header(0) + _wifi_meta()[:10]

    with pytest.raises(FrameDecodeError, match="meta"):
        parse_wifi(payload)


def test_parse_wifi_rejects_ssid_past_payload():
    payload = _wifi_header(0) + _wifi_meta(flags=0x08, ssid=b'ab',
                                           ssid_len=10)

    with pytest.raises(FrameDecodeError, match="ssid"):
        parse_wifi(payload)


def test_parse_wifi_rejects_truncated_ie_block():
    payload = _wifi_header(0) + _wifi_meta(flags=0x20) + b'\x00' * 10

    with pytest.raises(FrameDecodeError, match="ie"):
        parse_wifi(payload)


def test_parse_wifi_accepts_a_valid_frame():
    payload = _wifi_header(4) + b'abcd' + _wifi_meta(flags=0x08,
                                                     ssid=b'home')

    frame = parse_wifi(payload)

    assert frame.psdu == b'abcd'
    assert frame.meta is not None
    assert frame.meta.ssid == 'home'


def test_parse_fm_rejects_short_header():
    with pytest.raises(FrameDecodeError, match="fm"):
        parse_fm(_fm_payload()[:10])


def test_parse_fm_rejects_missing_rds_block():
    payload = struct.pack('<dII', 1.5, 43750, 0) + bytes(RDS_WIRE_SIZE - 1)

    with pytest.raises(FrameDecodeError, match="rds"):
        parse_fm(payload)


def test_parse_fm_rejects_oversized_sample_count():
    payload = (struct.pack('<dII', 1.5, 43750, MAX_FM_SAMPLES + 1)
               + bytes(RDS_WIRE_SIZE))

    with pytest.raises(FrameDecodeError, match="samples"):
        parse_fm(payload)


def test_parse_fm_rejects_truncated_audio():
    payload = _fm_payload(4, audio_samples=2)

    with pytest.raises(FrameDecodeError, match="audio"):
        parse_fm(payload)


def test_parse_fm_accepts_a_valid_frame():
    frame = parse_fm(_fm_payload(2))
    assert frame.audio == [0.0, 0.0]


def test_parse_iq_rejects_short_header():
    with pytest.raises(FrameDecodeError, match="iq"):
        parse_iq(b'\x00' * 10)


def test_parse_iq_rejects_truncated_samples_as_decode_error():
    payload = _iq_header(4) + b'\x00\x01\x02'

    with pytest.raises(FrameDecodeError, match="iq"):
        parse_iq(payload)


def test_parse_iq_accepts_a_valid_frame():
    frame = parse_iq(_iq_header(2) + b'\x00\x01\x02\x03')
    assert frame.n_samples == 2
