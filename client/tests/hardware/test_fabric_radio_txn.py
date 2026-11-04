"""Fabric reconfiguration is a serialized radio transaction.

Requires: Pluto TX -> SMA cable + attenuator -> Pluto RX (full-duplex
loopback).  The fabric owns both LOs; a retune while the fabric runs must
move TX and RX together.  SET_FPGA must not enter or leave the fabric while
any TX is active.  A redundant bandwidth or sample-rate request must not
cycle the producer.

These are the real coverage for the command dispatch in main.c; the host
suite only covers the policy helpers and the deimos hold/release primitive.
"""
import time

import pytest

from shesha.channels import CHANNEL_TO_FREQ, freq_to_channel
from shesha.client import DaemonClient
from shesha.frames import build_beacon
from shesha.protocol import DaemonMode, TX_REPEAT_FOREVER, TX_STOP_ALL
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

RATE_MBPS = 6
WIFI_RATE = 20_000_000
DEFAULT_FREQ_HZ = 2_457_000_000
TX_INTERVAL_US = 2_000
PHASE_SEC = 4.0
LOAD_PHASE_SEC = 1.0
RETUNE_CHANNELS = (11, 6, 11, 6)


def _connect() -> DaemonClient:
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT, timeout=5.0)
    c.connect()
    return c


