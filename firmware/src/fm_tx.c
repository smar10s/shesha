#include "fm_tx.h"
#include "fm_mod.h"
#include "tx_scheduler.h"
#include "dma_tx.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * Audio ring capacity, samples.  Power of two so the wrap is a mask.
 * 262144 = 5.9 s at 44.1 kHz = 1 MB.
 *
 * Bounded on purpose.  The previous implementation grew one realloc'd
 * buffer holding the entire file and resized it on every push — an O(n^2)
 * copy of up to 32 MB, performed while holding the very mutex the TX
 * thread needed to generate samples.  That was the trigger for the first
 * underrun.  A bounded ring makes daemon memory independent of file
 * length, and makes "producer too fast" a backpressure signal instead of
 * an allocation.
 */
#define FM_AUDIO_RING_SAMPLES  (1u << 18)

/*
 * Largest audio window handed to the modulator in one go.  A DAC chunk of
 * DMA_TX_STREAM_CHUNK samples needs chunk * audio_rate / dac_rate audio
 * samples — 86 at 44.1 kHz into 2.1 MSPS — so linearising out of the ring
 * costs a few hundred bytes of memcpy.  Sized for the degenerate
 * audio_rate == dac_rate case so that even then a full chunk is produced.
 */
#define FM_AUDIO_WINDOW  (DMA_TX_STREAM_CHUNK + 2)

typedef struct {
    /* Ring storage.  Producer writes at head, consumer reads at tail. */
    float   *ring;
    uint32_t size;             /* power of two */
    uint32_t mask;

    /*
     * Cursors in absolute sample counts, never wrapped.  Wrapped indices
     * would make "full" and "empty" indistinguishable; absolute counts
     * cannot be confused, and 64 bits will not roll over in any realistic
     * runtime (2^64 samples at 44.1 kHz is 13 million years).
     *
     * head is written only by the producer, tail only by the consumer, so
     * acquire/release on these two is the whole synchronisation protocol.
     * No mutex is taken anywhere, which is the point: the old code held
     * one across sample generation.
     *
     * A seek cannot move head below a tail the consumer may be about to
     * publish.  The producer instead publishes the discard point here and
     * the consumer applies it to its own tail.  UINT64_MAX means "none".
     */
    _Atomic uint64_t head;
    _Atomic uint64_t tail;
    _Atomic uint64_t flush_target;

    _Atomic bool end_of_stream;
    _Atomic bool finished;     /* consumer has stopped touching the ring */

    /* Consumer-owned. */
    fm_mod_t mod;

    uint32_t audio_rate;
    uint32_t dac_rate;

    /* Telemetry. */
    _Atomic uint64_t starved_samples;
    _Atomic uint32_t push_rejects;
} fm_stream_t;

/*
 * Owned by the poll-loop thread.  Only that thread allocates and frees it,
 * so there is no race on the pointer itself; the TX thread only ever
 * dereferences the value passed to it as its user argument.
 */
static fm_stream_t *g_fm_stream;

/* ---- one-shot ---- */

typedef struct {
    float   *audio;
    uint32_t n_audio;
    fm_mod_t mod;
} fm_oneshot_t;

static ssize_t fm_produce(float *real, float *imag, size_t max_chunk,
                          void *user)
{
    fm_oneshot_t *fm = (fm_oneshot_t *)user;

    /* wrap = false, so generation stops when the buffer is exhausted and
     * returning 0 ends the stream. */
    return fm_mod_generate(&fm->mod, fm->audio, fm->n_audio, false,
                           real, imag, max_chunk);
}

static void fm_cleanup(void *user)
{
    fm_oneshot_t *fm = (fm_oneshot_t *)user;
    if (fm) {
        free(fm->audio);
        free(fm);
    }
}

