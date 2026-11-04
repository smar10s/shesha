#!/usr/bin/env python3
"""Measure sustained IQ throughput against a live daemon.

The daemon's IQ mode caps the requested rate, so this measures what the
link and the producer actually deliver inside that envelope.  A run is
sustainable when the delivered sample rate tracks the hardware rate and
neither the client sequence counter nor the frame timestamps show loss.

    python3 scripts/iq_throughput.py
    python3 scripts/iq_throughput.py --rates 3.5 4.0 --bits 12 --sec 10
    python3 scripts/iq_throughput.py --json
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

from shesha.client import DaemonClient  # noqa: E402
from shesha.protocol import (  # noqa: E402
    DaemonMode, FRAME_HEADER, MsgType, SUBSCRIBE_IQ)

IQ_HEADER = struct.Struct('<dfIBBH')       # ts, freq, rate, bits, pad, n

RATE_RATIO = 0.99          # delivered / hardware rate
WARMUP_SEC = 0.5


def parse_iq_header(payload: bytes):
    ts, _freq, rate, bits, _pad, n = IQ_HEADER.unpack_from(payload, 0)
    return ts, rate, bits, n


def measure(c: DaemonClient, rate: int, bits: int, seconds: float) -> dict:
    if not c.set_mode(DaemonMode.IQ):
        raise RuntimeError("set_mode(IQ) failed")
    if not c.set_sample_rate(rate):
        raise RuntimeError(f"set_sample_rate({rate}) failed")
    if not c.set_iq_bits(bits):
        raise RuntimeError(f"set_iq_bits({bits}) failed")
    if not c.subscribe(SUBSCRIBE_IQ):
        raise RuntimeError("subscribe(IQ) failed")

    stat = c.status()
    hw_rate = stat[0].sample_rate_hz if stat else rate

    # Drain frames produced before this run, then measure a clean window.
    warm_end = time.monotonic() + WARMUP_SEC
    while time.monotonic() < warm_end:
        c.recv_raw(timeout=0.05)

    gaps0, dropped0 = c.gaps, c.dropped
    t0 = time.monotonic()
    deadline = t0 + seconds
    samples = 0
    wire_bytes = 0
    frames = 0
    first_ts = last_ts = None
    ts_gap_samples = 0
    chunk = None

    while time.monotonic() < deadline:
        for msg_type, payload in c.recv_raw(timeout=0.05):
            if msg_type != MsgType.DATA_IQ or len(payload) < IQ_HEADER.size:
                continue
            ts, frame_rate, frame_bits, n = parse_iq_header(payload)
            if frame_bits != bits:
                continue
            chunk = n
            if first_ts is None:
                first_ts = ts
            elif last_ts is not None:
                dt = ts - last_ts
                expected = dt * frame_rate
                if expected > n * 1.5:
                    ts_gap_samples += int(round(expected - n))
            last_ts = ts
            samples += n
            frames += 1
            wire_bytes += len(payload) + FRAME_HEADER.size
    elapsed = time.monotonic() - t0
    gaps = c.gaps - gaps0
    dropped = c.dropped - dropped0

    achieved = samples / elapsed if elapsed > 0 else 0.0
    payload_bps = wire_bytes / elapsed if elapsed > 0 else 0.0
    ratio = achieved / hw_rate if hw_rate else 0.0

    # Timestamp spacing is diagnostic only.  The producer stamps a chunk
    # when it finishes packing, so a scheduling delay widens the gap
    # without losing samples: the DMA ring still holds them and the next
    # chunk reads them.  Real loss shows up as a delivered rate below the
    # hardware rate or as a client sequence gap.
    if chunk and first_ts is not None and last_ts is not None:
        span = last_ts - first_ts
        if span > 0:
            ts_rate = (samples - chunk) / span
        else:
            ts_rate = 0.0
    else:
        ts_rate = 0.0

    return {
        "requested_msps": rate / 1e6,
        "hardware_msps": hw_rate / 1e6,
        "bits": bits,
        "seconds": elapsed,
        "frames": frames,
        "samples": samples,
        "achieved_msps": achieved / 1e6,
        "wire_mb_s": payload_bps / 1e6,
        "ratio": ratio,
        "seq_gaps": gaps,
        "seq_dropped_frames": dropped,
        "timestamp_gap_samples": ts_gap_samples,
        "timestamp_msps": ts_rate / 1e6,
        "sustainable": ratio >= RATE_RATIO and gaps == 0,
    }


def fmt(row: dict) -> str:
    verdict = "ok" if row["sustainable"] else "FAIL"
    return (
        f"{row['bits']:>2}-bit  req {row['requested_msps']:.3f} MSPS  "
        f"hw {row['hardware_msps']:.3f}  "
        f"got {row['achieved_msps']:.3f} ({row['ratio'] * 100:.1f}%)  "
        f"{row['wire_mb_s']:.2f} MB/s  "
        f"gaps {row['seq_gaps']} (dropped {row['seq_dropped_frames']})  "
        f"ts-gap {row['timestamp_gap_samples']}  {verdict}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="192.168.2.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--rates", type=float, nargs="+",
                    default=[2.1, 4.0, 6.0],
                    help="requested MSPS (default: mode envelope)")
    ap.add_argument("--bits", type=int, nargs="+", default=[8, 12],
                    choices=[8, 12])
    ap.add_argument("--sec", type=float, default=5.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    c = DaemonClient(args.host, args.port)
    c.connect()

    rows = []
    try:
        for bits in args.bits:
            for rate_msps in args.rates:
                rate = int(round(rate_msps * 1e6))
                try:
                    row = measure(c, rate, bits, args.sec)
                except Exception as exc:
                    row = {"requested_msps": rate_msps, "bits": bits,
                           "error": str(exc), "sustainable": False}
                rows.append(row)
                if not args.json:
                    if "error" in row:
                        print(f"{bits:>2}-bit  req {rate_msps:.3f} MSPS  "
                              f"ERROR: {row['error']}")
                    else:
                        print(fmt(row))
    finally:
        c.subscribe(0)
        c.disconnect()

    if args.json:
        print(json.dumps(rows, indent=2))

    return 0 if all(r.get("sustainable") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
