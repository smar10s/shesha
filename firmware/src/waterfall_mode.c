/*
 * The software PSD producer.
 *
 * One thread per worker, pinned one per core, each with its own waterfall_t
 * and its own SPSC ring.  Workers take disjoint blocks of the sample stream
 * from the shared claim (wf_claim.h), so a second worker transforms samples
 * the first one did not rather than duplicating its work.
 *
 * Why per-worker rings instead of one shared ring: the ring is a
 * single-producer primitive that WiFi, FM, and IQ also use, and the shared
 * claim already stamps every frame with a global index.  Merging two SPSC
 * rings on that index is strictly cheaper than weakening a primitive three
 * other modes depend on.
 *
 * Why pinning: two unpinned workers can be scheduled onto the same core, at
 * which point they serialise and the second one buys nothing but overhead.
 */
#define _GNU_SOURCE
#include "waterfall_mode.h"
#include "waterfall.h"
#include "wf_claim.h"
#include "wf_mag.h"
#include "wf_workers_policy.h"
#include "protocol.h"
#include "radio.h"
#include "spsc_ring.h"
#include "ring_slots.h"
#include "thread_stats.h"
#include <sched.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>

extern atomic_bool g_running;
extern waterfall_t *g_waterfall;
extern spsc_ring_t *g_ringbuf;
extern wf_claim_t g_wf_claim;
extern radio_state_t g_radio;

static atomic_bool g_producer_running = false;
/* Workers that have entered their loop and not yet left it.  The status
 * field means "actually running", so a worker that failed to start (or has
 * already exited) must not be counted. */
static atomic_int g_workers_live = 0;

/* Worker 0 uses g_waterfall and g_ringbuf so the status and teardown paths in
 * main.c keep working unchanged; workers 1.. own theirs. */
typedef struct {
    pthread_t    thread;
    waterfall_t *wf;
    spsc_ring_t *ring;
    int          index;
    bool         started;
} wf_worker_t;

static wf_worker_t g_workers[WF_MAX_WORKERS];

/* What the user asked for, and what the policy actually runs.  They differ
 * when the request is clamped at or below fs_max (wf_workers_policy.h).  The
 * request is remembered; the clamp is re-evaluated from the current rate
 * whenever the target is asked for or a producer starts. */
static int g_workers_requested = 1;
static int g_n_started;

extern double monotonic_sec(void);

static void *run(void *arg)
{
    wf_worker_t *w = arg;

    /* Must be the first statement; see thread_stats.h.  Names truncate at 15
     * characters, so the suffix has to stay short. */
    char name[16];
    snprintf(name, sizeof(name), "wf-producer-%d", w->index);
    thread_stats_name_self(name);

    atomic_fetch_add(&g_workers_live, 1);

#ifdef __linux__
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(w->index, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0)
        fprintf(stderr, "wf: could not pin worker %d to cpu %d\n",
                w->index, w->index);
#endif

    size_t current_size = waterfall_get_fft_size(w->wf);
    size_t slot_sz = psd_slot_size(current_size);
    psd_slot_t *slot = malloc(slot_sz);
    if (!slot) goto out;

    while (atomic_load(&g_running) && atomic_load(&g_producer_running)) {
        size_t fft_size = waterfall_get_fft_size(w->wf);
        if (fft_size != current_size) {
            current_size = fft_size;
            slot_sz = psd_slot_size(current_size);
            psd_slot_t *new_slot = realloc(slot, slot_sz);
            if (!new_slot) break;
            slot = new_slot;
        }
        uint64_t capture_freq_hz;
        uint32_t capture_sample_rate_hz;
        radio_capture_snapshot(&capture_freq_hz, &capture_sample_rate_hz);
        if (waterfall_process(w->wf, slot->power) == 0) {
            slot->ts = monotonic_sec();
            slot->frame_index = waterfall_last_frame_index(w->wf);
            slot->freq_hz = capture_freq_hz;
            slot->sample_rate_hz = capture_sample_rate_hz;
            spsc_ring_push(w->ring, slot);
        }
    }
    free(slot);
out:
    atomic_fetch_sub(&g_workers_live, 1);
    return NULL;
}

