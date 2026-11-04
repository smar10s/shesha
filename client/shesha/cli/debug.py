"""Developer diagnostics that are too specialized for the release surface."""
from __future__ import annotations

import argparse
import sys
import time

from shesha.client import MODE_NAMES
from shesha.protocol import DaemonMode

from .common import (
    AGC_CODES, AGC_NAMES, RadioState, _connect, check, fail, print_json,
    resolve_freq, restore_radio,
)


def _cpu_report(d) -> str:
    """Human-readable CPU breakdown for one measurement window."""
    lines = [
        f"window {d.window_sec:.2f} s   "
        f"cores {d.n_cpus}   "
        f"system {d.system_busy_pct:5.1f}%   "
        f"daemon {d.daemon_pct:5.1f}%   "
        f"other {d.other_pct:5.1f}%   "
        f"rss {d.vm_rss_kb / 1024.0:.1f} MB",
        f"  {'thread':<16} {'cpu%':>7} {'vol/s':>9} {'invol/s':>9}",
    ]
    for t in sorted(d.threads, key=lambda x: -x.cpu_pct):
        vol = t.voluntary_switches / d.window_sec if d.window_sec else 0.0
        inv = t.involuntary_switches / d.window_sec if d.window_sec else 0.0
        lines.append(f"  {t.name:<16} {t.cpu_pct:7.1f} {vol:9.1f} {inv:9.1f}")
    return "\n".join(lines)


def do_cpu(args: argparse.Namespace) -> None:
    """Per-thread CPU share of the available cores.

    Samples twice and differences, because the daemon reports cumulative
    counters.  The interval is spent in `c.wait`, not `time.sleep`: the
    daemon evicts a client that stops draining the broadcast for ~100 ms.
    """
    if args.count < 1:
        fail("--count must be at least 1")
    if args.interval <= 0:
        fail("--interval must be positive")

    c = _connect(args)
    try:
        prev = c.thread_stats()
        if prev is None:
            fail("no RESP_THREAD_STATS received")

        for i in range(args.count):
            c.wait(args.interval)
            curr = c.thread_stats()
            if curr is None:
                fail("no RESP_THREAD_STATS received")
            d = curr.delta(prev)

            if args.json:
                print_json({
                    "window_sec": round(d.window_sec, 3),
                    "system_busy_pct": round(d.system_busy_pct, 2),
                    "daemon_pct": round(d.daemon_pct, 2),
                    "other_pct": round(d.other_pct, 2),
                    "vm_rss_kb": d.vm_rss_kb,
                    "n_cpus": d.n_cpus,
                    "threads": [
                        {
                            "name": t.name,
                            "tid": t.tid,
                            "cpu_pct": round(t.cpu_pct, 2),
                            "voluntary_switches": t.voluntary_switches,
                            "involuntary_switches": t.involuntary_switches,
                        }
                        for t in sorted(d.threads, key=lambda x: -x.cpu_pct)
                    ],
                })
            else:
                if i:
                    print()
                print(_cpu_report(d))
            prev = curr
    finally:
        c.disconnect()


def _diag_dict(d) -> dict:
    return {
        "captures": d.captures,
        "decode_ok": d.decode_ok,
        "decode_pct": (d.decode_ok / d.captures * 100) if d.captures else 0.0,
        "fcs_ok": d.fcs_ok,
        "fcs_bad": d.fcs_bad,
        "iq_rms": d.iq_rms,
        "iq_peak": d.iq_peak,
    }


