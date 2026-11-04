#include "fm_mode.h"
#include "fm_rx.h"
#include "fm_rx_fir.h"
#include "adc_clip.h"
#include "spsc_ring.h"
#include "ring_slots.h"
#include "fm_protocol.h"
#include "dma_rx.h"
#include "radio.h"
#include "thread_stats.h"
#include "hal.h"

#include <stdlib.h>
#include <stdio.h>
#include <pthread.h>
#include <stdatomic.h>
#include <string.h>
#include <unistd.h>

extern atomic_bool g_running;
extern radio_state_t g_radio;
extern spsc_ring_t *g_fm_ringbuf;
extern double monotonic_sec(void);

static pthread_t g_producer_thread;
static atomic_bool g_producer_running = false;
/* Set only after the demod and buffers are ready.  pthread_create returning
 * success says nothing about whether the producer can actually run. */
static atomic_bool g_producer_ready = false;

/* The RX FIR config lives in fm_rx_fir.h so test_fm_rx_fir.c can assert its
 * unity DC gain; see the header for why that matters. */

/*
 * 49152 = 48 * 1024, so one chunk decimates to exactly one 1024-sample audio
 * ring slot (ring_slots.h: FM_AUDIO_FRAME_SIZE). Do not change one without
 * the other: at the old decimation of 47 this same chunk produced 1045
 * samples and the push silently discarded 21 of them per frame -- a 2%
 * shortfall and a discontinuity at every frame boundary, invisible because
 * the frame still carried a plausible rate the client believed.
 */
#define FM_CAPTURE_CHUNK 49152
#define FM_AUDIO_OUT_MAX 2048

static void *run(void *arg)
{
    (void)arg;
    thread_stats_name_self("fm-rx-producer");
    atomic_store(&g_producer_ready, false);

    fm_demod_t demod;
    if (fm_demod_init(&demod,
            g_radio.sample_rate_hz,
            FM_DEFAULT_DEVIATION,
            FM_DEFAULT_DEEMPHASIS) != 0) {
        fprintf(stderr, "fm_mode: demod init failed\n");
        goto done;
    }

    float *iq_real = malloc(FM_CAPTURE_CHUNK * sizeof(float));
    float *iq_imag = malloc(FM_CAPTURE_CHUNK * sizeof(float));
    float *audio   = malloc(FM_AUDIO_OUT_MAX * sizeof(float));
    if (!iq_real || !iq_imag || !audio) {
        fprintf(stderr, "fm_mode: buffer alloc failed\n");
        free(iq_real);
        free(iq_imag);
        free(audio);
        goto done;
    }

    uint32_t rd_ptr = dma_rx_wr_ptr();
    double t_last = monotonic_sec();
    unsigned long iter = 0;  /* skip first (slow) iteration in timing */

    atomic_store(&g_producer_ready, true);

    /*
     * Cost attribution.  `target/elapsed` below is capped at 100% by the
     * usleep() gate, so it proves liveness and nothing else; it cannot say
     * where the time goes. These two accumulators can, and they are the
     * whole reason a separate offline bench is unnecessary: the split is
     * measured in the real path, over the real uncached DDR mapping, where
     * a harness over malloc'd memory would read cached RAM and overstate
     * throughput ~8x.
     *
     * `shesha debug cpu` gives the producer's total share of the
     * core; these give the breakdown within it.
     */
    double t_read = 0.0, t_demod = 0.0;

    while (atomic_load(&g_running) && atomic_load(&g_producer_running)) {
        uint32_t wr = dma_rx_wr_ptr();
        uint32_t avail;
        if (wr >= rd_ptr)
            avail = wr - rd_ptr;
        else
            avail = (DMA_RX_BUF_SAMPLES - rd_ptr) + wr;

        if (avail < FM_CAPTURE_CHUNK) {
            usleep(500);
            continue;
        }

        double t0 = monotonic_sec();
        if (dma_rx_read(rd_ptr, FM_CAPTURE_CHUNK,
                        iq_real, iq_imag) <= 0) {
            fprintf(stderr, "fm_mode: dma_rx_read failed\n");
            continue;
        }
        double t1 = monotonic_sec();
        t_read += t1 - t0;

        rd_ptr += FM_CAPTURE_CHUNK;
        if (rd_ptr >= DMA_RX_BUF_SAMPLES)
            rd_ptr -= DMA_RX_BUF_SAMPLES;

        size_t n_audio = FM_AUDIO_OUT_MAX;
        uint64_t clips_before = demod.clip_count;
        uint64_t samples_before = demod.sample_count;
        if (fm_demod_process(&demod, iq_real, iq_imag, FM_CAPTURE_CHUNK,
                             audio, &n_audio) != 0) {
            fprintf(stderr, "fm_mode: demod process failed\n");
            fm_demod_reset(&demod);
            continue;
        }
        t_demod += monotonic_sec() - t1;
        adc_clip_add(demod.clip_count - clips_before,
                     demod.sample_count - samples_before);

        if (n_audio > 0) {
            fm_slot_t slot;
            slot.ts = monotonic_sec();
            size_t copy_n = n_audio < FM_AUDIO_FRAME_SIZE
                            ? n_audio : FM_AUDIO_FRAME_SIZE;
            memcpy(slot.frame.audio, audio, copy_n * sizeof(float));
            slot.frame.n_samples = (uint32_t)copy_n;
            slot.frame.audio_rate = fm_demod_audio_rate(&demod);
            slot.frame.rds = *rds_rx_state(&demod.rds);
            spsc_ring_push(g_fm_ringbuf, &slot);
        }

        /* throughput measurement: each iteration is 23.4 ms of IQ data */
        iter++;
        if (iter > 1 && (iter & 0xFF) == 0) {  /* every 256 iters, skip first */
            double now = monotonic_sec();
            double elapsed = now - t_last;
            double target = 256.0 * (double)FM_CAPTURE_CHUNK
                          / (double)g_radio.sample_rate_hz;
            double pct = target / elapsed * 100.0;
            printf("fm: %lu iters %.1fs elapsed (target %.1fs) = %.0f%% realtime"
                   " | read %.3f ms/chunk (%.1f%% of core)"
                   " | demod %.3f ms/chunk (%.1f%% of core)\n",
                   (unsigned long)iter, elapsed, target, pct,
                   t_read * 1000.0 / 256.0, t_read / target * 100.0,
                   t_demod * 1000.0 / 256.0, t_demod / target * 100.0);
            t_last = now;
            t_read = 0.0;
            t_demod = 0.0;
        }
    }

    free(iq_real);
    free(iq_imag);
    free(audio);

done:
    atomic_store(&g_producer_ready, false);
    return NULL;
}

