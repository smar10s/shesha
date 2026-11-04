"""Hardware test: IQ TX → 20 dB attenuated loopback → IQ RX.

Full-duplex at the shared clock: stream a sine via the IQ TX path while
concurrently capturing DATA_IQ frames, then verify the tone shows up at
the expected frequency.  Excluded from normal test runs (needs the Pluto
with the loopback cable).
"""
import math
import struct

from shesha.client import DaemonClient, PushResult
from shesha.protocol import DaemonMode, SUBSCRIBE_IQ

FREQ_HZ = 2_400_000_000
# 2.5 MSPS: above the AD9361 driver's 2.0833 MHz minimum sampling rate
# (25 MHz ADC clock / 12) — 2.0 MSPS is rejected by the hardware.
SR_HZ = 2_500_000
BITS = 12
TONE_HZ = 100_000
AMPLITUDE = 1000          # int12 full scale is 2047; leave headroom
TX_SECONDS = 1.5
CAPTURE_SECONDS = 2.0


def _pack_sine(bits, sr, tone_hz, amp, n) -> bytes:
    out = bytearray()
    for i in range(n):
        phase = 2.0 * math.pi * tone_hz * i / sr
        re = int(round(amp * math.cos(phase)))
        im = int(round(amp * math.sin(phase)))
        re = max(-2048, min(2047, re))
        im = max(-2048, min(2047, im))
        if bits == 8:
            out.append((re >> 4) & 0xFF)
            out.append((im >> 4) & 0xFF)
        else:
            re &= 0xFFF
            im &= 0xFFF
            out.append(re & 0xFF)
            out.append(((re >> 8) & 0xF) | ((im & 0xF) << 4))
            out.append((im >> 4) & 0xFF)
    return bytes(out)


def _unpack_samples(frames, bits) -> list[complex]:
    out = []
    for f in frames:
        b = f.samples
        if bits == 8:
            for i in range(0, len(b) - 1, 2):
                re = struct.unpack('b', b[i:i + 1])[0] << 4
                im = struct.unpack('b', b[i + 1:i + 2])[0] << 4
                out.append(complex(re, im))
        else:
            for i in range(0, len(b) - 2, 3):
                v = b[i] | (b[i + 1] << 8) | (b[i + 2] << 16)
                re = v & 0xFFF
                if re & 0x800:
                    re -= 0x1000
                im = (v >> 12) & 0xFFF
                if im & 0x800:
                    im -= 0x1000
                out.append(complex(re, im))
    return out


def _goertzel(x, k):
    """Power at bin k over all of x (bin width = SR_HZ / len(x)).

    Returns the real magnitude-squared |X(k)|^2 / n^2 — `x` is complex,
    so a naive s1*s1 would leave an imaginary residue.
    """
    w = 2.0 * math.pi * k / len(x)
    cw = 2.0 * math.cos(w)
    s1 = s2 = 0j
    for v in x:
        s0 = v + cw * s1 - s2
        s2 = s1
        s1 = s0
    power = (abs(s1) ** 2 + abs(s2) ** 2
             - cw * (s1 * s2.conjugate()).real)
    return power / (len(x) * len(x))


def test_iq_loopback_tone():
    c = DaemonClient()
    c.connect()

    try:
        st, mode = c.status()
        assert st is not None

        assert c.set_mode(DaemonMode.IQ)
        assert c.set_sample_rate(SR_HZ)
        assert c.set_iq_bits(BITS)
        assert c.set_freq(FREQ_HZ)
        assert c.set_agc(0)
        assert c.set_gain(40.0)
        assert c.set_tx_atten(20.0)
        c.subscribe(SUBSCRIBE_IQ)

        # A frame-less capture is ambiguous: the producer may never have
        # started, or the loopback may be silent.  Prove the producer is live
        # before transmitting so the assertion below can only mean no signal.
        st, mode = c.status()
        assert st is not None, "status unavailable"
        assert mode == DaemonMode.IQ and st.rx_active, \
            "IQ producer is not running"
        assert st.sample_rate_hz == SR_HZ, (
            f"sample rate is {st.sample_rate_hz}, expected {SR_HZ}")

        payload = _pack_sine(BITS, SR_HZ, TONE_HZ, AMPLITUDE,
                             int(SR_HZ * TX_SECONDS))
        bps = 3 if BITS == 12 else 2
        chunk = 4096 * bps
        sent = 0
        while sent < len(payload):
            end = min(sent + chunk, len(payload))
            while True:
                r = c.iq_tx_stream_data(payload[sent:end])
                if r == PushResult.OK:
                    break
                if r == PushResult.RETRY:
                    c.wait(0.01)
                    continue
                raise AssertionError(
                    f"push failed at {sent}: {c.last_error}")
            sent = end
        assert c.iq_tx_stream_end()

        # Drain stale frames, then capture.
        c.recv_raw(timeout=0.0)
        frames = list(c.iq_rx(duration=CAPTURE_SECONDS))

        samples = _unpack_samples(frames, BITS)
        assert len(samples) > SR_HZ * 0.5, f"only {len(samples)} samples"

        # Trim edges, take a stable window.
        x = samples[2000:2000 + 100000]
        k = round(TONE_HZ * len(x) / SR_HZ)
        power = _goertzel([s.real + 1j * s.imag for s in x], k)
        total = sum(abs(s) ** 2 for s in x) / len(x)

        # The tone bin must dominate the total power.
        assert power > 1000, f"tone bin power {power:.0f} too low"
        assert power / max(total, 1.0) > 0.1, \
            f"tone fraction {power / max(total, 1.0):.3f} too low"
    finally:
        c.tx_stop()
        c.disconnect()
