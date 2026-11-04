#pragma once
#include <stddef.h>

/*
 * Forward-only split-complex FFT for the waterfall producer.
 *
 * lib80211's public FFT API pays an interleave/de-interleave copy to match
 * FFTW's interleaved format.  The producer's I/Q arrays are already split, so
 * this plans an fftwf guru split-complex transform and executes it on the
 * caller's arrays directly, skipping that copy.
 *
 * Forward-only is a property of the FFTW guru split interface, which has no
 * direction argument; the producer only ever transforms forward.
 *
 * Kept in shesha, not lib80211: the producer is the only consumer, and the
 * shared library gains nothing from owning a one-caller special case (plus a
 * direct fftw3f dependency).  lib80211 stays the general FFT for WiFi.
 *
 * The input arrays are read-only: an out-of-place complex transform preserves
 * its input.  The output arrays are overwritten.  A plan carries FFTW
 * workspace and is not thread-safe; give each worker its own.
 */
typedef struct wf_fft_plan wf_fft_plan;

wf_fft_plan *wf_fft_plan_create_n(size_t n);
void wf_fft_plan_destroy(wf_fft_plan *plan);

void wf_fft_forward_split(wf_fft_plan *plan,
                          const float *in_real, const float *in_imag,
                          float *out_real, float *out_imag);
