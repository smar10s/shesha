#include "waterfall.h"
#include "wf_claim.h"
#include "dma_rx.h"
#include "hal.h"
#include "wf_convert.h"
#include "adc_clip.h"
#include "wf_mag.h"
#include "wf_fft.h"
#include <stdbool.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define CAPTURE_TIMEOUT_MS 50


struct waterfall {
    size_t fft_size;
    window_type_t win_type;
    float *win_coeffs;
    float  coherent_gain;
    float *iq_real;
    float *iq_imag;
    float *fft_real;
    float *fft_imag;
    wf_fft_plan *plan;

    /* The read position is shared with every other producer (wf_claim.h), so
     * it is not ours.  What is ours is where we are in the ADC stream: an
     * independent delta sum from the same starting write pointer, which is
     * monotonic modulo the ring and therefore needs no shared progress
     * state. */
    /* The read position, the origin, and how far the ADC has got are all
     * shared with the other producers; see wf_claim.h. */
    wf_claim_t *claim;
    uint32_t    wr0;             /* ring address of claim offset zero */
    bool        wr0_valid;

    _Atomic uint64_t frames;     /* frames transformed since counters reset */
    _Atomic uint64_t samples_skipped; /* samples jumped over because we fell behind */
    _Atomic uint64_t last_frame_index; /* global order of the frame just produced */

    /* Coverage over the recent past, for the status display.  Two buckets
     * rolled over on a fixed period: reporting the current one alone would
     * read 0% for an instant every rollover. */
    _Atomic uint64_t win_frames[2];
    _Atomic uint64_t win_skipped[2];
    _Atomic uint64_t win_started_ms;

    uint64_t capture_timeouts;   /* captures that gave up waiting */
};

/* How long each coverage bucket collects for.  Long enough that even N=8192
 * at a low rate lands tens of frames in it, short enough that the reported
 * figure follows a rate change within about a second. */
#define WF_COVERAGE_WINDOW_MS 500

/* Milliseconds from CLOCK_MONOTONIC, for the capture timeout only. */
static uint64_t wf_now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}

/* Roll the coverage window if the current bucket has collected long enough.
 * Called from the producer thread only, so the buckets need no locking. */
static void wf_coverage_roll(waterfall_t *wf)
{
    uint64_t now = wf_now_ms();
    uint64_t started = atomic_load(&wf->win_started_ms);
    if (started == 0) {
        atomic_store(&wf->win_started_ms, now);
        return;
    }
    if (now - started < WF_COVERAGE_WINDOW_MS)
        return;

    atomic_store(&wf->win_frames[1], atomic_load(&wf->win_frames[0]));
    atomic_store(&wf->win_skipped[1], atomic_load(&wf->win_skipped[0]));
    atomic_store(&wf->win_frames[0], 0);
    atomic_store(&wf->win_skipped[0], 0);
    atomic_store(&wf->win_started_ms, now);
}

/*
 * Capture the next n samples into wf->iq_{real,imag}, unpacking them with the
 * window folded into the same pass (see wf_convert.h).
 *
 * Which samples those are comes from the SHARED CLAIM (wf_claim.h), not from a
 * pointer of our own.  Blocks are contiguous in the stream, so consecutive
 * frames are contiguous in time (hop == N) and nothing is dropped between them
 * while the producers keep up.  An earlier version re-latched WR_PTR at the
 * top of every frame and then waited for n *fresh* samples, which silently
 * discarded everything that arrived during the previous frame's compute.  That
 * capped coverage at W/(C+W) -- about 71% even at 2.1 MSPS where compute has
 * ample headroom, and ~21% at 20 MSPS -- for no benefit, since the discarded
 * samples were already in DDR.
 *
 * Contiguity is bounded by LATENCY, not by the ring.  Above fs_max the
 * producers cannot keep up, so the backlog grows without limit; left alone it
 * only stops at the ring (33.5 M samples, i.e. 1.68 s at 20 MSPS), which
 * would make the waterfall show second-old spectrum.  A live display must
 * instead stay near the write pointer, so a block more than WF_MAX_LAG_FRAMES
 * behind is abandoned rather than transformed.
 *
 * Abandoning drags the shared claim forward, so the other producers do not
 * keep grinding through old blocks either, and the samples given up are
 * counted -- that is the honest form of the loss, reported through
 * waterfall_get_coverage().
 *
 * The cap costs nothing when the producers keep up (the backlog never reaches
 * it) and costs no coverage when they do not: above fs_max coverage is
 * fs_max/fs either way, because it is set by how fast frames can be computed,
 * not by where they are read from.  The cap only decides whether the samples
 * that do get transformed are recent or stale.
 *
 * Skipping forward -- rather than reading a frame that straddles the write
 * pointer -- matters because such a frame would splice two different times
 * into one FFT and produce a spectrum that was never on the air.
 *
 * The write-pointer ordering is restated here because the memcpy inside
 * wf_convert_windowed is not volatile-ordered.
 */