int fm_tx_start(const float *audio, uint32_t audio_rate,
                uint32_t deviation_hz, uint32_t dac_rate,
                size_t n_audio_samples, const rds_tx_config_t *rds)
{
    if (audio_rate == 0 || dac_rate == 0) {
        fprintf(stderr, "fm_tx: invalid rates\n");
        return -1;
    }
    if (n_audio_samples < 2 || audio == NULL) {
        fprintf(stderr, "fm_tx: need at least 2 audio samples\n");
        return -1;
    }

    fm_mod_lut_init();

    fm_oneshot_t *fm = calloc(1, sizeof(*fm));
    if (!fm) {
        fprintf(stderr, "fm_tx: alloc failed\n");
        return -1;
    }

    fm->audio = malloc(n_audio_samples * sizeof(float));
    if (!fm->audio) {
        fprintf(stderr, "fm_tx: audio alloc failed\n");
        free(fm);
        return -1;
    }
    memcpy(fm->audio, audio, n_audio_samples * sizeof(float));
    fm->n_audio = (uint32_t)n_audio_samples;

    fm_mod_init(&fm->mod, audio_rate, deviation_hz, dac_rate, FM_GEN_NCO);

    if (rds) {
        fm_mod_set_rds(&fm->mod, rds);
        if (rds->enabled)
            fm_mod_enable_subcarriers(&fm->mod, FM_SUB_PILOT_AMP,
                                      FM_SUB_RDS_AMP);
    }

    return tx_scheduler_start_stream(fm_produce, fm_cleanup, fm, dac_rate);
}

/* ---- streaming ---- */

/*
 * Consumer.  Runs on the TX thread.  Takes no lock, sleeps under no
 * circumstance, and always returns either a full chunk or 0.
 *
 * The old version returned ONE sample after a 10 ms usleep() when starved:
 * 100 samples/s offered to a 2,100,000 samples/s DAC, which guaranteed the
 * underrun it was reacting to.
 */
static ssize_t fm_stream_produce(float *real, float *imag,
                                 size_t max_chunk, void *user)
{
    fm_stream_t *s = (fm_stream_t *)user;

    /* Apply a pending seek before looking at the cursors.  Only the
     * consumer writes tail, so this cannot race a producer moving head. */
    uint64_t flush_to = atomic_exchange_explicit(
        &s->flush_target, UINT64_MAX, memory_order_acquire);
    if (flush_to != UINT64_MAX) {
        atomic_store_explicit(&s->tail, flush_to, memory_order_release);
        s->mod.audio_pos = 0.0;
    }

    uint64_t tail = atomic_load_explicit(&s->tail, memory_order_relaxed);
    uint64_t head = atomic_load_explicit(&s->head, memory_order_acquire);
    uint32_t avail = (uint32_t)(head - tail);

    size_t produced = 0;

    if (avail >= 2) {
        /* Only the samples the modulator will actually read. */
        uint32_t need = (uint32_t)(s->mod.audio_pos
                                   + (double)max_chunk * s->mod.audio_step) + 2;
        if (need > FM_AUDIO_WINDOW)
            need = FM_AUDIO_WINDOW;
        if (need > avail)
            need = avail;

        /* Linearise out of the ring.  Cheaper than teaching the modulator
         * ring geometry, and it keeps fm_mod.c shared with the instrument. */
        float window[FM_AUDIO_WINDOW];
        uint32_t start = (uint32_t)(tail & s->mask);
        uint32_t first = s->size - start;
        if (first > need)
            first = need;
        memcpy(window, &s->ring[start], first * sizeof(float));
        if (need > first)
            memcpy(window + first, &s->ring[0],
                   (need - first) * sizeof(float));

        produced = fm_mod_generate(&s->mod, window, need, false,
                                   real, imag, max_chunk);

        /* Release the audio the modulator consumed and rebase the
         * fractional read position onto the new ring tail. */
        uint32_t consumed = (uint32_t)s->mod.audio_pos;
        if (consumed > 0) {
            s->mod.audio_pos -= (double)consumed;
            atomic_store_explicit(&s->tail, tail + consumed,
                                  memory_order_release);
        }
    }

    if (produced == 0) {
        /* Genuinely done: the client has closed the stream and there is
         * nothing left that can be modulated.  Up to one audio sample is
         * left unplayed, because interpolation needs a pair — 23 us. */
        if (atomic_load_explicit(&s->end_of_stream, memory_order_acquire))
            return 0;

        /*
         * Starved, but more audio may still be coming.  Emit a full chunk
         * of unmodulated carrier and return immediately.  FM carries its
         * timeline in the phase trajectory, so feeding the DAC late does
         * not click — it time-stretches, and the listener hears the audio
         * drag.  Keeping the DAC fed at exactly the nominal rate costs
         * only the silence it stands in for.
         */
        fm_mod_carrier(&s->mod, real, imag, max_chunk);
        atomic_fetch_add_explicit(&s->starved_samples, max_chunk,
                                  memory_order_relaxed);
        return max_chunk;
    }

    return produced;
}

