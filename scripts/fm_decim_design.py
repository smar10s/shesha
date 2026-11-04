#!/usr/bin/env python3
"""
Design and evaluate the FM RX decimation chain, and emit its C tap tables.

This is the instrument for the decimator in firmware/src/fm_rx.c. It exists
because the single-stage design it replaces was chosen without any
frequency-domain evidence and was wrong in a way no passband test could see:
a critically sampled 47-tap boxcar passes the 19 kHz stereo pilot and folds
the 38 kHz stereo subcarrier and 57 kHz RDS into the audio band.

Modes:
  --selftest      assert every design invariant, including the two findings
                  that determined the architecture
  --sweep         two-stage factorisations of D_TOTAL, for reference
  --search        multistage search, mono-only policy
  --search-stereo multistage search, stage 1 preserving the composite
  --analyse       the chosen chain in detail
  --compare       old single boxcar vs the chosen chain
  --signal        end to end: synthesise an FM stereo multiplex, modulate to
                  IQ, demodulate through a Python model of fm_rx.c, and
                  measure delivered energy per component with Goertzel
  --emit          paste-ready C tap tables

--analyse is exact but only proves the filters are *specified* right.
--signal is approximate but proves the cascade is *wired* right. They fail
differently, so both exist.

Deliberately stdlib-only. A Kaiser-windowed sinc is ten lines of arithmetic;
importing scipy for it would add a heavy dependency to a repo that has none,
for a script whose entire output is three tap tables. Cross-checked against
scipy.signal.firwin during development: identical beta, taps agreeing to
3.1e-17. Order differs by one because this file forces odd length for an
integer group delay, which scipy's kaiserord does not.

Usage:
    python3 scripts/fm_decim_design.py --selftest
    python3 scripts/fm_decim_design.py --analyse
    python3 scripts/fm_decim_design.py --search-stereo
    python3 scripts/fm_decim_design.py --signal
    python3 scripts/fm_decim_design.py --emit
"""

import argparse
import cmath
import math
import sys

# ---------------------------------------------------------------------------
# D_TOTAL = 48, not the 47 the old code derived from iq_rate/audio_rate.
# Two independent reasons, both hard:
#
#   1. 47 is prime, so it cannot be split into cascaded stages at all. A
#      single stage doing the 15 kHz -> 19 kHz transition at 2.1 MSPS needs
#      about 1900 taps for 60 dB (83 MMAC/s); split three ways it needs
#      33+23+81 (17 MMAC/s). The factorisation *is* the design, so the old
#      decimation factor foreclosed the fix.
#   2. 49152 / 48 = 1024 exactly, which is the audio ring slot size, so the
#      2% silent frame truncation disappears with no change to the capture
#      chunk. At 47 it produced 1045 samples into a 1024 slot.
#
# Cost: the delivered rate is 43750 Hz rather than 44100. Every DATA_FM frame
# carries its own rate and the client reads it from the frame, so this is a
# stamped constant, not a pitch error.
# ---------------------------------------------------------------------------
IQ_RATE = 2_100_000
D_TOTAL = 48

# The chosen chain: three decimating Kaiser FIRs, 6 x 4 x 2.
#
# Selected by --search-stereo over every ordered factorisation of 48. It has
# the best aliased-noise figure of any stereo-capable schedule (-30.8 dB) and
# its stage-1 output at 350 kHz is flat to +/-0.02 dB across the entire
# 23-57 kHz composite, which is what makes it a usable stereo tap point.
#
# Why there is no boxcar anywhere: a cascade of boxcars whose lengths equal
# their decimation factors is *exactly* one boxcar of the product length --
# asserted in --selftest, and it follows from
# [sin(4x)/(4 sin x)] * [sin(12x)/(3 sin 4x)] = sin(12x)/(12 sin x). So the
# old single boxcar cannot be improved by splitting it into cheap stages, and
# longer boxcars do not help either, because the f^2 noise weighting is
# dominated by far sidelobes and a boxcar's decay is only 6 dB/octave. The
# high-rate stage has to be a real filter. That is the central finding.
SCHEDULE = (6, 4, 2)
STEREO_TAP = 0            # stage whose output carries the composite
STEREO_BW = 57_000.0      # composite bandwidth stage 1 keeps alias-free

AUDIO_RATE = IQ_RATE / D_TOTAL   # 43750 Hz delivered

PASS_HZ = 15_000                 # FM broadcast audio limit
STOP_HZ = 19_000                 # stereo pilot: put it at full attenuation
ATTEN_DB = 60

