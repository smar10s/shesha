# PSD / Waterfall

The spectrum display: the Pluto computes FFTs on its own ARM cores and ships
only the resulting bins, instead of streaming raw IQ to the host. This is the
project's central bet, so this document states what that buys, what it costs,
and where it stops working.

Everything here is measured on a PlutoSDR (Zynq 7010, two Cortex-A9 at 667
MHz) with the tools in `firmware/tools/`. Reproduce with `make
bench-waterfall`, `make bench-cover`, `make bench-scale`.

## Why on-device at all

A host-side FFT has to get raw 32-bit IQ across the wire first. The Pluto's
link is USB 2.0 (and TCP over the RNDIS gadget), 4 bytes per complex sample at
~30-40 MB/s practical throughput, so sustained real-time tops out at
**~5-7 MSPS**. That is the raw path; the packed 8/12-bit IQ path in `iq.md`
carries fewer bytes per sample and has a lower measured ceiling. Above that
the IIO DDR ring overruns and samples are dropped at the source. "20 MSPS
waterfall" on a host-side tool means the first few MSPS in real time.

Shipping bins instead moves the ceiling somewhere else. At N=4096 and 50 fps
the PSD stream is ~800 KiB/s, about 100x less than raw IQ, so the link stops
being the constraint and the ARM cores become it. The cost is **duty cycle**:
the FFT competes with everything else on two cores.

Neither approach is free, and they fail differently:

| | host-side FFT | shesha on-device PSD |
|---|---|---|
| Bottleneck | USB 2.0 link | ARM compute |
| Real-time ceiling | ~5-7 MSPS | 20+ MSPS, at reduced coverage |
| What is lost when saturated | dropped buffers at the source | samples between frames |
| Loss visible? | yes, as IIO overruns | yes, as coverage (below) |
| Raw IQ available? | yes, that is the point | no -- `iq` mode hits the same USB2 ceiling |

A fabric FFT (cf. maia-sdr) has neither ceiling, because the transform never
touches an ARM core or the link. That is the real answer at high rates and is
out of scope here; this document covers the software implementation.

## The two numbers: duty and coverage

**Duty cycle** is what the PSD costs: the producer thread's share of one core.

**Coverage** is what it buys: the fraction of the incoming sample stream that
actually passes through an FFT. Unlike duty, it says how much signal you get,
and it is exactly measurable: the producer transforms exactly `N` samples per
frame and the ADC produces exactly `fs` samples per second, so

    coverage = frames x N / samples produced

is arithmetic on two counters, with no signal, detector, or threshold
involved. `wf_cover` reads both from the DMA write pointer.

Coverage below 100% means the spectrum is **gappy in time**: some of the
signal was never transformed, and a short burst landing in a gap is not
displayed at all. It is not a resolution or accuracy loss -- the frames that
are computed are exact.

### Per-frame compute, `C(N)`

`make bench-waterfall ARGS=1500`, microseconds per frame:

| N | convert+window | FFT | magnitude | `C` | `fs_max` |
|---:|---:|---:|---:|---:|---:|
| 256 | 10.7 | 19.7 | 4.6 | 35.1 | 7.30 |
| 512 | 20.0 | 42.0 | 8.4 | 70.4 | 7.27 |
| 1024 | 39.7 | 91.6 | 16.1 | 147.3 | 6.95 |
| 2048 | 76.2 | 198.7 | 30.0 | 304.9 | 6.72 |
| 4096 | 153.1 | 551.7 | 62.2 | 767.0 | 5.34 |
| 8192 | 312.2 | 1247.1 | 128.9 | 1688.3 | 4.85 |

`fs_max = N/C` in MSPS is the sample rate at which compute exactly keeps up
with the ADC on one core. The FFT is ~72% of the frame at N=4096; it is
FFTW3's NEON codelets and is not ours to improve further.

Duty is not simply `C x fps`, because the wait for samples is not free: the
producer polls the DMA write pointer with `usleep(50)`, which burns about 16-19%
of the wait. With `W = N/fs`:

    duty(fs, N) ~ (C + k*W) / (C + W),  k ~ 0.17

Validated against `shesha debug cpu` end-to-end. Ignoring `k` underestimates the
producer by ~11 points at 2.1 MSPS.

### The operating envelope

`make bench-cover ARGS=2`, free-running producer, one core:

