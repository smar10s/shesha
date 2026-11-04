// SPDX-License-Identifier: MIT
#include "fm_mod.h"

#include <math.h>

#ifdef __ARM_NEON
#include <arm_neon.h>
#endif

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* Scratch block for the radian-phase generators.  Small enough to stay
 * on the stack in any thread, large enough that the loop overhead and
 * (for NEON) the 4-wide tail are amortised. */
#define FM_MOD_BLOCK 256

/* ==================================================================
 * NEON 4-wide sin/cos
 *
 * Ported from styxsdr firmware/tools/platform/pluto_stream_test.c, where
 * its comment claims "minimax, ~20 bits".  It is not: this is a 7th-order
 * Taylor series applied across all of [-pi, pi], and at x = pi
 * 1 - x^2/2 + x^4/24 - x^6/720 evaluates to -1.2114 against a true cos of
 * -1.  fm_stream_test --verify measures 432 DAC LSB of 2047.
 *
 * Kept only so that measurement stays reproducible — do not select it for
 * transmission.  styxsdr's own dropout test cannot see the defect because
 * it only checks a chirp's power envelope, which stays flat regardless.
 * ================================================================== */

#ifdef __ARM_NEON

static const float SINCOS_TWO_PI     = 6.28318530717958647692f;
static const float SINCOS_INV_TWO_PI = 0.15915494309189533577f;

static inline float32x4_t neon_range_reduce(float32x4_t x)
{
    /* n = round(x / 2pi), via truncation of x/2pi +/- 0.5 */
    float32x4_t n  = vmulq_n_f32(x, SINCOS_INV_TWO_PI);
    float32x4_t hf = vdupq_n_f32(0.5f);
    uint32x4_t  neg = vcltq_f32(n, vdupq_n_f32(0.0f));
    n = vaddq_f32(n, vbslq_f32(neg, vnegq_f32(hf), hf));
    float32x4_t nf = vcvtq_f32_s32(vcvtq_s32_f32(n));
    return vmlsq_n_f32(x, nf, SINCOS_TWO_PI);
}

static inline void neon_sincos(float32x4_t x,
                               float32x4_t *out_sin,
                               float32x4_t *out_cos)
{
    x = neon_range_reduce(x);

    float32x4_t x2 = vmulq_f32(x, x);
    float32x4_t x3 = vmulq_f32(x2, x);
    float32x4_t x4 = vmulq_f32(x2, x2);
    float32x4_t x5 = vmulq_f32(x4, x);
    float32x4_t x6 = vmulq_f32(x4, x2);
    float32x4_t x7 = vmulq_f32(x6, x);

    /* sin(x) ~ x - x^3/6 + x^5/120 - x^7/5040 */
    float32x4_t s = x;
    s = vmlaq_f32(s, x3, vdupq_n_f32(-1.0f / 6.0f));
    s = vmlaq_f32(s, x5, vdupq_n_f32( 1.0f / 120.0f));
    s = vmlaq_f32(s, x7, vdupq_n_f32(-1.0f / 5040.0f));
    *out_sin = s;

    /* cos(x) ~ 1 - x^2/2 + x^4/24 - x^6/720 */
    float32x4_t c = vdupq_n_f32(1.0f);
    c = vmlaq_f32(c, x2, vdupq_n_f32(-1.0f / 2.0f));
    c = vmlaq_f32(c, x4, vdupq_n_f32( 1.0f / 24.0f));
    c = vmlaq_f32(c, x6, vdupq_n_f32(-1.0f / 720.0f));
    *out_cos = c;
}

#endif /* __ARM_NEON */

/* ==================================================================
 * Quadrature NCO: integer phase accumulator + interpolated LUT
 *
 * Chosen over a polynomial after measurement (fm_stream_test --verify):
 *
 *   - Phase lives in a uint32 in units of 2^32 = one full turn, so the
 *     modulo-2pi wrap is a free integer overflow.  No fmod, no double
 *     precision, and no drift however long it runs.  That matters for an
 *     indefinite-runtime target; styxsdr's stream test accumulates phase
 *     in a float and loses resolution after a few seconds.
 *
 *   - One table holds sin over a full turn plus a quarter turn, so cos is
 *     the same table read at a +N/4 offset.  N=4096 entries + 1024 guard
 *     = 20 KB, which fits the Cortex-A9's 32 KB L1 D-cache.
 *
 *   - Linear interpolation between entries.  Worst-case error is
 *     (pi/N)^2/2 = 2.9e-7, i.e. 6e-4 DAC LSB — three orders of magnitude
 *     below the 12-bit quantisation floor.
 * ================================================================== */

