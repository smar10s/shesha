"""DATA_WIFI labels each frame with the frequency it was captured on.

Requires: Pluto TX -> SMA cable + attenuator -> Pluto RX (full-duplex
loopback).  Both the software and fabric WiFi producers must stamp the slot
at capture time; the host's tuning state is not allowed to fill the gap.

Both producers must also decode the beacon.  The fabric path derives its
tuning from the daemon's LO readback, so a bad frequency-to-channel mapping
shows up as a 5 MHz detune and FCS failures rather than a label mismatch.
"""
import time

import pytest

from shesha.channels import CHANNEL_TO_FREQ, freq_to_channel
from shesha.client import DaemonClient
from shesha.frames import build_beacon
from shesha.protocol import DaemonMode
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

CHANNEL = 6
FREQ_HZ = CHANNEL_TO_FREQ[CHANNEL] * 1_000_000
RATE_MBPS = 6
SSID = "RX_FREQ_TEST"
TX_DURATION = 10.0
TX_INTERVAL_US = 5_000
TX_COUNT = int(TX_DURATION / (TX_INTERVAL_US / 1_000_000))
STREAM_DURATION = TX_DURATION + 1.0
DEFAULT_FREQ_HZ = 2_457_000_000


def _connect() -> DaemonClient:
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT, timeout=5.0)
    c.connect()
    return c


def _wait_for(client: DaemonClient, predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None and predicate(*result):
            return result
        time.sleep(0.1)
    return None


@pytest.fixture
def client():
    c = _connect()
    try:
        c.tx_stop()
        yield c
    finally:
        try:
            c.tx_stop()
            c.set_fpga(False)
            c.set_mode(DaemonMode.WATERFALL)
            c.set_freq(DEFAULT_FREQ_HZ)
        except Exception:
            pass
        c.disconnect()


@pytest.mark.parametrize("fpga", [False, True], ids=["sw", "fpga"])
def test_loopback_labels_capture_frequency(client: DaemonClient, fpga: bool):
    tx = _connect()
    try:
        assert client.set_mode(DaemonMode.WIFI), "could not select wifi mode"
        assert client.set_sample_rate(20_000_000), "could not set 20 MSPS"
        assert client.set_freq(FREQ_HZ), "could not tune the channel"
        assert client.set_agc(0), "could not select manual AGC"
        assert client.set_gain(25.0), "could not set gain"
        if fpga:
            assert client.set_fpga(True), "could not enable the WiFi FPGA"
            assert _wait_for(
                client,
                lambda st, mode: (mode == DaemonMode.WIFI and st.fpga_active
                                  and st.fpga_running),
            ) is not None, "FPGA producer never started"
        else:
            # The previous test may have left the fabric variant active.  This
            # case must exercise the software producer, so select it outright
            # instead of inheriting whatever the last test left behind.
            assert client.set_fpga(False), "could not select the software producer"
            assert _wait_for(
                client,
                lambda st, mode: (mode == DaemonMode.WIFI and st.rx_active
                                  and not st.fpga_active),
            ) is not None, "software producer never started"
        time.sleep(0.5)

        psdu = build_beacon(ssid=SSID, channel=CHANNEL)
        assert tx.wifi_tx(RATE_MBPS, psdu, repeat=TX_COUNT,
                          interval_us=TX_INTERVAL_US), (
            f"WiFi TX rejected: {tx.last_error}")

        result = client.stream(duration=STREAM_DURATION, verbose=True)
        frames = result.wifi_frames
        assert frames, (
            f"no DATA_WIFI frames after {TX_DURATION}s of {TX_COUNT} "
            f"scheduled TX ({'fpga' if fpga else 'sw'} producer).")

        found = [f for f in frames
                 if f.fcs_ok and f.psdu and SSID.encode() in f.psdu]
        assert found, (
            f"Beacon with SSID={SSID!r} not received back via loopback "
            f"after {TX_DURATION}s of {TX_COUNT} scheduled TX "
            f"({'fpga' if fpga else 'sw'} producer).")
        for frame in found:
            assert freq_to_channel(frame.rx_freq_hz / 1e6) == CHANNEL, (
                f"frame labelled {frame.rx_freq_hz / 1e6:.3f} MHz, "
                f"expected channel {CHANNEL}")
    finally:
        tx.disconnect()
