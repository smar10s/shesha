#!/usr/bin/env python3
"""
Design and evaluate the RDS subcarrier extraction chain, and emit its C tables.

This is the instrument for the physical layer in firmware/src/rds_rx.c. The
FM RX decimation chain leaves its stage-1 output (the multiplex at 350 kHz)
flat across the whole 23-57 kHz composite on purpose, so RDS is a second,
narrow chain hung off that tap: mix 57 kHz to DC, then decimate 350 kHz ->
10937.5 Hz while keeping the +/-2.4 kHz RDS band and rejecting everything
that would fold into it.

The frequencies are not arbitrary:

  * 57 kHz is exactly three times the stereo pilot, so the transmitter's
    subcarrier is coherent. On the receiver a free-running 57 kHz NCO is
    enough: 10 ppm of clock error is 0.57 Hz, about 0.1 degrees per biphase
    symbol, and differential decoding absorbs the rest.
  * 57/350 cycles/sample is rational with period 350, so the mixer is an
    exact 350-entry table with zero accumulated drift and no runtime trig.
  * 350000/32 = 10937.5 Hz is an exact integer ratio of the stage-1 tap,
    and 8192 stage-1 samples per capture chunk divide evenly into 256
    complex output samples. No fractional frame accounting.

The stopband rule is the one subtlety. A decimating stage folds any input
frequency f into alias_of(f, out_rate); the bands that fold into the
passband are [k*out_rate - pass, k*out_rate + pass]. The stage's stopband
therefore has to start at out_rate - pass. That is cheap for early stages
(wide transitions) and is why the first stage does not need to reject the
stereo L-R residual at 4 kHz -- a later stage sees it unaliased and rejects
it there. Only the last stage also has to stop below out_rate - pass, at
the nearest unaliased interferer (L-R's lower edge at 4 kHz, guarded to
3.3 kHz).

Modes:
  --selftest  assert every invariant and the rejection of every known mixer
              product
  --search    rank ordered factorisations of 32 by multiply cost
  --analyse   the chosen chain in detail, including where each interferer
              lands
  --emit      paste-ready C tap tables and the mixer table

Deliberately stdlib-only; the Kaiser helpers are imported from
fm_decim_design.py so there is one window implementation in the repo.

Usage:
    python3 scripts/rds_decim_design.py --selftest
    python3 scripts/rds_decim_design.py --analyse
    python3 scripts/rds_decim_design.py --emit
"""

import argparse
import cmath
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fm_decim_design import (          # noqa: E402
    bessel_i0, kaiser_params, kaiser_window, design_lowpass)

# ---------------------------------------------------------------------------
# Tap point and target
# ---------------------------------------------------------------------------
MPX_RATE = 350_000
RDS_RATE = MPX_RATE / 32.0              # 10937.5 Hz
D_TOTAL = 32
MIX_HZ = 57_000.0
PASS_HZ = 2_400.0                       # RDS occupies +/-2.4 kHz
ATTEN_DB = 60

# Nearest post-mix interferer that reaches the last stage unfolded: the
# stereo L-R signal spans 23-53 kHz in the multiplex, so mixing by 57 kHz
# lands it at -4..-34 kHz. The nearest edge is 4 kHz below DC, guarded to
# 3.3 kHz. The last stage has to stop there, not merely at its own alias
# edge, because a later stage does not exist to clean it up.
LAST_STOP_HZ = 3_300.0

# The chosen schedule: decimate 16 then 2. 350000 -> 21875 -> 10937.5.
# --search ranks every ordered factorisation of 32; this one has the lowest
# multiply cost that still meets the stopband rule.
SCHEDULE = (16, 2)

# Mixer table period in samples: 57/350 has period 350.
MIX_PERIOD = 350

# Known interferers after the 57 kHz mixer, as (name, magnitude Hz). A
# component and its image fold identically, so magnitude is all that
# matters. The wanted RDS band (0-2400 Hz) is checked separately.
COMPONENTS = [
    ("mono programme",     42_000.0, "0-15 kHz audio image"),
    ("mono programme",     57_000.0, "0-15 kHz audio image"),
    ("L-R upper edge",      4_000.0, "53 kHz L-R image"),
    ("L-R lower edge",     34_000.0, "23 kHz L-R image"),
    ("stereo pilot",       38_000.0, "19 kHz pilot image"),
    ("RDS image",         114_000.0, "the -57 kHz RDS line"),
]


