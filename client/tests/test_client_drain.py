"""Client survival under a high-rate broadcast.

These tests exist because a polling client never worked with its own default
2.0 s pause: it must sample twice and difference, and it slept between
the samples without reading the socket.  The daemon meanwhile broadcasts
DATA_PSD at ~750 KiB/s, which fills the client's SO_RCVBUF in ~0.17 s; TCP
then stops acking, the daemon's `send` returns EAGAIN, and `send_all`
(firmware/src/main.c) gives up after 200 retries x 500 us = 100 ms and
evicts the client.

The harness below is a real TCP server with real frames and real
backpressure, replicating that eviction budget.  A mock cannot test this —
the bug *is* the socket behaviour.
"""
import selectors
import socket
import struct
import threading
import time

import pytest

from shesha.client import DaemonClient
from shesha.protocol import MsgType, FRAME_HEADER, build_frame, cmd_status


# The daemon's send_all budget: 200 EAGAIN retries at 500 us.
EVICT_BUDGET_SEC = 200 * 0.0005

# DATA_PSD as the daemon actually sends it: 4096 float bins + 24 B header.
PSD_PAYLOAD_LEN = 24 + 4096 * 4

# A polling client's default pause. The value that never worked.
PAUSE_INTERVAL = 2.0


def _psd_frame() -> bytes:
    return FRAME_HEADER.pack(MsgType.DATA_PSD, PSD_PAYLOAD_LEN, 0) + \
        b'\x00' * PSD_PAYLOAD_LEN


def _bind_free_port() -> tuple[socket.socket, int]:
    """Bind a listening socket on a free port. Returns (socket, port)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    return srv, srv.getsockname()[1]


def _status_frame() -> bytes:
    """Minimal RESP_STATUS. Payload contents are irrelevant here — these
    tests are about whether the frame arrives at all."""
    payload = b'\x00' * 91
    return FRAME_HEADER.pack(MsgType.RESP_STATUS, len(payload), 0) + payload


class BroadcastingDaemon:
    """A real TCP server that floods DATA_PSD and evicts slow readers.

    Mirrors the firmware: non-blocking sends, and a client that cannot
    absorb the stream for EVICT_BUDGET_SEC gets dropped.

    The receive window is clamped to the Pluto's actual SO_RCVBUF (131768 B).
    Loopback defaults are several times larger, which would let a 1 s pause
    survive and turn these tests into false passes.  The socket buffer is a
    condition of the bug, so the test has to pin it.
    """

    def __init__(self, fps: int = 47, rcvbuf: int = 131768):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Set on the listening socket so it is inherited by the accepted
        # connection before the handshake sizes the window.
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, rcvbuf)
        self._srv.bind(('127.0.0.1', 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._interval = 1.0 / fps
        self._stop = threading.Event()
        self.evicted = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def _serve(self):
        self._srv.settimeout(5.0)
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setblocking(False)
        frame = _psd_frame()
        next_send = time.monotonic()
        try:
            while not self._stop.is_set():
                # Serve any pending request immediately, as the poll loop does.
                try:
                    req = conn.recv(65536)
                    if req:
                        for _ in range(len(req) // FRAME_HEADER.size):
                            self._send_all(conn, _status_frame())
                    elif req == b'':
                        break
                except (BlockingIOError, OSError):
                    pass

                now = time.monotonic()
                if now >= next_send:
                    if not self._send_all(conn, frame):
                        self.evicted.set()
                        break
                    next_send = now + self._interval
                time.sleep(0.001)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send_all(self, conn, buf: bytes) -> bool:
        """The firmware's send_all: bounded EAGAIN retries, then give up."""
        written = 0
        deadline = time.monotonic() + EVICT_BUDGET_SEC
        while written < len(buf):
            try:
                written += conn.send(buf[written:])
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.0005)
            except OSError:
                return False
        return True

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3.0)
        try:
            self._srv.close()
        except OSError:
            pass


