"""TX a beacon via CMD_TX, verify it arrives back on RX as DATA_WIFI.

Requires: Pluto TX -> SMA cable + attenuator -> Pluto RX (full-duplex loopback).
FM antennas are not sufficient; over-the-air coupling is intermittent.

The daemon's RX duty cycle is ~10% (20ms capture / ~170ms total cycle).
Combined with decode success rate, the overall hit rate is ~0.5-1% per TX.
We send a burst of beacons via daemon-side scheduling and stream simultaneously.
"""
import time
import pytest
from shesha.frames import build_beacon
from shesha.client import DaemonClient
from shesha.protocol import cmd_tx_wifi, MsgType, DaemonMode

SSID = "LOOPBACK_TEST"
CHANNEL = 6
RATE_MBPS = 6
TX_DURATION = 10.0
TX_INTERVAL_US = 5000  # 5 ms between beacons (~200/s)
TX_COUNT = int(TX_DURATION / (TX_INTERVAL_US / 1_000_000))
STREAM_DURATION = 11.0


@pytest.fixture
def tx_client():
    from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    yield c
    c.disconnect()


def test_beacon_loopback(daemon, tx_client):
    from shesha.channels import CHANNEL_TO_FREQ

    freq_hz = int(CHANNEL_TO_FREQ[CHANNEL] * 1e6)
    daemon.set_freq(freq_hz)
    daemon.set_agc(0)
    daemon.set_gain(25.0)
    daemon.set_mode(DaemonMode.WIFI)
    time.sleep(0.5)

    psdu = build_beacon(ssid=SSID, channel=CHANNEL)

    tx_client.send_raw(cmd_tx_wifi(RATE_MBPS, psdu,
                                    repeat=TX_COUNT,
                                    interval_us=TX_INTERVAL_US))

    result = daemon.stream(duration=STREAM_DURATION, verbose=True)

    fcs_ok = [f for f in result.wifi_frames if f.fcs_ok]
    found = [f for f in fcs_ok if f.psdu and SSID.encode() in f.psdu]

    assert found, (
        f"Beacon with SSID={SSID!r} not received back via loopback "
        f"after {TX_DURATION}s of {TX_COUNT} scheduled TX.\n"
        f"  Total wifi frames: {len(result.wifi_frames)}\n"
        f"  FCS OK: {len(fcs_ok)}\n"
        f"  Cabled SMA loopback with attenuator is required; "
        f"FM antennas are not sufficient."
    )


def test_beacon_loopback_enriched(daemon, tx_client):
    """Verify enriched frame fields: frame_class, meta, IE summary."""
    from shesha.channels import CHANNEL_TO_FREQ

    freq_hz = int(CHANNEL_TO_FREQ[CHANNEL] * 1e6)
    daemon.set_freq(freq_hz)
    daemon.set_agc(0)
    daemon.set_gain(25.0)
    daemon.set_mode(DaemonMode.WIFI)
    time.sleep(0.5)

    bssid = b'\xde\xad\xbe\xef\x00\x01'
    psdu = build_beacon(ssid=SSID, channel=CHANNEL, bssid=bssid)

    tx_client.send_raw(cmd_tx_wifi(RATE_MBPS, psdu,
                                    repeat=TX_COUNT,
                                    interval_us=TX_INTERVAL_US))

    result = daemon.stream(duration=STREAM_DURATION, verbose=True)

    fcs_ok = [f for f in result.wifi_frames if f.fcs_ok]
    found = [f for f in fcs_ok if f.psdu and SSID.encode() in f.psdu]

    assert found, (
        f"Beacon with SSID={SSID!r} not received back via loopback "
        f"after {TX_DURATION}s of {TX_COUNT} scheduled TX.\n"
        f"  Total wifi frames: {len(result.wifi_frames)}\n"
        f"  FCS OK: {len(fcs_ok)}\n"
        f"  Cabled SMA loopback with attenuator is required; "
        f"FM antennas are not sufficient.")

    frame = found[0]

    # Frame class should be BEACON (3)
    assert frame.frame_class == 3, f"Expected BEACON(3), got {frame.frame_class}"
    assert frame.frame_class_name == "beacon"

    # Meta should have BSSID matching our TX
    assert frame.meta is not None, "meta is None"
    assert frame.meta.bssid == "de:ad:be:ef:00:01", f"BSSID mismatch: {frame.meta.bssid}"
    assert frame.meta.ssid == SSID, f"SSID mismatch: {frame.meta.ssid}"
    assert frame.meta.is_eapol is False

    # The capture frequency is stamped by the producer, not guessed by the
    # host's tuning state.
    from shesha.channels import freq_to_channel
    assert frame.rx_freq_hz > 0, "rx frequency missing from DATA_WIFI"
    assert freq_to_channel(frame.rx_freq_hz / 1e6) == CHANNEL, (
        f"rx channel mismatch: {frame.rx_freq_hz / 1e6:.3f} MHz")
    assert abs(frame.rx_freq_hz - freq_hz) <= 1_000_000, (
        f"rx frequency {frame.rx_freq_hz} is not near {freq_hz}")

    # IE summary should be present (our beacon has DS Parameter IE)
    assert frame.ie is not None, "IE summary is None"
    assert frame.ie.channel == CHANNEL, f"IE channel mismatch: {frame.ie.channel}"
    # Our test beacon has no RSN IE → security should be "open"
    assert frame.ie.security == "open", f"Expected open, got {frame.ie.security}"