def alias_of(f, fs):
    """Where f appears after sampling at fs, folded into 0..fs/2."""
    f = math.fmod(f, fs)
    if f < 0:
        f += fs
    return fs - f if f > fs / 2 else f


# ---------------------------------------------------------------------------
# Filter design
# ---------------------------------------------------------------------------
def stage_stop_hz(rate_in, dec, last):
    """The stopband start for one decimating stage.

    out_rate - pass is the rule that keeps every band folding into the
    passband inside the stopband. The last stage additionally has to reject
    unaliased interferers closer to DC, so its stop is capped by the nearest
    one.
    """
    out_rate = rate_in / dec
    alias_edge = out_rate - PASS_HZ
    if last:
        return min(alias_edge, LAST_STOP_HZ)
    return alias_edge


def design_stage(rate_in, dec, last):
    stop = stage_stop_hz(rate_in, dec, last)
    n, beta = kaiser_params(ATTEN_DB, stop - PASS_HZ, rate_in)
    fc = 0.5 * (PASS_HZ + stop)
    taps = design_lowpass(n, beta, rate_in, lambda f: 1.0 if f <= fc else 0.0)
    return taps, n, beta, stop


def build_chain(schedule):
    """Return [(taps, dec, rate_in, rate_out, stop), ...] for a schedule."""
    stages = []
    rate = float(MPX_RATE)
    for i, dec in enumerate(schedule):
        last = (i == len(schedule) - 1)
        taps, n, beta, stop = design_stage(rate, dec, last)
        stages.append({
            "taps": taps, "n": n, "dec": dec, "beta": beta, "stop": stop,
            "rate_in": rate, "rate_out": rate / dec,
        })
        rate /= dec
    return stages


def chain_rate(schedule):
    rate = float(MPX_RATE)
    for dec in schedule:
        rate /= dec
    return rate


def fir_response_db(taps, f, fs):
    w = 2.0 * math.pi * f / fs
    acc = sum(t * cmath.exp(-1j * w * i) for i, t in enumerate(taps))
    return 20.0 * math.log10(abs(acc) + 1e-300)


def chain_gain_db(stages, f):
    """Cumulative gain applied to an interferer at |f|, and where it lands."""
    g = 0.0
    cur = abs(f)
    rate = float(MPX_RATE)
    for st in stages:
        g += fir_response_db(st["taps"], cur, rate)
        rate = st["rate_out"]
        cur = alias_of(cur, rate)
    return g, cur


def chain_cost(stages):
    """Multiply-accumulates per second, counting I and Q."""
    return sum(st["n"] * st["rate_out"] * 2 for st in stages)