@pytest.fixture
def daemon():
    d = BroadcastingDaemon()
    d.start()
    yield d
    d.stop()


def _client(daemon, rcvbuf: int = 131768) -> DaemonClient:
    """Connect with the Pluto's receive-window size.

    `DaemonClient.connect` does not size SO_RCVBUF, so on loopback it would
    inherit a much larger default and the backlog would never build.
    """
    c = DaemonClient(host='127.0.0.1', port=daemon.port)
    c.connect()
    c._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    return c


def test_slow_reader_is_evicted_without_draining(daemon):
    """Guard on the harness itself: the eviction must actually happen.

    If this passes vacuously the other tests prove nothing, because a large
    enough receive window makes the whole bug disappear.

    Asserted against the server's own view rather than the client's: TCP
    means the client does not observe the close until it next touches the
    socket, so `c.connected` lags reality.
    """
    c = _client(daemon)
    try:
        assert c.status() is not None
        time.sleep(1.5)               # the buggy pattern: sleep, don't drain
        assert daemon.evicted.wait(timeout=2.0), \
            "harness failed to reproduce eviction; receive window too large"
        assert c.status() is None, "evicted client should get no response"
    finally:
        c.disconnect()


def test_request_succeeds_after_pause_under_broadcast(daemon):
    """A client that pauses between two requests must still be served.

    This is the polling-client pattern: sample, wait, sample.  Without
    draining during the wait the client is evicted and the second request
    never gets a response.

    The pause is the client's real default (2.0 s) rather than something
    near the eviction threshold — at 1.0 s on loopback the outcome is
    marginal, which would make this a flaky regression guard.
    """
    c = _client(daemon)
    try:
        assert c.status() is not None, "first request should be served"

        c.wait(PAUSE_INTERVAL)

        assert c.connected, "client was evicted during the pause"
        assert c.status() is not None, "second request should be served"
    finally:
        c.disconnect()


def test_wait_returns_buffered_broadcast_frames(daemon):
    """`wait` must surface the frames it drains, not silently discard them.

    A consumer that waits should not lose the broadcast payloads that
    arrived while it was waiting.
    """
    c = _client(daemon)
    try:
        frames = c.wait(0.5)
        assert any(mt == MsgType.DATA_PSD for mt, _ in frames), \
            "expected DATA_PSD frames collected during the wait"
    finally:
        c.disconnect()


def test_wait_honours_its_duration(daemon):
    """`wait` is a sleep replacement, so it must actually take that long."""
    c = _client(daemon)
    try:
        t0 = time.monotonic()
        c.wait(0.5)
        elapsed = time.monotonic() - t0
        assert 0.5 <= elapsed < 0.9, f"wait(0.5) took {elapsed:.3f}s"
    finally:
        c.disconnect()


