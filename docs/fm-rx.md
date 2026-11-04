# FM Receive

FM broadcast receive on the PlutoSDR. The daemon captures IQ at 2.1 MSPS,
discriminates, decimates to audio, and decodes RDS from a parallel tap. This
document states the signal chain, why each stage is shaped the way it is, and
the measured behaviour behind those choices.

Everything here is measured on a Pluto (Zynq 7010, two Cortex-A9 at 667 MHz).
See `fm-tx.md` for transmit, `pluto-hardware.md` for board-level facts, and
`ARCHITECTURE.md` for the mode and threading model.

## Signal chain

```
DDR ring -> float IQ -> DC block -> discriminator -> 3-stage FIR decimate -> de-emphasis -> DATA_FM
  2.1 MSPS                                        /6, /4, /2 = /48         75 us     43750 Hz
```

RDS taps the stage-1 output:

```
stage-1 MPX (350 kHz) -> mix 57 kHz -> /8 -> /4 -> 10.9375 kHz complex
  -> resample to 19 kHz (16 samples/bit) -> 16-phase biphase bank
  -> differential BPSK bits -> 26-bit block sync/CRC
  -> groups (0A/0B, 2A/2B, 4A, 10A, 3A/3B) -> rds_state_t
```

The decimator taps are generated, not hand-written:
`scripts/fm_decim_design.py --emit` prints `firmware/src/fm_decim_taps.h`,
and `scripts/rds_decim_design.py --emit` prints
`firmware/src/rds_decim_taps.h`.

Every `DATA_FM` frame carries its own audio rate. A client reads the rate from
the frame and never assumes a constant: it is a property of the decimator
chain and has changed once (44680.85 -> 43750 Hz).

## Decimation factor is part of the design

The factor must factor into stages. 47 is prime, so a decimator built on it
cannot be split at all. One stage handling the 15 kHz -> 19 kHz transition at
2.1 MSPS needs ~1900 taps for 60 dB, or 83 MMAC/s. Split 6 x 4 x 2, the same
job takes 33 + 23 + 81 taps and 17 MMAC/s. The factorization *is* the design.

48 also makes 49152 / 48 exactly 1024, the ring slot size, so the capture
chunk divides without truncation.

## Cascading boxcars buys nothing

A cascade of boxcars whose lengths equal their decimation factors is identical
to one boxcar of the product length:

```
[sin(4x)/(4 sin x)] * [sin(12x)/(3 sin 4x)] = sin(12x)/(12 sin x)
```

Verified numerically to 1e-15. Adding a second averaging stage is therefore
void. Longer boxcars do not help either: their stopband decays at only
6 dB/octave, while the aliased noise that matters is dominated by far
sidelobes. The high-rate stage has to be a real filter.
`scripts/fm_decim_design.py --selftest` asserts the identity.

## Rank decimators by aliased noise, not worst-case fold

An FM discriminator turns a flat RF noise floor into an output noise PSD that
rises as f^2. What matters is the integral over every band that folds into the
passband, weighted by f^2.

A worst-single-fold figure can look acceptable while hiding the problem. A
boxcar decimator scores -20.6 dB on that metric, yet delivers 18.1 dB more
aliased noise than direct noise on the f^2 integral. The three-stage chain
delivers -30.8 dB, a 48.8 dB improvement. A flat-noise metric understates
high-frequency folding by 20 dB or more.

## The chain

Three Kaiser FIRs, 6 x 4 x 2, designed for 60 dB, passband 0-15 kHz, stopband
from 19 kHz so the pilot lands at full attenuation:

| stage | taps | decim | rate in -> out | role |
|---|---|---|---|---|
| 1 | 33 | 6 | 2100000 -> 350000 | keeps the 57 kHz composite alias-free |
| 2 | 23 | 4 | 350000 -> 87500 | narrows to the audio band |
| 3 | 81 | 2 | 87500 -> 43750 | 15k/19k transition + droop compensation |

Predicted rejection: 19 kHz pilot -62.1 dB, 38 kHz stereo sidebands -84.6 and
-96.4 dB, 57 kHz RDS -138.8 dB, passband flat to 0.009 dB. Stages 1 and 2
droop; stage 3 carries the inverse, so compensation costs nothing at runtime.

