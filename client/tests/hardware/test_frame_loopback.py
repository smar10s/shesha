"""Loopback tests for deauth and raw frame TX.

Requires: Pluto TX -> SMA cable + attenuator -> Pluto RX (full-duplex
loopback). The daemon's RX duty cycle is low, so each test sends a burst
while streaming.
"""
import time

import pytest

from shesha.client import DaemonClient
from shesha.frames import BROADCAST, build_deauth, build_raw, parse_mac
from shesha.protocol import DaemonMode, cmd_tx_wifi

CHANNEL = 6
RATE_MBPS = 6
TX_DURATION = 10.0
TX_INTERVAL_US = 5000
TX_COUNT = int(TX_DURATION / (TX_INTERVAL_US / 1_000_000))
STREAM_DURATION = 11.0


@pytest.fixture
def tx_client():
    from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    yield c
    c.disconnect()


def _loopback(daemon, tx_client, psdu):
    from shesha.channels import CHANNEL_TO_FREQ

    freq_hz = int(CHANNEL_TO_FREQ[CHANNEL] * 1e6)
    daemon.set_freq(freq_hz)
    daemon.set_agc(0)
    daemon.set_gain(25.0)
    daemon.set_mode(DaemonMode.WIFI)
    time.sleep(0.5)

    tx_client.send_raw(cmd_tx_wifi(RATE_MBPS, psdu, repeat=TX_COUNT,
                                   interval_us=TX_INTERVAL_US))
    result = daemon.stream(duration=STREAM_DURATION, verbose=True)
    return [f for f in result.wifi_frames
            if f.fcs_ok and f.psdu[:len(psdu)] == psdu]


def test_deauth_loopback(daemon, tx_client):
    bssid = parse_mac("aa:bb:cc:dd:ee:ff")
    psdu = build_deauth(da=bssid, bssid=bssid, reason=7)

    found = _loopback(daemon, tx_client, psdu)

    assert found, (
        f"Deauth frame not received back via loopback after {TX_DURATION}s "
        f"of {TX_COUNT} scheduled TX.")


def test_raw_loopback(daemon, tx_client):
    psdu = build_raw(_probe_request("LOOPBACK_RAW"))

    found = _loopback(daemon, tx_client, psdu)

    assert found, (
        f"Raw probe request not received back via loopback after "
        f"{TX_DURATION}s of {TX_COUNT} scheduled TX.")


def _probe_request(ssid: str) -> bytes:
    sa = parse_mac("de:ad:be:ef:00:02")
    header = (b"\x40\x00"          # frame control: probe request
              + b"\x00\x00"        # duration
              + BROADCAST          # DA
              + sa                 # SA
              + BROADCAST          # BSSID
              + b"\x00\x00")       # sequence
    encoded = ssid.encode()
    return header + bytes([0, len(encoded)]) + encoded