def test_request_response_does_not_spin_when_disconnected():
    """A closed socket must not turn the poll loop into a busy-spin.

    After eviction `recv_raw` returns instantly, so `_request_response`
    burned a whole core until its timeout (measured 16.5M iterations in
    3 s on hardware).  It should give up promptly instead.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    c = DaemonClient(host='127.0.0.1', port=port)
    c.connect()
    conn, _ = srv.accept()
    conn.close()          # peer vanishes, as eviction does

    try:
        t0 = time.monotonic()
        result = c.status()
        elapsed = time.monotonic() - t0
        assert result is None
        assert elapsed < 0.5, \
            f"spun for {elapsed:.3f}s on a dead socket instead of bailing"
    finally:
        c.disconnect()
        srv.close()


def _socketpair_client() -> tuple[DaemonClient, socket.socket]:
    """A connected client whose peer end the test controls."""
    c = DaemonClient()
    a, b = socket.socketpair()
    a.settimeout(0)
    c._sock = a
    c._sel.register(a, selectors.EVENT_READ)
    return c, b


def test_stream_stops_when_link_drops(monkeypatch):
    """A dropped link must end the stream instead of busy-spinning.

    `recv_raw` returns instantly once the socket is gone, so the duration
    loop burned a core until its deadline (measured 882k-1.4M calls in
    0.2 s).  Nothing more can arrive, so it should return promptly.
    """
    c, peer = _socketpair_client()
    monkeypatch.setattr(c, "subscribe", lambda mask=0: True)
    peer.close()

    try:
        started = time.monotonic()
        result = c.stream(duration=2.0)
        elapsed = time.monotonic() - started

        assert result.psd_frames == []
        assert elapsed < 0.5, \
            f"spun for {elapsed:.3f}s on a dead socket instead of returning"
    finally:
        c.disconnect()


def test_iq_rx_stops_when_link_drops(monkeypatch):
    """An unbounded IQ capture must end at EOF, not spin until Ctrl-C."""
    c, peer = _socketpair_client()
    monkeypatch.setattr(c, "subscribe", lambda mask=0: True)
    peer.close()

    try:
        started = time.monotonic()
        frames = list(c.iq_rx(duration=2.0))
        elapsed = time.monotonic() - started

        assert frames == []
        assert elapsed < 0.5, \
            f"spun for {elapsed:.3f}s on a dead socket instead of returning"
    finally:
        c.disconnect()


# ---------------------------------------------------------------------------
# Gap detection
#
# These use DATA_WIFI because its seq IS a per-client delivery counter.  PSD
# is deliberately exempt -- see the PSD section below.
# ---------------------------------------------------------------------------


def test_gap_detection_no_gaps():
    """Sequential seq numbers produce zero gaps."""
    srv, port = _bind_free_port()
    srv.listen(1)
    c = DaemonClient('127.0.0.1', port)
    c.connect()
    conn, _ = srv.accept()

    # Send 3 frames with sequential seq: 1, 2, 3
    for seq in (1, 2, 3):
        payload = b'\x00' * 16
        hdr = FRAME_HEADER.pack(MsgType.DATA_WIFI, len(payload), seq)
        conn.sendall(hdr + payload)

    time.sleep(0.05)
    c.recv_raw(timeout=0.1)

    assert c.gaps == 0
    assert c.dropped == 0

    conn.close()
    c.disconnect()
    srv.close()


def test_gap_detection_with_drops():
    """Jump in seq numbers is detected as a gap."""
    srv, port = _bind_free_port()
    srv.listen(1)
    c = DaemonClient('127.0.0.1', port)
    c.connect()
    conn, _ = srv.accept()

    # Send seq 1, 2, then skip to 5 (dropped 3, 4), then 6
    payload = b'\x00' * 16
    for seq in (1, 2, 5, 6):
        hdr = FRAME_HEADER.pack(MsgType.DATA_WIFI, len(payload), seq)
        conn.sendall(hdr + payload)

    time.sleep(0.05)
    c.recv_raw(timeout=0.1)

    assert c.gaps == 1
    assert c.dropped == 2  # frames 3 and 4 were lost

    conn.close()
    c.disconnect()
    srv.close()


def test_gap_detection_resets_on_reconnect():
    """Gap counters reset when connecting fresh."""
    srv, port = _bind_free_port()
    srv.listen(1)
    c = DaemonClient('127.0.0.1', port)
    c.connect()
    conn, _ = srv.accept()

    # Create a gap
    payload = b'\x00' * 16
    for seq in (1, 10):
        hdr = FRAME_HEADER.pack(MsgType.DATA_WIFI, len(payload), seq)
        conn.sendall(hdr + payload)

    time.sleep(0.05)
    c.recv_raw(timeout=0.1)
    assert c.gaps == 1

    # Reconnect
    conn.close()
    c.disconnect()

    srv.listen(1)
    c.connect()
    conn2, _ = srv.accept()

    assert c.gaps == 0
    assert c.dropped == 0

    conn2.close()
    c.disconnect()
    srv.close()


# ---------------------------------------------------------------------------
# PSD sequence numbers are a producer frame index, not a delivery counter
# ---------------------------------------------------------------------------


def test_psd_seq_jumps_are_not_counted_as_loss():
    """A free-running PSD producer advances seq by many frames per broadcast.

    The producer computes ~27 frames per broadcast at 20 MSPS and accumulation
    reduces them to one, so the frame index on the wire jumps by ~27 every
    healthy frame.  Counting that as loss reported thousands of dropped frames
    in normal operation.
    """
    srv, port = _bind_free_port()
    srv.listen(1)
    c = DaemonClient('127.0.0.1', port)
    c.connect()
    conn, _ = srv.accept()

    payload = b'\x00' * 16
    for seq in (27, 54, 81, 108):
        hdr = FRAME_HEADER.pack(MsgType.DATA_PSD, len(payload), seq)
        conn.sendall(hdr + payload)

    time.sleep(0.05)
    c.recv_raw(timeout=0.1)

    assert c.gaps == 0
    assert c.dropped == 0

    conn.close()
    c.disconnect()
    srv.close()


def test_psd_frames_do_not_disturb_other_gap_counting():
    """PSD must not perturb the delivery counter other data types share.

    PSD and WiFi are never both subscribed in practice, but the seq cursor is
    one field: if PSD wrote to it, an interleaved stream would manufacture
    gaps on whichever type was unlucky.
    """
    srv, port = _bind_free_port()
    srv.listen(1)
    c = DaemonClient('127.0.0.1', port)
    c.connect()
    conn, _ = srv.accept()

    payload = b'\x00' * 16
    # WiFi is contiguous; PSD jumps wildly in between.
    for msg_type, seq in ((MsgType.DATA_WIFI, 1),
                          (MsgType.DATA_PSD, 5000),
                          (MsgType.DATA_WIFI, 2),
                          (MsgType.DATA_PSD, 9999),
                          (MsgType.DATA_WIFI, 3)):
        hdr = FRAME_HEADER.pack(msg_type, len(payload), seq)
        conn.sendall(hdr + payload)

    time.sleep(0.05)
    c.recv_raw(timeout=0.1)

    assert c.gaps == 0
    assert c.dropped == 0

    conn.close()
    c.disconnect()
    srv.close()


def test_psd_frame_index_is_exposed():
    """The frame index is a stream position, and the drain's merge key.

    It counts frame-sized blocks of the sample stream, including ones the
    producer abandoned, so it is NOT a transformed-frame count and cannot be
    used to derive coverage.  What it is good for is ordering -- including
    across two producers, whose own transform counts would not be
    comparable.
    """
    srv, port = _bind_free_port()
    srv.listen(1)
    c = DaemonClient('127.0.0.1', port)
    c.connect()
    conn, _ = srv.accept()

    assert c.psd_frame_index is None

    payload = b'\x00' * 16
    for seq in (27, 54):
        hdr = FRAME_HEADER.pack(MsgType.DATA_PSD, len(payload), seq)
        conn.sendall(hdr + payload)

    time.sleep(0.05)
    c.recv_raw(timeout=0.1)

    assert c.psd_frame_index == 54

    conn.close()
    c.disconnect()
    srv.close()


def test_psd_frame_index_resets_on_reconnect():
    """A stale index would look like an enormous forward jump on the first
    frame after reconnecting, since the producer's position restarts."""
    srv, port = _bind_free_port()
    srv.listen(1)
    c = DaemonClient('127.0.0.1', port)
    c.connect()
    conn, _ = srv.accept()

    payload = b'\x00' * 16
    hdr = FRAME_HEADER.pack(MsgType.DATA_PSD, len(payload), 4242)
    conn.sendall(hdr + payload)
    time.sleep(0.05)
    c.recv_raw(timeout=0.1)
    assert c.psd_frame_index == 4242

    conn.close()
    c.disconnect()

    srv.listen(1)
    c.connect()
    conn2, _ = srv.accept()

    assert c.psd_frame_index is None

    conn2.close()
    c.disconnect()
    srv.close()
