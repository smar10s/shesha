"""IQ and FM drains must not be paced by the waterfall FPS target.

The poll loop used to derive its timeout from g_target_fps in every mode.
At fps=1 that meant one wake per second, so IQ's sample chunks overran
its bounded ring and most frames never reached the wire.  These tests run
at fps=1 and require the natural frame rate to survive.
"""
import time

import pytest

from shesha.client import DaemonClient
from shesha.protocol import (
    DaemonMode,
    MsgType,
    SUBSCRIBE_FM,
    SUBSCRIBE_IQ,
)
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

IQ_RATE = 2_500_000
IQ_DRAIN_SEC = 2.0
# IQ slaves 16384 samples per frame (ring_slots.h).  At 2.5 MSPS that is
# ~153 frames/s, so a healthy 2 s drain carries ~305.  Demand 250 to leave
# startup and scheduling margin while still catching FPS pacing, which
# would deliver only a couple of frames.
IQ_MIN_FRAMES = 250

FM_DRAIN_SEC = 2.0
# One 49152-sample chunk at 2.1 MSPS yields one audio frame every ~23.4 ms,
# so a healthy 2 s drain should carry ~85 frames.
FM_MIN_FRAMES = 60


def _wait_status(client: DaemonClient, predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None and predicate(*result):
            return result
        time.sleep(0.05)
    return None


def _count_frames(client: DaemonClient, msg_type: int,
                   duration: float) -> int:
    frames = 0
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        for received_type, _payload in client.recv_raw(timeout=0.05):
            if received_type == msg_type:
                frames += 1
    return frames


def _restore_waterfall(client: DaemonClient) -> None:
    try:
        client.set_mode(DaemonMode.WATERFALL)
        client.set_bw(20_000_000)
        client.set_fps(50)
        client.set_iq_bits(12)
    except Exception:
        pass


@pytest.fixture
def client():
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    yield c
    _restore_waterfall(c)
    c.disconnect()


def test_iq_drain_independent_of_fps(client: DaemonClient):
    try:
        assert client.set_mode(DaemonMode.IQ), "could not select IQ mode"
        assert client.set_sample_rate(IQ_RATE), "could not set 2.5 MSPS"
        assert client.set_iq_bits(8), "could not select 8-bit IQ"
        assert client.set_fps(1), "could not set fps 1"

        assert _wait_status(
            client,
            lambda st, mode: (mode == DaemonMode.IQ and st.rx_active
                              and st.sample_rate_hz == IQ_RATE),
        ) is not None, "IQ producer did not start at 2.5 MSPS"

        assert client.subscribe(SUBSCRIBE_IQ), "could not subscribe to IQ"
        frames = _count_frames(client, MsgType.DATA_IQ, IQ_DRAIN_SEC)

        assert frames > IQ_MIN_FRAMES, (
            f"only {frames} IQ frames in {IQ_DRAIN_SEC:.1f}s at fps=1 "
            f"(expected > {IQ_MIN_FRAMES}); the drain is paced by FPS")
        assert client.gaps == 0, (
            f"{client.gaps} IQ frame gaps at fps=1; the ring overran")
    finally:
        _restore_waterfall(client)


def test_fm_drain_independent_of_fps(client: DaemonClient):
    try:
        assert client.set_mode(DaemonMode.FM), "could not select FM mode"
        assert client.set_fps(1), "could not set fps 1"

        assert _wait_status(
            client,
            lambda st, mode: (mode == DaemonMode.FM and st.rx_active
                              and st.sample_rate_hz == 2_100_000),
        ) is not None, "FM producer did not start at 2.1 MSPS"

        assert client.subscribe(SUBSCRIBE_FM), "could not subscribe to FM"
        frames = _count_frames(client, MsgType.DATA_FM, FM_DRAIN_SEC)

        assert frames > FM_MIN_FRAMES, (
            f"only {frames} FM frames in {FM_DRAIN_SEC:.1f}s at fps=1 "
            f"(expected > {FM_MIN_FRAMES}); the drain is paced by FPS")
        assert client.gaps == 0, (
            f"{client.gaps} FM frame gaps at fps=1; the ring overran")
    finally:
        _restore_waterfall(client)