int waterfall_mode_enter(void)
{
    if (atomic_load(&g_producer_running)) return 0;

    /* Worker 0 borrows the daemon's shared instances.  Starting without
     * them would dereference NULL in the producer thread, so refuse before
     * any thread is created. */
    if (!g_waterfall || !g_ringbuf) {
        fprintf(stderr, "wf: shared waterfall state is not allocated\n");
        return -1;
    }

    size_t fft_size = waterfall_get_fft_size(g_waterfall);
    window_type_t win = waterfall_get_window(g_waterfall);

    /*
     * A producer start is a new stream.  Reset the shared claim, and drop
     * worker 0's cached ring position with it, so an offset left ahead of the
     * ADC by the previous run cannot stall this one.  Workers 1.. are created
     * below and latch the same origin.
     */
    wf_claim_reset(&g_wf_claim, fft_size);
    waterfall_relatch(g_waterfall);

    /*
     * The request is clamped by measurement, not by the user: at or below
     * fs_max one worker already covers the stream and a second only spends
     * duty waiting (wf_workers_policy.h).  Applied on every producer start,
     * so the clamp always reflects the rate at the moment it starts; a rate
     * change only re-clamps on the next cycle.
     */
    int want = wf_workers_effective(g_workers_requested,
                                    g_radio.sample_rate_hz, fft_size);

    /*
     * Build every worker's state here, on the calling thread, BEFORE any
     * thread starts: waterfall_create plans an FFT, and FFTW's planner is not
     * thread-safe.  Two workers planning concurrently is a corruption bug
     * that would show up as intermittent garbage, not as an error.
     */
    memset(g_workers, 0, sizeof(g_workers));
    for (int i = 0; i < want; i++) {
        g_workers[i].index = i;
        if (i == 0) {
            g_workers[i].wf = g_waterfall;
            g_workers[i].ring = g_ringbuf;
        } else {
            g_workers[i].wf = waterfall_create(fft_size, win, &g_wf_claim);
            g_workers[i].ring = spsc_ring_create(psd_slot_size(fft_size),
                                                 RING_SLOTS_PSD);
            if (!g_workers[i].wf || !g_workers[i].ring) {
                fprintf(stderr, "wf: worker %d alloc failed, "
                        "running with %d\n", i, i);
                if (g_workers[i].wf) waterfall_destroy(g_workers[i].wf);
                if (g_workers[i].ring) spsc_ring_destroy(g_workers[i].ring);
                g_workers[i].wf = NULL;
                g_workers[i].ring = NULL;
                want = i;
                break;
            }
        }
    }

    /*
     * Start every worker's counters at zero for this cycle.  Worker 0 borrows
     * main.c's long-lived g_waterfall, so without this its lifetime totals
     * span every rate/worker change since the last FFT resize, while workers
     * 1.. start fresh -- an asymmetry that reads as a bug at the exit tally.
     * The claim was reset above; only the per-instance coverage counters are
     * reset here.
     */
    for (int i = 0; i < want; i++)
        if (g_workers[i].wf)
            waterfall_reset_coverage(g_workers[i].wf);

    /* The latency cap scales with the worker count, so the claim has to know
     * it before any capture runs (wf_claim.h). */
    wf_claim_set_workers(&g_wf_claim, want);

    atomic_store(&g_producer_running, true);
    g_n_started = 0;
    for (int i = 0; i < want; i++) {
        if (pthread_create(&g_workers[i].thread, NULL, run, &g_workers[i]) != 0) {
            fprintf(stderr, "wf: could not start worker %d\n", i);
            break;
        }
        g_workers[i].started = true;
        g_n_started++;
    }

    if (g_n_started == 0) {
        atomic_store(&g_producer_running, false);
        for (int i = 1; i < want; i++) {
            if (g_workers[i].wf) waterfall_destroy(g_workers[i].wf);
            if (g_workers[i].ring) spsc_ring_destroy(g_workers[i].ring);
            g_workers[i].wf = NULL;
            g_workers[i].ring = NULL;
        }
        return -1;
    }

    printf("mode: waterfall (%d worker%s)\n",
           g_n_started, g_n_started == 1 ? "" : "s");
    return 0;
}