| fs | N | frames/s | coverage | duty |
|---:|---:|---:|---:|---:|
| 2.1 MSPS | 1024 | 2051 | **100%** | 38% |
| 2.1 MSPS | 4096 | 512 | **100%** | 46% |
| 2.1 MSPS | 8192 | 256 | **100%** | 50% |
| 5.0 MSPS | 1024 | 4882 | **100%** | 70% |
| 5.0 MSPS | 4096 | 1220 | **100%** | 93% |
| 5.0 MSPS | 8192 | 608 | 99.6% | 100% |
| 20 MSPS | 1024 | 7707 | 39.5% | 100% |
| 20 MSPS | 4096 | 1346 | 27.6% | 100% |
| 20 MSPS | 8192 | 625 | 25.6% | 100% |

Read it as three regimes:

- **`fs` well below `fs_max`** (e.g. 2.1 MSPS): coverage is 100% -- every
  sample is transformed -- and duty is the cost. Nothing is lost.
- **`fs` near `fs_max`** (5 MSPS at N=4096): still 100% coverage, but a core
  is nearly saturated. This is the practical sweet spot, and notably it is
  also where a host-side FFT runs out of link.
- **`fs` above `fs_max`** (20 MSPS): coverage falls as `fs_max/fs` and a core
  is pinned. The display is live and correct but temporally gappy.

Coverage above 100% is not possible: the producer uses no overlap (hop == N).

## What gappy coverage actually costs

Coverage predicts detection. `wf_burst` transmits a gated tone of known
length into an attenuated loopback and counts how many bursts appear in a
frame, at N=4096 / 20 MSPS (coverage 24%, frame 205 us, gap ~640 us):

| burst length | predicted | measured |
|---:|---:|---:|
| 50 us | 30.1% | 31.0% |
| 100 us | 36.0% | 34.0% |
| 205 us | 48.4% | 45.0% |
| 500 us | 83.3% | 82.5% |
| 1 ms | 100% | 102% |
| 5 ms | 100% | 100% |

So the rule of thumb is: **a signal longer than the gap between frames is
always seen; one much shorter is seen with probability ~= coverage.** At 20
MSPS / N=4096 that boundary is around 700 us. For continuous signals -- which
is most spectrum monitoring -- coverage is irrelevant and 20 MSPS is fine.
For short bursts it is the number that matters, and the fix is a lower sample
rate (coverage rises to 100%) or a smaller `N`.

## Knobs

| knob | command | effect | affects producer CPU? |
|---|---|---|---|
| sample rate | `:set bw` (waterfall derives `sr = bw`) | spectrum span; sets coverage via `fs_max/fs` | **yes** -- the main lever |
| FFT size | `:set fft_size` | resolution; larger N = lower `fs_max` | **yes** |
| workers | `:set workers` | run the producer on a second core; clamped to 1 at or below `fs_max` | **yes** -- only above `fs_max` |
| FPS | `:set fps` | delivery rate to the client | **no** -- gates the drain only |
| accum | `:set viz.waterfall.accum` | how buffered frames reduce to one | no |
| window | `:set viz.waterfall.window` | sidelobe/resolution trade | no |

Two of these commonly surprise:

**FPS does not reduce producer CPU.** The producer is un-paced; `fps` gates
only the drain/broadcast side. Lowering it reduces network traffic and client
work, not ARM load.

**`accum` is a reduction, not a producer setting.** Because the producer is
un-paced it computes many frames per broadcast (~27 at N=4096 / 20 MSPS), and
`accum` decides how they collapse into the one frame sent:

- `none` -- the latest frame; the rest are discarded.
- `max` (default) -- per-bin peak-hold across all of them.
- `avg` -- per-bin mean power across all of them.

`max` and `avg` therefore use the whole covered record, and are what makes
free-running worthwhile: with `none`, 26 of 27 computed frames are thrown
away. If you set `accum none`, you are paying full duty for ~1/27 of the
benefit. Prefer `max` for finding things and `avg` for measuring them.

The reduction runs in the linear power domain and the dB conversion happens
once, afterwards. Averaging dB would be a geometric mean -- about 2.4 dB low
on noise-like bins, and biased against brief signals.

## Pacing: why the producer free-runs

The producer could be paced to the delivery rate instead of running flat out.
The measured trade at 50 fps:

| | free-running | paced to 50 fps |
|---|---|---|
| duty @ 20 MSPS, N=4096 | 100% | 4% |
| coverage | 27.6% | 1.03% |
| coverage @ 2.1 MSPS | 100% | 9.9% |

Pacing frees ~95% of a core and gives up **10-27x of the coverage**. For a
spectrum display whose job is to show you signals that are there, that is the
wrong side of the trade: at 2.1 MSPS it would mean discarding 90% of the
signal while a core sat idle.

So the producer free-runs by design, and the cost is in the table above. The
knob that actually controls the cost is the sample rate, which also controls
coverage -- one lever, both effects, which is simpler to reason about than a
separate CPU budget.

