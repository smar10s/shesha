"""stream() summaries by default, retains or sinks frames on request."""
import socket
import selectors
import struct
import threading
import time

import pytest

from shesha.client import DaemonClient
from shesha.protocol import MsgType, RDS_WIRE_SIZE, build_frame


def _paired_client() -> tuple[DaemonClient, socket.socket]:
    c = DaemonClient()
    a, b = socket.socketpair()
    a.settimeout(0)
    c._sock = a
    c._sel.register(a, selectors.EVENT_READ)
    return c, b


def _send_later(peer: socket.socket, frames: list[bytes],
                delay: float = 0.15) -> None:
    def go():
        time.sleep(delay)
        for frame in frames:
            peer.send(frame)
    threading.Thread(target=go, daemon=True).start()


def _psd_frame(ts: float, center: float, bins: list[float]) -> bytes:
    payload = struct.pack('<dfffI', ts, center, -100.0, -10.0, len(bins))
    payload += struct.pack(f'<{len(bins)}f', *bins)
    return build_frame(MsgType.DATA_PSD, payload)


def _stream_with_psd(**kwargs):
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_OK, bytes([MsgType.CMD_SUBSCRIBE])))
    _send_later(peer, [
        _psd_frame(1.0, 2437.0, [-80.0, -70.0, -60.0, -50.0]),
        _psd_frame(1.05, 2437.0, [-81.0, -71.0, -61.0, -51.0]),
    ])
    return c.stream(duration=0.5, **kwargs)


def test_stream_aggregates_psd_without_retaining_frames():
    result = _stream_with_psd()

    assert result.psd_count == 2
    assert result.psd_frames == []
    assert result.psd.bin_count == 4
    assert result.psd.mean_db == -65.5
    assert result.psd.peak_db == -50.0
    assert result.psd.peak_bin == 3
    assert result.psd.last_center_mhz == 2437.0


def test_stream_retains_psd_frames_when_verbose():
    result = _stream_with_psd(verbose=True)

    assert result.psd_count == 2
    assert len(result.psd_frames) == 2
    assert result.psd_frames[1].bins == [-81.0, -71.0, -61.0, -51.0]


def test_stream_psd_sink_sees_each_frame():
    seen = []
    result = _stream_with_psd(psd_sink=seen.append)

    assert len(seen) == 2
    assert seen[0].bins == [-80.0, -70.0, -60.0, -50.0]
    assert result.psd_frames == []


def test_stream_psd_sink_errors_propagate():
    def sink(_frame):
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        _stream_with_psd(psd_sink=sink)


def test_stream_summary_counts_psd_without_frames():
    result = _stream_with_psd()

    data = result.to_dict()

    assert data["psd_count"] == 2
    assert data["psd_fps"] == 4.0


def _wifi_frame(ts: float = 1.0, *, fcs_ok: bool = True) -> bytes:
    payload = struct.pack('<dBBBHBfHQ', ts, 0x0B, 6, 1 if fcs_ok else 0,
                          100, 3, 50.0, 0, 5_180_000_000)
    payload += bytes(20)
    return build_frame(MsgType.DATA_WIFI, payload)


def _fm_frame(ts: float = 1.0, audio: tuple[float, ...] = (0.1, -0.2)) -> bytes:
    payload = struct.pack('<dII', ts, 43750, len(audio))
    payload += bytes(RDS_WIRE_SIZE)
    payload += struct.pack(f'<{len(audio)}f', *audio)
    return build_frame(MsgType.DATA_FM, payload)


def _iq_frame(ts: float = 1.0, samples: bytes = b'\x01\x02\x03\x04') -> bytes:
    payload = struct.pack('<dfIBBH', ts, 915.0, 2_000_000, 8, 0,
                          len(samples) // 2)
    payload += samples
    return build_frame(MsgType.DATA_IQ, payload)


def _stream_with_frames(frames: list[bytes], **kwargs):
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_OK, bytes([MsgType.CMD_SUBSCRIBE])))
    _send_later(peer, frames)
    return c.stream(duration=0.5, **kwargs)


def test_stream_counts_wifi_without_retaining_frames():
    result = _stream_with_frames([_wifi_frame(fcs_ok=True),
                                  _wifi_frame(fcs_ok=False)])

    assert result.wifi_count == 2
    assert result.wifi_fcs_ok == 1
    assert result.wifi_frames == []


def test_stream_counts_fm_and_iq_without_retaining_frames():
    result = _stream_with_frames([_fm_frame(), _iq_frame()])

    assert result.fm_count == 1
    assert result.iq_count == 1
    assert result.fm_frames == []
    assert result.iq_frames == []


def test_stream_retains_data_frames_when_verbose():
    result = _stream_with_frames(
        [_wifi_frame(), _fm_frame(), _iq_frame()], verbose=True)

    assert [f.length for f in result.wifi_frames] == [100]
    assert [f.audio_rate_hz for f in result.fm_frames] == [43750]
    assert [f.n_samples for f in result.iq_frames] == [2]
    assert result.wifi_count == 1
    assert result.fm_count == 1
    assert result.iq_count == 1


def test_stream_data_sinks_see_each_frame():
    wifi, fm, iq = [], [], []
    result = _stream_with_frames(
        [_wifi_frame(), _fm_frame(), _iq_frame()],
        wifi_sink=wifi.append, fm_sink=fm.append, iq_sink=iq.append)

    assert len(wifi) == 1
    assert len(fm) == 1
    assert len(iq) == 1
    assert result.wifi_frames == []
    assert result.fm_frames == []
    assert result.iq_frames == []


@pytest.mark.parametrize("sink_name", ["wifi_sink", "fm_sink", "iq_sink"])
def test_stream_data_sink_errors_propagate(sink_name):
    def sink(_frame):
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        _stream_with_frames(
            [_wifi_frame(), _fm_frame(), _iq_frame()], **{sink_name: sink})


def test_stream_records_a_bad_frame_and_keeps_receiving():
    bad = build_frame(
        MsgType.DATA_WIFI,
        struct.pack('<dBBBHBfHQ', 1.0, 0x0B, 6, 1, 100, 3, 50.0,
                    10, 5_180_000_000) + b'\x00' * 4)
    good = _wifi_frame()

    result = _stream_with_frames([bad, good])

    assert result.wifi_count == 1
    assert any("bad WiFi frame" in error for error in result.errors)


def test_stream_summary_uses_data_counters():
    result = _stream_with_frames([_wifi_frame(fcs_ok=True),
                                  _wifi_frame(fcs_ok=False),
                                  _fm_frame(), _iq_frame()])

    data = result.to_dict()

    assert data["wifi_count"] == 2
    assert data["fm_count"] == 1
    assert data["iq_count"] == 1