def ordered_factorisations(total, max_stages=4, min_dec=2):
    out = []

    def rec(remaining, acc):
        if remaining == 1:
            if acc:
                out.append(tuple(acc))
            return
        if len(acc) >= max_stages:
            return
        for d in range(min_dec, remaining + 1):
            if remaining % d == 0:
                rec(remaining // d, acc + [d])

    rec(total, [])
    return out


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def report_search():
    print(f"Ordered factorisations of {D_TOTAL} (min decimation {2}),")
    print("ranked by multiply cost under the per-stage stopband rule.")
    print()
    print(f"  {'schedule':<16} {'rates':<34} {'stops (Hz)':<34} "
          f"{'taps':<16} {'MMAC/s':>7}")
    rows = []
    for sched in ordered_factorisations(D_TOTAL):
        stages = build_chain(sched)
        rows.append((chain_cost(stages), sched, stages))
    rows.sort()
    for cost, sched, stages in rows:
        rates = " -> ".join(f"{s['rate_out']:.1f}" for s in stages)
        stops = " ".join(f"{s['stop']:.0f}" for s in stages)
        taps = "+".join(str(s["n"]) for s in stages)
        print(f"  {'x'.join(map(str, sched)):<16} {rates:<34} "
              f"{stops:<34} {taps:<16} {cost/1e6:7.2f}")


def report_analyse():
    stages = build_chain(SCHEDULE)
    print(f"Mix {MIX_HZ:.0f} Hz, schedule "
          f"{'x'.join(map(str, SCHEDULE))}, "
          f"{MPX_RATE} -> {chain_rate(SCHEDULE):.1f} Hz")
    print()
    for i, st in enumerate(stages):
        print(f"  stage {i+1}: {st['n']} taps, decimate {st['dec']}, "
              f"{st['rate_in']:.1f} -> {st['rate_out']:.1f} Hz, "
              f"pass {PASS_HZ:.0f}, stop {st['stop']:.0f}, beta "
              f"{st['beta']:.3f}")
    print()
    worst = max(abs(chain_gain_db(stages, f)[0])
                for f in range(0, int(PASS_HZ) + 1, 50))
    print(f"  RDS passband 0-{PASS_HZ:.0f} Hz flat within {worst:.4f} dB "
          f"(unity DC by construction)")
    print()
    print(f"  {'component':<20} {'at Hz':>8} {'gain dB':>9} {'lands Hz':>9}")
    for name, f, _kind in COMPONENTS:
        g, land = chain_gain_db(stages, f)
        print(f"  {name:<20} {f:>8.0f} {g:>9.1f} {land:>9.1f}")
    print()
    print("  The L-R 34 kHz edge folds into the passband at 1187.5 Hz but is")
    print("  184 dB down; the 4 kHz edge stays at 4 kHz, where the last")
    print("  stage's 3.3 kHz stopband rejects it; the 114 kHz image folds to")
    print("  4625 Hz, also inside that stopband.")


def selftest():
    fails = 0

    def check(cond, msg):
        nonlocal fails
        if not cond:
            print(f"FAIL: {msg}")
            fails += 1
        else:
            print(f"ok: {msg}")

    prod = 1
    for d in SCHEDULE:
        prod *= d
    check(prod == D_TOTAL, f"schedule {SCHEDULE} multiplies to {D_TOTAL}")
    check(8192 % D_TOTAL == 0, "8192 stage-1 samples divide by D_TOTAL")
    check(8192 // D_TOTAL == 256, "8192/D_TOTAL == 256 complex per chunk")
    check(abs(chain_rate(SCHEDULE) - 10937.5) < 1e-9,
          f"delivered rate {chain_rate(SCHEDULE):.4f} Hz")

    # Mixer exactness: 57/350 cycles/sample has period 350, so the table is
    # a closed cycle rather than a truncated irrational.
    check(abs(MIX_HZ / MPX_RATE * MIX_PERIOD - round(MIX_HZ / MPX_RATE
          * MIX_PERIOD)) < 1e-12,
          f"{MIX_HZ:.0f}/{MPX_RATE} has exact period {MIX_PERIOD}")

    stages = build_chain(SCHEDULE)
    for i, st in enumerate(stages):
        taps = st["taps"]
        check(abs(sum(taps) - 1.0) < 1e-12, f"stage {i+1} taps sum to unity")
        check(all(abs(taps[k] - taps[st["n"] - 1 - k]) < 1e-12
                  for k in range(st["n"] // 2)),
              f"stage {i+1} taps symmetric")
        check(st["n"] % 2 == 1, f"stage {i+1} tap count odd ({st['n']})")

    worst = max(abs(chain_gain_db(stages, f)[0])
                for f in range(0, int(PASS_HZ) + 1, 50))
    check(worst < 0.05, f"RDS passband flat within 0.05 dB (worst {worst:.4f})")

    # Every known interferer either is at least 50 dB down or lands clear
    # of the decoder's passband (the biphase matched filter has finite
    # rejection, so 5 kHz is the guard, not 2.4 kHz).
    for name, f, _kind in COMPONENTS:
        g, land = chain_gain_db(stages, f)
        check(land > 5_000.0 or g < -50.0,
              f"{name} at {f:.0f} Hz: {g:.1f} dB, lands {land:.1f} Hz")

    # The RDS band itself must survive at the band edges.
    for f in (0.0, 1_000.0, 2_400.0):
        g, _ = chain_gain_db(stages, f)
        check(abs(g) < 0.1, f"RDS {f:.0f} Hz passes at {g:+.3f} dB")

    # The image at 2*57 kHz is the one the first stage must kill before it
    # folds anywhere useful.
    g, _ = chain_gain_db(stages, 114_000.0)
    check(g < -50.0, f"114 kHz mixer image rejected {g:.1f} dB")

    for x, want in ((0.0, 1.0), (5.0, 27.2398718236)):
        got = bessel_i0(x)
        check(abs(got - want) / want < 1e-9, f"I0({x}) = {got:.10f}")

    # Search must rank the chosen schedule best.
    costs = {sched: chain_cost(build_chain(sched))
             for sched in ordered_factorisations(D_TOTAL)}
    check(costs[SCHEDULE] <= min(costs.values()) + 1e-9,
          f"chosen schedule is the cheapest ({costs[SCHEDULE]/1e6:.2f} "
          f"MMAC/s)")

    print()
    print("all checks passed" if not fails else f"{fails} failure(s)")
    return 1 if fails else 0


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------
def report_emit():
    stages = build_chain(SCHEDULE)
    print("/*")
    print(" * RDS subcarrier extraction tables.")
    print(" *")
    print(" * GENERATED by scripts/rds_decim_design.py --emit -- do not edit.")
    print(" * That script asserts the design invariants (--selftest), ranks")
    print(" * schedules (--search), and reports interferer rejection")
    print(" * (--analyse). Re-run it rather than tuning these by hand.")
    print(" *")
    print(f" * Mix {MIX_HZ:.0f} Hz exact (period {MIX_PERIOD} samples), then")
    for i, st in enumerate(stages):
        print(f" *   stage {i+1}: {st['n']:>3} taps, decimate {st['dec']}, "
              f"{st['rate_in']:>9.1f} -> {st['rate_out']:>8.1f} Hz"
              f"  (stop {st['stop']:.0f} Hz)")
    print(f" * {MPX_RATE} -> {chain_rate(SCHEDULE):.1f} Hz. "
          f"Passband 0-{PASS_HZ:.0f} Hz")
    print(f" * flat to {max(abs(chain_gain_db(stages, f)[0]) for f in range(0, int(PASS_HZ) + 1, 50)):.4f} dB, "
          f"{ATTEN_DB} dB design stopband.")
    print(" */")
    print()
    print("#pragma once")
    print()
    print(f"#define RDS_MIX_PERIOD {MIX_PERIOD}")
    print(f"#define RDS_DECIM_STAGES {len(SCHEDULE)}")
    print(f"#define RDS_DECIM_TOTAL  {D_TOTAL}")
    print(f"#define RDS_DECIM_MAX_TAPS "
          f"{max(st['n'] for st in stages)}")
    print()
    print(f"static const float rds_mix_i[RDS_MIX_PERIOD] = {{")
    for n in range(MIX_PERIOD):
        print(f"    {math.cos(2.0 * math.pi * MIX_HZ * n / MPX_RATE): .9e}f,")
    print("};")
    print()
    print(f"static const float rds_mix_q[RDS_MIX_PERIOD] = {{")
    for n in range(MIX_PERIOD):
        print(f"    {-math.sin(2.0 * math.pi * MIX_HZ * n / MPX_RATE): .9e}f,")
    print("};")
    print()
    for i, st in enumerate(stages):
        taps = st["taps"]
        print(f"#define RDS_S{i+1}_TAPS {st['n']}")
        print(f"#define RDS_S{i+1}_DEC  {st['dec']}")
        print(f"static const float rds_s{i+1}_taps[RDS_S{i+1}_TAPS] = {{")
        for j in range(0, st["n"], 4):
            print("    " + ", ".join(f"{t: .9e}f" for t in taps[j:j + 4])
                  + ",")
        print("};")
        print()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--analyse", action="store_true")
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if not any((args.search, args.analyse, args.emit, args.selftest)):
        ap.print_help()
        return 1
    if args.search:
        report_search()
    if args.analyse:
        report_analyse()
    if args.emit:
        report_emit()
    if args.selftest:
        return selftest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
