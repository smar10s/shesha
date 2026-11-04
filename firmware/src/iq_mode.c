#include "iq_mode.h"
#include "iq_pack.h"
#include "adc_clip.h"
#include "radio.h"
#include "spsc_ring.h"
#include "ring_slots.h"
#include "protocol.h"
#include "dma_rx.h"
#include "hal.h"
#include "thread_stats.h"

#include <stdlib.h>
#include <stdio.h>
#include <pthread.h>
#include <stdatomic.h>
#include <string.h>
#include <unistd.h>

extern atomic_bool g_running;
extern atomic_uint g_iq_bits;
extern spsc_ring_t *g_iq_ringbuf;
extern double monotonic_sec(void);

static pthread_t g_producer_thread;
static atomic_bool g_producer_running = false;
/* Set only after the buffers are ready.  pthread_create success is not
 * evidence that the producer can actually run. */
static atomic_bool g_producer_ready = false;
/* Cleared while no client subscribes to DATA_IQ.  The producer still owns
 * the DMA read pointer, but skips the DDR read and packing work. */
static atomic_bool g_rx_wanted = true;

/*
 * The whole point of this producer is to move DDR words to the wire with
 * no float roundtrip: memcpy-staged bulk read of the raw mapping (the
 * convert.c discipline — memcpy is not volatile-ordered, so the barrier
 * states the WR_PTR ordering), then shift/OR packing.  At 2 MSPS that is
 * 6 MB/s of packing, far below what even one Cortex-A9 core can do.
 */
static void *run(void *arg)
{
    (void)arg;
    thread_stats_name_self("iq-rx-producer");
    atomic_store(&g_producer_ready, false);

    uint32_t *words = malloc(IQ_CHUNK_SAMPLES * sizeof(uint32_t));
    iq_slot_t *slot = malloc(sizeof(iq_slot_t));
    if (!words || !slot) {
        fprintf(stderr, "iq_mode: buffer alloc failed\n");
        free(words);
        free(slot);
        goto done;
    }

    uint32_t rd_ptr = dma_rx_wr_ptr();

    atomic_store(&g_producer_ready, true);

    while (atomic_load(&g_running) && atomic_load(&g_producer_running)) {
        if (!atomic_load_explicit(&g_rx_wanted, memory_order_relaxed)) {
            rd_ptr = dma_rx_wr_ptr();
            usleep(1000);
            continue;
        }

        uint32_t wr = dma_rx_wr_ptr();
        uint32_t avail;
        if (wr >= rd_ptr)
            avail = wr - rd_ptr;
        else
            avail = (DMA_RX_BUF_SAMPLES - rd_ptr) + wr;

        if (avail < IQ_CHUNK_SAMPLES) {
            usleep(500);
            continue;
        }

        __sync_synchronize();

        const volatile uint32_t *rx = hal_ddr_rx_buf();
        if (rd_ptr + IQ_CHUNK_SAMPLES <= DMA_RX_BUF_SAMPLES) {
            memcpy(words, (const uint32_t *)&rx[rd_ptr],
                   IQ_CHUNK_SAMPLES * sizeof(uint32_t));
        } else {
            size_t first = DMA_RX_BUF_SAMPLES - rd_ptr;
            size_t second = IQ_CHUNK_SAMPLES - first;
            memcpy(words, (const uint32_t *)&rx[rd_ptr],
                   first * sizeof(uint32_t));
            memcpy(words + first, (const uint32_t *)&rx[0],
                   second * sizeof(uint32_t));
        }

        rd_ptr += IQ_CHUNK_SAMPLES;
        if (rd_ptr >= DMA_RX_BUF_SAMPLES)
            rd_ptr -= DMA_RX_BUF_SAMPLES;

        uint64_t clips = 0;
        uint8_t bits = (uint8_t)atomic_load(&g_iq_bits);
        uint64_t capture_freq_hz;
        uint32_t capture_sample_rate_hz;
        radio_capture_snapshot(&capture_freq_hz, &capture_sample_rate_hz);
        if (bits == IQ_BITS_8)
            iq_pack_8(words, IQ_CHUNK_SAMPLES, slot->packed, &clips);
        else
            iq_pack_12(words, IQ_CHUNK_SAMPLES, slot->packed, &clips);
        adc_clip_add(clips, (uint64_t)IQ_CHUNK_SAMPLES * 2);

        slot->ts = monotonic_sec();
        slot->n = IQ_CHUNK_SAMPLES;
        slot->bits = bits;
        slot->freq_hz = capture_freq_hz;
        slot->sample_rate_hz = capture_sample_rate_hz;
        spsc_ring_push(g_iq_ringbuf, slot);
    }

    free(words);
    free(slot);
done:
    atomic_store(&g_producer_ready, false);
    return NULL;
}

