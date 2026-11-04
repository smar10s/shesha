# FM Transmit

FM broadcast transmit on the PlutoSDR. The client applies pre-emphasis and
sends audio; the daemon modulates it into IQ and streams it to the DMA ring.
This document covers the modulator's design constraints, the streaming
discipline that keeps it fed, and the measured behaviour behind both.

Everything here is measured on a Pluto (Zynq 7010, two Cortex-A9 at 667 MHz).
See `fm-rx.md` for receive, `pluto-hardware.md` for board-level facts, and
`ARCHITECTURE.md` for the mode and threading model.

## Signal chain

```
audio (client) -> pre-emphasis -> NCO phase modulation -> int12 pack -> DDR stream
```

Audio arrives in chunks over `CMD_TX_STREAM_DATA`, so a long file does not
need one giant DDR frame. Pre-emphasis runs in the client
(`client/shesha/fm_audio.py`). `fm_mod.c` is compiled into both the daemon and
`firmware/tools/fm_stream_test.c`; keep it that way, so the gate certifies the
code that actually reaches the air.

## Pilot and RDS share one phase accumulator

`fm_mod.c` keeps a single pilot phase `theta` at the DAC rate. The pilot is
`cos(theta)` and the 57 kHz RDS carrier is `cos(3*theta) = 4c^3 - 3c` from the
same `c`. Two independent oscillators would drift; RDS must be exactly 3x the
pilot, and a triple angle from one phase cannot drift.

## The pilot is a table lookup

The pilot reads the same interpolated NCO table as the FM carrier, through an
integer 19 kHz phase (`sub.pilot_iphase`, one turn = 2^32).

Calling `cosf()` per DAC sample costs roughly half a Cortex-A9 core at
2.1 MS/s. It also empties the DMA ring and time-stretches the audio: `rate_frac
= emitted/elapsed/dac_rate` falls below 100%. The table lookup measures:

| | `cosf` per sample | table lookup |
|---|---|---|
| TX-thread CPU | 99.1% | 86.1% |
| minimum ring depth | 4.4k samples | 623k samples |
| `rate_frac` | 98.4% | 100.0% |

`test_fm_mod.c` pins the pilot to 19 kHz by Goertzel on the instantaneous
deviation.

## Subcarriers default off, and the disabled path is bit-identical

`fm_mod_init` leaves `sub.enabled` false. There is one generation loop per
path, not a copy: when disabled, `extra == 0.0f`, and adding `0.0f` is exact,
so the output does not move. `test_fm_mod.c` pins the disabled output at fixed
samples against a stored golden, which protects the instrument's `--verify`
vector.

## The composite lands on full deviation, not past it

Pilot 0.09 and RDS 0.03 are fractions of full deviation (standard injection),
so the modulator scales program audio to 0.88 whenever the subcarriers are
enabled: `sub.audio_amp = 1 - pilot - rds`.

The client normalizes the decoded track to 1.0 and does not scale for RDS.
Keeping the headroom in the modulator is what makes a live `CMD_SET_RDS`
on/off correct on air without reloading the track. With RDS off the audio stays
at 1.0, so loudness does not change until RDS is switched on.

`test_fm_mod.c` feeds full-scale program audio, recovers the instantaneous
modulation index from the IQ phase, and asserts the peak is <= 1.0 across the
0.88/0.09/0.03 design point. The test fails if the modulator does not apply the
headroom.

## RDS transmit

A 1 kHz program tone plus RDS (PS, RT, PI, PTY) transmitted at 20 dB
attenuation and captured through the daemon RX decodes with `pi_valid`, a
complete PS, a complete RT, and the expected PTY. A live `CMD_SET_RDS` then
changes PS and RT on air.

The program tone is required: the RX DC block subtracts the signal's own
mean and distorts a single subcarrier (see `fm-rx.md`). The encoder assumes
program audio anyway.

The loopback is a shape oracle, not a gold standard. The cabled path is
compressed and the RX is marginal, so decoded PS/RT can contain occasional
character corruption and partial segments. Assert on whole-field presence (PI,
a complete PS) rather than every packet. A station's old PS can also persist
mixed with new characters, so confirm a live change by the *new* complete PS
appearing, not by the old one vanishing.

