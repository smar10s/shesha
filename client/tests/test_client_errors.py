"""Errors the client must report instead of crashing or going silent."""
import socket
import selectors

import pytest

from shesha.client import DaemonClient, SubscriptionError
from shesha.protocol import MsgType, build_frame


def _paired_client() -> tuple[DaemonClient, socket.socket]:
    c = DaemonClient()
    a, b = socket.socketpair()
    a.settimeout(0)
    c._sock = a
    c._sel.register(a, selectors.EVENT_READ)
    return c, b


def test_status_returns_none_on_resp_error():
    c, peer = _paired_client()
    peer.send(build_frame(
        MsgType.RESP_ERROR,
        bytes([MsgType.CMD_STATUS]) + b'daemon busy\x00'))

    assert c.status() is None
    assert c.last_error == 'daemon busy'


def test_caps_returns_none_on_resp_error():
    c, peer = _paired_client()
    peer.send(build_frame(
        MsgType.RESP_ERROR,
        bytes([MsgType.CMD_CAPS]) + b'not supported\x00'))

    assert c.caps() is None
    assert c.last_error == 'not supported'


def test_status_returns_none_on_a_malformed_frame():
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_STATUS, b'\x00' * 10))

    assert c.status() is None
    assert "status" in (c.last_error or "")


def test_caps_returns_none_on_a_malformed_frame():
    c, peer = _paired_client()
    peer.send(build_frame(MsgType.RESP_CAPS, b'\x00' * 10))

    assert c.caps() is None
    assert "caps" in (c.last_error or "")


def test_disconnect_closes_selector_and_reconnect_recreates_it():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(('127.0.0.1', 0))
    srv.listen(5)
    c = DaemonClient('127.0.0.1', srv.getsockname()[1])
    try:
        c.connect()
        first = c._sel
        c.disconnect()
        assert c._sel is None, "disconnect leaked the selector"

        c.connect()
        assert c.connected
        assert c._sel is not None
        assert c._sel is not first
        c.disconnect()
    finally:
        srv.close()


def test_stream_reports_a_rejected_subscription():
    c, peer = _paired_client()
    peer.send(build_frame(
        MsgType.RESP_ERROR,
        bytes([MsgType.CMD_SUBSCRIBE]) + b'IQ tx is active\x00'))

    result = c.stream(duration=0.1)

    assert result.errors
    assert "IQ tx is active" in result.errors[0]
    assert result.psd_frames == []


def test_iq_rx_raises_on_a_rejected_subscription():
    c, peer = _paired_client()
    peer.send(build_frame(
        MsgType.RESP_ERROR,
        bytes([MsgType.CMD_SUBSCRIBE]) + b'IQ tx is active\x00'))

    with pytest.raises(SubscriptionError, match="IQ tx is active"):
        list(c.iq_rx(duration=0.1))