The schedule comes from searching every ordered factorization of 48
(`--search-stereo`), not from a plausible-looking split. It has the best
aliased-noise figure of any stereo-capable schedule. Cheaper schedules exist
(13.7 MMAC/s against 17.1) and reject less.

**Stage 1's output is a deliberate stereo tap point.** It runs at 350 kHz,
Nyquist 175 kHz, flat to +/-0.02 dB across the whole 23-57 kHz composite. A
future stereo decoder taps there and adds its own chain. Nothing is exposed
for it yet. Keeping it open costs 3.2 MMAC/s (17.1 against 13.9 for a
mono-only chain); a schedule that decimated harder in stage 1 would destroy
RDS and the upper L-R sideband.

## Measured on air

8 s captures at 96.7 MHz:

| | boxcar (/47) | 3-stage chain |
|---|---|---|
| below 3 kHz | 18% | 65-83% |
| 8-15 kHz | **40.3%** | **4.1-6.5%** |
| above 15 kHz | **29.4%** | **0.7-0.9%** |
| 19 kHz pilot | -2.8 dB (passed) | **-83 dB** |
| short frames | every frame (1045 into 1024) | **0 of 342** |
| realtime | 100% | 99.85-100.05% |

Energy above 15 kHz is the cleanest single number: an FM transmitter
band-limits program audio to 15 kHz, so anything above it is pilot or alias,
never program. The shares below 15 kHz move between runs because the
material is live; the >15 kHz figure does not.

The narrow probes at 2750 / 8750 / 13250 Hz that
`scripts/fm_rx_measure.py` prints are not diagnostic on real material. Those
frequencies are inside the audio band, where music has its own energy, so they
measure program plus alias and cannot separate them. The synthetic multiplex
in `firmware/tests/test_fm_rx.c` pins them instead, because there the only
energy at those frequencies is the alias: -92 to -155 dB relative to the mono
tone.

## The stopband cannot regress silently

`test_fm_rx.c` synthesizes a full FM stereo multiplex -- 1 kHz mono, 19 kHz
pilot, 38+/-3 kHz DSB-SC, 57 kHz RDS, at standard injection levels -- and asserts
each alias is 60 dB below the recovered mono tone. A boxcar decimator fails by
27 dB.

Passband-only tests cannot see this defect, and a clean tone from the
transmitter cannot excite it either. The synthetic multiplex is the only
signal that does.

## AD9361 hardware RX FIR

The chip's programmable RX FIR coefficients are Q1.15, so the filter's DC gain
is `sum(taps) / 32768`. Keep the sum at unity.

The `GAIN` field does not scale the taps. The driver maps it to the chip's
four-step RX FIR gain register (`val = 3 - (gain_dB + 12)/6`, i.e. only
+6/0/-6/-12 dB), so a tap set with the wrong sum cannot be compensated there.

A tap set summing to 327724 (+20 dB) saturates the accumulator at high RX
gain. Live FM decode then produces zero groups with no PS/RT. The saturation
is sticky: lowering gain, re-entering FM, restarting the daemon, an ENSM
alert/fdd cycle, and `rssi_gain_step`/`rf_dc_offs` recalibration all fail to
clear it; only a power cycle does. Disabling `in_voltage_filter_fir_en`
bypasses it immediately, which is the tell.

IQ mode has the RX FIR disabled. "Offline IQ decodes, live FM does not"
therefore points at the hardware filter, not the signal or the decoder.

Normalize the taps to sum 32768 (`firmware/src/fm_rx_fir.h`) and assert it in
`firmware/tests/test_fm_rx_fir.c`. Decode then holds across 20-70 dB of gain
(best 30-50), and the AGC works at 61 dB. Unity costs nothing in the stopband:
the quantized response matches ideal within ~0.2 dB at 300 kHz.

## RDS on a weak signal

### It is signal-limited; an LNA at the port clears it

At the edge of coverage, a station decodes zero groups at every manual gain
from 40 to 70 and in both AGC modes. A 20 dB wideband LNA (0.1 MHz-6 GHz)
ahead of the Pluto makes it solid: synced, complete PS and RT, ~25-45 groups
per 5 s window, and about 19 dB hotter at the ADC.

