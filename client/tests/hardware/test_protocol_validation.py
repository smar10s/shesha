"""Command validation against a live daemon.

These commands used to be accepted and either silently ignored (gain while
AGC was active, AGC mode 3) or partially applied before the hardware
rejected them.  Every rejection here must be a RESP_ERROR with no TX
session, sample-rate, or mode side effect.
"""
import struct
import time

import pytest

from shesha.client import DaemonClient, PushResult
from shesha.frames import build_beacon
from shesha.protocol import (
    MsgType,
    DaemonMode,
    PROTO_RDS_TX_SIZE,
    TxFlags,
    TxProtocol,
    cmd_tx,
)
from tests.hardware.conftest import PLUTO_HOST, PLUTO_PORT

DEFAULT_FREQ_HZ = 2_457_000_000
CHANNEL = 6
RATE_MBPS = 6


def _connect() -> DaemonClient:
    c = DaemonClient(PLUTO_HOST, PLUTO_PORT, timeout=5.0)
    c.connect()
    return c


def _raw_exchange(client: DaemonClient, frame: bytes, cmd: int,
                  timeout: float = 3.0):
    """Send one command and return its RESP_OK/RESP_ERROR, skipping data."""
    client.send_raw(frame)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        for msg_type, payload in client.recv_raw(timeout=min(0.2, remaining)):
            if msg_type == MsgType.RESP_STATUS:
                continue
            if (msg_type in (MsgType.RESP_OK, MsgType.RESP_ERROR)
                    and payload and payload[0] == cmd):
                return msg_type, payload
    return None


def _fm_sub() -> bytes:
    return (struct.pack('<IIII', 44_100, 75_000, 2_100_000, 0)
            + b'\x00' * PROTO_RDS_TX_SIZE)


def _wifi_sub(rate_mbps: int = RATE_MBPS) -> bytes:
    psdu = build_beacon(ssid="VALIDATE", channel=CHANNEL)
    return struct.pack('<BH', rate_mbps, len(psdu)) + psdu


def _assert_rejected(client: DaemonClient, proto: int, flags: int,
                     repeat: int, interval_us: int, sub: bytes,
                     expected: str) -> None:
    result = _raw_exchange(
        client, cmd_tx(proto, flags, repeat, interval_us, sub),
        MsgType.CMD_TX)
    assert result is not None, "daemon did not answer CMD_TX"
    msg_type, payload = result
    assert msg_type == MsgType.RESP_ERROR, (
        f"CMD_TX proto={proto} flags={flags} repeat={repeat} "
        f"interval={interval_us} was accepted")
    message = payload[1:].decode("utf-8", "replace")
    assert expected in message.lower(), (
        f"expected {expected!r} in rejection message, got {message!r}")


def _assert_no_tx(client: DaemonClient) -> None:
    result = client.status()
    assert result is not None, "status unavailable"
    assert not result[0].tx_busy, "a rejected CMD_TX still started a session"
    stats = client.tx_stats()
    assert stats is not None, "tx stats unavailable"
    assert not stats.tx_active, "a rejected CMD_TX still reports tx_active"


