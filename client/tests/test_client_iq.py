"""Tests for the DaemonClient IQ streaming API, over a socketpair.

A socketpair gives a real file descriptor (recv_raw needs a selectable
socket) without a server thread; responses are pre-written into the peer
end so the client's request-response loop finds them immediately.
"""
import socket
import selectors
import struct
import threading
import time

import pytest

from shesha.client import DaemonClient, PushResult
from shesha.protocol import (
    MsgType,
    FRAME_HEADER,
    FrameDecodeError,
    build_frame,
    SUBSCRIBE_IQ,
)


def _paired_client() -> tuple[DaemonClient, socket.socket]:
    c = DaemonClient()
    a, b = socket.socketpair()
    a.settimeout(0)
    c._sock = a
    c._sel.register(a, selectors.EVENT_READ)
    return c, b


def _send_later(peer: socket.socket, frames: list[bytes],
                delay: float = 0.15) -> None:
    """Send frames after `delay`, so the client's request-response loop
    (which consumes everything buffered in one read and drops non-matching
    frames) cannot swallow them before the iteration starts."""
    def go():
        time.sleep(delay)
        for f in frames:
            peer.send(f)
    threading.Thread(target=go, daemon=True).start()


def _iq_frame(bits: int = 12, n: int = 4, ts: float = 1.0) -> bytes:
    head = struct.pack('<dfIBBH', ts, 915.0, 2_000_000, bits, 0, n)
    bps = 3 if bits == 12 else 2
    payload = head + bytes(range(n * bps))
    return build_frame(MsgType.DATA_IQ, payload)


def test_mode_names_include_iq():
    from shesha.client import MODE_NAMES, MODE_BY_NAME
    from shesha.protocol import DaemonMode
    assert MODE_NAMES[DaemonMode.IQ] == 'iq'
    assert MODE_BY_NAME['iq'] == DaemonMode.IQ


def test_iq_tx_stream_data_ok():
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_OK,
                          bytes([MsgType.CMD_IQ_TX_STREAM_DATA])))
    assert c.iq_tx_stream_data(b'\x00' * 12) == PushResult.OK
    # The peer received the push command
    req = peer.recv(4096)
    mtype, plen, _seq = FRAME_HEADER.unpack_from(req, 0)
    assert mtype == MsgType.CMD_IQ_TX_STREAM_DATA
    assert plen == 4 + 12


def test_iq_tx_stream_data_retry():
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_RETRY,
                          bytes([MsgType.CMD_IQ_TX_STREAM_DATA])))
    assert c.iq_tx_stream_data(b'\x00' * 12) == PushResult.RETRY


def test_iq_tx_stream_data_error():
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_ERROR,
                          bytes([MsgType.CMD_IQ_TX_STREAM_DATA]) + b'nope'))
    assert c.iq_tx_stream_data(b'\x00' * 12) == PushResult.ERROR
    assert c.last_error == 'nope'


def test_iq_tx_stream_data_error_without_message():
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_ERROR,
                          bytes([MsgType.CMD_IQ_TX_STREAM_DATA])))
    assert c.iq_tx_stream_data(b'\x00' * 12) == PushResult.ERROR
    assert c.last_error == ''


def test_iq_rx_yields_parsed_frames():
    c, peer = _paired_client()
    # Pre-answer the subscribe that iq_rx sends on entry
    peer.send(build_frame(MsgType.RESP_OK, bytes([MsgType.CMD_SUBSCRIBE])))
    _send_later(peer, [_iq_frame(bits=12, n=4)])

    got = list(c.iq_rx(duration=0.4))
    assert len(got) == 1
    assert got[0].n_samples == 4
    assert got[0].bits == 12
    assert got[0].sample_rate_hz == 2_000_000

    # The client subscribed to the IQ stream
    req = peer.recv(4096)
    mtype, _plen, _seq = FRAME_HEADER.unpack_from(req, 0)
    assert mtype == MsgType.CMD_SUBSCRIBE
    assert req[FRAME_HEADER.size:] == bytes([SUBSCRIBE_IQ])


def test_iq_rx_raises_a_clear_error_for_a_bad_frame():
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_OK, bytes([MsgType.CMD_SUBSCRIBE])))
    _send_later(peer, [build_frame(MsgType.DATA_IQ, b'\x00' * 10)])

    with pytest.raises(FrameDecodeError, match="iq"):
        list(c.iq_rx(duration=0.4))


def test_iq_rx_ignores_non_iq_frames():
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_OK, bytes([MsgType.CMD_SUBSCRIBE])))
    _send_later(peer, [
        build_frame(MsgType.RESP_STATUS, b'\x00' * 91),
        _iq_frame(bits=8, n=2, ts=2.0),
    ])

    got = list(c.iq_rx(duration=0.4))
    assert len(got) == 1
    assert got[0].bits == 8
    assert got[0].n_samples == 2