The LNA must be matched by dropping manual gain. At gain 40 the ADC reaches
full scale (capture rms 0.98) and decode collapses; 50-60 is worse than 30-35.
Attenuate the front end, not the decode.

The LNA's power switch is not a bypass. Unpowered it kills the path: the
station vanishes from the capture (rms 0.0039, no carrier). Only "powered"
versus "removed from the chain" is a meaningful comparison.

`in_voltage0_rssi` is not a usable level index. It moves the wrong way: high
with no signal, lower with the antenna direct, lowest with the strongest LNA
input. Use captured rms or, better, RDS group yield.

Do not attribute a capture-to-capture CNR change to the receiver. A station
near the decoder cliff fades across it; repeat captures minutes apart recover
with no setting change. The LNA lowers the system noise figure enough to keep
the station above the cliff, which is the real effect.

### A cabled loopback proves the chain, not the noise figure

`firmware/tests/rds_mpx_gen.c` (`make rds-mpx`) synthesizes an FM multiplex --
1 kHz program + 19 kHz pilot + 57 kHz RDS carrying real groups -- as packed
12-bit IQ. It decodes 140/140 groups offline with 0 block errors.

Through TX -> 20 dB -> RX at gain 40, the captured signal decodes PI, PS, and RT
with 1-6 block errors per ~360 across **40 dB** of TX attenuation (capture rms
0.09-0.91). It fails only at rms > 1.0, where the ADC clips. The RX and
decoder therefore have large margin; on-air RDS failures are CNR at the
antenna.

A cabled loopback cannot measure the LNA's noise figure. With no noise in the
cable, the LNA's gain is re-attenuated and its NF is irrelevant. Only an
on-air or noise-injected test measures NF.

The multiplex must include program audio. An RDS-only signal does not decode
through the daemon, because the FM path's DC block subtracts the signal's own
mean and distorts a single subcarrier. The generator always includes a
program tone and the pilot.

### A scalar CNR does not predict decode

Neither the +/-120 kHz versus 300-900 kHz noise ratio nor a constant-envelope
|z| variance predicts group yield reliably. Adjacent channels and ADC clipping
confound both: an LNA-removed capture can read "38.7 dB" while decoding zero
groups, and a capture full of strong adjacent channels can have the worst
variance and still decode.

Record group yield, block errors, and sync losses. Do not reduce receiver
quality to one number.

### PI is confirmed before it is exposed

A random 26-bit window matches an offset syndrome about once per 1024 bit
positions per phase, so the first match can commit a PI out of noise. That is
the on-air "flicker": a marginal station shows random PI/PTY values while
unsynced.

PI is exposed only after `RDS_PI_CONFIRM` (2) consecutive A blocks carry the
same value. Once committed, a different PI must also repeat before it replaces
the station and resets its PS/RT. The D block's copy of PI is deliberately not
re-counted, or a single group would confirm itself.

`test_no_signal_does_not_invent_pi` feeds 4 s of pure noise and asserts
`pi_valid` stays 0. The cost is a one-group (~0.35 s) delay in PI lock and in
resetting on a station change.

## Cost

### Where RX time goes

Measured in the daemon on the real 49152-sample chunk at 2.1 MSPS:

| | boxcar | 3-stage FIR | |
|---|---|---|---|
| `dma_rx_read` (DDR -> float) | 8.6% | 8.5% | unchanged |
| `fm_demod_process` | 16.4% | **31.2%** | discriminator ~16% + decimator ~15% |
| producer total (`shesha debug cpu`) | 27.3% | **41.9%** | +14.6 points |

These numbers predate the symmetric-folding `fir_dot` described below; its
decimator and producer rows are now lower.

That is the price of 48.8 dB of aliasing rejection. It fits inside two cores
(102.9% of 200% with TX), but it does not fit a one-core TX+RX budget, which
the boxcar held at 88.3%.

### The RDS tap is what doubles the demod

The same in-daemon instrument, one core, RX alone:

