"""FM TX stop paths — the transmitter must never outlive its client.

Two ways a stream can be orphaned, both of which used to leave the DAC
radiating pad samples forever:

1. The client that started the stream disconnects without sending any
   stop.  The daemon now tracks the stream owner and stops on disconnect.
2. The client sends CMD_TX_STOP and closes immediately.  The STOP can
   arrive in the same poll as POLLERR/POLLHUP; the daemon now reads
   before removing the client, so the command is not discarded.

Each test starts a stream from a throwaway socket, tears the socket
down, and asserts a separate observer client sees tx_busy clear.  The
last test goes further and checks the RF: a stopped transmitter must not
leave the DAC holding its final sample as a carrier.

Antennas or a loopback cable both work.
"""
import socket
import subprocess
import time

from shesha.client import DaemonClient
from shesha.protocol import MsgType, cmd_tx_fm_stream_start, cmd_tx_stop

from tests.hardware.conftest import (
    PLUTO_HOST, PLUTO_PASS, PLUTO_PORT, PLUTO_USER, REPO_ROOT,
)

RX_LO_PATH = "/sys/bus/iio/devices/iio:device1/out_altvoltage0_RX_LO_frequency"
SHUTDOWN_FREQ_HZ = 100_100_000
SHUTDOWN_OFFSET_HZ = 100_000


def _ssh(command: str, timeout: float = 30.0):
    argv = [
        "sshpass", "-p", PLUTO_PASS,
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=3",
        f"{PLUTO_USER}@{PLUTO_HOST}", command,
    ]
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout)


