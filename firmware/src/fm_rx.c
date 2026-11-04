#include "fm_rx.h"
#include "adc_clip.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* RDS is fed in batches of this many stage-1 outputs, so the call does not
 * land on the per-sample path. */
#define RDS_MPX_BATCH 256

/* Keeps the power reciprocals finite when the signal is silent. */
#define FM_POWER_FLOOR 1e-6f

static float clampf(float v, float lo, float hi)
{
    if (isnan(v)) return 0.0f;
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static float floor_power(float p)
{
    /* Also catches NaN: a poisoned power estimate must recover. */
    return (p >= FM_POWER_FLOOR) ? p : FM_POWER_FLOOR;
}

/*
 * The decimating FIR primitive (fir_init / fir_dot / fir_push) lives in
 * fir.c now that the RDS chain shares it. The tuning rationale is recorded
 * there; test_fm_rx.c's golden vector still certifies this path bit for bit.
 */

int fm_demod_init(fm_demod_t *d, uint32_t iq_rate,
                  uint32_t deviation_hz, uint32_t deemphasis_us)
{
    if (!d || deviation_hz == 0)
        return -1;

    /*
     * The tap tables are designed for one rate pair and are wrong at any
     * other: their stopband edges are placed relative to 2.1 MSPS and the
     * intermediate rates it produces. Failing here is deliberate. Accepting
     * the rate and filtering at the wrong frequencies would deliver audio
     * that sounds plausible and aliases silently, which is exactly the class
     * of bug this whole chain replaces.
     */
    if (iq_rate != FM_DEFAULT_IQ_RATE) {
        fprintf(stderr, "fm_demod_init: iq_rate %u unsupported, "
                        "decimation taps are designed for %u only\n",
                (unsigned)iq_rate, (unsigned)FM_DEFAULT_IQ_RATE);
        return -1;
    }

    memset(d, 0, sizeof(*d));

    d->iq_rate = iq_rate;
    d->deviation_hz = deviation_hz;
    d->deemphasis_us = deemphasis_us;

    d->dc_alpha = 1.0f / 1024.0f;

    /* freq_scale = iq_rate / (2 * pi * deviation)
     * Approximately 4.456 at 2.1 MSPS / 75 kHz. */
    d->freq_scale = (float)d->iq_rate
                  / (6.283185307f * (float)deviation_hz);

    if (fir_init(&d->stage[0], fm_s1_taps, FM_S1_TAPS, FM_S1_DEC) != 0 ||
        fir_init(&d->stage[1], fm_s2_taps, FM_S2_TAPS, FM_S2_DEC) != 0 ||
        fir_init(&d->stage[2], fm_s3_taps, FM_S3_TAPS, FM_S3_DEC) != 0 ||
        fir_init(&d->rds_stage1, fm_s1_taps, FM_S1_TAPS, FM_S1_DEC) != 0)
        return -1;

    if (rds_rx_init(&d->rds) != 0)
        return -1;

    /*
     * De-emphasis is derived from the rate actually delivered, not from a
     * nominal one. The previous code used a caller-supplied 44100 while
     * emitting 44680, so tau was 1.3% short of the 75 us it claimed.
     */
    double dt = 1.0 / (double)FM_AUDIO_RATE;
    double tau = deemphasis_us * 1e-6;
    d->deemph_alpha = (float)(dt / (dt + tau));

    fm_demod_reset(d);

    return 0;
}

void fm_demod_reset(fm_demod_t *d)
{
    d->dc_i = 0.0f;
    d->dc_q = 0.0f;
    d->i_prev = 0.0f;
    d->q_prev = 0.0f;
    d->a2_est = FM_POWER_FLOOR;
    d->inv_a2 = d->freq_scale / d->a2_est;
    d->a2_prev = FM_POWER_FLOOR;
    d->inv_denom = d->freq_scale / d->a2_est;
    d->deemph_state = 0.0f;
    for (int k = 0; k < FM_DECIM_STAGES; k++) {
        d->stage[k].phase = 0;
        d->stage[k].wr = 0;
        memset(d->stage[k].hist, 0, sizeof(d->stage[k].hist));
    }
    d->rds_stage1.phase = 0;
    d->rds_stage1.wr = 0;
    memset(d->rds_stage1.hist, 0, sizeof(d->rds_stage1.hist));
    rds_rx_reset(&d->rds);
}

uint32_t fm_demod_audio_rate(const fm_demod_t *d)
{
    return d->iq_rate / FM_DECIM_TOTAL;
}

int fm_demod_process(fm_demod_t *d, const float *iq_real,
                     const float *iq_imag, size_t n_iq,
                     float *audio_out, size_t *n_audio_out)
{
    if (!d || !iq_real || !iq_imag || !audio_out || !n_audio_out)
        return -1;

    size_t max_out = *n_audio_out;
    size_t out_idx = 0;

    /*
     * Scalar state is pulled into locals so the compiler can keep it in
     * registers: with these living in the struct it could not prove that a
     * store through d did not alias d->dc_i, and reloaded every field each
     * iteration. The FIR stages stay in the struct -- their state is arrays,
     * and fir_push is inlined, so there is nothing to gain.
     *
     * The decimator here replaces a single critically sampled 47-tap boxcar.
     * That boxcar's Nyquist was 22.34 kHz, so it passed the 19 kHz stereo
     * pilot at -2.8 dB and folded the 38 kHz stereo subcarrier and 57 kHz RDS
     * straight into the audio band. Integrated over an f^2 discriminator
     * noise PSD it delivered 18 dB *more* aliased than direct noise. The
     * three-stage chain rejects the pilot by 62 dB and lands at -30.8 dB
     * aliased, a 48.8 dB improvement. See scripts/fm_decim_design.py.
     */
    float dc_i = d->dc_i;
    float dc_q = d->dc_q;
    const float dc_alpha = d->dc_alpha;

    float i_prev = d->i_prev;
    float q_prev = d->q_prev;
    float a2_est = d->a2_est;
    float a2_prev = d->a2_prev;
    const float a_alpha = 1.0f / 2048.0f;
    const float freq_scale = d->freq_scale;

    float deemph_state = d->deemph_state;
    const float deemph_alpha = d->deemph_alpha;

    uint64_t clips = d->clip_count;
    uint64_t samples = d->sample_count;

    /*
     * Two normalisations of the same cross product.
     *
     * The audio path keeps the smoothed-power reciprocal it was measured
     * with: the DC block translates the signal circle off the origin, so
     * the envelope varies with the modulation, and dividing by the smoothed
     * power is what measures flat across 0-15 kHz.
     *
     * RDS cannot use it. During a deep fade the smoothed power is still
     * large while the instantaneous product is pure noise, so the output
     * exploded past the clamp and spread impulsive noise across the whole
     * multiplex. On a loopback capture whose envelope faded ~20 dB, the
     * resulting 57 kHz biphase coherence was 0.014 and zero groups decoded,
     * while the audio stayed apparently fine. Dividing by the mean of the
     * previous and current sample powers bounds the fade gain, and on that
     * same capture measures coherence 0.947 and decodes the full message
     * (PI 8203, PS "TESTFM").
     *
     * Both reciprocals are refreshed once per stage-1 output, not per
     * sample: the RDS one used to divide by that mean on every sample, and
     * it is a VFP divide, which does not pipeline. An isolated A/B put the
     * per-sample divide and the live state it forced at 10.9 of the RDS
     * increment's 32.5 points of a core (against 12.9 for the second 33-tap
     * stage-1 decimator and 8.7 for the batched decoder), but removing it in
     * place saves 6.5 points -- 64.4% of a core to 57.9% -- because the
     * divide was partly hidden behind the second decimator. Holding the
     * reciprocal across the six samples between stage-1 outputs is sound
     * because a fade moves negligibly in 2.9 us.
     */
    float inv_a2 = d->inv_a2;
    float inv_denom = d->inv_denom;

    /*
     * RDS is fed from its own stage-1 output, batched 256 samples at a
     * time so the call stays out of the per-sample path.
     */
    float mpx[RDS_MPX_BATCH];
    size_t mpx_n = 0;

    for (size_t i = 0; i < n_iq; i++) {
        clips += adc_clip_is_float(iq_real[i]);
        clips += adc_clip_is_float(iq_imag[i]);
        samples += 2;

        dc_i += dc_alpha * (iq_real[i] - dc_i);
        dc_q += dc_alpha * (iq_imag[i] - dc_q);
        float i_dc = iq_real[i] - dc_i;
        float q_dc = iq_imag[i] - dc_q;

        float a2_inst = i_dc * i_dc + q_dc * q_dc;
        a2_est += a_alpha * (a2_inst - a2_est);
        a2_est = floor_power(a2_est);
        float disc = i_prev * q_dc - q_prev * i_dc;
        float freq = clampf(disc * inv_a2, -1.0f, 1.0f);
        float freq_rds = clampf(disc * inv_denom, -1.0f, 1.0f);

        float y1r;
        if (fir_push(&d->rds_stage1, freq_rds, &y1r)) {
            mpx[mpx_n++] = y1r;
            if (mpx_n == RDS_MPX_BATCH) {
                rds_rx_process(&d->rds, mpx, mpx_n);
                mpx_n = 0;
            }
        }

        float y1, y2, y3;
        if (!fir_push(&d->stage[0], freq, &y1)) {
            i_prev = i_dc;
            q_prev = q_dc;
            a2_prev = a2_inst;
            continue;
        }
        float denom = 0.5f * (a2_prev + a2_inst);
        i_prev = i_dc;
        q_prev = q_dc;
        a2_prev = a2_inst;
        inv_a2 = freq_scale / floor_power(a2_est);
        inv_denom = freq_scale / floor_power(denom);

        if (!fir_push(&d->stage[1], y1, &y2))
            continue;
        if (!fir_push(&d->stage[2], y2, &y3))
            continue;

        deemph_state += deemph_alpha * (y3 - deemph_state);

        /* max_out was computed and never used before, so the output was
         * unbounded. 49152 IQ samples now yield exactly 1024, but the guard
         * stays: it is the only thing standing between a larger capture
         * chunk and a buffer overflow.  Check capacity before the store too:
         * a caller passing zero must not lose the first sample past the end
         * of its buffer. */
        if (out_idx >= max_out)
            break;
        audio_out[out_idx++] = deemph_state;

        if (out_idx >= max_out)
            break;
    }

    if (mpx_n)
        rds_rx_process(&d->rds, mpx, mpx_n);

    d->dc_i = dc_i;
    d->dc_q = dc_q;
    d->i_prev = i_prev;
    d->q_prev = q_prev;
    d->a2_est = a2_est;
    d->inv_a2 = inv_a2;
    d->a2_prev = a2_prev;
    d->inv_denom = inv_denom;
    d->deemph_state = deemph_state;
    d->clip_count = clips;
    d->sample_count = samples;

    *n_audio_out = out_idx;
    return 0;
}