| build | demod |
|---|---|
| audio only (RDS removed) | 31.9% (7.44 ms/chunk) |
| + `freq_rds` divide, forced live | 42.7% (10.01) |
| + second 33-tap stage-1 decimator + mpx | 55.6% (13.02) |
| + batched `rds_rx_process` (full) | 64.4% (15.0) |

Of the ~32.5-point RDS increment, the per-sample normalization divide and the
live state it forces account for 10.9 points, the duplicate full-rate
decimator 12.9, and the batched decoder 8.7. The RDS tap costs about a third of
a core, not 5%.

Static inspection of the `-O3` ARM loop shows the audio path already spills
`dc_i`/`dc_q` to the stack every sample, so the wider RDS live set adds only
~2 stack accesses per sample. Spills are a contributor, not the cause.

### Holding the reciprocal

Refreshing `inv_denom` once per stage-1 output, as `inv_a2` already is, instead
of dividing every sample takes the demod from 64.4% to 57.9% (producer 74% to
67.7%), with the fade test and on-air PI/PS intact.

An isolated A/B measures a 10.9-point win; in place it is 6.5. The isolated
variant had the second decimator removed, so it measured the divide without
the register pressure it actually competes with; in place, part of the gain
hides behind the FIR. The remainder is anchored by the duplicate stage-1
decimator, which cannot be folded into the audio path: that path's
smoothed-power normalization is exactly what fails the fade bound, so the two
taps need different inputs.

### The decimator inner loop

`make bench-fir` cross-compiles `firmware/tests/bench_fir.c` with the daemon's
exact flags and runs it on the target. Isolated, per output sample:

| variant | 33 tap | 23 tap | 81 tap | whole decimator |
|---|---|---|---|---|
| single accumulator, runtime n | 382.8 ns | 351.0 ns | 645.2 ns | 19.3% |
| four accumulators, runtime n | 375.1 | 344.1 | 568.7 | 18.6% |
| single accumulator, const n | 316.4 | 301.4 | 496.5 | 15.9% |
| four accumulators, const n | 361.6 | 342.0 | 549.3 | 18.1% |
| symmetric folding, const n (deployed) | 283.5 | 342.0 | 454.6 | **14.9%** |
| four accumulators, uint16_t j | 365.5 | 328.5 | 549.3 | 18.1% |
| eight accumulators, const n | 361.5 | 349.5 | 560.0 | 18.2% |

These are isolated numbers under the production flags, and they disagree with
the older `-O2` run that chose the previous shape:

- **Symmetric folding is now the cheapest isolated loop.** It halves the
  multiplies and is 3.2 points ahead of the four-accumulator shape it replaced.
- **Four accumulators are no longer a win.** A single compile-time accumulator
  is faster than both four-accumulator variants. The vectoriser already hides
  the `vmla.f32` dependency chain.
- **Eight accumulators no longer spill catastrophically.** At 18.2% it ties
  four, so the old 21-point loss was an artifact of the older codegen.

`fir_dot` now uses symmetric folding. An A/B/A/B run on the target, with the
daemon in FM RX at 2.1 MSPS, dropped the producer thread from 92.9% to 88.9% of
one core. The older in-place result that chose four accumulators (37.9% against
36.0%, then 31.2% `noinline`) was measured under the `-O2` baseline.

### The demodulator, not the convert path

At baseline, `fm_demod_process` is 3.6x the DDR-to-float convert that feeds it.
The convert path costs 8.5% of a core with `memcpy` staging, and staging wins
1.18x. The demodulator is the cost, not the convert path.

DDR is mapped uncached and `volatile` forbids the compiler widening the loads.
Both are real; they are worth 1.6 points, not 19. See `pluto-hardware.md` for
why `O_SYNC` stays.

## Tools

| tool | question |
|---|---|
| `fm_decim_design.py --selftest --search-stereo` | is the filter design sound, and is this the best schedule? |
| `make test-firmware` | does the chain meet the stopband on a synthetic multiplex? |
| `make bench-fir` | how fast is the decimator on the target? |
| `make rds-mpx` | does the RDS decoder work on a synthetic multiplex? |
| `scripts/fm_rx_measure.py` | what does the live station look like by band? |
| `shesha debug cpu` | where does the RX producer spend a core? |

All measurements assume the daemon is stopped unless the tool says otherwise.
