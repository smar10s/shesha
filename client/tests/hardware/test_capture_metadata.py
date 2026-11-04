"""Retune labeling for PSD and IQ frames.

After a retune the daemon must label subsequent frames with the new
frequency, at least one old-frequency frame captured before the retune must
still arrive before the first new-frequency one, and no old-frequency frame
may follow it.  This is an end-to-end retune smoke test, not proof that the
slot was stamped at capture time: reader and kernel buffering decide which
old frames survive, so a drain-time stamp could look identical here.
`firmware/tests/test_drain_metadata.c` covers capture-time stamping at the
drain level.
"""
import time

import pytest

from shesha.client import DaemonClient
from shesha.protocol import (
    MsgType,
    DaemonMode,
    SUBSCRIBE_PSD,
    SUBSCRIBE_IQ,
    cmd_set_freq,
    parse_iq,
    parse_psd,
)
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

FREQ_A = 2_457_000_000
FREQ_B = 2_437_000_000
DEFAULT_FREQ_HZ = 2_457_000_000
FREQ_TOL_MHZ = 0.01
PSD_STALL_SEC = 0.6
IQ_STALL_SEC = 0.25
POST_RETUNE_WINDOW_SEC = 0.3
DRAIN_TIMEOUT_SEC = 8.0


def _connect() -> DaemonClient:
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT, timeout=5.0)
    c.connect()
    return c


def _near(value_mhz: float, hz: int) -> bool:
    return abs(value_mhz - hz / 1e6) <= FREQ_TOL_MHZ


def _wait_for_frequency(client: DaemonClient, data_type: int, parser,
                        freq_of, hz: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        for msg_type, payload in client.recv_raw(timeout=min(0.25, remaining)):
            if msg_type != data_type:
                continue
            try:
                frame = parser(payload)
            except Exception:
                continue
            if _near(freq_of(frame), hz):
                return True
    return False


def _retune_and_collect(client: DaemonClient, data_type: int, parser,
                        freq_of, before_hz: int, after_hz: int):
    """Send the retune, then collect frames across the change."""
    client.send_raw(cmd_set_freq(after_hz))
    frames = []
    saw_response = False
    first_new_at = None
    deadline = time.monotonic() + DRAIN_TIMEOUT_SEC

    while time.monotonic() < deadline:
        if (first_new_at is not None
                and time.monotonic() - first_new_at >= POST_RETUNE_WINDOW_SEC):
            break
        remaining = deadline - time.monotonic()
        for msg_type, payload in client.recv_raw(timeout=min(0.25, remaining)):
            if msg_type == data_type:
                try:
                    frames.append(parser(payload))
                except Exception:
                    continue
            elif (msg_type in (MsgType.RESP_OK, MsgType.RESP_ERROR)
                  and payload and payload[0] == MsgType.CMD_SET_FREQ):
                assert msg_type == MsgType.RESP_OK, (
                    "retune rejected: "
                    + payload[1:].decode("utf-8", "replace"))
                saw_response = True
        if (saw_response and first_new_at is None
                and any(_near(freq_of(f), after_hz) for f in frames)):
            first_new_at = time.monotonic()

    assert saw_response, "no response to the retune"
    assert any(_near(freq_of(f), after_hz) for f in frames), (
        "no frames captured at the new frequency")

    first_new = next(i for i, f in enumerate(frames)
                     if _near(freq_of(f), after_hz))
    old_before_new = [f for f in frames[:first_new]
                      if _near(freq_of(f), before_hz)]
    late_old = [f for f in frames[first_new:]
                if not _near(freq_of(f), after_hz)]

    assert old_before_new, (
        "no old-frequency frame arrived before the first new-frequency frame")
    assert not late_old, (
        f"{len(late_old)} old-frequency frame(s) arrived after the first "
        "new-frequency frame")
    return frames


@pytest.fixture
def client():
    c = _connect()
    try:
        c.tx_stop()
        yield c
    finally:
        try:
            c.tx_stop()
            c.set_mode(DaemonMode.WATERFALL)
            c.set_freq(DEFAULT_FREQ_HZ)
        except Exception:
            pass
        c.disconnect()


def test_psd_frames_carry_capture_frequency(client: DaemonClient):
    assert client.set_mode(DaemonMode.WATERFALL), "could not select waterfall"
    assert client.set_fps(50), "could not set FPS 50"
    assert client.set_freq(FREQ_A), "could not tune to frequency A"
    assert client.subscribe(SUBSCRIBE_PSD), "could not subscribe to PSD"

    assert _wait_for_frequency(client, MsgType.DATA_PSD, parse_psd,
                               lambda f: f.center_freq_mhz, FREQ_A), (
        "no PSD frame at frequency A")

    # Stop reading so the daemon queues frames captured before the retune.
    time.sleep(PSD_STALL_SEC)
    _retune_and_collect(client, MsgType.DATA_PSD, parse_psd,
                        lambda f: f.center_freq_mhz, FREQ_A, FREQ_B)


def test_iq_frames_carry_capture_frequency_and_rate(client: DaemonClient):
    assert client.set_mode(DaemonMode.IQ), "could not select IQ mode"
    assert client.set_freq(FREQ_A), "could not tune to frequency A"

    result = client.status()
    assert result is not None, "status unavailable"
    expected_rate = result[0].sample_rate_hz
    assert expected_rate > 0

    assert client.subscribe(SUBSCRIBE_IQ), "could not subscribe to IQ"
    assert _wait_for_frequency(client, MsgType.DATA_IQ, parse_iq,
                               lambda f: f.freq_mhz, FREQ_A), (
        "no IQ frame at frequency A")

    time.sleep(IQ_STALL_SEC)
    frames = _retune_and_collect(
        client, MsgType.DATA_IQ, parse_iq, lambda f: f.freq_mhz,
        FREQ_A, FREQ_B)

    assert frames
    # Constant-rate smoke test: the host drain test is the discriminating
    # rate test, because a retune here never changes the sample rate.
    assert all(f.sample_rate_hz == expected_rate for f in frames), (
        "IQ frames do not carry the sample rate current at capture")