#define NCO_LUT_BITS   12
#define NCO_LUT_SIZE   (1u << NCO_LUT_BITS)          /* 4096 */
#define NCO_LUT_QUART  (NCO_LUT_SIZE / 4)            /* cos offset */
#define NCO_FRAC_BITS  (32 - NCO_LUT_BITS)           /* 20 */
#define NCO_FRAC_MASK  ((1u << NCO_FRAC_BITS) - 1u)
#define NCO_FRAC_SCALE (1.0f / (float)(1u << NCO_FRAC_BITS))

/* +1 so the final linear-interpolation pair never reads past the end. */
static float g_nco_lut[NCO_LUT_SIZE + NCO_LUT_QUART + 1];
static bool  g_nco_lut_ready;

void fm_mod_lut_init(void)
{
    if (g_nco_lut_ready)
        return;
    for (uint32_t i = 0; i < NCO_LUT_SIZE + NCO_LUT_QUART + 1; i++)
        g_nco_lut[i] = sinf(2.0f * (float)M_PI * (float)(i % NCO_LUT_SIZE)
                            / (float)NCO_LUT_SIZE);
    g_nco_lut_ready = true;
}

static inline void nco_sincos(uint32_t phase, float *s, float *c)
{
    uint32_t idx  = phase >> NCO_FRAC_BITS;
    float    frac = (float)(phase & NCO_FRAC_MASK) * NCO_FRAC_SCALE;

    const float *ls = &g_nco_lut[idx];
    const float *lc = &g_nco_lut[idx + NCO_LUT_QUART];

    *s = ls[0] + frac * (ls[1] - ls[0]);
    *c = lc[0] + frac * (lc[1] - lc[0]);
}

/* Convert a phase increment to the uint32 accumulator.  A hostile client
 * can pair a huge deviation with a huge audio sample, so the product can
 * leave int32 range; converting that double is undefined.  Saturation is
 * safe here because the accumulator is modulo 2^32. */
static uint32_t nco_phase_inc(double scale, double value)
{
    double inc = scale * value;
    if (inc > 2147483647.0)
        inc = 2147483647.0;
    else if (inc < -2147483647.0)
        inc = -2147483647.0;
    return (uint32_t)(int32_t)inc;
}

/* ==================================================================
 * Modulator
 * ================================================================== */

bool fm_mod_deviation_valid(uint32_t deviation_hz, uint32_t dac_rate)
{
    return dac_rate != 0
        && (uint64_t)deviation_hz * 2 < (uint64_t)dac_rate;
}

void fm_mod_init(fm_mod_t *m, uint32_t audio_rate, uint32_t deviation_hz,
                 uint32_t dac_rate, fm_gen_kind_t kind)
{
    m->audio_pos  = 0.0;
    m->audio_step = (double)audio_rate / (double)dac_rate;
    m->phase      = 0.0;
    m->phase_step = 2.0 * M_PI * (double)deviation_hz / (double)dac_rate;
    m->iphase     = 0;
    m->idev_scale = (double)deviation_hz / (double)dac_rate * 4294967296.0;
    m->dac_rate   = dac_rate;
    m->kind       = kind;

    atomic_store_explicit(&m->sub.enabled, false, memory_order_relaxed);
    m->sub.pilot_amp  = 0.0f;
    m->sub.rds_amp    = 0.0f;
    m->sub.audio_amp  = 1.0f;
    m->sub.pilot_iphase = 0;
    m->sub.pilot_istep  = (dac_rate > 0)
        ? (uint32_t)llround(19000.0 / (double)dac_rate * 4294967296.0)
        : 0;
    rds_tx_init(&m->sub.rds, dac_rate);
}

void fm_mod_enable_subcarriers(fm_mod_t *m, float pilot_amp, float rds_amp)
{
    /* The pilot reads the NCO table below, so make sure it exists before
     * the flag that makes the TX thread take this path. */
    fm_mod_lut_init();

    /* Publish the amplitudes before the flag that makes the TX thread use
     * them, so a live enable never reads a torn pair.  The programme audio
     * is scaled to leave exactly the subcarriers' share of full deviation,
     * which is what makes a live enable/disable correct on air. */
    m->sub.pilot_amp = pilot_amp;
    m->sub.rds_amp = rds_amp;
    m->sub.audio_amp = 1.0f - pilot_amp - rds_amp;
    atomic_store_explicit(&m->sub.enabled, true, memory_order_release);
}

