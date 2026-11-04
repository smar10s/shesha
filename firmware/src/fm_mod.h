// SPDX-License-Identifier: MIT
#ifndef SHESHA_FM_MOD_H
#define SHESHA_FM_MOD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "rds_tx.h"

/*
 * FM modulator — the single implementation, shared by the daemon
 * (fm_tx.c) and the on-device instrument (tools/fm_stream_test.c).
 *
 * Shared on purpose.  fm_stream_test --verify compares two generators
 * from this file and --bench measures this file's throughput; if the
 * daemon carried its own copy, the instrument would be certifying code
 * that never reaches the air.
 *
 * Pre-emphasis is deliberately absent.  It belongs to whoever knows the
 * whole signal: 75 us pre-emphasis boosts 15 kHz by ~17 dB, so the peak
 * cannot be bounded from a streaming window, and exceeding 1.0 here does
 * not clip IQ (|IQ| = 1 for FM) — it clips *deviation*, which reaches the
 * receiver as overdeviation and distortion after de-emphasis.  The client
 * has the entire file, so it filters and normalises there.
 */

typedef enum {
    FM_GEN_LIBM = 0,  /* scalar cosf/sinf — the reference */
    FM_GEN_NEON,      /* styxsdr's NEON Taylor sincos (measurably broken) */
    FM_GEN_NCO,       /* integer phase accumulator + interpolated LUT */
} fm_gen_kind_t;

/*
 * Subcarrier amplitudes as fractions of full deviation.  The modulator owns
 * the programme-audio headroom: when the subcarriers are enabled it scales
 * the audio to (1 - pilot - rds), so pilot + RDS + programme lands on full
 * deviation.  That keeps a live enable/disable correct on air without the
 * client re-scaling the track.
 */
#define FM_SUB_PILOT_AMP 0.09f
#define FM_SUB_RDS_AMP   0.03f

/*
 * Optional subcarriers summed into the modulation signal.  Disabled by
 * default, so the modulator's numbers are unchanged until asked.  The pilot
 * and the RDS carrier are coherent by construction: one pilot phase theta
 * drives the pilot (cos theta) and the 57 kHz carrier (cos 3theta), so the
 * two cannot drift apart.
 */
typedef struct {
    _Atomic bool enabled;
    float    pilot_amp;    /* fraction of full deviation, ~0.09 */
    float    rds_amp;      /* fraction of full deviation, ~0.03 */
    float    audio_amp;    /* programme-audio scale; 1 - pilot - rds when on */
    uint32_t pilot_iphase; /* integer phase, one turn = 2^32 */
    uint32_t pilot_istep;  /* 19000 / dac_rate * 2^32 */
    rds_tx_t rds;
} fm_mod_sub_t;

typedef struct {
    /* Resampler: fractional read position into the caller's audio window,
     * and how far it advances per DAC sample. */
    double   audio_pos;
    double   audio_step;

    /* libm/NEON path: carrier phase in radians, double accumulator. */
    double   phase;
    double   phase_step;   /* 2*pi*deviation/dac_rate */

    /* NCO path: phase in units of 2^32 per turn, so the modulo-2pi wrap is
     * a free integer overflow and there is no drift however long it runs. */
    uint32_t iphase;
    double   idev_scale;   /* deviation/dac_rate * 2^32 */

    uint32_t     dac_rate;
    fm_mod_sub_t sub;

    fm_gen_kind_t kind;
} fm_mod_t;

/**
 * Build the NCO table.  Idempotent, so every entry point can call it
 * without coordinating.
 */
void fm_mod_lut_init(void);

/**
 * Initialise a modulator.  Zeroes phase and position; call
 * fm_mod_lut_init() at least once before generating with FM_GEN_NCO.
 */
void fm_mod_init(fm_mod_t *m, uint32_t audio_rate, uint32_t deviation_hz,
                 uint32_t dac_rate, fm_gen_kind_t kind);

/* Reject deviations that push the NCO phase step past half the sample
 * rate.  That is both the aliasing limit and the point where the
 * double→int32 phase increment stops being representable. */
bool fm_mod_deviation_valid(uint32_t deviation_hz, uint32_t dac_rate);

/**
 * Modulate from a contiguous audio window into IQ.
 *
 * `audio[0 .. n_audio-1]` must be readable.  Only about
 * n * audio_step + 2 of those samples are touched — at 44.1 kHz audio and
 * a 2.1 MSPS DAC that is ~88 samples per 4096-sample chunk.  A ring
 * consumer should therefore linearise into a small stack buffer rather
 * than teach this function ring geometry.
 *
 * wrap = true   audio_pos wraps at n_audio, so a finite buffer loops
 *               seamlessly and the return value is always n.
 * wrap = false  generation stops when the window runs out, and the return
 *               value is how many IQ samples were produced.  On return,
 *               audio_pos is the absolute fractional position reached, so
 *               the caller advances its ring by (uint32_t)audio_pos and
 *               keeps the fraction.
 *
 * @return  IQ samples written (<= n)
 */
size_t fm_mod_generate(fm_mod_t *m, const float *audio, uint32_t n_audio,
                       bool wrap, float *re, float *im, size_t n);

/**
 * Unmodulated carrier at the current phase.
 *
 * This is the correct underrun response for FM.  The modulation timeline
 * lives in the phase trajectory, so feeding the DAC late does not glitch
 * — it time-stretches, which a listener hears as the audio dragging.
 * Emitting carrier keeps the DAC fed at exactly the nominal rate and
 * costs only the silence it stands in for.  Phase is left unchanged so
 * audio resumes from where it stopped.
 */
void fm_mod_carrier(const fm_mod_t *m, float *re, float *im, size_t n);

/**
 * Enable the 19 kHz pilot and 57 kHz RDS subcarriers.  Amplitudes are
 * fractions of full deviation.  Idempotent; the modulator starts disabled.
 */
void fm_mod_enable_subcarriers(fm_mod_t *m, float pilot_amp, float rds_amp);

/* Turn the subcarriers off (programme audio only). */
void fm_mod_disable_subcarriers(fm_mod_t *m);

/**
 * Publish a new RDS group configuration to the live modulator.  Off the TX
 * thread; the TX thread only sees a consistent bit pattern (see rds_tx.h).
 */
void fm_mod_set_rds(fm_mod_t *m, const rds_tx_config_t *cfg);

#endif /* SHESHA_FM_MOD_H */