DEVIATION = 75_000
DEEMPHASIS_US = 75

OLD_D = 47                       # the single boxcar being replaced
OLD_RATE = IQ_RATE / OLD_D       # 44680.85 Hz

# Broadcast multiplex components, with standard injection levels as a
# fraction of peak deviation.
MPX = [
    ("mono L+R 1 kHz",         1_000, 0.45),
    ("stereo pilot",          19_000, 0.09),
    ("stereo L-R lower",      35_000, 0.225),   # 38k - 3k, DSB-SC
    ("stereo L-R upper",      41_000, 0.225),   # 38k + 3k
    ("RDS",                   57_000, 0.03),
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
def bessel_i0(x):
    """Modified Bessel function of the first kind, order 0.

    Ascending series. Every term is positive so there is no cancellation, and
    it converges quickly for the |x| < 12 that Kaiser betas produce.
    """
    total = term = 1.0
    k = 1
    while True:
        term *= (x / (2.0 * k)) ** 2
        total += term
        if term < 1e-18 * total:
            return total
        k += 1
        if k > 300:
            return total


def kaiser_params(atten_db, trans_hz, fs):
    """Kaiser's empirical order and beta for a given attenuation."""
    if atten_db > 50:
        beta = 0.1102 * (atten_db - 8.7)
    elif atten_db >= 21:
        beta = 0.5842 * (atten_db - 21) ** 0.4 + 0.07886 * (atten_db - 21)
    else:
        beta = 0.0
    # N = (A - 8) / (2.285 * dOmega), dOmega in radians/sample. Against a
    # transition width in Hz that constant is 2.285 * 2*pi = 14.357. Using 22
    # (a half-remembered form of the same rule) makes the filter 1.5x too
    # short, which does not fail visibly -- it just widens the real transition
    # until the 19 kHz pilot sits inside it at -26 dB instead of -60.
    dw = trans_hz / fs
    n = int(math.ceil((atten_db - 8.0) / (2.285 * 2.0 * math.pi * dw)))
    return (n + 1 if n % 2 == 0 else n), beta


def kaiser_window(n_taps, beta):
    m = n_taps - 1
    denom = bessel_i0(beta)
    return [bessel_i0(beta * math.sqrt(max(0.0, 1.0 - (2.0 * i / m - 1.0) ** 2)))
            / denom for i in range(n_taps)]


def design_lowpass(n_taps, beta, fs, desired, grid=2048):
    """Frequency-sampling design windowed by a Kaiser.

    For a brick-wall `desired` this reproduces a windowed sinc exactly. It is
    written generally so droop compensation can be folded in: the final
    stage's passband target is the reciprocal of every earlier stage's droop,
    so the same taps that reject the stopband also flatten the passband, at
    zero runtime cost.

    The response is real and even, so a cosine transform suffices.
    """
    m = n_taps - 1
    half = m // 2
    nyq = fs / 2.0
    grid_f = [(i + 0.5) * nyq / grid for i in range(grid)]
    dvals = [desired(f) for f in grid_f]
    h = []
    for k in range(-half, half + 1):
        acc = 0.0
        for f, dv in zip(grid_f, dvals):
            acc += dv * math.cos(2.0 * math.pi * f * k / fs)
        h.append(acc / grid)
    w = kaiser_window(n_taps, beta)
    taps = [hv * wv for hv, wv in zip(h, w)]
    s = sum(taps)
    return [t / s for t in taps]          # unity DC gain


def design_stage_fir(rate_in, pass_hz, stop_hz, atten_db=ATTEN_DB,
                     droop_fn=None):
    """One decimating stage's lowpass.

    The target is a brick wall at the transition midpoint. An earlier version
    tapered the transition with a raised cosine, reasoning that a smooth
    target would not fight the window. It cost 31 dB of pilot rejection: the
    taper only reaches zero *at* 19 kHz, so the realised response there is
    mid-transition leakage (-27 dB) rather than full stopband (-58 dB). The
    window already shapes the transition; specifying it twice is worse.
    """
    n, beta = kaiser_params(atten_db, stop_hz - pass_hz, rate_in)
    fc = 0.5 * (pass_hz + stop_hz)

    def desired(f):
        if f > fc:
            return 0.0
        if droop_fn is None:
            return 1.0
        return 10.0 ** (-droop_fn(min(f, pass_hz)) / 20.0)

    return design_lowpass(n, beta, rate_in, desired), n, beta


# ---------------------------------------------------------------------------
# Chain model.
#
# A chain is a list of stages applied to the discriminator output at IQ_RATE:
#   ("box", L)        block-reset boxcar of length L, decimating by L
#   ("fir", taps, D)  FIR decimating by D
#
# Tracking the aliasing *between* stages is the point: a component a stage
# fails to reject does not stay where it was, it reappears lower down, and
# the next stage can only act on where it landed.
# ---------------------------------------------------------------------------
def boxcar_response_db(length, f, fs):
    if length == 1 or f == 0:
        return 0.0
    x = math.pi * f * length / fs
    d = length * math.sin(math.pi * f / fs)
    if abs(d) < 1e-300:
        return -300.0
    return 20.0 * math.log10(abs(math.sin(x) / d) + 1e-300)


def fir_response_db(taps, f, fs):
    w = 2.0 * math.pi * f / fs
    acc = sum(t * cmath.exp(-1j * w * i) for i, t in enumerate(taps))
    return 20.0 * math.log10(abs(acc) + 1e-300)


def chain_gain_db(chain, f, fs_in=IQ_RATE):
    """Total gain applied to an input component at f, and where it lands."""
    g = 0.0
    rate = float(fs_in)
    cur = f
    for stage in chain:
        if stage[0] == "box":
            g += boxcar_response_db(stage[1], cur, rate)
            rate /= stage[1]
        else:
            g += fir_response_db(stage[1], cur, rate)
            rate /= stage[2]
        cur = alias_of(cur, rate)
    return g, cur


def chain_rate(chain, fs_in=IQ_RATE):
    rate = float(fs_in)
    for stage in chain:
        rate /= stage[1] if stage[0] == "box" else stage[2]
    return rate


def aliased_noise_db(chain, fs_in=IQ_RATE, step=50.0):
    """Integrated noise folded into the passband, relative to what lands
    there legitimately, under an f^2 discriminator noise PSD.

    An FM discriminator turns a flat RF noise floor into an output noise PSD
    rising as f^2 -- the FM noise triangle. So what matters is not the worst
    single fold but the integral over every band that folds in, weighted by
    f^2. A flat-noise metric understates high-frequency folding by 20 dB or
    more at exactly the frequencies where a decimator is weakest, which is
    why the shipped single boxcar looked merely mediocre on a worst-fold
    figure (-20.6 dB) while actually delivering 18 dB *more* aliased noise
    than direct noise.
    """
    out_rate = chain_rate(chain, fs_in)
    direct = folded = 0.0
    f = step
    while f < fs_in / 2:
        g, _ = chain_gain_db(chain, f, fs_in)
        if alias_of(f, out_rate) <= PASS_HZ:
            power = (10.0 ** (g / 10.0)) * f * f
            if f <= PASS_HZ:
                direct += power
            else:
                folded += power
        f += step
    return 10.0 * math.log10((folded + 1e-300) / (direct + 1e-300))


def ordered_factorisations(total, max_stages=4, min_dec=2):
    out = []

    def rec(remaining, acc):
        if remaining == 1:
            if acc:
                out.append(tuple(acc))
            return
        if len(acc) == max_stages:
            return
        for d in range(min_dec, remaining + 1):
            if remaining % d == 0:
                rec(remaining // d, acc + [d])
    rec(total, [])
    return out


def build_multistage(decims, stereo_first=True, atten_db=ATTEN_DB,
                     protect_hz=PASS_HZ):
    """Design a full chain for a decimation schedule.

    Standard multistage rule: an intermediate stage must not fold anything
    into the protected band, so its stopband edge is (its own output rate -
    protected bandwidth). The final stage's stopband edge is STOP_HZ, which
    is what puts the pilot at full attenuation.

    When stereo_first, stage 1 protects the whole 57 kHz composite instead of
    just the audio band, so its output is a usable stereo tap point. That
    costs more, because stage 1 runs at the highest rate and a wider
    protected band means a narrower transition there.

    Returns (chain, taps_per_stage, MMAC/s) or None if the schedule cannot be
    filtered at all.
    """
    chain, taps_list = [], []
    rate = float(IQ_RATE)
    total_mac = 0.0
    for k, dec in enumerate(decims):
        out_rate = rate / dec
        last = (k == len(decims) - 1)
        if last:
            p_hz, s_hz = float(PASS_HZ), float(STOP_HZ)
        elif k == 0 and stereo_first:
            p_hz, s_hz = STEREO_BW, out_rate - STEREO_BW
        else:
            p_hz, s_hz = float(protect_hz), out_rate - protect_hz
        if s_hz <= p_hz:
            return None

        # Compensate every earlier stage's droop, in the final stage only,
        # where taps are cheapest.
        droop_fn = None
        if last and chain:
            def droop_fn(f, _c=list(chain)):
                return chain_gain_db(_c, f)[0]

        taps, n, _beta = design_stage_fir(rate, p_hz, s_hz, atten_db, droop_fn)
        chain.append(("fir", taps, dec))
        taps_list.append(n)
        total_mac += n * out_rate
        rate = out_rate
    return chain, taps_list, total_mac / 1e6


CHOSEN, TAPS_PER_STAGE, MMAC = build_multistage(SCHEDULE)
OLD_CHAIN = [("box", OLD_D)]


def stage_rates(decims=SCHEDULE):
    """(input rate, output rate) per stage."""
    out = []
    r = float(IQ_RATE)
    for d in decims:
        out.append((r, r / d))
        r /= d
    return out


# ---------------------------------------------------------------------------
# Signal-domain model
# ---------------------------------------------------------------------------
def synth_mpx_iq(n, components=MPX, iq_rate=IQ_RATE, deviation=DEVIATION):
    """Constant-envelope FM carrying a stereo multiplex.

    Phase is integrated sample by sample rather than evaluated closed form,
    so the result does not depend on a trig identity holding.
    """
    re = [0.0] * n
    im = [0.0] * n
    phase = 0.0
    two_pi = 2.0 * math.pi
    for i in range(n):
        t = i / iq_rate
        mpx = 0.0
        for _name, f, amp in components:
            mpx += amp * math.sin(two_pi * f * t)
        phase += two_pi * deviation * mpx / iq_rate
        if phase > math.pi:
            phase -= two_pi
        elif phase < -math.pi:
            phase += two_pi
        re[i] = math.cos(phase)
        im[i] = math.sin(phase)
    return re, im


def discriminate(re, im, iq_rate=IQ_RATE, deviation=DEVIATION):
    """Cross-product discriminator, tracking fm_rx.c rather than an
    idealisation of it: same leaky DC blocker, same envelope normalisation,
    same clamp."""
    dc_alpha = 1.0 / 1024.0
    a_alpha = 1.0 / 2048.0
    freq_scale = iq_rate / (2.0 * math.pi * deviation)
    dc_i = dc_q = i_prev = q_prev = 0.0
    a2 = 1e-6
    out = []
    for i in range(len(re)):
        dc_i += dc_alpha * (re[i] - dc_i)
        dc_q += dc_alpha * (im[i] - dc_q)
        i_dc = re[i] - dc_i
        q_dc = im[i] - dc_q
        a2 += a_alpha * (i_dc * i_dc + q_dc * q_dc - a2)
        disc = i_prev * q_dc - q_prev * i_dc
        out.append(max(-1.0, min(1.0, disc * freq_scale / a2)))
        i_prev, q_prev = i_dc, q_dc
    return out


def apply_box(x, length):
    """Block-reset boxcar decimator, exactly as the old firmware did it."""
    out, acc, n = [], 0.0, 0
    for v in x:
        acc += v
        n += 1
        if n == length:
            out.append(acc / length)
            acc, n = 0.0, 0
    return out


def apply_fir(x, taps, d):
    """Decimating FIR. Only output samples are computed, which is what
    polyphase means in the firmware."""
    out = []
    nt = len(taps)
    hist = [0.0] * nt
    pos = 0
    for v in x:
        hist[pos % nt] = v
        pos += 1
        if pos % d == 0:
            acc = 0.0
            for j, t in enumerate(taps):
                acc += t * hist[(pos - 1 - j) % nt]
            out.append(acc)
    return out


def apply_chain(x, chain):
    for stage in chain:
        x = apply_box(x, stage[1]) if stage[0] == "box" \
            else apply_fir(x, stage[1], stage[2])
    return x


def deemphasis(x, fs, us=DEEMPHASIS_US):
    a = (1.0 / fs) / (1.0 / fs + us * 1e-6)
    state = 0.0
    out = []
    for v in x:
        state += a * (v - state)
        out.append(state)
    return out


def goertzel_mag(samples, freq, fs):
    """Amplitude at one frequency, via the Goertzel algorithm.

    Kept local so this file stands alone.
    """
    n = len(samples)
    if n == 0:
        return 0.0
    k = 2.0 * math.cos(2.0 * math.pi * freq / fs)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + k * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - k * s1 * s2
    return 2.0 * math.sqrt(max(0.0, power)) / n


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def report_sweep():
    print(f"Two-stage factorisations of D_TOTAL = {D_TOTAL}, for reference.")
    print("  Kept because it shows why two stages is not enough: the")
    print("  cheapest two-stage chain costs more than the chosen three-stage")
    print("  one and rejects less.")
    print()
    print(f"  {'D1':>3} {'D2':>3} {'F1 kHz':>8} {'taps':>9} {'MMAC/s':>7} "
          f"{'aliasN':>7} {'pilot':>7}")
    for d1 in range(2, D_TOTAL):
        if D_TOTAL % d1 or D_TOTAL // d1 < 2:
            continue
        built = build_multistage((d1, D_TOTAL // d1))
        if built is None:
            continue
        chain, taps, mmac = built
        pilot, _ = chain_gain_db(chain, STOP_HZ)
        print(f"  {d1:>3} {D_TOTAL//d1:>3} {IQ_RATE/d1/1000:>8.2f} "
              f"{'+'.join(map(str, taps)):>9} {mmac:>7.2f} "
              f"{aliased_noise_db(chain):>7.1f} {pilot:>7.1f}")


def report_search(stereo_first=True, atten_db=ATTEN_DB, top=12):
    print(f"Multistage search, D_TOTAL = {D_TOTAL}, {atten_db} dB design")
    print("  policy: " + ("stage 1 preserves the 57 kHz composite"
                          if stereo_first
                          else "mono only, every stage protects 0-15 kHz"))
    print()
    results = []
    for decims in ordered_factorisations(D_TOTAL):
        built = build_multistage(decims, stereo_first=stereo_first,
                                 atten_db=atten_db)
        if built is None:
            continue
        chain, taps, mmac = built
        pilot, _ = chain_gain_db(chain, STOP_HZ)
        flat = max(abs(chain_gain_db(chain, f)[0])
                   for f in range(100, PASS_HZ + 1, 100))
        results.append((mmac, decims, taps, aliased_noise_db(chain), pilot,
                        flat))
    results.sort()
    print(f"  {'MMAC/s':>7} {'schedule':<12} {'taps':<16} {'aliasN':>7} "
          f"{'pilot':>7} {'flat':>6}")
    for mmac, decims, taps, an, pilot, flat in results[:top]:
        mark = "  <-- chosen" if decims == SCHEDULE else ""
        print(f"  {mmac:>7.2f} {'x'.join(map(str, decims)):<12} "
              f"{'+'.join(map(str, taps)):<16} {an:>7.1f} {pilot:>7.1f} "
              f"{flat:>6.3f}{mark}")
    print()
    print("  MMAC/s  sum over stages of taps * that stage's output rate")
    print("  aliasN  f^2-weighted folded noise vs direct, dB. Negative good.")
    print("  flat    worst passband deviation from 0 dB, after the final")
    print("          stage compensates every earlier stage's droop")
    print()
    print("  Sorted by cost, but cost is not the binding constraint here --")
    print("  aliasN is. The chosen schedule is the best aliasN among the")
    print("  stereo-capable ones, not the cheapest.")
    return results


def report_analyse():
    print("Chosen chain")
    print(f"  IQ rate            {IQ_RATE} Hz")
    for i, (dec, (r_in, r_out)) in enumerate(zip(SCHEDULE, stage_rates())):
        n = TAPS_PER_STAGE[i]
        role = ("composite-preserving" if i == STEREO_TAP
                else "audio-band protect" if i < len(SCHEDULE) - 1
                else "final anti-alias + droop compensation")
        print(f"  stage {i+1}            {n:>3}-tap FIR decimate {dec}: "
              f"{r_in:>9.0f} -> {r_out:>8.0f} Hz   {role}")
    print(f"  total decimation   {D_TOTAL}")
    print(f"  delivered rate     {AUDIO_RATE:.0f} Hz")
    print(f"  passband           0 - {PASS_HZ} Hz, droop compensated")
    print(f"  stopband           {STOP_HZ} Hz +, {ATTEN_DB} dB design")
    print(f"  total cost         {MMAC:.2f} MMAC/s "
          f"({'+'.join(map(str, TAPS_PER_STAGE))} taps)")
    print(f"  aliased noise      {aliased_noise_db(CHOSEN):.1f} dB vs direct "
          f"(old chain {aliased_noise_db(OLD_CHAIN):+.1f})")
    print()
    tap_rate = stage_rates()[STEREO_TAP][1]
    print(f"  Stereo tap point: stage {STEREO_TAP+1} output at "
          f"{tap_rate:.0f} Hz, Nyquist {tap_rate/2:.0f} Hz.")
    print("  Response across the composite, which is what makes it usable:")
    s1 = CHOSEN[:STEREO_TAP + 1]
    for f in (19_000, 23_000, 38_000, 53_000, 57_000):
        print(f"    {f:>6} Hz  {chain_gain_db(s1, f)[0]:>+7.3f} dB")
    print("  A future stereo decoder adds its own chain from there. Nothing")
    print("  is exposed for it yet -- the door is open, not walked through.")
    print()
    print("Multiplex component rejection, whole chain")
    print(f"  {'component':<20} {'freq':>7} {'lands':>7} {'total dB':>9}")
    for name, f, _amp in MPX:
        g, _ = chain_gain_db(CHOSEN, f)
        print(f"  {name:<20} {f:>7} {alias_of(f, AUDIO_RATE):>7.0f} {g:>9.2f}")
    print()
    print("Passband flatness, whole chain")
    for f in (100, 1_000, 5_000, 10_000, 13_000, 15_000):
        print(f"  {f:>6} Hz  {chain_gain_db(CHOSEN, f)[0]:>+7.3f} dB")


def report_compare():
    print(f"Old single boxcar({OLD_D}) vs the chosen three-stage chain")
    print()
    print(f"  {'freq':>7} {'old lands':>10} {'old dB':>8} "
          f"{'new lands':>10} {'new dB':>8} {'delta':>8}")
    for name, f, _amp in MPX:
        og, _ = chain_gain_db(OLD_CHAIN, f)
        ng, _ = chain_gain_db(CHOSEN, f)
        print(f"  {f:>7} {alias_of(f, OLD_RATE):>10.0f} {og:>8.2f} "
              f"{alias_of(f, AUDIO_RATE):>10.0f} {ng:>8.2f} "
              f"{ng-og:>8.2f}   {name}")
    print()
    print(f"  aliased noise vs direct  old {aliased_noise_db(OLD_CHAIN):+.1f}"
          f" dB   new {aliased_noise_db(CHOSEN):+.1f} dB")
    print(f"  delivered rate           old {OLD_RATE:.2f} Hz   "
          f"new {AUDIO_RATE:.0f} Hz")
    print(f"  output per 49152 chunk   old {49152//OLD_D} samples into a 1024"
          f" slot (2% dropped)   new {49152//D_TOTAL} exactly")


def report_signal(dur_s=0.20):
    n_iq = int(IQ_RATE * dur_s)
    print(f"Signal test: {dur_s:.2f} s of FM stereo multiplex "
          f"({n_iq} IQ samples)")
    print("  synthesising IQ...", flush=True)
    re, im = synth_mpx_iq(n_iq)
    print("  discriminating...", flush=True)
    disc = discriminate(re, im)
    print(f"  old chain: boxcar({OLD_D})...", flush=True)
    old = deemphasis(apply_chain(disc, OLD_CHAIN), OLD_RATE)
    print("  new chain: " + " -> ".join(
        f"fir({n})/{d}" for n, d in zip(TAPS_PER_STAGE, SCHEDULE))
        + "...", flush=True)
    new = deemphasis(apply_chain(disc, CHOSEN), AUDIO_RATE)

    # Drop the first 10%: the DC blocker (alpha 1/1024), the envelope
    # estimator (1/2048) and the FIR history buffers all need time to settle.
    old = old[len(old) // 10:]
    new = new[len(new) // 10:]

    print()
    print(f"  {'component':<20} {'old at':>7} {'amp':>9} "
          f"{'new at':>7} {'amp':>9} {'delta dB':>9}")
    for name, f, _amp in MPX:
        fo, fn = alias_of(f, OLD_RATE), alias_of(f, AUDIO_RATE)
        mo = goertzel_mag(old, fo, OLD_RATE)
        mn = goertzel_mag(new, fn, AUDIO_RATE)
        d = 20.0 * math.log10((mn + 1e-30) / (mo + 1e-30))
        print(f"  {name:<20} {fo:>7.0f} {mo:>9.5f} {fn:>7.0f} {mn:>9.5f} "
              f"{d:>9.2f}")

    print()
    print("  Band energy share of delivered audio (mean removed first,")
    print("  because the audio has a DC offset and nothing removes it)")
    for label, sig, fs in (("old", old, OLD_RATE), ("new", new, AUDIO_RATE)):
        mean = sum(sig) / len(sig)
        ac = [v - mean for v in sig]
        bands = [(0, 3_000), (3_000, 8_000), (8_000, 15_000), (15_000, fs / 2)]
        vals, tot = [], 0.0
        for lo, hi in bands:
            acc, f = 0.0, lo + 125.0
            while f < hi:
                m = goertzel_mag(ac, f, fs)
                acc += m * m
                f += 250.0
            vals.append(acc)
            tot += acc
        parts = "  ".join(f"{lo/1000:g}-{hi/1000:.4g}k {100*v/tot:5.1f}%"
                          for (lo, hi), v in zip(bands, vals))
        print(f"    {label}: {parts}")
    print()
    print("  Reference, measured on 8 s of real 96.7 MHz with the old chain")
    print("  (docs/fm-rx.md): 18% below 3 kHz, 40.3% in 8-15 kHz, 29.4% above")
    print("  15 kHz. Not directly comparable -- real programme material is")
    print("  not five tones -- but the shape is.")


def report_emit():
    rates = stage_rates()
    print("#pragma once")
    print()
    print("/*")
    print(" * FM RX decimation chain filter coefficients.")
    print(" *")
    print(" * GENERATED by scripts/fm_decim_design.py --emit -- do not edit.")
    print(" * That script also reports the multiplex rejection (--analyse),")
    print(" * the search that chose this schedule (--search-stereo), and")
    print(" * asserts every design invariant (--selftest). Re-run it rather")
    print(" * than tuning these by hand.")
    print(" *")
    print(f" * Schedule {'x'.join(map(str, SCHEDULE))}, total decimation "
          f"{D_TOTAL}: {IQ_RATE} -> {AUDIO_RATE:.0f} Hz")
    for i, (dec, (r_in, r_out)) in enumerate(zip(SCHEDULE, rates)):
        print(f" *   stage {i+1}: {TAPS_PER_STAGE[i]:>3} taps, decimate {dec}"
              f", {r_in:>9.0f} -> {r_out:>8.0f} Hz")
    print(" *")
    print(f" * Passband 0-{PASS_HZ} Hz flat to "
          f"{max(abs(chain_gain_db(CHOSEN, f)[0]) for f in range(100, PASS_HZ+1, 100)):.3f}"
          " dB (stage 1 and 2 droop")
    print(" * compensated inside the stage 3 taps, at no runtime cost).")
    pilot, _ = chain_gain_db(CHOSEN, STOP_HZ)
    print(f" * Stopband {STOP_HZ} Hz +, {ATTEN_DB} dB design. "
          f"19 kHz pilot {pilot:.1f} dB.")
    print(f" * Aliased noise {aliased_noise_db(CHOSEN):.1f} dB vs direct, "
          f"f^2 weighted")
    print(f" * (the boxcar({OLD_D}) this replaces: "
          f"{aliased_noise_db(OLD_CHAIN):+.1f} dB).")
    print(" *")
    print(f" * Stage 1 output ({rates[STEREO_TAP][1]:.0f} Hz) is flat to "
          f"+/-0.02 dB across the")
    print(" * 23-57 kHz stereo composite, so it is a usable tap point for a")
    print(" * future stereo decoder. Nothing consumes it yet.")
    print(" */")
    print()
    print(f"#define FM_DECIM_STAGES {len(SCHEDULE)}")
    print(f"#define FM_DECIM_TOTAL  {D_TOTAL}")
    print(f"#define FM_DECIM_MAX_TAPS {max(TAPS_PER_STAGE)}")
    print()
    for i, n in enumerate(TAPS_PER_STAGE):
        taps = CHOSEN[i][1]
        print(f"#define FM_S{i+1}_TAPS {n}")
        print(f"#define FM_S{i+1}_DEC  {SCHEDULE[i]}")
        print(f"static const float fm_s{i+1}_taps[FM_S{i+1}_TAPS] = {{")
        for j in range(0, n, 4):
            print("    " + ", ".join(f"{t: .9e}f" for t in taps[j:j + 4])
                  + ",")
        print("};")
        print()


# ---------------------------------------------------------------------------
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
    check(49152 % D_TOTAL == 0, "49152 divides evenly by D_TOTAL")
    check(49152 // D_TOTAL == 1024, "49152/D_TOTAL == 1024 (ring slot size)")
    check(IQ_RATE % D_TOTAL == 0, "delivered rate is an exact integer")
    check(chain_rate(CHOSEN) == AUDIO_RATE, "chain output rate matches")

    # The reason D=47 had to change at all.
    check(all(OLD_D % i for i in range(2, OLD_D)),
          f"old decimation {OLD_D} is prime, so it cannot be staged")

    # The finding that killed the cheap-cascade idea: a cascade of
    # critically decimated boxcars is exactly one boxcar of the product
    # length, so splitting the old design buys nothing.
    worst = 0.0
    for f in (15e3, 57e3, 160e3, 400e3, 900e3):
        a, _ = chain_gain_db([("box", 4), ("box", 3)], f)
        b, _ = chain_gain_db([("box", 12)], f)
        worst = max(worst, abs(a - b))
    check(worst < 1e-12,
          f"box(4)+box(3) is identical to box(12) (worst {worst:.1e} dB)")
    check(abs(aliased_noise_db([("box", 4), ("box", 3)])
              - aliased_noise_db([("box", 12)])) < 1e-9,
          "and therefore has identical aliased noise")

    # Stereo tap point.
    tap_rate = stage_rates()[STEREO_TAP][1]
    check(tap_rate / 2 > STEREO_BW,
          f"stage {STEREO_TAP+1} Nyquist {tap_rate/2:.0f} Hz clears "
          f"{STEREO_BW:.0f} Hz RDS")
    s1 = CHOSEN[:STEREO_TAP + 1]
    comp_worst = max(abs(chain_gain_db(s1, f)[0])
                     for f in range(19_000, 57_001, 500))
    check(comp_worst < 0.1,
          f"composite flat to {comp_worst:.3f} dB at the stereo tap")

    for i, n in enumerate(TAPS_PER_STAGE):
        taps = CHOSEN[i][1]
        check(abs(sum(taps) - 1.0) < 1e-12, f"stage {i+1} taps sum to unity")
        check(all(abs(taps[k] - taps[n - 1 - k]) < 1e-12
                  for k in range(n // 2)), f"stage {i+1} taps symmetric")
        check(n % 2 == 1, f"stage {i+1} tap count odd ({n})")

    worst = max(abs(chain_gain_db(CHOSEN, f)[0])
                for f in range(100, PASS_HZ + 1, 100))
    check(worst < 0.05, f"passband flat within 0.05 dB (worst {worst:.4f})")

    g, _ = chain_gain_db(CHOSEN, 1_000)
    check(abs(g) < 0.05, f"1 kHz mono passes at {g:+.4f} dB")

    for name, f, _amp in MPX:
        if f < STOP_HZ:
            continue
        g, _ = chain_gain_db(CHOSEN, f)
        check(g < -(ATTEN_DB - 6), f"{name} at {f} Hz rejected {g:.1f} dB")

    old_an = aliased_noise_db(OLD_CHAIN)
    new_an = aliased_noise_db(CHOSEN)
    check(old_an > 0,
          f"old chain delivers more aliased than direct noise ({old_an:+.1f} dB)")
    check(new_an < -25, f"new chain aliased noise {new_an:.1f} dB")
    check(new_an < old_an - 40,
          f"aliased noise improved by {old_an - new_an:.1f} dB")

    for x, want in ((0.0, 1.0), (1.0, 1.2660658778), (5.0, 27.2398718236),
                    (10.0, 2815.7166284663)):
        got = bessel_i0(x)
        check(abs(got - want) / want < 1e-9, f"I0({x}) = {got:.10f}")

    print()
    print("all checks passed" if not fails else f"{fails} failure(s)")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--search-stereo", action="store_true",
                    dest="search_stereo")
    ap.add_argument("--analyse", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--signal", action="store_true")
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dur", type=float, default=0.20,
                    help="signal test duration in seconds (default 0.20)")
    args = ap.parse_args()

    steps = [("sweep", report_sweep),
             ("search", lambda: report_search(stereo_first=False)),
             ("search_stereo", lambda: report_search(stereo_first=True)),
             ("analyse", report_analyse),
             ("compare", report_compare),
             ("emit", report_emit)]
    if not any(getattr(args, f) for f, _ in steps) \
            and not args.signal and not args.selftest:
        ap.print_help()
        return 1
    for flag, fn in steps:
        if getattr(args, flag):
            fn()
    if args.signal:
        report_signal(args.dur)
    if args.selftest:
        return selftest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
