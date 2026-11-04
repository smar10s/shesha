"""RESP_STATUS must report the producer's real state, not configuration.

An FM TX at a DAC rate different from the RX rate suspends the RX producer
without changing the configured mode.  During that window status must say:

  * daemon_mode still names the configured mode (waterfall here),
  * rx_active is false,
  * fpga_running is false (no FPGA producer is running).

After the TX stops the producer resumes and rx_active becomes true again.
The test also pins the accumulation-mode byte.  The WiFi FPGA case must
report fpga_running only while that producer runs; disabling it must leave
a running SW producer behind.
"""
import math
import time

import pytest

from shesha.client import DaemonClient, PushResult
from shesha.protocol import (
    DaemonMode,
    MsgType,
    TX_PROTO_IQ,
    parse_status,
)
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

TONE_DUR = 0.5
AUDIO_RATE = 44100
TONE_HZ = 1000
TONE_AMP = 0.8

RX_RATE = 20_000_000
FM_TX_RATE = 2_100_000


def _make_tone(dur_s: float, freq_hz: float = TONE_HZ,
               rate: int = AUDIO_RATE, amp: float = TONE_AMP) -> list[float]:
    n = int(dur_s * rate)
    return [amp * math.sin(2.0 * math.pi * freq_hz * i / rate)
            for i in range(n)]


def _poll_until(client: DaemonClient, predicate, timeout: float = 10.0,
                interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None:
            status, mode = result
            if predicate(status, mode):
                return True
        time.sleep(interval)
    return False


def _best_effort(fn) -> None:
    try:
        fn()
    except Exception:
        pass


def _wait_status_push(client: DaemonClient, predicate,
                      timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        for msg_type, payload in client.recv_raw(timeout=min(0.25, remaining)):
            if msg_type != MsgType.RESP_STATUS:
                continue
            try:
                status = parse_status(payload)
            except Exception:
                continue
            if predicate(status):
                return True
    return False


@pytest.fixture
def client():
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    yield c
    c.disconnect()


def test_status_reports_suspended_and_resumed_producer(client: DaemonClient):
    try:
        assert client.tx_stop(), "could not clear any leftover TX"
        assert client.set_mode(DaemonMode.WATERFALL), "could not select waterfall"
        assert client.set_bw(RX_RATE), "could not set 20 MSPS bandwidth"
        assert _poll_until(
            client,
            lambda st, mode: st.rx_active and st.sample_rate_hz == RX_RATE,
        ), "waterfall producer did not start at 20 MSPS"

        # The DAC rate differs from the RX rate, so starting the TX must
        # suspend the waterfall producer rather than running both.
        assert client.fm_tx_stream_start(
            _make_tone(TONE_DUR), dac_rate=FM_TX_RATE), "FM TX did not start"
        assert _poll_until(
            client, lambda st, _m: st.tx_busy,
        ), "FM TX never became busy"

        status, mode = client.status()
        assert status.tx_busy
        assert mode == DaemonMode.WATERFALL, \
            "configured mode must not change when a TX suspends the producer"
        assert status.daemon_mode == DaemonMode.WATERFALL
        assert not status.rx_active, \
            "rx_active must be false while the producer is suspended"
        assert not status.fpga_running, \
            "fpga_running must be false when no FPGA producer is running"

        assert client.tx_stop(), "TX stop failed"
        assert _poll_until(
            client, lambda st, _m: st.rx_active,
        ), "waterfall producer did not resume after TX"

        status, mode = client.status()
        assert mode == DaemonMode.WATERFALL
        assert status.rx_active
        assert not status.fpga_running

        assert client.set_accum(2), "could not set AVG accumulation"
        status, _mode = client.status()
        assert status.accum == 2, \
            f"expected accum=2 (avg), got {status.accum}"
    finally:
        _best_effort(client.tx_stop)
        _best_effort(lambda: client.set_accum(1))  # MAX
        _best_effort(lambda: client.set_mode(DaemonMode.WATERFALL))
        _best_effort(lambda: client.set_fps(50))
        _best_effort(lambda: client.set_fft_size(4096))


def test_fpga_running_reflects_actual(client: DaemonClient):
    try:
        assert client.set_mode(DaemonMode.WIFI), "could not select wifi mode"
        assert client.set_fpga(True), "could not enable the FPGA producer"
        assert _poll_until(
            client,
            lambda st, mode: (mode == DaemonMode.WIFI and st.fpga_active
                              and st.fpga_running and st.rx_active),
        ), "FPGA producer never reported configured, running, and rx_active"

        assert client.set_fpga(False), "could not disable the FPGA producer"
        assert _poll_until(
            client,
            lambda st, mode: (mode == DaemonMode.WIFI and not st.fpga_running
                              and st.rx_active),
        ), "SW producer did not report running after FPGA disable"
    finally:
        _best_effort(lambda: client.set_mode(DaemonMode.WATERFALL))


def test_implicit_iq_start_pushes_status_to_observer():
    observer = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    issuer = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    observer.connect()
    issuer.connect()
    try:
        # A status response proves the daemon registered the observer, so the
        # IQ start below cannot be broadcast before it exists.
        assert observer.status() is not None, \
            "daemon did not register the observer"

        assert issuer.tx_stop(), "could not clear any leftover TX"
        assert issuer.set_mode(DaemonMode.IQ), "could not select IQ mode"
        assert issuer.set_sample_rate(2_100_000), "could not set 2.1 MSPS"
        assert issuer.set_iq_bits(8), "could not select 8-bit IQ"

        assert issuer.iq_tx_stream_data(b"\x00" * (4096 * 2)) == PushResult.OK, \
            "first IQ chunk was rejected"

        assert _wait_status_push(
            observer,
            lambda st: st.tx_busy and st.tx_proto == TX_PROTO_IQ,
        ), "observer did not receive a status push for the implicit IQ start"

        assert issuer.iq_tx_stream_end(), "could not end the IQ stream"
        assert _poll_until(issuer, lambda st, _m: not st.tx_busy), \
            "IQ TX did not stop"
    finally:
        _best_effort(issuer.tx_stop)
        _best_effort(lambda: issuer.set_mode(DaemonMode.WATERFALL))
        _best_effort(issuer.disconnect)
        _best_effort(observer.disconnect)
