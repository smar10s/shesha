"""FPGA restore across FM TX — verifies the FPGA producer resumes after FM TX.

Scenario:
  1. Set mode to WiFi
  2. Enable FPGA acceleration (wifi_fpga producer)
  3. Confirm fpga_active=True via status
  4. Fire a short FM TX burst (the TX session claims the sample clock,
     suspending the producer)
  5. Wait for TX to complete (tx_busy goes False)
  6. Confirm fpga_active=True again — the resume path restored FPGA, not SW

This exercises tx_resume_producer() in main.c, specifically the r.fpga
path that calls producer_fpga_for_mode() and re-enters the FPGA variant.

Requires: Pluto with deimos bitstream flashed, dual-core enabled.
"""
import math
import time

import pytest
from shesha.client import DaemonClient
from shesha.protocol import DaemonMode, TX_STOP_ALL

PLUTO_HOST = "192.168.2.1"
PLUTO_PORT = 5555

# Short tone — just enough to exercise the suspend/resume path.
# 0.5s at 44100 Hz = 22050 samples.  The FM TX path needs to actually
# run long enough for the rate change + DMA setup, so don't go shorter.
TONE_DUR = 0.5
AUDIO_RATE = 44100
TONE_HZ = 1000
TONE_AMP = 0.8


def _make_tone(dur_s: float, freq_hz: float = TONE_HZ,
               rate: int = AUDIO_RATE, amp: float = TONE_AMP) -> list[float]:
    n = int(dur_s * rate)
    return [amp * math.sin(2.0 * math.pi * freq_hz * i / rate)
            for i in range(n)]


@pytest.fixture
def client():
    """Fresh connection per test (not session-scoped — we mutate mode)."""
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    try:
        _reset(c)
        yield c
    finally:
        c.tx_stop(TX_STOP_ALL)
        c.set_mode(DaemonMode.WATERFALL)
        c.disconnect()


def _require(client: DaemonClient, ok: bool, what: str) -> None:
    """Fail with the daemon's rejection reason when a command is refused."""
    assert ok, f"{what} failed: {client.last_error or 'no error reported'}"