static int wf_capture(waterfall_t *wf, size_t n)
{
    uint64_t t_start = wf_now_ms();

    if (!wf->wr0_valid) {
        /* Take the SHARED origin, latching the current write pointer only if
         * nobody has yet: claim offsets are only meaningful relative to one
         * agreed zero. */
        wf->wr0 = wf_claim_origin(wf->claim, dma_rx_wr_ptr());
        wf->wr0_valid = true;
    }

    /* Keep the backlog within a few frames so the display stays live, but
     * never below one frame (which must always be readable).  The frame
     * budget scales with the worker count so each block keeps the same
     * headroom however many workers are running (wf_claim.h). */
    uint32_t max_lag = (uint32_t)n
                       * wf_claim_lag_frames(wf->claim, WF_MAX_LAG_FRAMES);
    if (max_lag > DMA_RX_BUF_SAMPLES / 2)
        max_lag = DMA_RX_BUF_SAMPLES / 2;

    uint64_t off;
    for (;;) {
        /* Fold this reading into the SHARED progress count.  It has to be
         * shared: the write pointer carries 25 bits and wraps every 3.4 s
         * at 10 MSPS, so a worker unwrapping from its own private history
         * recovers the total only modulo the ring, and one that joins
         * after the first wrap is permanently short by whole ring
         * multiples.  Measured before this was shared: worker 1 was
         * handed offsets 14x past its own count and timed out on every
         * capture. */
        uint64_t produced = wf_claim_observe(wf->claim, dma_rx_wr_ptr(),
                                             DMA_RX_BUF_SAMPLES);

        /*
         * Reserve a block only after the ADC has written all of it.  The
         * previous claim-first loop advanced the read position before the
         * samples existed, then abandoned the block on timeout and claimed
         * another; a run of timeouts walked the claim arbitrarily far into
         * the future and froze the display until the ADC caught up.  A
         * refused reservation leaves the claim untouched, so that cannot
         * recur.
         */
        off = wf_claim_reserve(wf->claim, produced);
        if (off == WF_CLAIM_NONE) {
            if (wf_now_ms() - t_start >= CAPTURE_TIMEOUT_MS) {
                wf->capture_timeouts++;
                return -1;
            }
            /* Idle time: the producer is caught up and the next whole frame
             * is not written yet.  Measured: reclaiming this wait with a
             * tighter sleep does not move coverage; see the pacing notes in
             * docs/psd-waterfall.md. */
            usleep(50);
            continue;
        }

        /* Too far behind: by the time this block were transformed it would
         * be stale, and the DMA may already be overwriting it. */
        uint64_t target = off + n;
        if (produced <= target + max_lag)
            break;

        /* This worker gives up the block it reserved as well as asking
         * the claim to skip ahead; both are coverage loss. */
        uint64_t lost = wf_claim_abandon(wf->claim, produced, n);
        atomic_fetch_add(&wf->samples_skipped, lost);
        atomic_fetch_add(&wf->win_skipped[0], lost);
        /* Checked here too: a producer whose blocks keep going stale must
         * still return rather than spin in this loop. */
        if (wf_now_ms() - t_start >= CAPTURE_TIMEOUT_MS) {
            wf->capture_timeouts++;
            return -1;
        }
    }

    __sync_synchronize();

    uint32_t start = (uint32_t)((wf->wr0 + off) & (DMA_RX_BUF_SAMPLES - 1));
    const volatile uint32_t *rx = hal_ddr_rx_buf();
    uint64_t clips = 0;
    if (start + (uint32_t)n <= DMA_RX_BUF_SAMPLES) {
        wf_convert_windowed(&rx[start], n, wf->win_coeffs,
                            wf->iq_real, wf->iq_imag, &clips);
    } else {
        size_t first = DMA_RX_BUF_SAMPLES - start;
        size_t second = n - first;
        wf_convert_windowed(&rx[start], first, wf->win_coeffs,
                            wf->iq_real, wf->iq_imag, &clips);
        wf_convert_windowed(&rx[0], second, wf->win_coeffs + first,
                            &wf->iq_real[first], &wf->iq_imag[first], &clips);
    }
    adc_clip_add(clips, (uint64_t)n * 2);

    atomic_store(&wf->last_frame_index, wf_claim_frame_index(wf->claim, off));
    atomic_fetch_add(&wf->frames, 1);
    atomic_fetch_add(&wf->win_frames[0], 1);
    wf_coverage_roll(wf);
    return (int)n;
}