## Streamed TX

### Starving the feeder sounds like crackle, not a drag

A uniform rate deficit time-stretches, because FM carries its timeline in the
phase trajectory. A CPU-starved feeder is bursty instead: the drain FSM parks
in `D_WAIT` repeatedly for short intervals, freezing the carrier at constant
phase. The aggregate deficit is the integral of those stalls.

A run measured 92% of nominal with **zero reported underruns**. The underrun
counter samples once per feed loop and misses sub-poll stalls. Trust
`emitted/elapsed`, not the underrun count.

The diagnostic signature is DDR depth sitting far below the high-water bound
(18 ms of a 300 ms clamp) while the audio ring grows.

### Depth is signed, not a pair of positions

A position pair is ambiguous between "ARM ahead by D" and "FPGA ahead by
BUF-D", and both occur. A feeder waiting on an "available" value derived from
positions is throttled into a permanent underrun it can never exit. Use
`dma_tx_stream_depth()`, which is signed and based on monotonic counters. Do
not reintroduce single-shot pointer comparisons.

### Arm, prefill, trigger

Triggering the stream engine on an empty ring is a trap. The drain FSM starts
immediately and runs ahead of the write cursor, so the ARM begins the race
already behind. Always arm -> prefill -> trigger, the same discipline
`dma_tx_load()` / `dma_tx_trigger()` uses for one-shot.

### 2.1 MSPS is a hard floor

The AD9361 lands on 2,100,000 SPS exactly, so there is no playback pitch error
from rate mismatch. `filter_fir_config` exists, but `filter_fir_en` is 0.

### Do not port styxsdr's NEON sincos

It lives in
`firmware/platforms/styx/firmware/tools/platform/pluto_stream_test.c`. It
applies a 7th-order Taylor series across all of [-pi, pi]; at x = pi,
`1 - x^2/2 + x^4/24 - x^6/720` gives -1.2114 against a true cos of -1. Measured
error is 432.6 DAC LSB of 2047, against 0.0073 for the NCO, despite the code
claiming a minimax fit of ~20 bits. Its dropout test only checks a chirp's
power envelope, which stays flat regardless, so that test cannot see the
defect.

It remains selectable as `-g neon` purely so `--verify` stays a regression
guard.

### Fixed-scale conversion

`convert_float_to_tx_auto()` finds a peak and scales to it. Where |IQ| is
constant by construction that pass is pure waste, and it costs slightly more
than the entire FM modulation. A fixed scale of 2047 is exact, not an
approximation.

CPU headroom at 2.1 MSPS, single core, daemon stopped (`--bench`). These
measure the generator and pack path, not the daemon's `tx-stream` thread; that
thread measures 61.0% (`pluto-hardware.md`):

| generator | float -> int12 pack | MSPS | headroom |
|---|---|---|---|
| libm | auto-scale | 2.34 | 1.11x |
| libm | fixed scale | 2.49 | 1.18x |
| nco | auto-scale | 3.35 | 1.59x |
| nco | fixed scale | 3.63 | **1.74x** |
| nco | none | 7.00 | 3.33x |

`dma_tx_stream_feed_fixed()` uses the fixed path.

## Validation

`make fm-check` (60 s) and `make fm-gate` (30 s) assert the delivered output
rate, zero underruns and starvation, DMA depth near the clamp, a flat audio
ring, and served status polls. On air into an external receiver, a 1 kHz tone,
speech, and a 100 Hz-15 kHz sweep are clean and contiguous. Speech sounding
natural is the only available confirmation that client-side pre-emphasis
matches the receiver's de-emphasis.

Run both gates with CPU1 online; see `pluto-hardware.md`. Both fit on one core
at their acceptance thresholds, but two cores remove the feeder's transient
deficit.

## Tools

| tool | question |
|---|---|
| `make fm-check` | is the output rate and DMA depth holding over 60 s? |
| `make fm-gate` | does the same hold over 30 s under polling load? |
| `fm_stream_test --verify` | does the modulator match the known-good vector? |
| `fm_stream_test --bench` | what does the modulator produce, and at what CPU cost? |
| `shesha debug cpu` | where does the TX thread spend a core? |