void fm_mod_disable_subcarriers(fm_mod_t *m)
{
    atomic_store_explicit(&m->sub.enabled, false, memory_order_release);
}

void fm_mod_set_rds(fm_mod_t *m, const rds_tx_config_t *cfg)
{
    rds_tx_set_config(&m->sub.rds, cfg);
}

static inline bool sub_on(const fm_mod_t *m)
{
    return atomic_load_explicit(&m->sub.enabled, memory_order_acquire);
}

/*
 * One DAC sample of subcarrier: pilot + biphase RDS on a coherent 57 kHz
 * carrier.  cos(3*theta) = 4c^3 - 3c uses the same `c` as the pilot, so the
 * subcarrier is exactly three times the pilot with no second oscillator.
 *
 * `c` comes from the same interpolated NCO table the carrier uses, not
 * cosf().  A libm cosf per DAC sample is ~half a Cortex-A9 core at 2.1 MS/s
 * — enough to starve the DMA ring and time-stretch the audio — while a LUT
 * read is a few cycles and its 2.9e-7 error is far below the 12-bit floor.
 */
static inline float fm_mod_sub_sample(fm_mod_t *m)
{
    float s, c;
    nco_sincos(m->sub.pilot_iphase, &s, &c);
    (void)s;
    float cos3 = 4.0f * c * c * c - 3.0f * c;
    float chip = rds_tx_chip(&m->sub.rds);
    float extra = m->sub.pilot_amp * c + m->sub.rds_amp * chip * cos3;

    m->sub.pilot_iphase += m->sub.pilot_istep;
    return extra;
}

/* Linear interpolation with wraparound, for the looping-buffer case. */
static inline float interp_wrap(const float *audio, uint32_t n, double pos)
{
    uint32_t i = (uint32_t)pos;
    float frac = (float)(pos - (double)i);
    uint32_t j = i + 1;
    if (j >= n)
        j = 0;
    return audio[i] * (1.0f - frac) + audio[j] * frac;
}

/* Same, without the wrap test.  The caller bounds the sample count so
 * that i+1 always lands inside the window. */
static inline float interp_lin(const float *audio, double pos)
{
    uint32_t i = (uint32_t)pos;
    float frac = (float)(pos - (double)i);
    return audio[i] * (1.0f - frac) + audio[i + 1] * frac;
}

/*
 * Largest sample count k <= n for which every read stays inside
 * audio[0 .. n_audio-1], given that sample i reads indices
 * floor(pos + i*step) and +1.
 *
 * Derivation: the last sample read is at pos + (k-1)*step, and it needs
 * floor(.) + 1 <= n_audio - 1.  Taking k = floor(room / step) with
 * room = n_audio - 1 - pos gives pos + (k-1)*step <= n_audio - 1 - step,
 * which satisfies it with a full step to spare.
 */
static size_t bounded_count(double pos, double step, uint32_t n_audio,
                            size_t n)
{
    double room = (double)n_audio - 1.0 - pos;
    if (room <= 0.0 || step <= 0.0)
        return 0;
    double k = floor(room / step);
    if (k <= 0.0)
        return 0;
    return (k < (double)n) ? (size_t)k : n;
}

/* Advance the phase accumulator and record raw radian phase.  Kept
 * separate from sin/cos so libm and NEON share identical phase
 * arithmetic — the only variable under test is the transcendental. */
static void advance_phase(fm_mod_t *m, const float *audio, uint32_t n_audio,
                          bool wrap, float *phase_out, size_t n)
{
    double pos   = m->audio_pos;
    double ph    = m->phase;
    double pstep = m->phase_step;
    double astep = m->audio_step;

    if (wrap) {
        for (size_t i = 0; i < n; i++) {
            float av = interp_wrap(audio, n_audio, pos);
            float extra = 0.0f;
            if (sub_on(m)) {
                av *= m->sub.audio_amp;
                extra = fm_mod_sub_sample(m);
            }
            ph += pstep * (double)(av + extra);
            phase_out[i] = (float)ph;
            pos += astep;
            if (pos >= (double)n_audio)
                pos -= (double)n_audio;
        }
    } else {
        for (size_t i = 0; i < n; i++) {
            float av = interp_lin(audio, pos);
            float extra = 0.0f;
            if (sub_on(m)) {
                av *= m->sub.audio_amp;
                extra = fm_mod_sub_sample(m);
            }
            ph += pstep * (double)(av + extra);
            phase_out[i] = (float)ph;
            pos += astep;
        }
    }

    /* Wrap accumulated phase so float32 precision stays usable
     * indefinitely.  styxsdr's stream test deliberately does not, and
     * loses phase resolution after a few seconds; for an indefinite
     * runtime that is not acceptable. */
    m->audio_pos = pos;
    m->phase     = fmod(ph, 2.0 * M_PI);
}