static void fm_stream_free(fm_stream_t *s)
{
    if (!s)
        return;
    free(s->ring);
    free(s);
}

/* Called on the TX thread as it exits.  Deliberately does NOT free: the
 * poll loop may be inside fm_tx_stream_push() at this instant.  Ownership
 * stays with the poll-loop thread, which frees via
 * fm_tx_stream_release() once tx_scheduler_stop() has joined us. */
static void fm_stream_cleanup(void *user)
{
    fm_stream_t *s = (fm_stream_t *)user;
    if (s)
        atomic_store_explicit(&s->finished, true, memory_order_release);
}

void fm_tx_stream_release(void)
{
    fm_stream_free(g_fm_stream);
    g_fm_stream = NULL;
}

int fm_tx_stream_start(const float *first_chunk, uint32_t n,
                       uint32_t audio_rate, uint32_t deviation_hz,
                       uint32_t dac_rate, const rds_tx_config_t *rds)
{
    if (audio_rate == 0 || dac_rate == 0)
        return -1;
    if (n > 0 && first_chunk == NULL)
        return -1;
    if (n > FM_AUDIO_RING_SAMPLES)
        return -1;

    /* Joins any TX thread still running, which is what makes freeing the
     * previous stream object safe. */
    tx_scheduler_stop();
    fm_tx_stream_release();

    fm_mod_lut_init();

    fm_stream_t *s = calloc(1, sizeof(*s));
    if (!s)
        return -1;

    s->ring = malloc(FM_AUDIO_RING_SAMPLES * sizeof(float));
    if (!s->ring) {
        free(s);
        return -1;
    }
    s->size = FM_AUDIO_RING_SAMPLES;
    s->mask = FM_AUDIO_RING_SAMPLES - 1u;
    s->audio_rate = audio_rate;
    s->dac_rate = dac_rate;

    atomic_init(&s->head, 0);
    atomic_init(&s->tail, 0);
    atomic_init(&s->flush_target, UINT64_MAX);
    atomic_init(&s->end_of_stream, false);
    atomic_init(&s->finished, false);
    atomic_init(&s->starved_samples, 0);
    atomic_init(&s->push_rejects, 0);

    fm_mod_init(&s->mod, audio_rate, deviation_hz, dac_rate, FM_GEN_NCO);

    if (rds) {
        fm_mod_set_rds(&s->mod, rds);
        if (rds->enabled)
            fm_mod_enable_subcarriers(&s->mod, FM_SUB_PILOT_AMP,
                                      FM_SUB_RDS_AMP);
    }

    if (n > 0) {
        memcpy(s->ring, first_chunk, n * sizeof(float));
        atomic_store_explicit(&s->head, n, memory_order_release);
    }

    g_fm_stream = s;

    if (tx_scheduler_start_stream(fm_stream_produce, fm_stream_cleanup,
                                  s, dac_rate) != 0) {
        /* The scheduler already invoked the cleanup callback on failure;
         * the thread never ran, so freeing here is safe. */
        fm_tx_stream_release();
        return -1;
    }
    return 0;
}

