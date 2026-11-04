"""DaemonClient must tolerate the TUI's two-thread access pattern.

The event loop owns reads; key handlers may send.  The client itself still
guards against concurrent readers and writers so a caller mistake cannot
corrupt framing or interleave a frame on the wire.
"""
import threading
import time

import pytest

from shesha.client import DaemonClient
from shesha.protocol import FRAME_HEADER, MsgType


def test_recv_raw_serializes_concurrent_readers():
    class _Selector:
        def __init__(self) -> None:
            self.cond = threading.Condition()
            self.active = 0
            self.max_active = 0

        def select(self, timeout):
            with self.cond:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.active < 2:
                    self.cond.wait(timeout=0.2)
                self.active -= 1
                self.cond.notify_all()
            return []

    client = DaemonClient()
    client._sock = object()
    sel = client._sel = _Selector()

    threads = [threading.Thread(target=lambda: client.recv_raw(timeout=1))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)

    assert sel.max_active == 1


def test_send_raw_serializes_concurrent_writers():
    class _SlowSock:
        def __init__(self) -> None:
            self.out = bytearray()
            self._timeout = 0

        def gettimeout(self):
            return self._timeout

        def settimeout(self, timeout) -> None:
            self._timeout = timeout

        def sendall(self, data: bytes) -> None:
            for i in range(0, len(data), 32):
                self.out.extend(data[i:i + 32])
                time.sleep(0.0002)

    client = DaemonClient()
    sock = client._sock = _SlowSock()
    first = b'A' * 4096
    second = b'B' * 4096
    barrier = threading.Barrier(2)

    def send(payload: bytes) -> None:
        barrier.wait()
        client.send_raw(payload)

    threads = [threading.Thread(target=send, args=(p,))
               for p in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)

    assert bytes(sock.out) in (first + second, second + first)


def test_send_raw_uses_a_bounded_timeout(monkeypatch):
    """A wedged link must not block a sender forever."""
    import socket as socketlib

    monkeypatch.setattr('shesha.client._SEND_TIMEOUT', 0.1)
    a, b = socketlib.socketpair()
    a.settimeout(0)
    client = DaemonClient()
    client._sock = a
    done = threading.Event()
    errors = []

    def send() -> None:
        try:
            client.send_raw(b'x' * (4 << 20))
        except OSError as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        assert done.wait(1.0), 'send_raw blocked forever'
        assert errors
    finally:
        a.close()
        b.close()


def test_oversized_frame_disconnects_instead_of_growing_buffer():
    client = DaemonClient()
    client._buf = FRAME_HEADER.pack(MsgType.DATA_IQ, 0xFFFFFFFF, 0)

    assert client._parse_frames() == []
    assert client._buf == b''


def test_connect_closes_socket_when_connect_fails(monkeypatch):
    class _Sock:
        def __init__(self) -> None:
            self.closed = False

        def settimeout(self, timeout) -> None:
            pass

        def connect(self, address) -> None:
            raise OSError('unreachable')

        def close(self) -> None:
            self.closed = True

    sock = _Sock()
    monkeypatch.setattr('shesha.client.socket.socket',
                        lambda *args, **kwargs: sock)

    client = DaemonClient()
    with pytest.raises(OSError):
        client.connect()

    assert sock.closed
    assert client._sel is None