waterfall_t *waterfall_create(size_t fft_size, window_type_t win_type,
                              wf_claim_t *claim)
{
    waterfall_t *wf = calloc(1, sizeof(*wf));
    if (!wf)
        return NULL;

    atomic_init(&wf->frames, 0);
    atomic_init(&wf->samples_skipped, 0);
    atomic_init(&wf->last_frame_index, 0);
    for (int i = 0; i < 2; i++) {
        atomic_init(&wf->win_frames[i], 0);
        atomic_init(&wf->win_skipped[i], 0);
    }
    atomic_init(&wf->win_started_ms, 0);

    wf->fft_size = fft_size;
    wf->win_type = win_type;
    wf->claim = claim;

    wf->win_coeffs = malloc(fft_size * sizeof(float));
    wf->iq_real  = malloc(fft_size * sizeof(float));
    wf->iq_imag  = malloc(fft_size * sizeof(float));
    wf->fft_real = malloc(fft_size * sizeof(float));
    wf->fft_imag = malloc(fft_size * sizeof(float));
    if (!wf->win_coeffs || !wf->iq_real || !wf->iq_imag
            || !wf->fft_real || !wf->fft_imag) {
        waterfall_destroy(wf);
        return NULL;
    }

    wf->coherent_gain = window_generate(win_type, fft_size, wf->win_coeffs);
    wf->plan = wf_fft_plan_create_n(fft_size);
    if (!wf->plan) {
        waterfall_destroy(wf);
        return NULL;
    }
    return wf;
}

void waterfall_destroy(waterfall_t *wf)
{
    if (!wf) return;
    wf_fft_plan_destroy(wf->plan);
    free(wf->win_coeffs);
    free(wf->iq_real);
    free(wf->iq_imag);
    free(wf->fft_real);
    free(wf->fft_imag);
    free(wf);
}

int waterfall_set_fft_size(waterfall_t *wf, size_t fft_size)
{
    if (fft_size == wf->fft_size) return 0;

    /*
     * Build the replacement state first, so a failed allocation leaves the
     * old waterfall fully usable.  realloc is deliberately not used: it can
     * free the old block on success and return NULL on failure, which loses
     * the pointer the caller needs for rollback.
     */
    float *win_coeffs = malloc(fft_size * sizeof(float));
    float *iq_real  = malloc(fft_size * sizeof(float));
    float *iq_imag  = malloc(fft_size * sizeof(float));
    float *fft_real = malloc(fft_size * sizeof(float));
    float *fft_imag = malloc(fft_size * sizeof(float));
    wf_fft_plan *plan = wf_fft_plan_create_n(fft_size);

    if (!win_coeffs || !iq_real || !iq_imag || !fft_real || !fft_imag
            || !plan) {
        wf_fft_plan_destroy(plan);
        free(win_coeffs);
        free(iq_real);
        free(iq_imag);
        free(fft_real);
        free(fft_imag);
        return -1;
    }

    float coherent_gain = window_generate(wf->win_type, fft_size, win_coeffs);

    wf_fft_plan_destroy(wf->plan);
    free(wf->win_coeffs);
    free(wf->iq_real);
    free(wf->iq_imag);
    free(wf->fft_real);
    free(wf->fft_imag);

    /* Re-latch on the next frame and restart the counters: both the read
     * position and the coverage tally describe the old frame length.
     *
     * The shared claim also has to restart, but that is the caller's job --
     * it is shared by every producer, so resetting it here would reset it
     * once per instance. */
    wf->wr0_valid = false;
    atomic_store(&wf->last_frame_index, 0);
    waterfall_reset_coverage(wf);

    wf->fft_size = fft_size;
    wf->win_coeffs = win_coeffs;
    wf->coherent_gain = coherent_gain;
    wf->iq_real = iq_real;
    wf->iq_imag = iq_imag;
    wf->fft_real = fft_real;
    wf->fft_imag = fft_imag;
    wf->plan = plan;
    return 0;
}

int waterfall_set_window(waterfall_t *wf, window_type_t win_type)
{
    wf->win_type = win_type;
    wf->coherent_gain = window_generate(win_type, wf->fft_size, wf->win_coeffs);
    return 0;
}

