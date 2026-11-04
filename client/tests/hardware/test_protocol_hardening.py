"""Wire-level hardening tests against a live daemon.

Two failure modes are covered:

  * an oversized inbound length must close that connection, not wedge the
    daemon or wrap into a plausible allocation;
  * a subscriber that stops reading may lose data frames (its ring is
    bounded) but must never lose control frames: its own command response
    and an unsolicited status have to survive the backlog.

Also covers the observer path: a client that sent nothing still learns
about state another client changed.
"""
import socket
import struct
import time

import pytest

from shesha.client import DaemonClient
from shesha.protocol import (
    MsgType,
    SUBSCRIBE_PSD,
    DaemonMode,
    cmd_set_fps,
    parse_status,
)
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

SLOW_STALL_SEC = 6.0
DRAIN_TIMEOUT_SEC = 10.0
PSD_RATE_WINDOW_SEC = 0.2
MIN_BACKLOG_PSD_FRAMES = 64
BACKLOG_MARGIN_BYTES = 6 * 1024 * 1024


def _connect(timeout: float = 5.0) -> DaemonClient:
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT, timeout=timeout)
    c.connect()
    return c


@pytest.fixture(autouse=True)
def _restore_daemon():
    """Start and end every test in a known PSD-producing setup."""
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    try:
        assert c.set_mode(DaemonMode.WATERFALL), "could not select waterfall"
        assert c.set_fps(50), "could not set FPS 50"
        assert c.set_fft_size(4096), "could not set FFT 4096"
        yield
    finally:
        try:
            c.set_mode(DaemonMode.WATERFALL)
            c.set_fps(50)
            c.set_fft_size(4096)
        except Exception:
            pass
        finally:
            c.disconnect()


def test_malformed_length_does_not_hang():
    sock = socket.create_connection((PLUTO_HOST, PLUTO_PORT), timeout=2.0)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        sock.sendall(struct.pack("<BII", MsgType.CMD_STATUS, 0xFFFFFFF7, 0))
        sock.settimeout(0.2)
        start = time.monotonic()
        deadline = start + 2.0
        closed = False
        while time.monotonic() < deadline:
            try:
                if sock.recv(4096) == b"":
                    closed = True
                    break
            except socket.timeout:
                continue
            except ConnectionResetError:
                closed = True
                break
        elapsed = time.monotonic() - start
        assert closed, "daemon left the oversized-frame connection open"
        assert elapsed < 2.0, f"daemon took {elapsed:.2f}s to close"
    finally:
        sock.close()

    c = _connect()
    try:
        result = c.status()
        assert result is not None, "daemon stopped answering after bad frame"
    finally:
        c.disconnect()


