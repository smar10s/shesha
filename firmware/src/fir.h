#pragma once
#include <stdint.h>

/*
 * Shared decimating FIR.
 *
 * Extracted from fm_rx.c when the RDS chain needed the same primitive: the
 * MAC loop below is tuned and measured, and a second copy would be a second
 * thing to certify with the golden vector. The audio chain and both RDS
 * decimation stages all run through this code.
 */

/*
 * Largest tap count any caller needs. Sized for the RDS stage-2 filter (89
 * taps measured, 256 with headroom); the audio chain's 81 and the RDS
 * stage-1's 75 both fit. The waste is ~1.4 kB per instance and buys one
 * implementation.
 */
#define FIR_MAX_TAPS 256

/*
 * One decimating FIR stage, polyphase: only output samples cost taps.
 *
 * hist is a double-written circular buffer of 2*n_taps. Each input is stored
 * at both `wr` and `wr + n_taps`, which keeps the n_taps most recent samples
 * contiguous at hist[wr+1 .. wr+n_taps] and lets the MAC loop run without a
 * modulo or a wrap branch per tap. Costs one extra store per input sample,
 * which at the 2.1 MSPS first audio stage is much cheaper than the
 * alternative.
 */
typedef struct {
    const float *taps;
    uint16_t n_taps;
    uint16_t decim;
    uint16_t phase;      /* inputs consumed since the last output */
    uint16_t wr;         /* write cursor, in [0, n_taps) */
    float hist[2 * FIR_MAX_TAPS];
} fir_t;

/*
 * Validate and arm one stage. Symmetry is not decoration, it is
 * load-bearing: fir_push walks the history forwards and the taps forwards,
 * which is only a convolution if the taps are symmetric. Every generated
 * stage is linear phase and odd length, and the design scripts assert it,
 * but checking here means an asymmetric table cannot silently time-reverse
 * the filter. Returns 0 on success, -1 (with a message on stderr) if not.
 */
int fir_init(fir_t *f, const float *taps, uint16_t n, uint16_t dec);

/*
 * One output sample's dot product. Deliberately NOT inlined.
 *
 * The shape is measured, not assumed. firmware/tests/bench_fir.c runs the
 * variants with the daemon's production flags. Symmetric folding is the
 * cheapest isolated loop: 14.9% of a core for the 6x4x2 schedule, against
 * 18.1% for four accumulators and 18.2% for eight. The older -O2 run ranked
 * four accumulators first; production -O3 -funsafe-math-optimizations
 * vectorises the symmetric loop instead.
 *
 * Symmetric folding works because fir_init enforces symmetric taps, so each
 * multiply serves two history samples. The loop halves the multiplies and
 * pairs the oldest and newest sample, which the compiler turns into a
 * reversed NEON load.
 *
 * noinline keeps the MAC loop out of fm_demod_process's register allocation.
 * An A/B/A/B run on the target, with the daemon in FM RX at 2.1 MSPS, dropped
 * the producer thread by about four points of one core (92.9% to 88.9%).
 * The call lands on the rare path: 481k outputs/s against 2.1M pushes/s.
 *
 * Plain C rather than NEON intrinsics on purpose. Intrinsics would need an
 * #ifdef __ARM_NEON, which means the host unit tests would exercise a scalar
 * path that never reaches the air -- the golden vector would stop certifying
 * the deployed arithmetic. The summation order here is explicit, so host and
 * target agree bit for bit.
 *
 * `n` is int, not the uint16_t it comes from: the 16-bit loop arithmetic is
 * free when the trip count is a compile-time constant but not when it is
 * loaded from the struct.
 */
__attribute__((noinline))
float fir_dot(const float *h, const float *xs, int n);

/*
 * Push one input sample. Returns 1 and writes *out when the decimator
 * produces an output sample, 0 otherwise.
 *
 * This half stays inline because it runs at the full input rate and touches
 * no floating point beyond two stores. The window is walked oldest-first by
 * fir_dot, which is a convolution only because the taps are symmetric --
 * enforced in fir_init.
 */
static inline int fir_push(fir_t *f, float x, float *out)
{
    const uint16_t n = f->n_taps;
    const uint16_t w = f->wr;

    f->hist[w] = x;
    f->hist[w + n] = x;
    f->wr = (uint16_t)((w + 1u == n) ? 0u : (unsigned)(w + 1u));

    if (++f->phase < f->decim)
        return 0;
    f->phase = 0;

    *out = fir_dot(f->taps, &f->hist[w + 1], (int)n);
    return 1;
}