def _ensure_rx_probe() -> None:
    if _ssh("test -x /usr/bin/rx_probe").returncode == 0:
        return
    result = subprocess.run(
        ["make", "deploy-tools", "TOOLS=rx_probe"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, (
        "could not deploy rx_probe:\n" + result.stdout + result.stderr)


def _wait_daemon_gone(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _ssh("pidof shesha-daemon").stdout.strip():
            return True
        time.sleep(0.05)
    return False


def _start_daemon() -> None:
    subprocess.run(["make", "run"], cwd=REPO_ROOT, capture_output=True,
                   text=True, check=True)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection((PLUTO_HOST, PLUTO_PORT), timeout=0.5)
            s.close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("daemon did not come back after shutdown test")


def _rx_probe(offset_hz: int, rate_hz: int, samples: int = 16384) -> float:
    result = _ssh(f"rx_probe {offset_hz} {rate_hz} {samples}")
    assert result.returncode == 0, (
        "rx_probe failed:\n" + result.stdout + result.stderr)
    return float(result.stdout.strip())

AUDIO_RATE = 44100
START_SAMPLES = 4410          # 100 ms of audio, ring never runs dry
IDLE_TIMEOUT_S = 5.0
# A constant audio sample puts the FM carrier at a known offset.  The RX
# DC-tracking loop can cancel a DC carrier, and the TX/RX LO phase drifts
# a few Hz, so a DC measurement is not a reliable "carrier is up" signal.
AUDIO_VALUE = 0.5
TONE_HZ = 75_000.0 * AUDIO_VALUE


def _open_raw() -> socket.socket:
    s = socket.create_connection((PLUTO_HOST, PLUTO_PORT), timeout=2.0)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def _start_frame() -> bytes:
    return cmd_tx_fm_stream_start([0.0] * START_SAMPLES,
                                  audio_rate=AUDIO_RATE)


def _wait_tx_idle(observer: DaemonClient, timeout: float = IDLE_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = observer.status()
        if result is not None and not result[0].tx_busy:
            return True
        time.sleep(0.1)
    return False


def test_abrupt_disconnect_stops_orphaned_stream():
    """Start a stream, vanish without a stop, transmitter must go idle."""
    observer = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    observer.connect()
    try:
        raw = _open_raw()
        raw.sendall(_start_frame())
        time.sleep(0.3)          # let the daemon mark tx_busy

        # Replies (RESP_OK + status push) are deliberately left unread,
        # so close() sends RST rather than a clean FIN.
        raw.close()

        assert _wait_tx_idle(observer), \
            "TX stayed busy after the owning client disconnected"
    finally:
        observer.disconnect()


def test_stop_then_close_stops_stream():
    """CMD_TX_STOP sent immediately before close must still be honoured."""
    observer = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    observer.connect()
    try:
        raw = _open_raw()
        raw.sendall(_start_frame())
        time.sleep(0.3)

        # STOP and close back-to-back: the command may be queued behind
        # unread replies when the RST arrives.
        raw.sendall(cmd_tx_stop())
        raw.close()

        assert _wait_tx_idle(observer), \
            "TX stayed busy after STOP-then-close"
    finally:
        observer.disconnect()


def _unpack12(data: bytes, n: int) -> tuple[list[int], list[int]]:
    I, Q = [], []
    for k in range(n):
        b0, b1, b2 = data[3 * k], data[3 * k + 1], data[3 * k + 2]
        i = b0 | ((b1 & 0x0F) << 8)
        q = (b1 >> 4) | (b2 << 4)
        if i & 0x800:
            i -= 0x1000
        if q & 0x800:
            q -= 0x1000
        I.append(i)
        Q.append(q)
    return I, Q


def _iq_tone(c: DaemonClient, freq_hz: float, seconds: float = 0.05) -> float:
    """|mean IQ * exp(-j2pi f t)| — a radiated FM tone at f.

    A DC carrier is a poor reference here: the AD9361's RX DC-tracking
    loop can cancel it over the measurement window, and the TX/RX LO
    phase can drift a few Hz.  A constant non-zero audio sample puts the
    FM carrier at a known offset, where the tracking loop cannot remove it.
    """
    import cmath
    import math
    from shesha.protocol import parse_iq, IQ_BITS_12
    acc = 0j
    n = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        for mt, payload in c.recv_raw(timeout=0.05):
            if mt != MsgType.DATA_IQ:
                continue
            f = parse_iq(payload)
            if f.bits != IQ_BITS_12:
                continue
            i, q = _unpack12(f.samples, f.n_samples)
            sr = f.sample_rate_hz or 1
            for k, (re, im) in enumerate(zip(i, q)):
                t = f.timestamp + k / sr
                acc += complex(re, im) * cmath.exp(
                    -2j * math.pi * freq_hz * t)
                n += 1
    return abs(acc) / n if n else 0.0


def _iq_tone_median(c: DaemonClient, freq_hz: float, n: int = 5) -> float:
    """Median tone amplitude.  A few-Hz TX/RX LO offset beats against a
    single window; the median is stable and still ignores a missing tone."""
    values = sorted(_iq_tone(c, freq_hz) for _ in range(n))
    return values[n // 2]


def _iq_dc(c: DaemonClient, seconds: float = 0.25) -> float:
    """|mean IQ| — a held (constant) DAC word appears as a DC phasor."""
    from shesha.protocol import parse_iq, IQ_BITS_12
    I, Q = [], []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        for mt, payload in c.recv_raw(timeout=0.05):
            if mt == MsgType.DATA_IQ:
                f = parse_iq(payload)
                if f.bits == IQ_BITS_12:
                    i, q = _unpack12(f.samples, f.n_samples)
                    I.extend(i)
                    Q.extend(q)
    if not I:
        return 0.0
    mi = sum(I) / len(I)
    mq = sum(Q) / len(Q)
    return (mi * mi + mq * mq) ** 0.5


def test_stop_clears_held_carrier():
    """After stop the DAC must not hold its last word as a carrier.

    The FPGA drain FSM latches its final sample; the daemon plays a short
    zero burst so the held word is 0.  Verified as a DC phasor in a
    rate-matched IQ capture: large with the carrier up, ~0 after stop.
    """
    from shesha.protocol import DaemonMode, SUBSCRIBE_IQ

    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    try:
        assert c.set_freq(100_100_000)
        # Switch first: the rate setter validates against the active
        # producer, so setting 2.1 MSPS while in a 20 MSPS mode is rejected.
        assert c.set_mode(DaemonMode.IQ)
        assert c.set_sample_rate(2_100_000)
        assert c.set_agc(0)
        assert c.set_gain(30.0)
        # Explicit: a previous test may leave 20 dB here, which drops the
        # loopback carrier below the detection threshold.
        assert c.set_tx_atten(10.0)
        assert c.set_iq_bits(12)
        time.sleep(3.0)                  # let the RX DC offset settle

        # Subscribe only after the settle so no pre-TX frames are queued.
        assert c.subscribe(SUBSCRIBE_IQ)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if any(mt == MsgType.DATA_IQ
                   for mt, _pl in c.recv_raw(timeout=0.2)):
                break
        else:
            raise AssertionError("no DATA_IQ frames before TX")

        audio = [AUDIO_VALUE] * (AUDIO_RATE * 3)
        assert c.fm_tx_stream_start(audio[:AUDIO_RATE],
                                    audio_rate=AUDIO_RATE,
                                    dac_rate=2_100_000)
        c.wait(0.6)
        c.fm_tx_stream_push(audio[:AUDIO_RATE])
        c.wait(0.5)

        _iq_dc(c, 0.3)                   # discard pre-TX backlog
        tone_on = _iq_tone_median(c, TONE_HZ)
        assert tone_on > 100, \
            f"FM tone not radiated during TX (tone={tone_on:.1f})"

        assert c.tx_stop()
        # Keep draining while the DAC flushes and the RX DC loop re-converges.
        # Sleeping here would queue pre-stop carrier frames ahead of the
        # measurement.
        c.wait(3.0)
        _iq_dc(c, 0.3)                   # discard the settling tail
        dc_off = _iq_dc(c)
        tone_off = _iq_tone_median(c, TONE_HZ)
        assert dc_off < 200, f"held DC carrier after stop (off={dc_off:.1f})"
        assert tone_off < 100, \
            f"FM tone still radiated after stop (tone={tone_off:.1f})"
    finally:
        c.set_mode(DaemonMode.WATERFALL)
        c.disconnect()

def test_shutdown_clears_held_carrier():
    """SIGTERM during a live stream must not leave a carrier after exit.

    The RX LO is shifted by a known offset before shutdown, so a held DAC
    word appears as a tone at that offset instead of at DC, where the
    AD9361's DC tracking could hide it.  rx_probe then starts only the RX
    DMA: it deliberately does not retune the radio or change the sample
    clock, because either could mask the held word.

    The TX is verified live through the daemon's own IQ stream first, so a
    silent or failed stream cannot make this test pass trivially.

    This is an invariant guard, not an old-vs-new regression: the pre-fix
    daemon already measured clean in this path.  It exists so a future stop
    or shutdown change cannot silently leave the DAC holding a word.
    """
    from shesha.protocol import DaemonMode, SUBSCRIBE_IQ

    _ensure_rx_probe()

    c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
    c.connect()
    try:
        assert c.tx_stop(), "could not clear any leftover TX"
        assert c.set_freq(SHUTDOWN_FREQ_HZ), c.last_error
        assert c.set_mode(DaemonMode.IQ), c.last_error
        assert c.set_sample_rate(2_100_000), c.last_error
        assert c.set_agc(0), c.last_error
        assert c.set_gain(30.0), c.last_error
        assert c.set_tx_atten(10.0), c.last_error
        time.sleep(3.0)

        assert c.subscribe(SUBSCRIBE_IQ)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if any(mt == MsgType.DATA_IQ
                   for mt, _pl in c.recv_raw(timeout=0.2)):
                break
        else:
            raise AssertionError("no DATA_IQ frames before TX")

        audio = [AUDIO_VALUE] * (AUDIO_RATE * 3)
        assert c.fm_tx_stream_start(audio[:AUDIO_RATE],
                                    audio_rate=AUDIO_RATE,
                                    dac_rate=2_100_000)
        c.wait(0.6)
        c.fm_tx_stream_push(audio[:AUDIO_RATE])
        c.wait(0.5)

        _iq_dc(c, 0.3)                   # discard pre-TX backlog
        tone_on = _iq_tone_median(c, TONE_HZ)
        assert tone_on > 100, \
            f"FM tone not radiated before shutdown (tone={tone_on:.1f})"

        # Shift only the RX LO.  The TX LO, and therefore any held DAC
        # word, stays at SHUTDOWN_FREQ_HZ.
        assert _ssh(
            f"echo {SHUTDOWN_FREQ_HZ + SHUTDOWN_OFFSET_HZ} > {RX_LO_PATH}"
        ).returncode == 0, "could not offset the RX LO"
    finally:
        c.disconnect()

    try:
        assert _ssh("killall shesha-daemon").returncode == 0, \
            "could not signal the daemon"
        assert _wait_daemon_gone(), "daemon did not exit after SIGTERM"
        time.sleep(0.2)

        held = _rx_probe(SHUTDOWN_OFFSET_HZ, 2_100_000)
        assert held < 1e-3, (
            f"carrier still held after daemon shutdown (tone={held:.6f})")
    finally:
        _start_daemon()
        c = DaemonClient(PLUTO_HOST, PLUTO_PORT)
        c.connect()
        try:
            c.tx_stop()
            c.set_freq(2_457_000_000)
            c.set_mode(DaemonMode.WATERFALL)
        finally:
            c.disconnect()