int fm_tx_set_rds(const rds_tx_config_t *cfg)
{
    fm_stream_t *s = g_fm_stream;
    if (!s || !cfg)
        return -1;
    if (atomic_load_explicit(&s->finished, memory_order_acquire))
        return -1;

    /* Publish the new pattern before the flag that makes the TX thread use
     * the subcarriers, so a live enable never reads an empty pattern. */
    fm_mod_set_rds(&s->mod, cfg);
    if (cfg->enabled)
        fm_mod_enable_subcarriers(&s->mod, FM_SUB_PILOT_AMP, FM_SUB_RDS_AMP);
    else
        fm_mod_disable_subcarriers(&s->mod);
    return 0;
}

int fm_tx_stream_push(const float *chunk, uint32_t n)
{
    fm_stream_t *s = g_fm_stream;
    if (!s || !chunk)
        return FM_TX_PUSH_ERR;
    if (atomic_load_explicit(&s->finished, memory_order_acquire))
        return FM_TX_PUSH_ERR;
    /* END closes the stream.  Accepting audio after it would keep the TX
     * thread alive past a stop, because it only exits once the ring drains. */
    if (atomic_load_explicit(&s->end_of_stream, memory_order_acquire))
        return FM_TX_PUSH_ERR;
    if (n == 0)
        return FM_TX_PUSH_OK;
    if (n > s->size)
        return FM_TX_PUSH_ERR;

    uint64_t head = atomic_load_explicit(&s->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&s->tail, memory_order_acquire);
    uint32_t space = s->size - (uint32_t)(head - tail);

    if (n > space) {
        /* Backpressure, not failure.  Returning an error here would make
         * the client abandon a stream that is merely ahead of realtime. */
        atomic_fetch_add_explicit(&s->push_rejects, 1, memory_order_relaxed);
        return FM_TX_PUSH_FULL;
    }

    uint32_t start = (uint32_t)(head & s->mask);
    uint32_t first = s->size - start;
    if (first > n)
        first = n;
    memcpy(&s->ring[start], chunk, first * sizeof(float));
    if (n > first)
        memcpy(&s->ring[0], chunk + first, (n - first) * sizeof(float));

    /* Publish the data before the cursor that advertises it. */
    atomic_store_explicit(&s->head, head + n, memory_order_release);
    return FM_TX_PUSH_OK;
}

void fm_tx_stream_end(void)
{
    fm_stream_t *s = g_fm_stream;
    if (s)
        atomic_store_explicit(&s->end_of_stream, true, memory_order_release);
}

int fm_tx_stream_flush(void)
{
    fm_stream_t *s = g_fm_stream;
    if (!s)
        return -1;
    if (atomic_load_explicit(&s->finished, memory_order_acquire))
        return -1;

    /*
     * Drop the audio the consumer has not taken.  Publish the current head
     * as the consumer's new tail rather than moving head backwards: an
     * in-flight consume can commit a tail past a head we just lowered, and
     * the resulting tail > head makes the unsigned fill count wrap and
     * replay the ring.
     *
     * Snapshotting head here also preserves anything pushed after the
     * flush, because those samples sit beyond this target.  Only the poll
     * loop (this thread) pushes, so head cannot move while we read it.
     */
    uint64_t head = atomic_load_explicit(&s->head, memory_order_acquire);
    atomic_store_explicit(&s->flush_target, head, memory_order_release);
    return 0;
}

void fm_tx_stream_stats(fm_tx_stream_stats_t *out)
{
    memset(out, 0, sizeof(*out));

    fm_stream_t *s = g_fm_stream;
    if (!s)
        return;

    uint64_t head = atomic_load_explicit(&s->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&s->tail, memory_order_relaxed);

    out->active = !atomic_load_explicit(&s->finished, memory_order_relaxed);
    out->end_of_stream =
        atomic_load_explicit(&s->end_of_stream, memory_order_relaxed);
    out->ring_fill = (uint32_t)(head - tail);
    out->ring_size = s->size;
    out->audio_rate = s->audio_rate;
    out->audio_consumed = tail;
    out->starved_samples =
        atomic_load_explicit(&s->starved_samples, memory_order_relaxed);
    out->push_rejects =
        atomic_load_explicit(&s->push_rejects, memory_order_relaxed);
}