def _wait_status(client: DaemonClient, predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None and predicate(*result):
            return result
        time.sleep(0.1)
    return None


def _wait_diag(client: DaemonClient, predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        diag = client.diag()
        if diag is not None and predicate(diag):
            return diag
        time.sleep(0.1)
    return None


def _freq(channel: int) -> int:
    return int(CHANNEL_TO_FREQ[channel] * 1_000_000)


def _enable_fabric(client: DaemonClient, channel: int) -> None:
    assert client.set_mode(DaemonMode.WIFI), "could not select wifi mode"
    assert client.set_sample_rate(WIFI_RATE), "could not set 20 MSPS"
    assert client.set_freq(_freq(channel)), "could not tune the channel"
    assert client.set_fpga(True), "could not enable the WiFi FPGA"
    assert _wait_status(
        client,
        lambda st, mode: (mode == DaemonMode.WIFI and st.fpga_active
                          and st.fpga_running and st.rx_active),
    ) is not None, "FPGA producer never started"


def _send_beacons(tx: DaemonClient, ssid: str, channel: int,
                  repeat: int) -> bytes:
    psdu = build_beacon(ssid=ssid, channel=channel)
    assert tx.wifi_tx(RATE_MBPS, psdu, repeat=repeat,
                      interval_us=TX_INTERVAL_US), (
        f"WiFi TX rejected: {tx.last_error}")
    return psdu


def _beacon_frames(result, ssid: str):
    return [f for f in result.wifi_frames
            if f.fcs_ok and f.psdu and ssid.encode() in f.psdu]


@pytest.fixture
def client():
    c = _connect()
    try:
        c.tx_stop(TX_STOP_ALL)
        yield c
    finally:
        try:
            c.tx_stop(TX_STOP_ALL)
            c.set_fpga(False)
            c.set_mode(DaemonMode.WATERFALL)
            c.set_freq(DEFAULT_FREQ_HZ)
        except Exception:
            pass
        c.disconnect()


@pytest.fixture
def tx():
    c = _connect()
    try:
        c.tx_stop(TX_STOP_ALL)
        yield c
    finally:
        try:
            c.tx_stop(TX_STOP_ALL)
        except Exception:
            pass
        c.disconnect()


def test_fabric_retune_moves_both_los(client: DaemonClient, tx: DaemonClient):
    _enable_fabric(client, 6)

    ssid = "FABRIC_BOTH_LO"
    _send_beacons(tx, ssid, 6, int(PHASE_SEC / (TX_INTERVAL_US / 1e6)))
    first = client.stream(duration=PHASE_SEC + 0.5, verbose=True)
    found = _beacon_frames(first, ssid)
    assert found, (
        "no ch6 loopback frames before the retune; the cabled SMA "
        "loopback with attenuator is required")
    for frame in found:
        assert freq_to_channel(frame.rx_freq_hz / 1e6) == 6, (
            f"pre-retune frame labelled {frame.rx_freq_hz / 1e6:.3f} MHz")

    assert client.set_freq(_freq(11)), "fabric retune to ch11 rejected"
    _send_beacons(tx, ssid, 11, int(PHASE_SEC / (TX_INTERVAL_US / 1e6)))
    second = client.stream(duration=PHASE_SEC + 0.5, verbose=True)
    found = _beacon_frames(second, ssid)
    assert found, (
        "no ch11 loopback frames after the retune; the TX LO probably "
        "stayed on ch6")
    for frame in found:
        assert freq_to_channel(frame.rx_freq_hz / 1e6) == 11, (
            f"post-retune frame labelled {frame.rx_freq_hz / 1e6:.3f} MHz")


def test_retune_under_load(client: DaemonClient, tx: DaemonClient):
    _enable_fabric(client, 6)

    ssid = "FABRIC_RETUNE_LOAD"
    _send_beacons(tx, ssid, 6, TX_REPEAT_FOREVER)
    try:
        _wait_status(client, lambda st, _m: st.tx_busy > 0)
        for i, channel in enumerate(RETUNE_CHANNELS):
            assert client.set_freq(_freq(channel)), (
                f"retune {i} to ch{channel} was rejected")
            status = _wait_status(
                client,
                lambda st, mode: (mode == DaemonMode.WIFI
                                  and st.fpga_running and st.rx_active),
                timeout=5.0)
            assert status is not None, (
                f"fabric stopped after retune {i} to ch{channel}")

            result = client.stream(duration=LOAD_PHASE_SEC, verbose=True)
            assert not result.errors, (
                f"decoder errors after retune {i}: {result.errors[:3]}")
            found = _beacon_frames(result, ssid)
            assert found, (
                f"no loopback frames after retune {i} to ch{channel}")
            for frame in found:
                assert freq_to_channel(frame.rx_freq_hz / 1e6) == channel, (
                    f"retune {i}: frame labelled "
                    f"{frame.rx_freq_hz / 1e6:.3f} MHz, expected ch{channel}")
    finally:
        tx.tx_stop(TX_STOP_ALL)


def test_set_fpga_locked_by_tx(client: DaemonClient, tx: DaemonClient):
    _enable_fabric(client, 11)

    ssid = "FABRIC_SET_FPGA_LOCK"
    _send_beacons(tx, ssid, 11, TX_REPEAT_FOREVER)
    try:
        assert _wait_status(client, lambda st, _m: st.tx_busy > 0) is not None, \
            "WiFi TX never became busy"

        assert not client.set_fpga(False), (
            "set_fpga(False) was accepted during an active TX")
        assert "locked by active tx" in client.last_error, client.last_error

        assert not client.set_fpga(True), (
            "set_fpga(True) was accepted during an active TX")
        assert "locked by active tx" in client.last_error, client.last_error

        status = _wait_status(
            client,
            lambda st, _m: st.tx_busy > 0 and st.fpga_running)
        assert status is not None, (
            "the rejected set_fpga commands disturbed the TX or the fabric")

        result = client.stream(duration=LOAD_PHASE_SEC + 0.5, verbose=True)
        assert _beacon_frames(result, ssid), (
            "loopback stopped after a rejected set_fpga")
    finally:
        tx.tx_stop(TX_STOP_ALL)

    assert _wait_status(
        client, lambda st, _m: st.tx_busy == 0, timeout=10.0) is not None, \
        "TX did not go idle"
    assert client.set_fpga(False), (
        "set_fpga(False) still rejected after the TX stopped")


def test_redundant_bw_sr_does_not_cycle(client: DaemonClient,
                                        tx: DaemonClient):
    _enable_fabric(client, 6)

    ssid = "FABRIC_BW_SR"
    _send_beacons(tx, ssid, 6, TX_REPEAT_FOREVER)
    try:
        assert _wait_diag(client, lambda d: d.decode_ok > 0) is not None, \
            "no FPGA decodes before the commands"
        time.sleep(1.0)
        before_diag = client.diag()
        assert before_diag is not None, "no diag before the commands"
        status = client.status()
        assert status is not None, "status unavailable"
        bw_hz = status[0].bw_hz
        sample_rate_hz = status[0].sample_rate_hz

        assert client.set_bw(bw_hz), "redundant set_bw was rejected"
        assert client.set_sample_rate(sample_rate_hz), (
            "redundant set_sample_rate was rejected")

        immediate = client.diag()
        assert immediate is not None, "no diag after the commands"
        assert immediate.decode_ok >= before_diag.decode_ok, (
            "decode counters reset after redundant bw/sr; the producer "
            "was cycled")
        assert immediate.fcs_ok >= before_diag.fcs_ok, (
            "fcs_ok counters reset after redundant bw/sr")

        after_status = _wait_status(
            client,
            lambda st, _m: (st.fpga_running and st.bw_hz == bw_hz
                            and st.sample_rate_hz == sample_rate_hz))
        assert after_status is not None, (
            "redundant bw/sr changed the running producer state")

        result = client.stream(duration=LOAD_PHASE_SEC + 0.5, verbose=True)
        assert _beacon_frames(result, ssid), (
            "no loopback frames after redundant bw/sr")

        after_diag = _wait_diag(
            client, lambda d: d.decode_ok > before_diag.decode_ok)
        assert after_diag is not None, (
            "decode counters stopped after redundant bw/sr")
        assert after_diag.fcs_ok >= before_diag.fcs_ok, (
            "fcs_ok counters went backwards after redundant bw/sr")
    finally:
        tx.tx_stop(TX_STOP_ALL)