def do_diag(args: argparse.Namespace) -> None:
    """Poll CMD_WIFI_DIAG to see STF detection and decode stats."""
    c = _connect(args)
    state = None
    first = True
    try:
        st, prev_mode = c.status() or (None, None)
        if st is None:
            fail("daemon not responding")
        state = RadioState.from_status(st, prev_mode)

        if args.gain is not None and args.agc not in (None, "manual"):
            fail("--gain requires --agc manual")

        check(c, c.set_mode(DaemonMode.WIFI), "could not switch to wifi mode")

        if args.agc is not None:
            check(c, c.set_agc(AGC_CODES[args.agc]), "could not set AGC")
        elif args.gain is not None:
            check(c, c.set_agc(0), "could not switch AGC to manual")
        if args.gain is not None:
            check(c, c.set_gain(float(args.gain)), "could not set gain")
        freq_mhz = resolve_freq(args)
        if freq_mhz is not None:
            check(c, c.set_freq(int(round(freq_mhz * 1e6))),
                  "could not set frequency")

        st2, _ = c.status() or (None, -1)
        if st2:
            print(f"configured: freq={st2.freq_hz/1e6:.0f}MHz  "
                  f"gain={st2.gain_db:.0f}dB  "
                  f"AGC={AGC_NAMES.get(st2.agc_mode, '?')}",
                  file=sys.stderr)

        interval = max(args.interval, 0.5)
        start = time.monotonic()
        while time.monotonic() - start < args.duration:
            d = c.diag()
            if d is None:
                print("  (no diag response)", file=sys.stderr)
                c.wait(interval)
                continue

            if args.json:
                print_json(_diag_dict(d))
            elif args.quiet:
                elapsed = time.monotonic() - start
                print(f"{elapsed:.1f},{d.captures},{d.decode_ok},"
                      f"{d.fcs_ok},{d.fcs_bad},{d.iq_rms:.4f},{d.iq_peak:.4f}")
            elif args.continuous:
                elapsed = time.monotonic() - start
                if first:
                    print(f"{'elapsed':>6} {'captures':>8} {'decode_ok':>8} "
                          f"{'fcs_ok':>8} {'fcs_bad':>8} {'iq_rms':>8} "
                          f"{'iq_peak':>8}")
                print(f"{elapsed:6.1f} {d.captures:8d} {d.decode_ok:8d} "
                      f"{d.fcs_ok:8d} {d.fcs_bad:8d} {d.iq_rms:8.4f} "
                      f"{d.iq_peak:8.4f}")
            else:
                decode_pct = (d.decode_ok / d.captures * 100) if d.captures else 0
                print(f"\ncaptures:  {d.captures}")
                print(f"decode_ok: {d.decode_ok} ({decode_pct:.1f}%)")
                print(f"fcs_ok:    {d.fcs_ok}")
                print(f"fcs_bad:   {d.fcs_bad}")
                print(f"iq_rms:    {d.iq_rms:.4f}  (relative to ADC FS)")
                print(f"iq_peak:   {d.iq_peak:.4f}")
                print("\nInterpretation:")
                print("  iq_rms < 0.01 -> signal very weak, increase gain")
                print("  iq_rms ~ 0.05-0.2 -> reasonable signal level")
                print("  iq_rms > 0.5 -> possible ADC clipping")
                if d.captures > 0 and d.decode_ok == 0:
                    print("  STF not triggering (need autocorr >= 0.5)")
                    print("  -> try increasing gain, or check frequency")
                break
            first = False

            if not args.continuous:
                break
            c.wait(interval)

    except KeyboardInterrupt:
        pass
    finally:
        restore_radio(c, state)
        c.disconnect()


def do_stream(args: argparse.Namespace) -> None:
    """Capture a stream and summarize the frames that arrived."""
    c = _connect(args)
    state = None
    try:
        st, prev_mode = c.status() or (None, None)
        if st:
            state = RadioState.from_status(st, prev_mode)

        if args.mode is not None:
            from shesha.client import MODE_BY_NAME
            check(c, c.set_mode(MODE_BY_NAME[args.mode]),
                  f"could not switch to {args.mode} mode")

        result = c.stream(duration=args.duration, verbose=args.verbose)
        # The stream itself carries no mode unless a status broadcast
        # arrived, so ask once for the summary.
        current = c.status()
        if current is not None:
            result.mode = current[1]
        if args.json:
            print_json(result.to_dict(verbose=args.verbose))
        else:
            print(f"capture: {result.duration:.1f} s, "
                  f"mode={MODE_NAMES.get(result.mode, '?')}")
            print(f"psd:     {result.psd_count} frames "
                  f"({result.psd_fps:.1f} fps)")
            print(f"wifi:    {result.wifi_count} frames "
                  f"({result.wifi_fps:.1f} fps, {result.wifi_fcs_ok} FCS OK)")
            print(f"fm:      {result.fm_count} frames")
            print(f"iq:      {result.iq_count} frames")
            for e in result.errors:
                print(f"error:   {e}")
    finally:
        restore_radio(c, state)
        c.disconnect()
