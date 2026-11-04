"""Targeted CMD_TX_STOP and generic status during IQ TX.

Verifies two daemon invariants:
  * status reports the protocol that actually owns TX, including IQ;
  * `wifi tx stop` does not tear down an FM or IQ transmission.
"""
import time

import pytest

from shesha.client import DaemonClient, PushResult
from shesha.frames import build_beacon
from shesha.protocol import (
    DaemonMode, TX_PROTO_FM, TX_PROTO_IQ, TX_PROTO_WIFI, TX_REPEAT_FOREVER,
    TX_STOP_ALL,
)
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT


@pytest.fixture
def client():
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    yield c
    try:
        c.tx_stop(TX_STOP_ALL)
        c.set_mode(DaemonMode.WATERFALL)
    except Exception:
        pass
    c.disconnect()


def _wait_status(c, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = c.status()
        if result is not None and predicate(*result):
            return result
        time.sleep(0.05)
    return None


def _wait_idle(c, timeout=5.0) -> bool:
    return _wait_status(c, lambda st, _m: not st.tx_busy,
                        timeout) is not None


def test_iq_status_and_wifi_stop_does_not_stop_iq(client):
    assert client.set_mode(DaemonMode.IQ)
    assert client.set_sample_rate(2_100_000)
    assert client.set_iq_bits(8)

    assert client.iq_tx_stream_data(b"\x00" * (4096 * 2)) == PushResult.OK

    state = _wait_status(
        client, lambda st, _m: st.tx_busy and st.tx_proto == TX_PROTO_IQ)
    assert state is not None, "status did not report IQ TX"

    assert client.tx_stop(TX_PROTO_WIFI)
    time.sleep(0.2)
    result = client.status()
    assert result is not None
    status, _mode = result
    assert status.tx_busy, "wifi stop killed the IQ transmission"
    assert status.tx_proto == TX_PROTO_IQ

    assert client.tx_stop(TX_STOP_ALL)
    assert _wait_idle(client), "stop-all left IQ TX running"


def test_wifi_stop_stops_wifi_tx(client):
    from shesha.channels import CHANNEL_TO_FREQ

    assert client.set_mode(DaemonMode.WIFI)
    assert client.set_freq(int(CHANNEL_TO_FREQ[6] * 1e6))
    psdu = build_beacon(ssid="TARGETED_STOP", channel=6)
    assert client.wifi_tx(6, psdu, repeat=TX_REPEAT_FOREVER,
                          interval_us=10_000)

    state = _wait_status(
        client, lambda st, _m: st.tx_busy and st.tx_proto == TX_PROTO_WIFI)
    assert state is not None, "status did not report WiFi TX"

    assert client.tx_stop(TX_PROTO_WIFI)
    assert _wait_idle(client), "targeted wifi stop left TX running"


def test_wifi_stop_does_not_stop_fm_tx(client):
    assert client.set_mode(DaemonMode.FM)
    assert client.fm_tx_stream_start([0.0] * 4410, audio_rate=44100,
                                     dac_rate=2_100_000)

    state = _wait_status(
        client, lambda st, _m: st.tx_busy and st.tx_proto == TX_PROTO_FM)
    assert state is not None, "status did not report FM TX"

    assert client.tx_stop(TX_PROTO_WIFI)
    time.sleep(0.2)
    result = client.status()
    assert result is not None
    status, _mode = result
    assert status.tx_busy, "wifi stop killed the FM transmission"
    assert status.tx_proto == TX_PROTO_FM

    assert client.tx_stop(TX_STOP_ALL)
    assert _wait_idle(client), "stop-all left FM TX running"
