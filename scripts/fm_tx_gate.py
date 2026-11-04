#!/usr/bin/env python3
"""FM TX gate — the daemon-level counterpart to firmware/tools/fm_stream_test.

fm_stream_test proves the ARM can sustain FM modulation with nothing else
running.  That is necessary but not sufficient, and the gap is exactly what
burned an earlier session: inside the daemon the same modulation competes
with the poll loop, command dispatch, and the TCP receive path carrying the
audio itself, on a CPU that exposes ONE core.

This script closes that loop programmatically.  It streams a tone through
the real client path and then asserts against CMD_TX_STATS, whose `emitted`
counter comes from the FPGA drain FSM — the only trustworthy output-rate
measurement, because RD_PTR advances solely when a sample actually reaches
the DAC.  A deficit there is precisely the time-stretch a listener hears as
the audio dragging.

Nothing here needs a human to listen to anything.

    python3 scripts/fm_tx_gate.py                    # 30 s, default bounds
    python3 scripts/fm_tx_gate.py -d 120 --poll-hz 20
    python3 scripts/fm_tx_gate.py --json             # machine-readable only
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'client'))

from shesha.client import DaemonClient, PushResult          # noqa: E402
from shesha.fm_audio import prepare_fm_audio                # noqa: E402

# Pass bounds.  RATE_FLOOR is the whole point of the exercise: 0.999 means
# at most 0.1% time-stretch, which is inaudible.  The early
# loopback scripts accepted 30%.
RATE_FLOOR = 0.999
MAX_STARVED_SEC = 0.0     # carrier emitted for want of audio
FEED_CONSISTENCY = 0.02   # |fed - emitted| / fed, guards against stale replay

FM_LEAD_SEC = 1.0
FM_CHUNK_SEC = 0.1


def make_tone(rate: int, seconds: float, hz: int, amp: float) -> list[float]:
    n = int(rate * seconds)
    return [amp * math.sin(2 * math.pi * hz * i / rate) for i in range(n)]


class StatusLoad:
    """Background CMD_STATUS poller, standing in for an attached TUI.

    Contention on the single core is the thing under test, so the gate has
    to generate some.  This is deliberately the daemon's own command path
    rather than an RX producer: concurrent TX and RX at 2.1 MSPS is a
    separate question (both directions on one core) and would make a
    failure here unattributable.
    """

    def __init__(self, host: str, port: int, hz: float):
        self.hz = hz
        self.host, self.port = host, port
        self.polls = 0
        self.failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if self.hz > 0:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self):
        c = DaemonClient(host=self.host, port=self.port)
        try:
            c.connect()
        except OSError:
            self.failures += 1
            return
        try:
            interval = 1.0 / self.hz
            while not self._stop.is_set():
                if c.status() is None:
                    self.failures += 1
                else:
                    self.polls += 1
                self._stop.wait(interval)
        finally:
            c.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.2.1")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("-d", "--duration", type=float, default=30.0,
                    help="tone duration in seconds (default 30)")
    ap.add_argument("-f", "--freq", type=float, default=100.1,
                    help="carrier in MHz (default 100.1)")
    ap.add_argument("-t", "--tone", type=int, default=1000,
                    help="audio tone in Hz (default 1000)")
    ap.add_argument("-a", "--tx-atten", type=float, default=20.0,
                    help="TX attenuation in dB (default 20)")
    ap.add_argument("--rate", type=int, default=44100)
    ap.add_argument("--deviation", type=int, default=75000)
    ap.add_argument("--dac-rate", type=int, default=2_100_000)
    ap.add_argument("--poll-hz", type=float, default=10.0,
                    help="background CMD_STATUS rate, 0 to disable "
                         "(default 10)")
    ap.add_argument("--json", action="store_true",
                    help="suppress the human report")
    args = ap.parse_args()

    def say(*a):
        if not args.json:
            print(*a, file=sys.stderr, flush=True)

    raw = make_tone(args.rate, args.duration, args.tone, 0.8)
    # Same conditioning the CLI applies, so the gate exercises the shipping
    # path rather than a private one.
    audio, preemph_peak, scale = prepare_fm_audio(raw, args.rate, peak=1.0)
    total = len(audio)

    say("fm_tx_gate")
    say(f"  carrier:    {args.freq:.3f} MHz  atten {args.tx_atten:.1f} dB")
    say(f"  audio:      {args.tone} Hz tone, {args.rate} Hz, "
        f"{args.deviation} Hz deviation")
    say(f"  preemph:    peak {preemph_peak:.3f} -> scale {scale:.4f}")
    say(f"  duration:   {args.duration:.1f} s")
    say(f"  background: CMD_STATUS at {args.poll_hz:g} Hz")

    c = DaemonClient(host=args.host, port=args.port)
    try:
        c.connect()
    except OSError as exc:
        say(f"FAIL: cannot connect to daemon at {args.host}:{args.port}: "
            f"{exc} (is it running?)")
        print(json.dumps({"mode": "fm_tx_gate", "passed": False,
                          "errors": [f"connect failed: {exc}"]}))
        return 1

    load = StatusLoad(args.host, args.port, args.poll_hz)
    errors: list[str] = []
    retries = 0
    first_n = min(args.rate, total)
    chunk_n = max(1, int(args.rate * FM_CHUNK_SEC))

    try:
        with load:
            c.set_freq(int(args.freq * 1e6))
            c.set_tx_atten(args.tx_atten)

            if not c.fm_tx_stream_start(audio[:first_n],
                                        audio_rate=args.rate,
                                        deviation=args.deviation,
                                        dac_rate=args.dac_rate):
                errors.append("fm_tx_stream_start rejected")
                raise SystemExit(1)

            t0 = time.monotonic()
            sent = first_n
            next_report = t0 + 5.0
            worst_rate = 1e9

            while sent < total:
                ahead = sent / args.rate - (time.monotonic() - t0)
                if ahead > FM_LEAD_SEC:
                    time.sleep(min(0.05, ahead - FM_LEAD_SEC))
                    continue

                end = min(sent + chunk_n, total)
                result = c.fm_tx_stream_push(audio[sent:end])
                if result is PushResult.RETRY:
                    retries += 1
                    time.sleep(0.05)
                    continue
                if result is PushResult.ERROR:
                    errors.append(f"push failed at {sent}/{total}")
                    break
                sent = end

                now = time.monotonic()
                if now >= next_report:
                    ts = c.tx_stats()
                    if ts is not None:
                        # Only meaningful once the engine has been running
                        # long enough for the ratio to settle.
                        if ts.elapsed_us > 2_000_000:
                            worst_rate = min(worst_rate, ts.rate_frac)
                        say(f"  t={now - t0:6.1f}s  "
                            f"out {ts.rate_frac * 100:7.3f}%  "
                            f"ddr {ts.depth_ms:6.1f}ms  "
                            f"ring {ts.ring_fill_ms / 1000:4.2f}s  "
                            f"underruns {ts.underruns}  "
                            f"starved {ts.starved_samples}")
                    next_report = now + 5.0

            c.fm_tx_stream_end()

            # Let playout finish so the final counters cover the whole run.
            deadline = time.monotonic() + args.duration + 30
            final = None
            while time.monotonic() < deadline:
                ts = c.tx_stats()
                if ts is not None:
                    final = ts
                    if not ts.tx_active:
                        break
                time.sleep(0.2)

        if final is None:
            errors.append("no tx_stats available")
            raise SystemExit(1)

        rate_frac = final.rate_frac
        starved_sec = (final.starved_samples / final.dac_rate
                       if final.dac_rate else 0.0)
        feed_delta = ((final.emitted - final.fed) / final.fed
                      if final.fed else 1.0)

        if rate_frac < RATE_FLOOR:
            errors.append(f"output rate {rate_frac * 100:.3f}% below "
                          f"{RATE_FLOOR * 100:.1f}% — audio drags")
        if final.underruns:
            errors.append(f"{final.underruns} underrun polls")
        if starved_sec > MAX_STARVED_SEC:
            errors.append(f"{starved_sec:.3f}s of carrier emitted for want "
                          f"of audio (client could not keep up)")
        if final.max_depth > final.high_water:
            errors.append(f"depth {final.max_depth} exceeded high water "
                          f"{final.high_water}")
        if feed_delta > FEED_CONSISTENCY:
            errors.append(f"emitted exceeds fed by {feed_delta * 100:.2f}% "
                          f"— replaying stale ring contents")

        passed = not errors

        say("")
        say(f"  fed:          {final.fed} samples")
        say(f"  DAC emitted:  {final.emitted} samples "
            f"({final.emitted / max(final.elapsed_us, 1) * 1e6 / 1e6:.3f} MSPS)")
        say(f"  output rate:  {rate_frac * 100:.3f}% of nominal")
        say(f"  worst sample: {worst_rate * 100:.3f}%"
            if worst_rate < 1e8 else "  worst sample: n/a")
        say(f"  max depth:    {final.max_depth * 1000.0 / final.dac_rate:.1f} ms "
            f"of {final.high_water * 1000.0 / final.dac_rate:.1f} ms bound")
        say(f"  underruns:    {final.underruns}")
        say(f"  starved:      {starved_sec:.3f} s of carrier")
        say(f"  push retries: {retries} (backpressure, not errors)")
        say(f"  ring rejects: {final.push_rejects}")
        say(f"  status polls: {load.polls} ok, {load.failures} failed")
        for e in errors:
            say(f"  ERROR: {e}")
        say("")
        say(f"  RESULT: {'PASS' if passed else 'FAIL'}")

        print(json.dumps({
            "mode": "fm_tx_gate", "passed": passed, "errors": errors,
            "nominal_sps": final.dac_rate,
            "rate_frac": round(rate_frac, 6),
            "worst_rate_frac": (round(worst_rate, 6)
                                if worst_rate < 1e8 else None),
            "fed": final.fed, "emitted": final.emitted,
            "elapsed_us": final.elapsed_us,
            "underruns": final.underruns,
            "starved_samples": final.starved_samples,
            "max_depth": final.max_depth, "high_water": final.high_water,
            "push_retries": retries, "ring_rejects": final.push_rejects,
            "status_polls": load.polls, "status_poll_failures": load.failures,
        }))
        return 0 if passed else 1

    except SystemExit:
        say("")
        for e in errors:
            say(f"  ERROR: {e}")
        say("  RESULT: FAIL")
        print(json.dumps({"mode": "fm_tx_gate", "passed": False,
                          "errors": errors}))
        return 1
    finally:
        c.disconnect()


if __name__ == "__main__":
    sys.exit(main())
