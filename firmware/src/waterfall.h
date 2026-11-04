#pragma once
#include <stddef.h>
#include <stdint.h>
#include "window.h"
#include "wf_claim.h"

/*
 * How far the producer may fall behind the DMA write pointer before it skips
 * forward, in frames.  This bounds display latency: at 4 frames and N=4096 /
 * 20 MSPS the waterfall is at most ~0.8 ms stale, against the 1.68 s the full
 * DDR ring would allow.  It does not affect coverage, which above fs_max is
 * set by compute (fs_max/fs) regardless of where frames are read from.  Small
 * enough to feel live, large enough to absorb one slow frame without skipping.
 *
 * Exposed so the measurement tools can reproduce the producer's behaviour.
 */
#define WF_MAX_LAG_FRAMES 4

typedef struct waterfall waterfall_t;

/* The claim is shared by every producer and outlives them; see wf_claim.h. */
waterfall_t *waterfall_create(size_t fft_size, window_type_t win_type,
                              wf_claim_t *claim);
void waterfall_destroy(waterfall_t *wf);
int waterfall_set_fft_size(waterfall_t *wf, size_t fft_size);
int waterfall_set_window(waterfall_t *wf, window_type_t win_type);
/* Capture, window, transform, and write fft_size LINEAR POWER bins.
 * The dB conversion belongs to the consumer, after accumulation — see
 * wf_mag.h.  Returns 0, or -1 if the capture timed out. */
int waterfall_process(waterfall_t *wf, float *pow_out);
size_t waterfall_get_fft_size(const waterfall_t *wf);

/* Global order of the frame most recently returned by waterfall_process.
 * The drain merges producers on this, and the wire carries it as the
 * sequence number. */
uint64_t waterfall_last_frame_index(const waterfall_t *wf);

/* Captures that waited 50 ms without a whole new frame appearing.  Zero on a
 * healthy stream at any supported rate; nonzero means the samples stopped or
 * the producer and DMA disagree about the rate. */
uint64_t waterfall_capture_timeouts(const waterfall_t *wf);

/* Coverage counters: how much of the sample stream reached an FFT.
 * coverage = transformed / (transformed + skipped).  Exact, and independent
 * of any signal — see waterfall.c.  Counters reset on an FFT-size change. */
void waterfall_get_coverage(const waterfall_t *wf, uint64_t *frames,
                            uint64_t *samples_transformed,
                            uint64_t *samples_skipped);

/* Coverage over the last fraction of a second, rather than since the
 * counters were reset.  Coverage depends on the sample rate, so a lifetime
 * ratio keeps reporting conditions that no longer hold — this is the one a
 * status display wants. */
void waterfall_get_recent_coverage(const waterfall_t *wf,
                                   uint64_t *samples_transformed,
                                   uint64_t *samples_skipped);
void waterfall_reset_coverage(waterfall_t *wf);
window_type_t waterfall_get_window(const waterfall_t *wf);

/* Drop the latched claim origin so the next capture re-latches it.
 *
 * Call this after the shared claim is reset.  Offsets in the new run are
 * counted from a new origin, so the ring position cached for the old run
 * would resolve them to the wrong samples. */
void waterfall_relatch(waterfall_t *wf);
