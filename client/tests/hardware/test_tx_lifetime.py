"""Repeated one-shot WiFi TX must not accumulate threads or memory.

Every CMD_TX starts a scheduler thread that is joined by the next start.
A per-transmission leak is invisible after one frame, so send enough
one-shots that a leak grows the process measurably, then compare thread
count and RSS against the idle baseline.  A failing DMA start is also
reported: it must stay false across all N frames.
"""
import time

import pytest

from shesha.channels import CHANNEL_TO_FREQ
from shesha.client import DaemonClient
from shesha.frames import build_beacon
from shesha.protocol import DaemonMode, TX_STOP_ALL
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

N_FRAMES = 100
CHANNEL = 6
RATE_MBPS = 6
WIFI_RATE = 20_000_000
DEFAULT_FREQ_HZ = 2_457_000_000
RSS_GROWTH_LIMIT_KB = 4 * 1024


def _wait_status(client: DaemonClient, predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None and predicate(*result):
            return result
        time.sleep(0.05)
    return None


def _wait_idle(client: DaemonClient, timeout: float = 5.0) -> bool:
    return _wait_status(
        client, lambda st, _m: not st.tx_busy, timeout) is not None


@pytest.fixture
def client():
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    yield c
    try:
        c.tx_stop(TX_STOP_ALL)
    except Exception:
        pass
    try:
        c.set_mode(DaemonMode.WATERFALL)
        c.set_freq(DEFAULT_FREQ_HZ)
    except Exception:
        pass
    c.disconnect()


def test_repeated_wifi_one_shot_stable(client: DaemonClient):
    assert client.tx_stop(TX_STOP_ALL), "could not clear any leftover TX"
    assert client.set_mode(DaemonMode.WIFI), "could not select wifi mode"
    assert client.set_fpga(False), "could not select the software producer"
    assert client.set_freq(int(CHANNEL_TO_FREQ[CHANNEL] * 1e6)), \
        "could not tune the channel"
    assert client.set_sample_rate(WIFI_RATE), "could not set 20 MSPS"
    assert _wait_status(
        client,
        lambda st, mode: (mode == DaemonMode.WIFI and st.rx_active
                          and st.sample_rate_hz == WIFI_RATE),
    ) is not None, "wifi producer did not start at 20 MSPS"

    time.sleep(0.2)
    before = client.thread_stats()
    assert before is not None, "baseline thread stats unavailable"

    psdu = build_beacon(ssid="LIFETIME", channel=CHANNEL)
    failed_frames = []
    for i in range(N_FRAMES):
        assert client.wifi_tx(RATE_MBPS, psdu, repeat=1), \
            f"one-shot {i} rejected: {client.last_error}"
        assert _wait_idle(client), f"one-shot {i} never completed"

        # A mid-run failure must be caught here: the next start clears it.
        stats = client.tx_stats()
        assert stats is not None, f"tx_stats unavailable after one-shot {i}"
        if stats.dma_start_failed:
            failed_frames.append(i)

    assert not failed_frames, (
        f"{len(failed_frames)} of {N_FRAMES} one-shots failed to start the "
        f"DMA engine; first failing index {failed_frames[0]}")

    # Join the last one-shot thread (the next start normally does this).
    assert client.tx_stop(TX_STOP_ALL), "final tx_stop failed"
    time.sleep(0.2)

    after = client.thread_stats()
    assert after is not None, "post-TX thread stats unavailable"

    before_threads = len(before.threads)
    after_threads = len(after.threads)
    assert after_threads <= before_threads, (
        f"thread count grew from {before_threads} to {after_threads} "
        f"over {N_FRAMES} one-shot transmissions")

    growth_kb = after.vm_rss_kb - before.vm_rss_kb
    assert growth_kb < RSS_GROWTH_LIMIT_KB, (
        f"RSS grew {growth_kb} KiB over {N_FRAMES} one-shot transmissions "
        f"(limit {RSS_GROWTH_LIMIT_KB} KiB)")


def _poll_repeat_burst(client: DaemonClient, psdu: bytes, interval_us: int):
    assert client.wifi_tx(RATE_MBPS, psdu, repeat=2,
                          interval_us=interval_us), \
        f"repeat WiFi TX rejected: {client.last_error}"

    saw_busy = False
    saw_active = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None and result[0].tx_busy:
            saw_busy = True
            stats = client.tx_stats()
            assert stats is not None, "tx_stats unavailable during TX"
            if stats.tx_active:
                saw_active = True
                break
        time.sleep(0.02)
    return saw_busy, saw_active


def test_tx_stats_active_tracks_repeat_wifi_burst(client: DaemonClient):
    """tx_active is the TX session, not the streaming scheduler flag.

    A finite repeat burst is one-shot work: the scheduler's stream
    telemetry stays inactive while the session is live across the
    inter-frame interval.  Polling during that interval catches a
    regression to the scheduler flag.
    """
    assert client.tx_stop(TX_STOP_ALL), "could not clear any leftover TX"
    assert client.set_mode(DaemonMode.WIFI), "could not select wifi mode"
    assert client.set_fpga(False), "could not select the software producer"
    assert client.set_freq(int(CHANNEL_TO_FREQ[CHANNEL] * 1e6)), \
        "could not tune the channel"
    assert client.set_sample_rate(WIFI_RATE), "could not set 20 MSPS"
    assert _wait_status(
        client,
        lambda st, mode: (mode == DaemonMode.WIFI and st.rx_active
                          and st.sample_rate_hz == WIFI_RATE),
    ) is not None, "wifi producer did not start at 20 MSPS"

    psdu = build_beacon(ssid="TX_ACTIVE", channel=CHANNEL)
    try:
        for interval_us in (1_000_000, 2_000_000):
            saw_busy, saw_active = _poll_repeat_burst(client, psdu, interval_us)
            if saw_active:
                break
            if saw_busy:
                pytest.fail(
                    "status reported the repeat burst busy but "
                    "tx_stats.tx_active stayed false")
            assert client.tx_stop(TX_STOP_ALL), "could not clear the burst"
            assert _wait_idle(client), "previous burst did not clear"
        else:
            pytest.fail(
                "repeat burst completed before any poll saw tx_busy; "
                "tx_active was never observed")
    finally:
        client.tx_stop(TX_STOP_ALL)

    assert _wait_idle(client), "TX did not clear after tx_stop"
    stats = client.tx_stats()
    assert stats is not None and not stats.tx_active, \
        "tx_stats.tx_active stayed true after the TX stopped"