int fm_mode_enter(void)
{
    if (atomic_load(&g_producer_running))
        return 0;

    /* The sample rate is settled by the caller (switch_producer) before
     * this runs, so a rate change never happens with the producer live. */

    /* Enable the AD9361 hardware channel filter.  This narrows the RF
     * bandwidth to ±120 kHz around the tuned frequency before the data
     * reaches DMA, reducing noise by ~3 dB.  The DMA rate stays 2.1 MSPS
     * (the driver adjusts the HB chain to compensate for FIR decimation).
     * Loaded after the rate is set and DMA is running — the driver handles
     * the clock reconfiguration transparently.  Non-fatal if it fails. */
    if (hal_ad9361_load_rx_fir(FM_RX_FIR_CONFIG) == 0) {
        if (hal_ad9361_set_rx_fir_en(1) != 0)
            fprintf(stderr, "fm_mode: FIR enable failed (non-fatal)\n");
    } else {
        fprintf(stderr, "fm_mode: FIR load failed (non-fatal)\n");
    }

    if (!g_fm_ringbuf)
        g_fm_ringbuf = spsc_ring_create(sizeof(fm_slot_t), RING_SLOTS_FM);
    if (!g_fm_ringbuf) {
        fprintf(stderr, "fm_mode: ringbuf create failed\n");
        hal_ad9361_set_rx_fir_en(0);
        return -1;
    }

    atomic_store(&g_producer_ready, false);
    atomic_store(&g_producer_running, true);
    if (pthread_create(&g_producer_thread, NULL, run, NULL) != 0) {
        atomic_store(&g_producer_running, false);
        fprintf(stderr, "fm_mode: thread create failed\n");
        hal_ad9361_set_rx_fir_en(0);
        return -1;
    }

    printf("mode: fm\n");
    return 0;
}

void fm_mode_exit(void)
{
    if (!atomic_load(&g_producer_running))
        return;
    atomic_store(&g_producer_running, false);
    pthread_join(g_producer_thread, NULL);

    /* Disable the channel filter before leaving FM mode — other modes
     * use the full bandwidth. */
    hal_ad9361_set_rx_fir_en(0);

    spsc_ring_destroy(g_fm_ringbuf);
    g_fm_ringbuf = NULL;
}

bool fm_mode_running(void)
{
    return atomic_load(&g_producer_running) && atomic_load(&g_producer_ready);
}

bool fm_mode_active(void)
{
    return atomic_load(&g_producer_running);
}

void drain_fm(const drain_ctx_t *ctx)
{
    static fm_slot_t *fm_slots;

    if (!g_fm_ringbuf) return;

    if (!fm_slots) {
        fm_slots = calloc(FM_POP_MAX, sizeof(fm_slot_t));
        if (!fm_slots) return;
    }

    size_t start_seq;
    int n = spsc_ring_pop_all(g_fm_ringbuf, fm_slots,
                              FM_POP_MAX, &start_seq);
    for (int i = 0; i < n; i++) {
        size_t frame_len = proto_build_fm(
            ctx->sendbuf, (uint32_t)(start_seq + i),
            fm_slots[i].ts,
            fm_slots[i].frame.audio_rate,
            fm_slots[i].frame.n_samples,
            fm_slots[i].frame.audio,
            &fm_slots[i].frame.rds);
        ctx->sink(ctx->sendbuf, frame_len, ctx->sink_user);
    }
}
