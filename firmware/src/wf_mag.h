#pragma once
#include <stddef.h>
#include "wf_fastlog.h"

/*
 * Magnitude with fftshift, shared by the waterfall producer, the drain, and
 * the on-target benchmark (firmware/tools/wf_bench.c).
 *
 * Output: index 0 = -fs/2, index N/2 = DC, index N-1 ~ +fs/2.
 *
 * The producer emits LINEAR POWER (wf_magnitude_power); the dB conversion
 * happens once per broadcast frame in the drain (wf_power_to_db).  The split
 * matters because the producer is un-paced: it computes ~23 frames for every
 * one the drain sends at N=4096, so a per-bin log in the producer runs ~23x
 * more often than anything can observe it.  Moving it cuts the stage ~69% and
 * `C` ~16% at N=4096.
 *
 * It is also the only correct place for it.  The drain reduces several frames
 * into one, and the reduction has to happen in the power domain: averaging dB
 * is a geometric mean, which reads ~2.4 dB low for noise-like bins and
 * under-weights brief signals (the exact error ACCUM_AVG used to have).
 * ACCUM_MAX is unaffected either way -- log is monotonic, so max-of-power and
 * max-of-dB agree -- but doing both in power keeps one code path.
 *
 * The power stage avoids two costs the obvious loop pays: no per-bin sqrtf
 * (|X|^2 is what the dB conversion wants anyway, since 20*log10|X| ==
 * 10*log10|X|^2) and no (i + n/2) % n per bin (the fftshift is two contiguous
 * runs).  Both stages floor at 1e-18, i.e. -180 dB.  The floor stays an `if`,
 * not fmaxf: with -funsafe-math-optimizations the `if` if-converts and
 * vectorises, whereas fmaxf compiles to a libm call that blocks the
 * vectoriser.
 *
 * wf_log10_fast is bounded at ~4e-5 dB worst case over the whole normal-float
 * range (test_wf_fastlog.c); the pre-optimisation libm loop is kept as the
 * oracle in test_wf_mag.c.
 */

/* Power floor, shared so the producer and the drain agree on -180 dB. */
#define WF_POWER_FLOOR 1e-18f

/*
 * FFT output -> normalised linear power, fftshifted.  `norm` is the same
 * amplitude normalisation as before (1 / (N * coherent_gain)); it is squared
 * here because the output is power.
 */
static inline void wf_magnitude_power(const float *fft_real,
                                      const float *fft_imag,
                                      size_t n, float norm, float *pow_out)
{
    size_t half = n / 2;
    float norm2 = norm * norm;

    for (size_t i = 0; i < half; i++) {
        size_t src = i + half;
        float re = fft_real[src];
        float im = fft_imag[src];
        float p = (re * re + im * im) * norm2;
        if (p < WF_POWER_FLOOR)
            p = WF_POWER_FLOOR;
        pow_out[i] = p;
    }
    for (size_t i = half; i < n; i++) {
        size_t src = i - half;
        float re = fft_real[src];
        float im = fft_imag[src];
        float p = (re * re + im * im) * norm2;
        if (p < WF_POWER_FLOOR)
            p = WF_POWER_FLOOR;
        pow_out[i] = p;
    }
}

/* Linear power -> dBFS, in place or out of place.  Run once per broadcast
 * frame by the drain, after any accumulation. */
static inline void wf_power_to_db(const float *pow_in, size_t n, float *db_out)
{
    for (size_t i = 0; i < n; i++) {
        float p = pow_in[i];
        if (p < WF_POWER_FLOOR)
            p = WF_POWER_FLOOR;
        db_out[i] = 10.0f * wf_log10_fast(p);
    }
}

/*
 * Fused power+dB, the pre-split behaviour.  Retained because it is the
 * reference the host and ARM accuracy tests pin (test_wf_mag.c), and because
 * wf_bench measures it to show what the split saves.  Not on the daemon's
 * path.
 */
static inline void wf_magnitude_db(const float *fft_real, const float *fft_imag,
                                   size_t n, float norm, float *bins_out)
{
    wf_magnitude_power(fft_real, fft_imag, n, norm, bins_out);
    wf_power_to_db(bins_out, n, bins_out);
}