Two caveats:
- With `accum none` the argument does not hold, because the extra frames are
  discarded. That combination is supported but wasteful.
- "100% duty" means the producer will use a whole core when one is available.
  It is a normal thread at normal priority, so it yields to TX, the poll loop,
  and the other mode producers; it does not starve them.

## Latency

The producer reads contiguous frames, so it stays as close to live as compute
allows, and skips forward when it falls more than `WF_MAX_LAG_FRAMES` (4)
frames behind the DMA writer. At N=4096 / 20 MSPS that bounds display lag at
~0.8 ms.

The bound matters: the DDR ring holds 33.5 M samples, so without it the
producer settles 1.68 s behind at 20 MSPS and shows spectrum from the recent
past. Capping lag costs no coverage -- above `fs_max`, coverage is set by how
fast frames can be computed, not by where in the ring they are read.

## The second core

These numbers assume CPU1 is online (`nproc` = 2); see
`pluto-hardware.md` for how and why.

`make bench-scale` runs the pipeline on one and two pinned cores over disjoint
blocks of the live stream:

| N | 1 core | 2 cores | speedup |
|---:|---:|---:|---:|
| 512 | 6.21 MSPS | 12.25 | 1.97x |
| 1024 | 5.75 | 11.59 | 2.02x |
| 2048 | 5.39 | 10.60 | 1.97x |
| 4096 | 4.47 | 8.81 | 1.97x |
| 8192 | 4.17 | 8.01 | 1.92x |

Near-linear, with no memory-bandwidth wall. But peak throughput is not the
decision: what matters is coverage gained against total daemon duty spent at a
rate someone would actually use, measured on the daemon itself, one worker
against two on the same build:

| fs | N | cover 1w | total 1w | cover 2w | total 2w | gain |
|---:|---:|---:|---:|---:|---:|---:|
| 2.1 MSPS | 4096 | 100.0% | 56% | 100.0% | 73% | 1.00x |
| 5.0 MSPS | 4096 | 100.0% | 115% | 100.0% | 131% | 1.00x |
| 10 MSPS | 1024 | 76.9% | 124% | 92.1% | 168% | 1.20x |
| 10 MSPS | 4096 | 62.3% | 120% | **87.3%** | 199% | 1.40x |
| 20 MSPS | 1024 | 41.4% | 124% | 64.3% | 199% | 1.55x |
| 20 MSPS | 4096 | 32.5% | 120% | 48.0% | 199% | 1.48x |
| 20 MSPS | 8192 | 31.6% | 121% | 42.6% | 199% | 1.35x |

`total` is the whole daemon's share of one core (producer plus drain), so it
can exceed 100% when both cores are busy. Producer-only duty at the same point
is lower (`pluto-hardware.md`).

Read it as two regimes, split at `fs_max`:

- **At or below `fs_max`** (2.1 and 5 MSPS): the gain is exactly **1.00x**. One
  core already transforms every sample, so there is nothing left to cover and
  the second worker only waits -- but waiting is not free, because it polls
  `WR_PTR`. It adds 14-17 points of total duty for no benefit whatsoever.
  A second worker here is pure tax.
- **Above `fs_max`** (10 and 20 MSPS): the gain tracks the cost. At 10 MSPS /
  N=4096 the second core turns 62% coverage into 87%; at 20 MSPS it nearly
  doubles a smaller figure.

The operating point the second core unlocks is **10 MSPS**: 87% coverage at
N=4096 and 92% at N=1024, against a ~5 MSPS USB2 ceiling for a host-side FFT.
At 20 MSPS the scaling is just as efficient but coverage stays gappy (~48%) --
the second core buys span, not completeness.

**Coverage tops out at ~87%, not ~100%, and the drain is why.**
`make bench-workers` measures the pipeline alone: it discards every frame and
owns both cores, so it reports ~100% at 10 MSPS. The daemon must also
accumulate ~37 frames of 16 KB per broadcast, convert to dB, and push TCP, and
that drain costs about 32% of a core:

    prototype : 184% producer +  0% consumer = 184% of 200
    daemon    : 167% producer + 32% consumer = 199% of 200  <- saturated

A causal check: dropping fps from 50 to 5 freed 10 points of consumer duty and
coverage rose 86.4% -> 91.5%. So ~87% is the SoC being full. Measure two-core
coverage on the daemon; `wf_workers` overstates it.

Tightening the capture-wait sleep does not raise coverage. The wait loop is
entered only when the producer is caught up (`produced < target`), so it is
idle time by construction. Where coverage is below 100%, the producer is
compute-bound and does not wait at all.

