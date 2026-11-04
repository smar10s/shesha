#include "iq_tx.h"
#include "iq_ring.h"
#include "iq_pack.h"
#include "tx_scheduler.h"
#include "dma_tx.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 1 MB: 170 ms of 12-bit IQ at 2 MSPS.  Bounded on purpose — "producer
 * too fast" must be a backpressure signal (RESP_RETRY), not an
 * allocation, mirroring the FM audio ring. */
#define IQ_TX_RING_BYTES (1u << 20)

typedef struct {
    iq_byte_ring_t ring;
    _Atomic bool finished;
    _Atomic uint32_t starved_chunks;   /* producer found the ring empty */
    uint8_t  bits;
    uint32_t dac_rate;
} iq_tx_stream_t;

/* Owned by the poll-loop thread, like fm_tx.c's g_fm_stream. */
static iq_tx_stream_t *g_iq_stream;

static ssize_t iq_tx_produce(float *real, float *imag, size_t max_chunk,
                             void *user)
{
    iq_tx_stream_t *s = (iq_tx_stream_t *)user;
    uint32_t bps = (s->bits == IQ_BITS_8) ? 2u : 3u;
    uint32_t want = (uint32_t)max_chunk * bps;

    uint8_t scratch[DMA_TX_STREAM_CHUNK * 3];
    uint32_t got = iq_byte_ring_peek(&s->ring, scratch, want);
    uint32_t n_samples = got / bps;

    if (n_samples == 0) {
        if (atomic_load_explicit(&s->ring.end_of_stream,
                                 memory_order_acquire))
            return 0;   /* drained: end the stream */
        /* Starved but more may come.  Unlike FM, whose unmodulated
         * carrier is a valid stand-in, a finite IQ vector must never be
         * padded with silence: the padding lands *inside* the waveform
         * and tears it apart (the DAC cannot tell it from samples the
         * client meant to send).  Report starvation and let the
         * scheduler wait for the host to catch up. */
        atomic_fetch_add_explicit(&s->starved_chunks, 1,
                                  memory_order_relaxed);
        return -1;
    }

    if (s->bits == IQ_BITS_8)
        iq_unpack_8(scratch, n_samples, real, imag);
    else
        iq_unpack_12(scratch, n_samples, real, imag);

    iq_byte_ring_consume(&s->ring, n_samples * bps);
    return n_samples;
}

/* Called on the TX thread as it exits.  Deliberately does NOT free: the
 * poll loop may be inside iq_tx_stream_push() at this instant.  Ownership
 * stays with the poll-loop thread (see iq_tx_stream_release). */
static void iq_tx_cleanup(void *user)
{
    iq_tx_stream_t *s = (iq_tx_stream_t *)user;
    if (s)
        atomic_store_explicit(&s->finished, true, memory_order_release);
}

void iq_tx_stream_release(void)
{
    if (g_iq_stream) {
        iq_byte_ring_destroy(&g_iq_stream->ring);
        free(g_iq_stream);
        g_iq_stream = NULL;
    }
}

bool iq_tx_stream_active(void)
{
    return g_iq_stream
        && !atomic_load_explicit(&g_iq_stream->finished,
                                 memory_order_acquire);
}

uint8_t iq_tx_stream_bits(void)
{
    return g_iq_stream ? g_iq_stream->bits : 0;
}

int iq_tx_stream_start(uint8_t bits, uint32_t dac_rate)
{
    if (bits != IQ_BITS_8 && bits != IQ_BITS_12)
        return -1;
    if (dac_rate == 0)
        return -1;

    /* Joins any TX thread still running, which is what makes freeing the
     * previous stream object safe. */
    tx_scheduler_stop();
    iq_tx_stream_release();

    iq_tx_stream_t *s = calloc(1, sizeof(*s));
    if (!s)
        return -1;
    if (iq_byte_ring_init(&s->ring, IQ_TX_RING_BYTES) != 0) {
        free(s);
        return -1;
    }
    s->bits = bits;
    s->dac_rate = dac_rate;
    atomic_init(&s->finished, false);

    g_iq_stream = s;

    if (tx_scheduler_start_stream(iq_tx_produce, iq_tx_cleanup, s,
                                  dac_rate) != 0) {
        /* The scheduler already invoked the cleanup callback on failure;
         * the thread never ran, so freeing here is safe. */
        iq_tx_stream_release();
        return -1;
    }
    return 0;
}

int iq_tx_stream_push(const uint8_t *data, uint32_t n)
{
    if (!g_iq_stream || !data)
        return IQ_TX_PUSH_ERR;
    if (atomic_load_explicit(&g_iq_stream->finished, memory_order_acquire))
        return IQ_TX_PUSH_ERR;
    if (atomic_load_explicit(&g_iq_stream->ring.end_of_stream,
                             memory_order_acquire))
        return IQ_TX_PUSH_ERR;
    if (n == 0)
        return IQ_TX_PUSH_OK;

    uint32_t bps = (g_iq_stream->bits == IQ_BITS_8) ? 2u : 3u;
    if (n % bps != 0)
        return IQ_TX_PUSH_ERR;   /* partial sample — would desync unpack */

    int rc = iq_byte_ring_push(&g_iq_stream->ring, data, n);
    if (rc == 1)
        return IQ_TX_PUSH_FULL;
    if (rc != 0)
        return IQ_TX_PUSH_ERR;
    return IQ_TX_PUSH_OK;
}

void iq_tx_stream_end(void)
{
    if (g_iq_stream)
        atomic_store_explicit(&g_iq_stream->ring.end_of_stream, true,
                              memory_order_release);
}

void iq_tx_stream_stats(iq_tx_stream_stats_t *out)
{
    memset(out, 0, sizeof(*out));
    if (!g_iq_stream)
        return;
    out->active = !atomic_load_explicit(&g_iq_stream->finished,
                                        memory_order_relaxed);
    out->ring_fill = iq_byte_ring_avail(&g_iq_stream->ring);
    out->ring_size = g_iq_stream->ring.size;
    out->push_rejects = atomic_load_explicit(
        &g_iq_stream->ring.push_rejects, memory_order_relaxed);
    out->starved_chunks = atomic_load_explicit(
        &g_iq_stream->starved_chunks, memory_order_relaxed);
}