int iq_mode_enter(void)
{
    if (atomic_load(&g_producer_running))
        return 0;

    /* A leftover FM narrow FIR must never apply to raw IQ.  FM exit
     * disables it, but a daemon started straight into IQ mode has no FM
     * exit to rely on.  Non-fatal. */
    if (hal_ad9361_set_rx_fir_en(0) != 0)
        fprintf(stderr, "iq_mode: FIR disable failed (non-fatal)\n");

    if (!g_iq_ringbuf)
        g_iq_ringbuf = spsc_ring_create(sizeof(iq_slot_t), RING_SLOTS_IQ);
    if (!g_iq_ringbuf) {
        fprintf(stderr, "iq_mode: ringbuf create failed\n");
        return -1;
    }

    atomic_store(&g_producer_ready, false);
    atomic_store(&g_producer_running, true);
    if (pthread_create(&g_producer_thread, NULL, run, NULL) != 0) {
        atomic_store(&g_producer_running, false);
        fprintf(stderr, "iq_mode: thread create failed\n");
        return -1;
    }

    printf("mode: iq\n");
    return 0;
}

void iq_mode_exit(void)
{
    if (!atomic_load(&g_producer_running))
        return;
    atomic_store(&g_producer_running, false);
    pthread_join(g_producer_thread, NULL);

    spsc_ring_destroy(g_iq_ringbuf);
    g_iq_ringbuf = NULL;
}

bool iq_mode_running(void)
{
    return atomic_load(&g_producer_running) && atomic_load(&g_producer_ready);
}

bool iq_mode_active(void)
{
    return atomic_load(&g_producer_running);
}

void iq_mode_set_rx_wanted(bool wanted)
{
    atomic_store_explicit(&g_rx_wanted, wanted, memory_order_relaxed);
}

void iq_mode_flush(void)
{
    if (!g_iq_ringbuf)
        return;
    static iq_slot_t *slot;
    if (!slot)
        slot = malloc(sizeof(*slot));
    if (!slot)
        return;
    size_t seq;
    while (spsc_ring_pop_all(g_iq_ringbuf, slot, 1, &seq) > 0) {
    }
}

void drain_iq(const drain_ctx_t *ctx)
{
    static iq_slot_t *iq_slots;

    if (!g_iq_ringbuf) return;

    if (!iq_slots) {
        iq_slots = calloc(IQ_POP_MAX, sizeof(iq_slot_t));
        if (!iq_slots) return;
    }

    size_t start_seq;
    int n = spsc_ring_pop_all(g_iq_ringbuf, iq_slots,
                              IQ_POP_MAX, &start_seq);
    for (int i = 0; i < n; i++) {
        size_t frame_len = proto_build_iq(
            ctx->sendbuf, (uint32_t)(start_seq + i),
            iq_slots[i].ts,
            (float)iq_slots[i].freq_hz / 1e6f,
            iq_slots[i].sample_rate_hz,
            iq_slots[i].bits,
            iq_slots[i].n,
            iq_slots[i].packed);
        ctx->sink(ctx->sendbuf, frame_len, ctx->sink_user);
    }
}