def _wait_tx_idle(client: DaemonClient, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.status()
        if result is not None and not result[0].tx_busy:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def client():
    c = _connect()
    try:
        c.tx_stop()
        assert c.set_mode(DaemonMode.WATERFALL), "could not select waterfall"
        assert c.set_freq(DEFAULT_FREQ_HZ), "could not restore frequency"
        assert c.set_agc(0), "could not select manual AGC"
        yield c
    finally:
        try:
            c.tx_stop()
        except Exception:
            pass
        try:
            c.set_agc(1)
        except Exception:
            pass
        try:
            c.set_mode(DaemonMode.WATERFALL)
            c.set_freq(DEFAULT_FREQ_HZ)
        except Exception:
            pass
        c.disconnect()


def test_gain_requires_manual_agc(client: DaemonClient):
    assert client.set_agc(1), "could not enable fast AGC"
    assert not client.set_gain(20.0), "gain accepted while AGC was active"
    assert "agc" in (client.last_error or "").lower(), (
        f"rejection must name AGC, got {client.last_error!r}")

    assert client.set_agc(0), "could not return to manual AGC"
    assert client.set_gain(20.0), (
        f"gain rejected after manual AGC: {client.last_error}")


def test_agc_out_of_range_rejected(client: DaemonClient):
    assert not client.set_agc(3), "AGC mode 3 was accepted"
    assert "agc" in (client.last_error or "").lower(), (
        f"rejection must name AGC, got {client.last_error!r}")

    result = client.status()
    assert result is not None
    assert result[0].agc_mode <= 2


def test_frequency_outside_advertised_range_rejected(client: DaemonClient):
    caps = client.caps()
    assert caps is not None, "caps unavailable"
    assert caps.min_freq_mhz == 70 and caps.max_freq_mhz == 6000, (
        f"caps advertise {caps.min_freq_mhz}-{caps.max_freq_mhz} MHz")

    low = caps.min_freq_mhz * 1_000_000 - 1_000_000
    high = caps.max_freq_mhz * 1_000_000 + 1_000_000
    assert not client.set_freq(low), f"{low} Hz was accepted"
    assert not client.set_freq(high), f"{high} Hz was accepted"
    assert client.set_freq(DEFAULT_FREQ_HZ), "could not restore frequency"


def test_non_finite_gain_and_attenuation_rejected(client: DaemonClient):
    assert not client.set_gain(float("nan")), "NaN gain was accepted"
    assert not client.set_tx_atten(float("inf")), "+Inf attenuation accepted"
    assert not client.set_tx_atten(float("-inf")), "-Inf attenuation accepted"

    assert client.set_gain(20.0), "finite gain rejected"
    assert client.set_tx_atten(10.0), "finite attenuation rejected"


def test_fm_flags_repeat_and_interval_rejected(client: DaemonClient):
    before = client.status()
    assert before is not None
    rate_before = before[0].sample_rate_hz

    sub = _fm_sub()
    _assert_rejected(client, TxProtocol.FM, TxFlags.CYCLIC, 1, 0, sub,
                     "flags")
    _assert_rejected(client, TxProtocol.FM, 0x04, 1, 0, sub, "flags")
    _assert_rejected(client, TxProtocol.FM, TxFlags.NONE, 2, 0, sub,
                     "repeat")
    _assert_rejected(client, TxProtocol.FM, TxFlags.NONE, 1, 5_000, sub,
                     "interval")

    _assert_no_tx(client)
    after = client.status()
    assert after is not None
    assert after[0].sample_rate_hz == rate_before, (
        "a rejected FM TX changed the sample rate")


@pytest.mark.parametrize("flags", [TxFlags.STREAM, 0x04])
def test_wifi_stream_and_unknown_flags_rejected(client: DaemonClient, flags):
    # Deliberately not in WiFi mode: flag validation must fire first, so
    # the error is about flags rather than about mode or rate.
    _assert_rejected(client, TxProtocol.WIFI, flags, 1, 0, _wifi_sub(),
                     "flags")
    _assert_no_tx(client)


def test_wifi_unsupported_rate_rejected(client: DaemonClient):
    assert client.set_mode(DaemonMode.WIFI), "could not select wifi mode"
    assert client.set_fpga(False), "could not select the software producer"
    assert client.set_sample_rate(20_000_000), "could not set 20 MSPS"

    result = _raw_exchange(
        client,
        cmd_tx(TxProtocol.WIFI, TxFlags.NONE, 1, 0, _wifi_sub(7)),
        MsgType.CMD_TX)
    assert result is not None, "daemon did not answer CMD_TX"
    msg_type, payload = result
    message = payload[1:].decode("utf-8", "replace")
    assert msg_type == MsgType.RESP_ERROR, (
        "unsupported WiFi rate 7 Mbps was accepted")
    assert "build" in message.lower() or "rate" in message.lower(), (
        f"expected 'build' or 'rate' in rejection message, got {message!r}")
    _assert_no_tx(client)


def test_bw_change_allowed_during_iq_tx(client: DaemonClient):
    """Bandwidth does not move an independent-rate mode's clock.

    IQ keeps its sample rate across a bandwidth change, so the analog
    filter remains settable while a TX stream is active.
    """
    try:
        assert client.set_mode(DaemonMode.IQ), "could not select IQ mode"
        assert client.set_sample_rate(2_100_000), "could not set 2.1 MSPS"
        assert client.set_bw(2_100_000), (
            f"could not set 2.1 MHz IQ bandwidth: {client.last_error}")
        assert client.set_iq_bits(8), "could not select 8-bit IQ"
        assert client.iq_tx_stream_data(b"\x00" * (4096 * 2)) == PushResult.OK, \
            "first IQ chunk was rejected"

        assert client.set_bw(4_000_000), (
            "bandwidth change rejected during an independent-rate IQ TX: "
            f"{client.last_error}")

        assert client.iq_tx_stream_end(), "could not end the IQ stream"
        assert _wait_tx_idle(client), "IQ TX did not stop"
    finally:
        try:
            client.tx_stop()
        except Exception:
            pass
        try:
            client.set_mode(DaemonMode.WATERFALL)
            client.set_bw(20_000_000)
        except Exception:
            pass


def test_bw_change_moving_shared_clock_locked_by_concurrent_tx(
        client: DaemonClient):
    """A derived-rate mode moves the shared clock with bandwidth.

    Waterfall derives its sample rate from bandwidth.  An FM TX at the same
    20 MSPS does not suspend the producer (there is no rate conflict), so
    the session runs concurrently and a bandwidth whose derived rate would
    differ must be rejected.
    """
    try:
        assert client.set_mode(DaemonMode.WATERFALL), "could not select waterfall"
        assert client.set_bw(20_000_000), (
            f"could not restore 20 MSPS bandwidth: {client.last_error}")
        assert client.set_sample_rate(20_000_000), (
            f"could not set 20 MSPS: {client.last_error}")

        audio = [0.0] * (44_100 * 5)
        assert client.fm_tx(audio, dac_rate=20_000_000), (
            f"could not start the 20 MSPS FM TX: {client.last_error}")
        result = client.status()
        assert result is not None and result[0].tx_busy, (
            "FM TX did not start a session")

        assert not client.set_bw(10_000_000), (
            "bandwidth change accepted while it would move the TX clock")
        assert "locked by active tx" in (client.last_error or "").lower(), (
            f"expected the TX lock in the rejection, got {client.last_error!r}")

        assert client.tx_stop(), "could not stop the FM TX"
        assert _wait_tx_idle(client), "FM TX did not stop"
    finally:
        try:
            client.tx_stop()
        except Exception:
            pass
        try:
            client.set_mode(DaemonMode.WATERFALL)
            client.set_bw(20_000_000)
        except Exception:
            pass


def test_sample_rate_locked_by_active_tx(client: DaemonClient):
    try:
        assert client.set_mode(DaemonMode.IQ), "could not select IQ mode"
        assert client.set_sample_rate(2_100_000), "could not set 2.1 MSPS"
        assert client.set_iq_bits(8), "could not select 8-bit IQ"
        assert client.iq_tx_stream_data(b"\x00" * (4096 * 2)) == PushResult.OK, \
            "first IQ chunk was rejected"

        assert not client.set_sample_rate(2_500_000), (
            "sample-rate change accepted during an active IQ TX")
        assert "locked by active tx" in (client.last_error or "").lower(), (
            f"expected the TX lock in the rejection, got {client.last_error!r}")

        assert client.iq_tx_stream_end(), "could not end the IQ stream"
        assert _wait_tx_idle(client), "IQ TX did not stop"

        assert client.set_sample_rate(2_500_000), (
            f"rate change rejected after TX ended: {client.last_error}")
        assert client.set_sample_rate(2_100_000), "could not restore the rate"
    finally:
        try:
            client.tx_stop()
        except Exception:
            pass
        try:
            client.set_mode(DaemonMode.WATERFALL)
        except Exception:
            pass