def test_slow_subscriber_keeps_control_frames():
    slow = _connect()
    try:
        fast = _connect()
        try:
            assert slow.subscribe(SUBSCRIBE_PSD)
            assert slow.set_fps(100)
            assert slow.set_fft_size(8192)

            # enter() returns after pthread_create, but the live-worker count
            # rises only when a worker starts.  Poll this cycle into life
            # rather than sampling that gap.
            deadline = time.monotonic() + DRAIN_TIMEOUT_SEC
            st = None
            while time.monotonic() < deadline:
                status = slow.status()
                if status is not None:
                    candidate = status[0]
                    if (candidate.rx_active
                            and candidate.fft_size == 8192
                            and candidate.target_fps == 100):
                        st = candidate
                        break
                time.sleep(0.05)
            assert st is not None, \
                "waterfall producer did not start at 8192/100"
            assert st.daemon_mode == DaemonMode.WATERFALL, (
                f"slow test needs waterfall mode, daemon is in {st.daemon_mode}")

            # Measure the real PSD byte rate.  Six seconds is only a timing
            # assumption unless the observed rate proves the bounded data
            # queue plus the socket buffers must overflow.
            rate_start = time.monotonic()
            rate_deadline = rate_start + PSD_RATE_WINDOW_SEC
            measured_bytes = 0
            while time.monotonic() < rate_deadline:
                remaining = rate_deadline - time.monotonic()
                if remaining <= 0:
                    break
                for msg_type, payload in slow.recv_raw(
                        timeout=min(remaining, 0.05)):
                    if msg_type == MsgType.DATA_PSD:
                        measured_bytes += len(payload)
            elapsed = time.monotonic() - rate_start
            observed_rate = measured_bytes / elapsed
            fill_bytes = observed_rate * SLOW_STALL_SEC
            assert fill_bytes > BACKLOG_MARGIN_BYTES, (
                f"PSD rate {observed_rate / 1024:.0f} KiB/s over "
                f"{SLOW_STALL_SEC:.0f}s only fills "
                f"{fill_bytes / (1024 * 1024):.1f} MiB; the 4 MiB data "
                "queue plus socket buffers could not be proven full")

            # Stop reading until the daemon's 4 MiB data ring and both kernel
            # socket buffers are full.  Data frames are expected to be dropped.
            time.sleep(SLOW_STALL_SEC)

            # The slow client asks for 40 without reading the reply.  Waiting
            # for the daemon to apply it keeps the later FPS change last: the
            # poll loop visits clients in reverse array order, not connect
            # order.
            slow.send_raw(cmd_set_fps(40))
            deadline = time.monotonic() + 2.0
            applied = False
            while time.monotonic() < deadline and not applied:
                result = fast.status()
                applied = result is not None and result[0].target_fps == 40
            assert applied, "daemon did not apply the slow client's FPS change"

            # A second client now changes FPS to 60, which pushes an
            # unsolicited status.  The slow client's data backlog must not
            # delay it.
            assert fast.set_fps(60), "fast client did not get RESP_OK"
            result = fast.status()
            assert result is not None and result[0].target_fps == 60

            saw_ok = False
            saw_status_60 = False
            psd_frames = 0
            deadline = time.monotonic() + DRAIN_TIMEOUT_SEC
            while time.monotonic() < deadline:
                for msg_type, payload in slow.recv_raw(timeout=0.5):
                    if msg_type == MsgType.DATA_PSD:
                        psd_frames += 1
                    elif (msg_type == MsgType.RESP_OK and payload
                            and payload[0] == MsgType.CMD_SET_FPS):
                        saw_ok = True
                    elif msg_type == MsgType.RESP_STATUS:
                        if parse_status(payload).target_fps == 60:
                            saw_status_60 = True
                if (saw_ok and saw_status_60
                        and psd_frames >= MIN_BACKLOG_PSD_FRAMES):
                    break

            assert psd_frames >= MIN_BACKLOG_PSD_FRAMES, (
                f"slow subscriber drained only {psd_frames} PSD frames; "
                "the bounded queue was not full during the stall")
            assert saw_ok, "slow subscriber lost its CMD_SET_FPS response"
            assert saw_status_60, "slow subscriber lost the unsolicited status"
        finally:
            try:
                fast.set_fps(50)
            finally:
                fast.disconnect()
    finally:
        slow.disconnect()


def test_mutation_pushes_status_to_observer():
    a = _connect()
    try:
        b = _connect()
        try:
            # The daemon accepts at most one connection per poll iteration.
            # A status response proves B is accepted and registered, so the
            # mutation below cannot be broadcast before B exists.
            assert b.status() is not None, "daemon did not register observer B"

            assert a.set_fps(37), "fps change rejected"

            seen = False
            deadline = time.monotonic() + DRAIN_TIMEOUT_SEC
            while time.monotonic() < deadline and not seen:
                for msg_type, payload in b.recv_raw(timeout=0.5):
                    if msg_type != MsgType.RESP_STATUS:
                        continue
                    if parse_status(payload).target_fps == 37:
                        seen = True

            assert seen, "observer did not receive a status push for the change"
        finally:
            b.disconnect()
    finally:
        try:
            a.set_fps(50)
        finally:
            a.disconnect()
