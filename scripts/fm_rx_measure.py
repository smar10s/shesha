#!/usr/bin/env python3
"""
Measure delivered FM RX audio from a real broadcast station.

This is the hardware half of the decimator verification. The host unit tests
(firmware/tests/test_fm_rx.c) assert the stopband against a synthetic
multiplex, which is deterministic and runs everywhere; this asserts it against
whatever a real transmitter is actually radiating, which is the only thing that
carries a real pilot, real stereo subcarrier, real RDS and real adjacent
channels.

What it reports, and why each number is here:

  realtime        delivered samples / wall clock. Catches a producer that
                  cannot keep up, which no spectral measurement would show.
  short frames    frames whose n_samples is not the full ring slot. Used to
                  be every frame: at the old decimation of 47 a 49152-sample
                  chunk produced 1045 audio samples into a 1024 slot and 21
                  were silently dropped.
  DC offset       the audio has one and nothing removes it, so it is
                  subtracted before any energy figure below.
  band shares     the headline. The old chain put 40.3% of energy in 8-15 kHz
                  and 29.4% above 15 kHz against 18% below 3 kHz.
  alias probes    energy at the exact frequencies the subcarriers fold to,
                  relative to the 0-3 kHz programme band. Narrow-band, so it
                  attributes a defect rather than just detecting one.

Usage:
    python3 scripts/fm_rx_measure.py --freq 96.7 --dur 8
    python3 scripts/fm_rx_measure.py --freq 96.7 --dur 8 --wav /tmp/fm.wav
"""
import argparse
import math
import os
import sys
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'client'))
from shesha.client import DaemonClient          # noqa: E402
from shesha.protocol import DaemonMode          # noqa: E402


def goertzel_power(x, freq, fs):
    """Power at one frequency, Hann windowed.

    Hann rather than rectangular because the programme material is 30-40 dB
    louder than the aliases being probed, and a rectangular window's leakage
    would dominate every probe.
    """
    n = len(x)
    if n < 4:
        return 0.0
    k = 2.0 * math.cos(2.0 * math.pi * freq / fs)
    s1 = s2 = 0.0
    wsum = 0.0
    for i, v in enumerate(x):
        w = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1))
        s0 = w * v + k * s1 - s2
        s2, s1 = s1, s0
        wsum += w
    p = s1 * s1 + s2 * s2 - k * s1 * s2
    return max(0.0, p) / (wsum * wsum)


def band_power(x, lo, hi, fs, step=250.0):
    acc = 0.0
    f = lo + step / 2.0
    while f < hi:
        acc += goertzel_power(x, f, fs)
        f += step
    return acc