def _wait_status(client: DaemonClient, predicate, timeout: float = 10.0):
    """Return the first matching (status, mode), or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None and predicate(*result):
            return result
        time.sleep(0.05)
    return None


def _reset(client: DaemonClient) -> None:
    """Clear TX and settle in waterfall so setup cannot race a previous test."""
    _require(client, client.tx_stop(TX_STOP_ALL), "tx_stop(ALL)")
    _require(client, client.set_mode(DaemonMode.WATERFALL), "set_mode(WATERFALL)")
    assert _wait_status(
        client,
        lambda st, mode: not st.tx_busy and mode == DaemonMode.WATERFALL,
    ) is not None, "daemon did not settle in waterfall"


def _set_fpga(client: DaemonClient, enable: bool) -> None:
    """Enable/disable the FPGA producer, tolerating a lost response.

    The daemon can apply CMD_SET_FPGA after the client's 2 s response window
    has closed. The command is idempotent, so wait for the requested state
    before giving up rather than assuming the switch failed.
    """
    what = f"set_fpga({enable})"
    if client.set_fpga(enable):
        return
    if client.last_error != "no response from daemon":
        _require(client, False, what)
    if _wait_status(
        client, lambda st, _mode: st.fpga_active == enable, timeout=5.0,
    ) is not None:
        return
    _require(client, client.set_fpga(enable), what)


def _enable_fpga(client: DaemonClient) -> None:
    """Select WiFi and enable the FPGA producer, waiting for each transition."""
    _require(client, client.set_mode(DaemonMode.WIFI), "set_mode(WIFI)")
    assert _wait_status(
        client, lambda st, mode: mode == DaemonMode.WIFI and not st.tx_busy,
    ) is not None, "daemon did not settle in wifi mode"
    _set_fpga(client, True)
    assert _wait_status(
        client, lambda st, _mode: st.fpga_active,
    ) is not None, "FPGA producer did not become active"


def test_fpga_restores_after_fm_tx(client: DaemonClient):
    """FPGA producer resumes after FM TX completes."""
    _enable_fpga(client)

    result = client.status()
    assert result is not None, "status() returned None"
    status, mode = result
    assert mode == DaemonMode.WIFI, f"expected WIFI mode, got {mode}"
    assert status.fpga_active, "FPGA not active after set_fpga(True)"

    # Fire a short FM TX burst; it claims the sample clock and suspends
    # the producer.
    _require(client, client.fm_tx(_make_tone(TONE_DUR)), "fm_tx()")

    assert _wait_status(
        client, lambda st, _mode: not st.tx_busy, timeout=10.0,
    ) is not None, "TX did not complete within 10s"

    # The resume path must re-enter the FPGA producer, not fall back to SW.
    result = _wait_status(
        client,
        lambda st, mode: (not st.tx_busy and mode == DaemonMode.WIFI
                          and st.fpga_active),
    )
    assert result is not None, (
        "FPGA NOT restored after FM TX — fell back to software producer")
    status, mode = result
    assert mode == DaemonMode.WIFI, \
        f"expected WIFI mode after resume, got {mode}"
    assert status.rx_active, "RX producer not active after resume"


def test_redundant_mode_set_preserves_fpga(client: DaemonClient):
    """A redundant CMD_SET_MODE must not tear down the fpga variant.

    A reconnecting TUI re-applies its saved mode; before the no-op guard
    this switched wifi_fpga -> wifi_sw silently (and settled bw back to
    20 MHz), leaving the client displaying state it never asked for.
    """
    _enable_fpga(client)

    result = _wait_status(
        client,
        lambda st, mode: (mode == DaemonMode.WIFI and st.fpga_active
                          and st.bw_hz == 28_000_000),
    )
    assert result is not None, "FPGA entry did not settle at 28 MHz"

    # The re-applied mode set: must be a no-op for the running producer.
    _require(client, client.set_mode(DaemonMode.WIFI),
             "redundant set_mode(WIFI)")
    result = _wait_status(
        client,
        lambda st, mode: (mode == DaemonMode.WIFI and st.fpga_active
                          and st.bw_hz == 28_000_000),
    )
    assert result is not None, (
        "redundant set_mode(WIFI) tore down the FPGA producer or changed bw")


def test_fpga_status_reflects_freq_and_agc(client: DaemonClient):
    """RESP_STATUS tells the truth about the fabric-driven radio.

    The deimos path drives LO/gain directly, which used to leave
    g_radio stale — the TUI's scan hop flashed but never moved, and
    gain/agc changes appeared to do nothing.  Also pins the fpga AGC
    lock: fast_attack only.
    """
    _enable_fpga(client)

    # Entry settles agc to fast_attack
    result = _wait_status(
        client, lambda st, _mode: st.fpga_active and st.agc_mode == 1)
    assert result is not None, "fpga entry should settle agc=fast(1)"

    # Freq changes must show up in status (scan hopping depends on this).
    # The daemon reports the RX LO readback, which can differ by a few Hz.
    _require(client, client.set_freq(5_220_000_000), "set_freq(5220 MHz)")
    result = _wait_status(
        client,
        lambda st, _mode: abs(st.freq_hz - 5_220_000_000) <= 1_000_000)
    assert result is not None, "fpga freq change not reflected"

    # AGC is locked to fast_attack
    _require(client, client.set_agc(1), "set_agc(fast)")
    assert not client.set_agc(2), "set_agc(slow) must be rejected in fpga mode"
    assert not client.set_agc(0), "set_agc(manual) must be rejected in fpga mode"
    assert not client.set_gain(40.0), "manual gain must be rejected in fpga mode"
    result = _wait_status(client, lambda st, _mode: st.agc_mode == 1)
    assert result is not None, "agc_mode drifted after rejected sets"

    # Back to the wifi scan home channel
    _require(client, client.set_freq(5_180_000_000), "set_freq(5180 MHz)")


def test_suspended_fpga_uses_normal_agc_policy(client: DaemonClient):
    """A suspended fabric producer must not lock AGC or gain.

    FM TX displaces wifi_fpga and takes the radio.  While it runs, the
    client must be able to select manual AGC and set gain through the
    normal path.  Resuming the fabric then reimposes fast_attack, and
    status must follow the hardware rather than stay on manual.
    """
    _enable_fpga(client)

    _require(client, client.fm_tx_stream_start(_make_tone(1.0)),
             "fm_tx_stream_start()")
    try:
        result = _wait_status(
            client, lambda st, _mode: st.tx_busy, timeout=5.0)
        assert result is not None, "FM TX did not become busy"
        status, mode = result
        assert mode == DaemonMode.WIFI, f"expected WIFI mode, got {mode}"
        assert status.fpga_active, "fabric configuration was lost"
        assert not status.rx_active, "fabric producer was not suspended"

        _require(client, client.set_agc(0),
                 "set_agc(manual) while fabric is suspended")
        _require(client, client.set_gain(35.0),
                 "set_gain() while fabric is suspended")

        status, _mode = client.status()
        assert status.agc_mode == 0, "manual AGC did not take effect"
        assert abs(status.gain_db - 35.0) < 0.1, "gain did not take effect"

        _require(client, client.tx_stop(TX_STOP_ALL), "tx_stop(ALL)")
        result = _wait_status(
            client,
            lambda st, _mode: not st.tx_busy and st.fpga_active,
            timeout=10.0)
        assert result is not None, "fabric did not resume after FM TX"
        status, _mode = result
        assert status.agc_mode == 1, (
            "resumed fabric did not restore fast_attack: "
            f"agc={status.agc_mode}")
    finally:
        client.tx_stop(TX_STOP_ALL)


def test_fpga_restore_does_not_corrupt_rate(client: DaemonClient):
    """Sample rate returns to WiFi rate (20 MSPS) after FM TX restore."""
    wifi_rate = 20_000_000  # FM TX uses 2.1 MSPS internally.
    _enable_fpga(client)

    result = _wait_status(
        client, lambda st, _mode: st.sample_rate_hz == wifi_rate)
    assert result is not None, "FPGA entry did not settle at 20 MSPS"

    _require(client, client.fm_tx(_make_tone(TONE_DUR)), "fm_tx()")
    assert _wait_status(
        client, lambda st, _mode: not st.tx_busy, timeout=10.0,
    ) is not None, "TX did not complete within 10s"

    result = _wait_status(
        client,
        lambda st, _mode: not st.tx_busy and st.sample_rate_hz == wifi_rate,
    )
    if result is None:
        current = client.status()
        got = current[0].sample_rate_hz if current else "no status"
        pytest.fail(f"rate not restored: expected {wifi_rate}, got {got}")