/*
 * Produce one frame of linear power bins.  `pow_out` holds fft_size floats;
 * the caller converts to dB after any accumulation (wf_mag.h explains why the
 * log is not done here).
 */
int waterfall_process(waterfall_t *wf, float *pow_out)
{
    size_t n = wf->fft_size;

    // Capture fresh IQ from the DMA ring buffer, applying the window in the
    // same pass (the unpack already has each sample in a register, so the
    // separate window_apply pass would only re-read and re-write the arrays).
    if (wf_capture(wf, n) != (int)n)
        return -1;

    // FFT (split-complex: no interleave/de-interleave copy)
    wf_fft_forward_split(wf->plan, wf->iq_real, wf->iq_imag,
                         wf->fft_real, wf->fft_imag);

    // Magnitude -> linear power with fftshift.  The dB conversion is the
    // drain's job (see wf_mag.h): the producer runs ~23x per broadcast frame,
    // and the drain's accumulation must reduce in the power domain anyway.
    float norm = 1.0f / ((float)n * wf->coherent_gain);
    wf_magnitude_power(wf->fft_real, wf->fft_imag, n, norm, pow_out);

    return 0;
}

size_t waterfall_get_fft_size(const waterfall_t *wf) { return wf->fft_size; }
window_type_t waterfall_get_window(const waterfall_t *wf) { return wf->win_type; }
void waterfall_relatch(waterfall_t *wf) { wf->wr0_valid = false; }
uint64_t waterfall_last_frame_index(const waterfall_t *wf)
{
    return atomic_load(&wf->last_frame_index);
}

uint64_t waterfall_capture_timeouts(const waterfall_t *wf)
{
    return wf->capture_timeouts;
}

/*
 * Coverage: the fraction of the sample stream that reached an FFT.
 *
 * The producer transforms exactly fft_size samples per frame and skips
 * whatever it had to jump over to stay ahead of the DMA, so
 *
 *     coverage = transformed / (transformed + skipped)
 *
 * is exact, needs no signal, and is the benefit side of the duty cycle.  Below
 * fs_max it approaches 1.0; above it, it falls as fs/fs_max.
 *
 * `frames` and `samples_skipped` are lifetime counters, reset only on an FFT
 * size change.  Those answer "how much has this producer covered" and are
 * what a long capture wants.  A STATUS DISPLAY wants a different question --
 * "what is coverage right now" -- because coverage depends on the sample rate,
 * and a rate change leaves the lifetime ratio averaging conditions that no
 * longer hold.  Measured: after stepping 2.1 -> 20 MSPS and back, the lifetime
 * figure read 19% where the live value was 100%.
 *
 * So the recent window below is kept alongside, advanced by the producer and
 * rolled over on a fixed period.  Two buckets rather than one, because a
 * single bucket reset to zero would read 0% for an instant every period.
 */
void waterfall_get_coverage(const waterfall_t *wf, uint64_t *frames,
                            uint64_t *samples_transformed,
                            uint64_t *samples_skipped)
{
    uint64_t n_frames = atomic_load(&wf->frames);
    if (frames)              *frames = n_frames;
    if (samples_transformed) *samples_transformed = n_frames * wf->fft_size;
    if (samples_skipped)     *samples_skipped = atomic_load(&wf->samples_skipped);
}

/*
 * Coverage over the recent window: transformed and skipped sample counts
 * from the current bucket plus the one before it, so the figure always spans
 * at least one full period and never momentarily reads zero.
 */
void waterfall_get_recent_coverage(const waterfall_t *wf,
                                   uint64_t *samples_transformed,
                                   uint64_t *samples_skipped)
{
    if (samples_transformed)
        *samples_transformed =
            (atomic_load(&wf->win_frames[0])
             + atomic_load(&wf->win_frames[1])) * wf->fft_size;
    if (samples_skipped)
        *samples_skipped = atomic_load(&wf->win_skipped[0])
                         + atomic_load(&wf->win_skipped[1]);
}

void waterfall_reset_coverage(waterfall_t *wf)
{
    atomic_store(&wf->frames, 0);
    atomic_store(&wf->samples_skipped, 0);
    atomic_store(&wf->win_frames[0], 0);
    atomic_store(&wf->win_frames[1], 0);
    atomic_store(&wf->win_skipped[0], 0);
    atomic_store(&wf->win_skipped[1], 0);
    atomic_store(&wf->win_started_ms, 0);
}