The worker count is therefore a knob rather than a constant, because below
`fs_max` it is dead weight. `:set workers 1|2` defaults to 1 and is clamped to
1 at or below `fs_max` -- the measured policy in the table above, as pure
arithmetic in `wf_workers_policy.c` and host-tested. The effective count is
recomputed from the current rate and FFT size on every producer start; a rate
or FFT change cycles the producer, so the clamp is applied every cycle (below
`fs_max` a request for two shows as one). `RESP_STATUS` reports the live count,
so it reads zero while the producer is stopped.

## The shared claim

Both workers draw blocks from one shared read position (`wf_claim.h`). A shared
claim is mandatory: two private read pointers would latch the same `WR_PTR` and
transform the same samples for twice the CPU. The claim uses a CAS rather than
a store, because both workers can notice staleness at once and only the
furthest skip may win.

The claim has seven invariants. Each is pinned by a host test in
`firmware/tests/test_wf_claim.c`:

- A block is reserved only after the ADC has written all of it, and a refusal
  leaves the read position untouched. Claiming first and abandoning on timeout
  walked the claim arbitrarily far into the future; the display then froze
  until the ADC caught up, which took minutes at a high rate.
- Each producer start resets the claim and re-latches the origin. A worker,
  bandwidth, or mode change must not inherit an offset from the previous run.
- Claim origins are per block, not per worker: resolving an offset against a
  worker's own origin points at the wrong samples.
- A skip must not hand out a block that starts at the write pointer: it would
  wait a frame period for samples that do not exist, then time out.
- The produced total is one shared, full 64-bit count, and the last write
  pointer is derived from it plus the origin. The 25-bit pointer wraps every
  3.4 s at 10 MSPS, so a total recovered modulo the ring is permanently
  short; a 39-bit count packed beside the pointer also wrapped after 7.6 h
  at 20 MSPS and froze the display.
- A reading behind the published one is not a wrap: two workers poll
  concurrently, and only a backward step of more than half a ring is.
- Abandoning a block counts the worker's reserved block as well as the claim
  advance. The advance starts at `next`, which is already past it.

The non-atomic claim is caught only by the threaded contention test. Mutation
testing leaves two equivalent survivors, not gaps.

The latency cap scales with the worker count (`wf_claim_lag_frames`): W workers
hold W blocks in flight, so the newest claimed block starts out (W-1) frames
ahead of the oldest one still in progress. Without that term the cap measures
the queue rather than the backlog and abandons blocks that were never late. It
does not affect coverage.

## Accuracy

The only approximation in the chain is `wf_log10_fast`, a degree-6 Chebyshev
fit used instead of libm's `log10f`. Its error depends only on the mantissa,
so `test_wf_fastlog` sweeps all 2^23 mantissas across five exponents for a
true worst case rather than a sample: **2.67e-05 dB** with the production ARM
codegen. For scale, that is ~1/10,000 of one colour step at an 80 dB span over
256 levels, and about 3x the float rounding already present.

`-funsafe-math-optimizations` is required for the NEON float paths (ARMv7 NEON
does not implement IEEE denormal semantics, so GCC will not vectorize float
loops without it). It does not imply `-ffinite-math-only`, so NaN/Inf
semantics survive. Measured output is unchanged.

## Tools

| tool | question it answers |
|---|---|
| `make bench-waterfall` | what does a frame cost, by stage? |
| `make bench-cover` | what fraction of the stream reaches an FFT, and at what duty? |
| `make bench-scale` | does the compute parallelize across both cores? |
| `make bench-workers` | does a second producer core buy coverage, and at which rates? |
| `wf_burst` | does coverage predict what gets detected? (needs loopback) |
| `make test-wf-target` | is the fast log still accurate under production codegen? |

All need the daemon stopped, since they take over the AD9361 and the DMA.
`wf_burst` transmits -- **attenuated loopback cable only**. It is single-worker:
it owns the radio, so it cannot validate the two-worker path. Two-worker
disjointness is checked instead through `transformed + skipped` tracking ADC
production, with coverage never exceeding 100%.

`scripts/psd_baseline.py` runs against the live daemon and reports producer duty
and delivered fps. It deliberately does not report coverage: deriving it from
the wire frame index reads 100% everywhere. Read coverage from
`status.psd_coverage` or `make bench-cover`.

Several measurement scripts end with a mode change to force a producer exit and
flush the per-worker tally. If a later run reports a stuck sample rate and no
`wf-producer` threads, the daemon is still in IQ mode -- set waterfall first.
The tally prints on every producer exit and is reset each cycle, so an
unbalanced worker set is visible rather than hidden in aggregate coverage.
