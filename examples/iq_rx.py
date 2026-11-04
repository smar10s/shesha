#!/usr/bin/env python3
"""Print IQ frame metadata from a running shesha daemon.

This is the smallest useful protocol client: connect, select a mode, tune,
subscribe, read frames. Run it against a Pluto, or against the offline TUI
dummy daemon if you only want to see the shapes.

    python3 examples/iq_rx.py --freq 90.1 --frames 3
"""

import argparse

from shesha.client import DaemonClient
from shesha.protocol import DaemonMode, SUBSCRIBE_IQ


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.2.1")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--freq", type=float, default=90.1, help="center, MHz")
    parser.add_argument("--rate", type=float, default=2.1, help="MSPS")
    parser.add_argument("--frames", type=int, default=3)
    args = parser.parse_args()

    client = DaemonClient(args.host, args.port)
    client.connect()
    try:
        client.set_mode(DaemonMode.IQ)
        client.set_freq(round(args.freq * 1e6))
        client.set_sample_rate(round(args.rate * 1e6))
        client.subscribe(SUBSCRIBE_IQ)

        seen = 0
        for frame in client.iq_rx():
            print(
                f"{frame.timestamp:.3f}s  {frame.freq_mhz:.3f} MHz  "
                f"{frame.sample_rate_hz / 1e6:.3f} MSPS  "
                f"{frame.bits}-bit  {frame.n_samples} samples"
            )
            seen += 1
            if seen >= args.frames:
                break
    finally:
        client.disconnect()

    if not seen:
        print("no IQ frames received")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
