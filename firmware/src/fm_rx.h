#pragma once
#include <stdint.h>
#include <stddef.h>
#include "fm_decim_taps.h"
#include "fir.h"
#include "rds_rx.h"

#define FM_DEFAULT_DEVIATION  75000
#define FM_DEFAULT_DEEMPHASIS 75
#define FM_DEFAULT_IQ_RATE    2100000

/*
 * The delivered audio rate is not configurable, and used to pretend to be.
 *
 * The decimation chain is three designed FIR stages (fm_decim_taps.h) whose
 * coefficients only anti-alias correctly for 2100000 Hz in and
 * 2100000/48 = 43750 Hz out. fm_demod_init() therefore takes no audio_rate:
 * the old parameter was a lie in three ways at once. Callers passed 44100,
 * the code truncated 2100000/44100 to a decimation of 47 and delivered
 * 44680.85, and the de-emphasis time constant was then derived from the 44100
 * that nothing anywhere produced -- a 1.3% error in tau.
 *
 * 48 rather than 47 is load-bearing. 47 is prime, so it cannot be split into
 * cascaded stages, and a single stage cannot do the 15 kHz -> 19 kHz
 * transition at 2.1 MSPS for less than ~1900 taps. 49152/48 is also exactly
 * 1024, the audio ring slot size, which is what removes the 2% silent frame
 * truncation.
 */
#define FM_AUDIO_RATE (FM_DEFAULT_IQ_RATE / FM_DECIM_TOTAL)   /* 43750 */

/*
 * One decimating FIR stage. The implementation and its sizing live in
 * fir.h; the alias name is kept because every audio-chain comment refers to
 * fm_fir_t.
 */
typedef fir_t fm_fir_t;

typedef struct {
    float dc_i;
    float dc_q;
    float dc_alpha;

    float i_prev;
    float q_prev;
    float a2_est;
    /*
     * The audio discriminator's normalisation reciprocal, refreshed once
     * per stage-1 output rather than per sample. It lives in the struct,
     * not on fm_demod_process's stack, so that a capture chunk boundary
     * landing mid-block does not force an early refresh and perturb the
     * audio -- see test_streaming_across_chunks_is_continuous.
     *
     * This is deliberately the smoothed power, not the instantaneous
     * amplitude product: the DC block translates the signal circle off the
     * origin, so the envelope varies strongly with the modulation, and the
     * audio passband was measured flat against this normalisation. RDS
     * needs the opposite property (no fade gain), so it gets its own
     * normalisation below.
     */
    float inv_a2;

    /*
     * The RDS discriminator's normalisation reciprocal, refreshed at the
     * same stage-1 output boundary as inv_a2 but from the mean of the
     * previous and current sample powers rather than the smoothed a2_est.
     * Dividing by that mean bounds the fade gain: the smoothed a2_est is
     * still large when a fade collapses the instantaneous product, and the
     * old division exploded into clipped impulsive noise that erased the
     * 57 kHz subcarrier while leaving audio apparently fine.
     *
     * Holding the reciprocal across the six samples between stage-1 outputs
     * removes a VFP divide from the per-sample path. A 300 Hz fade moves
     * negligibly in 2.9 us, so the held value tracks it; the floor only
     * stops a zero denominator turning it into infinity.
     */
    float inv_denom;
    /*
     * Per-sample power of the previous DC-blocked sample; with a2_inst it
     * forms the mean that inv_denom is refreshed from.
     */
    float a2_prev;

    /*
     * RDS gets its own stage-1 decimation fed from the per-sample
     * normalised discriminator, because the audio path's normalisation is
     * part of its measured sound and must not change.
     */
    fm_fir_t rds_stage1;

    fm_fir_t stage[FM_DECIM_STAGES];

    float deemph_state;
    float deemph_alpha;

    float freq_scale;

    /*
     * RDS hangs off the stage-1 output (the multiplex at 350 kHz), which is
     * why it lives in the demodulator rather than in fm_mode.c: the tap is
     * an intermediate value inside fm_demod_process, and materializing the
     * whole 350 kHz multiplex just to hand it out would waste 1.4 MB/s of
     * memory traffic.
     */
    rds_rx_t rds;

    uint32_t iq_rate;
    uint32_t deviation_hz;
    uint32_t deemphasis_us;

    /* Lifetime ADC rail components and components seen, for the clip meter. */
    uint64_t clip_count;
    uint64_t sample_count;
} fm_demod_t;

int  fm_demod_init(fm_demod_t *d, uint32_t iq_rate,
                   uint32_t deviation_hz, uint32_t deemphasis_us);
void fm_demod_reset(fm_demod_t *d);
int  fm_demod_process(fm_demod_t *d, const float *iq_real,
                      const float *iq_imag, size_t n_iq,
                      float *audio_out, size_t *n_audio_out);

/* Delivered audio rate in Hz: iq_rate / FM_DECIM_TOTAL. */
uint32_t fm_demod_audio_rate(const fm_demod_t *d);