size_t fm_mod_generate(fm_mod_t *m, const float *audio, uint32_t n_audio,
                       bool wrap, float *re, float *im, size_t n)
{
    if (n == 0 || n_audio == 0)
        return 0;

    if (!wrap) {
        n = bounded_count(m->audio_pos, m->audio_step, n_audio, n);
        if (n == 0)
            return 0;
    }

    /* NCO path is self-contained: integer phase means no radian scratch
     * buffer and no wrap bookkeeping. */
    if (m->kind == FM_GEN_NCO) {
        double   pos    = m->audio_pos;
        double   astep  = m->audio_step;
        double   dscale = m->idev_scale;
        uint32_t ph     = m->iphase;

        if (wrap) {
            for (size_t i = 0; i < n; i++) {
                float av = interp_wrap(audio, n_audio, pos);
                float extra = 0.0f;
                if (sub_on(m)) {
                    av *= m->sub.audio_amp;
                    extra = fm_mod_sub_sample(m);
                }
                /* Signed increment; the uint32 accumulator wraps for free. */
                ph += nco_phase_inc(dscale, (double)(av + extra));
                nco_sincos(ph, &im[i], &re[i]);
                pos += astep;
                if (pos >= (double)n_audio)
                    pos -= (double)n_audio;
            }
        } else {
            for (size_t i = 0; i < n; i++) {
                float av = interp_lin(audio, pos);
                float extra = 0.0f;
                if (sub_on(m)) {
                    av *= m->sub.audio_amp;
                    extra = fm_mod_sub_sample(m);
                }
                ph += nco_phase_inc(dscale, (double)(av + extra));
                nco_sincos(ph, &im[i], &re[i]);
                pos += astep;
            }
        }

        m->audio_pos = pos;
        m->iphase    = ph;
        return n;
    }

    /* Radian-phase generators, in blocks so the scratch buffer stays
     * small regardless of n. */
    for (size_t off = 0; off < n; off += FM_MOD_BLOCK) {
        float phase_buf[FM_MOD_BLOCK];
        size_t blk = n - off;
        if (blk > FM_MOD_BLOCK)
            blk = FM_MOD_BLOCK;

        advance_phase(m, audio, n_audio, wrap, phase_buf, blk);

#ifdef __ARM_NEON
        if (m->kind == FM_GEN_NEON) {
            /* Compute the vector bound up front.  The idiomatic
             * `i + 4 <= blk` form lets size_t arithmetic wrap in the
             * compiler's range analysis, which defeats bounds inference
             * on the scalar tail. */
            size_t nvec = blk & ~(size_t)3;
            size_t i;
            for (i = 0; i < nvec; i += 4) {
                float32x4_t ph = vld1q_f32(&phase_buf[i]);
                float32x4_t sv, cv;
                neon_sincos(ph, &sv, &cv);
                vst1q_f32(&re[off + i], cv);
                vst1q_f32(&im[off + i], sv);
            }
            for (; i < blk; i++) {
                re[off + i] = cosf(phase_buf[i]);
                im[off + i] = sinf(phase_buf[i]);
            }
            continue;
        }
#endif

        for (size_t i = 0; i < blk; i++) {
            re[off + i] = cosf(phase_buf[i]);
            im[off + i] = sinf(phase_buf[i]);
        }
    }

    return n;
}

void fm_mod_carrier(const fm_mod_t *m, float *re, float *im, size_t n)
{
    float c, s;

    if (m->kind == FM_GEN_NCO) {
        nco_sincos(m->iphase, &s, &c);
    } else {
        c = cosf((float)m->phase);
        s = sinf((float)m->phase);
    }

    for (size_t i = 0; i < n; i++) {
        re[i] = c;
        im[i] = s;
    }
}