void waterfall_mode_exit(void)
{
    if (!atomic_load(&g_producer_running)) return;
    atomic_store(&g_producer_running, false);

    for (int i = 0; i < WF_MAX_WORKERS; i++) {
        if (g_workers[i].started) {
            pthread_join(g_workers[i].thread, NULL);
            g_workers[i].started = false;
        }
    }

    /* Per-worker tallies, so an unbalanced producer set is visible rather
     * than hidden inside an aggregate coverage figure.  A worker starved by
     * a claim bug reads near-zero frames against many timeouts. */
    for (int i = 0; i < WF_MAX_WORKERS; i++) {
        if (!g_workers[i].wf) continue;
        uint64_t frames = 0, skipped = 0;
        waterfall_get_coverage(g_workers[i].wf, &frames, NULL, &skipped);
        fprintf(stderr, "wf: worker %d  frames %llu  skipped %llu  "
                "timeouts %llu\n", i,
                (unsigned long long)frames,
                (unsigned long long)skipped,
                (unsigned long long)waterfall_capture_timeouts(g_workers[i].wf));
    }

    /* Worker 0 borrows main.c's instances, which outlive the producer. */
    for (int i = 1; i < WF_MAX_WORKERS; i++) {
        if (g_workers[i].wf) waterfall_destroy(g_workers[i].wf);
        if (g_workers[i].ring) spsc_ring_destroy(g_workers[i].ring);
        g_workers[i].wf = NULL;
        g_workers[i].ring = NULL;
    }
    g_n_started = 0;
}

int waterfall_mode_worker_count(void)
{
    return atomic_load(&g_workers_live);
}

bool waterfall_mode_running(void)
{
    return atomic_load(&g_producer_running) && atomic_load(&g_workers_live) > 0;
}

bool waterfall_mode_active(void)
{
    return atomic_load(&g_producer_running);
}

void waterfall_mode_set_worker_count(int n)
{
    if (n < 1) n = 1;
    if (n > WF_MAX_WORKERS) n = WF_MAX_WORKERS;
    g_workers_requested = n;
}

int waterfall_mode_worker_requested(void)
{
    return g_workers_requested;
}

/*
 * Coverage summed across workers.  Coverage is a property of the producer
 * SET, not of one thread: with two workers each transforms about half the
 * frames, so either one alone reads about half the true figure.
 *
 * Skipped samples sum correctly too, which is worth stating because it looks
 * like it should double-count.  A skip is published by compare-and-swap and
 * wf_claim_skip_forward returns the real advance, so when two workers notice
 * the same backlog only the one that wins the CAS is credited and the loser
 * is told zero.  Each abandoned sample is therefore counted exactly once, by
 * whichever worker did the abandoning.
 */
void waterfall_mode_get_coverage(uint64_t *transformed, uint64_t *skipped)
{
    uint64_t t = 0, s = 0;
    for (int i = 0; i < WF_MAX_WORKERS; i++) {
        if (!g_workers[i].wf) continue;
        uint64_t wt = 0, ws = 0;
        waterfall_get_recent_coverage(g_workers[i].wf, &wt, &ws);
        t += wt;
        s += ws;
    }
    if (transformed) *transformed = t;
    if (skipped)     *skipped = s;
}

/* Drain every worker's ring into `out`, which must hold
 * WF_MAX_WORKERS * RING_SLOTS_PSD slots.  Returns the slot count. */
int waterfall_mode_pop_all(void *out, size_t slot_sz)
{
    int total = 0;
    for (int i = 0; i < WF_MAX_WORKERS; i++) {
        spsc_ring_t *ring = i == 0 ? g_ringbuf : g_workers[i].ring;
        if (!ring) continue;
        int n = spsc_ring_pop_all(ring,
                                  (char *)out + (size_t)total * slot_sz,
                                  RING_SLOTS_PSD, NULL);
        total += n;
    }
    return total;
}

