#!/usr/bin/env python3
"""Measure the PSD producer's cost against the live daemon.

The on-device tools (wf_cover, wf_workers) take over the radio, so they
measure the pipeline but not the daemon running it.  This measures the daemon
itself, which is what a refactor of the capture path has to leave unchanged.

What it reports:
  - producer duty, per thread, from the firmware's own /proc sampling
  - delivered frame rate, which the fps knob gates
  - PSD coverage from RESP_STATUS, the share of the sample stream transformed
  - the wire frame index slope, as a sanity check

Coverage is on the wire now: RESP_STATUS carries the producer's
transformed/skipped counters as a fraction.  Do not derive it from the frame
index: that index counts frame-sized blocks including ones the producer
abandoned, so its slope is always fs/N whether coverage is 100% or 25%.
Use `make bench-cover` for a controlled measurement.

Usage:
    python3 scripts/psd_baseline.py [--host 192.168.2.1] [--sec 5]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

from shesha.client import DaemonClient  # noqa: E402
from shesha.protocol import MsgType  # noqa: E402

# (sample rate, FFT size): below fs_max, at it, and above it.
POINTS = [
    (2_100_000, 1024),
    (2_100_000, 4096),
    (5_000_000, 4096),
    (10_000_000, 4096),
    (20_000_000, 1024),
    (20_000_000, 4096),
    (20_000_000, 8192),
]


def thread_duty(before, after, name_filter):
    """Per-thread CPU as a percentage of one core, between two samples."""
    if not before or not after:
        return {}
    wall_s = (after.wall_us - before.wall_us) / 1e6
    if wall_s <= 0:
        return {}
    prev = {t.tid: t for t in before.threads}
    out = {}
    for t in after.threads:
        if name_filter not in t.name:
            continue
        p = prev.get(t.tid)
        if p is None:
            continue
        jiffies = (t.utime_jiffies + t.stime_jiffies) - (
            p.utime_jiffies + p.stime_jiffies
        )
        out[t.name] = jiffies / after.clk_tck / wall_s * 100.0
    return out


def measure(c, seconds):
    """Duty per producer thread, delivered fps, and frame-index slope."""
    c.subscribe(15)
    c.wait(0.3)

    ts0 = c.thread_stats()
    idx0 = t0 = None
    delivered = 0
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        for msg_type, _ in c.recv_raw(timeout=0.2):
            if msg_type == MsgType.DATA_PSD:
                delivered += 1
        if c.psd_frame_index is not None and idx0 is None:
            idx0, t0 = c.psd_frame_index, time.monotonic()
    idx1, t1 = c.psd_frame_index, time.monotonic()
    ts1 = c.thread_stats()

    elapsed = time.monotonic() - start
    slope = 0.0
    if idx0 is not None and idx1 is not None and t1 > t0:
        slope = (idx1 - idx0) / (t1 - t0)
    return thread_duty(ts0, ts1, "wf-producer"), delivered / elapsed, slope


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.2.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--sec", type=float, default=5.0)
    args = ap.parse_args()

    c = DaemonClient(args.host, args.port)
    c.connect()

    print(f"PSD producer cost, live daemon at {args.host}, {args.sec:.0f} s/point")
    print("duty is CPU as a share of ONE core; idx/s should equal fs/N\n")
    print(f"{'fs(MSPS)':>9} {'N':>6} {'duty':>8} {'sent fps':>9} "
          f"{'cover':>6} {'idx/s':>8} {'fs/N':>8}  threads")

    for rate, fft in POINTS:
        c.set_bw(rate)
        c.wait(0.6)
        c.set_fft_size(fft)
        c.wait(1.2)

        st, _ = c.status()
        if st.fft_size != fft:
            print(f"  !! fft size is {st.fft_size}, wanted {fft}")

        duty, sent_fps, slope = measure(c, args.sec)
        end, _ = c.status()
        total = sum(duty.values())
        expect = st.sample_rate_hz / st.fft_size
        detail = " ".join(f"{k}={v:.0f}%" for k, v in sorted(duty.items()))
        print(f"{st.sample_rate_hz/1e6:9.1f} {st.fft_size:6d} {total:7.1f}% "
              f"{sent_fps:9.1f} {end.psd_coverage*100:5.1f}% "
              f"{slope:8.0f} {expect:8.0f}  {detail}")

    c.disconnect()


if __name__ == "__main__":
    main()