def save_wav(path, samples, rate):
    import array
    peak = max((abs(s) for s in samples), default=0.0) or 1.0
    with wave.open(path, 'w') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(round(rate)))
        w.writeframes(array.array('h', [
            int(max(-32768, min(32767, s / peak * 32767))) for s in samples
        ]).tobytes())
    return peak


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.2.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--freq", type=float, required=True,
                    help="station centre frequency in MHz")
    ap.add_argument("--dur", type=float, default=8.0)
    ap.add_argument("--wav", help="also write the audio here")
    args = ap.parse_args()

    c = DaemonClient(args.host, args.port)
    c.connect()
    try:
        c.set_freq(int(args.freq * 1e6))
        time.sleep(0.2)
        if not c.set_mode(DaemonMode.FM):
            print("FAIL: could not enter FM mode")
            return 1
        # Let the producer's read pointer and the demodulator's envelope
        # estimator settle before the window that gets measured.
        time.sleep(1.0)

        # Drain the audio ring before starting the clock. Without this the
        # settle above shows up as delivered audio that was captured before
        # t0, and the realtime figure reads ~112% -- which looks like the
        # producer running fast and is really just a backlog.
        c.stream(duration=0.5)

        t0 = time.monotonic()
        result = c.stream(duration=args.dur)
        wall = time.monotonic() - t0
    finally:
        c.disconnect()

    frames = result.fm_frames
    if not frames:
        print("FAIL: no DATA_FM frames received")
        return 1

    rate = frames[0].audio_rate_hz
    rates = {f.audio_rate_hz for f in frames}
    audio = [s for f in frames for s in f.audio]
    short = [f for f in frames if len(f.audio) != 1024]

    print(f"station          {args.freq} MHz")
    print(f"frames           {len(frames)}")
    print(f"reported rate    {rate} Hz" +
          ("" if len(rates) == 1 else f"  MIXED: {sorted(rates)}"))
    print(f"samples          {len(audio)}")
    print(f"wall clock       {wall:.2f} s")
    print(f"realtime         {len(audio) / rate / wall * 100:.2f}% "
          f"({len(audio) / rate:.2f} s of audio)")
    print(f"short frames     {len(short)} of {len(frames)}"
          + ("" if not short
             else f"   sizes {sorted({len(f.audio) for f in short})}"))

    # Drop the first half second: the FIR histories and the envelope
    # estimator are still filling.
    skip = int(0.5 * rate)
    x = audio[skip:]
    if len(x) < rate:
        print("FAIL: too little audio to measure")
        return 1

    mean = sum(x) / len(x)
    peak = max(abs(v) for v in x)
    rms = math.sqrt(sum((v - mean) ** 2 for v in x) / len(x))
    print(f"DC offset        {mean:+.4f}")
    print(f"peak / rms       {peak:.4f} / {rms:.4f}")

    ac = [v - mean for v in x]

    print()
    print("Band energy share of delivered audio")
    bands = [(0, 3000), (3000, 8000), (8000, 15000), (15000, rate / 2)]
    vals = [band_power(ac, lo, hi, rate) for lo, hi in bands]
    tot = sum(vals) or 1.0
    for (lo, hi), v in zip(bands, vals):
        print(f"  {lo/1000:>5.1f} - {hi/1000:>5.1f} kHz   {100*v/tot:>5.1f}%")
    print()
    print("  Energy above 15 kHz is the single cleanest number here: an FM")
    print("  transmitter band-limits programme audio to 15 kHz, so anything")
    print("  delivered above it is the pilot or an alias, never programme.")
    print("  Baseline with the old boxcar(47), 8 s of 96.7 MHz "
          "(docs/fm-rx.md):")
    print("  18% below 3 kHz, 40.3% in 8-15 kHz, 29.4% above 15 kHz.")

    print()
    print("Narrow probes, dB relative to the 0-3 kHz programme band")
    ref = vals[0] or 1e-30
    # Where each subcarrier lands after decimation to `rate`. At 43750 Hz:
    # 35k->8750, 41k->2750, 57k->13250; the 19 kHz pilot stays put.
    probes = [("19 kHz pilot", 19000.0, True),
              ("L-R lower (35 kHz)", abs(rate - 35000.0), False),
              ("L-R upper (41 kHz)", abs(rate - 41000.0), False),
              ("RDS (57 kHz)", abs(57000.0 - rate), False)]
    for name, f, diagnostic in probes:
        if f >= rate / 2:
            print(f"  {name:<22} lands at {f:>7.0f} Hz  above Nyquist, n/a")
            continue
        p = goertzel_power(ac, f, rate)
        print(f"  {name:<22} lands at {f:>7.0f} Hz  "
              f"{10*math.log10((p + 1e-30)/ref):>7.1f} dB"
              + ("" if diagnostic else "   (see note)"))
    print()
    print("  Only the pilot probe is diagnostic on real programme material.")
    print("  19 kHz is above the transmitter's 15 kHz audio limit, so nothing")
    print("  legitimate can be there and the figure is pure leakage. The")
    print("  other three land inside the audio band, where real music has")
    print("  its own energy, so they measure programme plus alias and cannot")
    print("  separate them. They are worth watching for a sudden jump, not")
    print("  for an absolute level. The synthetic multiplex in")
    print("  firmware/tests/test_fm_rx.c is what pins those three, because")
    print("  there the only thing at those frequencies IS the alias.")

    if args.wav:
        pk = save_wav(args.wav, x, rate)
        print()
        print(f"wrote {args.wav} ({len(x)} samples at {rate} Hz, "
              f"normalised by {pk:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