void drain_waterfall(const drain_ctx_t *ctx)
{
    static void *psd_pop_buf;
    static size_t psd_pop_buf_size;

    double now = monotonic_sec();
    double interval = 1.0 / ctx->target_fps;
    if (now - *ctx->last_broadcast < interval)
        return;

    uint32_t fft_size = g_waterfall
        ? (uint32_t)waterfall_get_fft_size(g_waterfall)
        : WATERFALL_DEFAULT_FFT_SIZE;
    size_t slot_sz = psd_slot_size(fft_size);

    /* Sized for every worker's ring at once: pop_all drains them all into
     * this one buffer. */
    size_t want = slot_sz * RING_SLOTS_PSD * WF_MAX_WORKERS;
    if (!psd_pop_buf || psd_pop_buf_size < want) {
        free(psd_pop_buf);
        psd_pop_buf_size = want;
        psd_pop_buf = malloc(psd_pop_buf_size);
        if (!psd_pop_buf) return;
    }

    int n = waterfall_mode_pop_all(psd_pop_buf, slot_sz);
    if (n > 0) {
        /*
         * Reduce the drained frames to one, in the LINEAR POWER domain the
         * producer emits (wf_mag.h), then convert to dB once at the end.
         * Reducing in power is what makes ACCUM_AVG a true power average;
         * averaging dB would be a geometric mean, ~2.4 dB low on noise-like
         * bins.  ACCUM_MAX is identical either way (log is monotonic).
         *
         * Slots arrive grouped by worker, not in frame order, so nothing here
         * may assume the last slot is the newest.  MAX and AVG do not care --
         * both are order-insensitive over the batch -- but the timestamp, the
         * wire sequence, and ACCUM_NONE all need the genuinely newest frame,
         * which is the one with the highest frame_index.
         */
        psd_slot_t *first = (psd_slot_t *)psd_pop_buf;
        float *acc = first->power;
        double frame_ts = first->ts;
        uint64_t frame_index = first->frame_index;
        uint64_t frame_freq = first->freq_hz;
        uint32_t frame_rate = first->sample_rate_hz;
        int newest = 0;

        for (int i = 1; i < n; i++) {
            psd_slot_t *s = (psd_slot_t *)((char *)psd_pop_buf + (size_t)i * slot_sz);
            if (s->frame_index > frame_index) {
                frame_index = s->frame_index;
                frame_ts = s->ts;
                frame_freq = s->freq_hz;
                frame_rate = s->sample_rate_hz;
                newest = i;
            }
        }
        (void)frame_rate;  /* the PSD wire format carries only frequency */

        accum_mode_t accum = (accum_mode_t)ctx->accum;
        if (accum == ACCUM_NONE) {
            psd_slot_t *s = (psd_slot_t *)((char *)psd_pop_buf
                                           + (size_t)newest * slot_sz);
            acc = s->power;
        } else {
            for (int i = 1; i < n; i++) {
                psd_slot_t *s = (psd_slot_t *)((char *)psd_pop_buf + (size_t)i * slot_sz);
                if (accum == ACCUM_MAX) {
                    for (uint32_t j = 0; j < fft_size; j++)
                        if (s->power[j] > acc[j])
                            acc[j] = s->power[j];
                } else { /* ACCUM_AVG: mean power */
                    for (uint32_t j = 0; j < fft_size; j++)
                        acc[j] += s->power[j];
                }
            }
            if (accum == ACCUM_AVG && n > 1) {
                float inv = 1.0f / (float)n;
                for (uint32_t j = 0; j < fft_size; j++)
                    acc[j] *= inv;
            }
        }

        /* Power -> dBFS, once, in place: `acc` points into the pop buffer,
         * which is scratch and is refilled on the next drain. */
        wf_power_to_db(acc, fft_size, acc);

        float center_mhz = (float)frame_freq / 1e6f;
        /*
         * The wire seq is the newest PRODUCER FRAME INDEX in this broadcast,
         * not the ring's write counter.  The ring counter advances by however
         * many frames the drain reduced -- ~27 at 20 MSPS -- so a client
         * reading it as a delivery counter saw normal operation as thousands
         * of dropped frames.  The frame index is the producer's own position
         * in the stream, which is also what makes it comparable across two
         * producers (wf_claim.h).
         */
        size_t frame_len = proto_build_psd(ctx->sendbuf,
            (uint32_t)frame_index,
            frame_ts, center_mhz, fft_size, acc);
        ctx->sink(ctx->sendbuf, frame_len, ctx->sink_user);
    }
    *ctx->last_broadcast = now;
}
