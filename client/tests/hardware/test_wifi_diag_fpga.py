"""FPGA WIFI_DIAG counters have distinct sources and consistent arithmetic.

In the fabric path `captures` is the tag-FIFO frame count and can reset on
flush/retune, while `decode_ok`/`fcs_ok` are software counters since the
FPGA producer started.  So `decode_ok >= fcs_ok` and
`fcs_bad == decode_ok - fcs_ok` always hold, but `captures >= decode_ok`
does not: a flush can leave the software counters ahead of the fabric.

The test requires the cabled SMA loopback with an attenuator; FM antennas
are not sufficient.  No captures is a failure, as is a capture with no
decodes.
"""
import time

import pytest

from shesha.channels import CHANNEL_TO_FREQ
from shesha.client import DaemonClient
from shesha.frames import build_beacon
from shesha.protocol import DaemonMode, TX_STOP_ALL
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

CHANNEL = 6
RATE_MBPS = 6
WIFI_RATE = 20_000_000
DEFAULT_FREQ_HZ = 2_457_000_000
TRAFFIC_SEC = 1.5


def _wait_status(client: DaemonClient, predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None and predicate(*result):
            return result
        time.sleep(0.1)
    return None


@pytest.fixture
def client():
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT, timeout=5.0)
    c.connect()
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


def test_fpga_diag_reports_distinct_counters(client: DaemonClient):
    assert client.set_mode(DaemonMode.WIFI), "could not select wifi mode"
    assert client.set_freq(int(CHANNEL_TO_FREQ[CHANNEL] * 1e6)), \
        "could not tune the channel"
    assert client.set_sample_rate(WIFI_RATE), "could not set 20 MSPS"
    assert client.set_fpga(True), "could not enable the FPGA producer"

    assert _wait_status(
        client,
        lambda st, mode: (mode == DaemonMode.WIFI and st.fpga_active
                          and st.fpga_running and st.rx_active),
    ) is not None, "FPGA producer never started"

    psdu = build_beacon(ssid="DIAG_FPGA", channel=CHANNEL)
    repeat = 400
    assert client.wifi_tx(RATE_MBPS, psdu, repeat=repeat,
                          interval_us=2_000), \
        f"WiFi TX rejected: {client.last_error}"

    deadline = time.monotonic() + TRAFFIC_SEC + 14.0
    last = None
    while time.monotonic() < deadline:
        diag = client.diag()
        if diag is not None:
            last = diag
            if diag.captures > 0 and diag.decode_ok > 0:
                break
        time.sleep(0.25)

    assert last is not None, "diag unavailable"
    assert last.captures > 0, (
        "no frame reached the FPGA RX; the cabled SMA loopback with "
        "attenuator is required. FM antennas are not sufficient.")

    assert last.decode_ok > 0, (
        f"{last.captures} captures but decode_ok is zero")
    assert last.decode_ok >= last.fcs_ok, (
        f"fcs_ok={last.fcs_ok} exceeds decode_ok={last.decode_ok}")
    assert last.fcs_bad == last.decode_ok - last.fcs_ok, (
        f"fcs_bad={last.fcs_bad} is not decode_ok-fcs_ok")
    if last.fcs_bad > 0:
        assert not (last.captures == last.decode_ok == last.fcs_ok), (
            "captures, decode_ok, and fcs_ok are all the same value; the "
            "fields are not measuring distinct events")
    assert last.iq_rms == 0.0 and last.iq_peak == 0.0, (
        "the FPGA path must leave the IQ fields at zero")
