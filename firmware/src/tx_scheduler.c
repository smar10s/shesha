#include "tx_scheduler.h"
#include "dma_tx.h"
#include "thread_stats.h"

#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>

/*
 * Bound on how far the DDR ring is allowed to run ahead of the DAC.
 *
 * This is a latency budget, not a throughput knob.  The ring holds ~4 s at
 * 2.1 MSPS, and filling it would mean 4 s between a client sending audio
 * and hearing it — plus a 4 s drain on stop.  300 ms is comfortably more
 * than the worst observed feed jitter and short enough to feel immediate.
 */
#define TX_STREAM_HIGH_WATER_MS  300

/*
 * |IQ| = 1 by construction for FM and every other constant-envelope
 * modulation, so the DAC scale is known up front and auto-scaling is both
 * wasted work and wrong: it renormalises each chunk independently, which
 * steps the transmitted amplitude at chunk boundaries.
 */
#define TX_IQ_FULL_SCALE  2047.0f

/* The platform's trigger check occasionally sees a zero status 100 us
 * after the write even though the engine is idle.  A fresh load and
 * trigger normally succeeds, so retry a bounded number of times before
 * treating the start as failed. */
#define TX_START_ATTEMPTS               3
#define TX_START_RETRY_DELAY_US      2000

/* An endless repeat keeps going after a skipped iteration, but must not
 * spin forever on an engine that never starts. */
#define TX_REPEAT_MAX_CONSEC_FAILURES   3
#define TX_REPEAT_RETRY_DELAY_US     5000

typedef struct {
    float *real;
    float *imag;
    size_t n_samples;
    uint8_t flags;
    uint16_t repeat;
    uint32_t interval_us;
    pthread_t thread;
    pthread_mutex_t lock;
    pthread_cond_t arm_cond;
    /* `thread_live` spans create..join so a naturally finished thread is
     * still joined before its slot is reused.  `running` is the TX work
     * state and goes false when the thread exits. */
    bool thread_live;
    bool running;
    bool arm_done;
    int  arm_rc;
    atomic_bool stop_requested;
    /* Set by the one-shot thread when the DMA engine refuses to start.
     * Cleared at the entry of every new one-shot or stream start. */
    _Atomic bool start_failed;
    bool is_stream;
    tx_stream_producer stream_produce;
    tx_stream_cleanup stream_cleanup;
    void *stream_user;
    uint32_t stream_dac_rate;

    /* Telemetry, published by the TX thread and read by the poll loop. */
    _Atomic bool     st_active;
    _Atomic uint32_t st_dac_rate;
    _Atomic uint64_t st_fed;
    _Atomic uint64_t st_emitted;
    _Atomic uint64_t st_trigger_us;
    _Atomic uint64_t st_elapsed_us;
    _Atomic int32_t  st_depth;
    _Atomic uint32_t st_max_depth;
    _Atomic uint32_t st_high_water;
    _Atomic uint32_t st_underruns;
} tx_state_t;

static tx_state_t g_tx;

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)ts.tv_nsec / 1000ULL;
}

/* ------------------------------------------------------------------ */

static void wait_dma_done(void)
{
    while (!atomic_load(&g_tx.stop_requested) && !dma_tx_done())
        usleep(100);
}

static void wait_interval(uint32_t interval_us)
{
    if (interval_us == 0)
        return;
    uint32_t elapsed = 0;
    while (elapsed < interval_us && !atomic_load(&g_tx.stop_requested)) {
        uint32_t chunk = (interval_us - elapsed) < 10000
            ? (interval_us - elapsed) : 10000;
        usleep(chunk);
        elapsed += chunk;
    }
}

/* A failed start leaves no transfer running, so the thread must not wait
 * for one to finish.  Record the failure for the client only after every
 * attempt has failed, and let the thread unwind through the normal finish
 * path. */
static bool start_dma(bool cyclic)
{
    for (int attempt = 1; attempt <= TX_START_ATTEMPTS; attempt++) {
        if (dma_tx_start(g_tx.real, g_tx.imag, g_tx.n_samples, cyclic) == 0)
            return true;

        fprintf(stderr, "tx_scheduler: DMA start failed (attempt %d/%d)\n",
                attempt, TX_START_ATTEMPTS);
        /* dma_tx_start() may have written the control register before it
         * discovered the engine was not idle, so a nonzero return does not
         * prove the engine stopped.  Safe even when load() failed. */
        dma_tx_stop();
        if (attempt < TX_START_ATTEMPTS)
            usleep(TX_START_RETRY_DELAY_US);
    }

    atomic_store(&g_tx.start_failed, true);
    return false;
}

static void *tx_scheduler_one_shot_thread(void *arg)
{
    (void)arg;
    thread_stats_name_self("tx-oneshot");

    pthread_mutex_lock(&g_tx.lock);
    bool cyclic = (g_tx.flags & TX_FLAG_CYCLIC) != 0;
    uint16_t repeat = g_tx.repeat;
    uint32_t interval = g_tx.interval_us;
    pthread_mutex_unlock(&g_tx.lock);

    if (cyclic) {
        if (!start_dma(true))
            goto done;
        while (!atomic_load(&g_tx.stop_requested))
            usleep(10000);
        dma_tx_stop();
    } else if (repeat == 0 || repeat == 1) {
        if (!start_dma(false))
            goto done;
        wait_dma_done();
        if (atomic_load(&g_tx.stop_requested))
            dma_tx_stop();
    } else {
        bool forever = (repeat == TX_REPEAT_FOREVER);
        uint16_t remaining = forever ? 1 : repeat;
        unsigned consecutive_failures = 0;

        while (remaining > 0 && !atomic_load(&g_tx.stop_requested)) {
            if (!start_dma(false)) {
                consecutive_failures++;
                fprintf(stderr, "tx_scheduler: repeat iteration failed"
                        " (%u consecutive)\n", consecutive_failures);

                if (!forever) {
                    remaining--;
                    /* The burst is over; do not spend the interval
                     * waiting for another iteration that will not run. */
                    if (remaining == 0)
                        break;
                }

                if (atomic_load(&g_tx.stop_requested))
                    break;

                /* A dead engine cannot be retried for ever. */
                if (forever && consecutive_failures >=
                    TX_REPEAT_MAX_CONSEC_FAILURES)
                    break;

                /* One skipped iteration must not truncate the burst.  For
                 * an endless repeat, do not let a zero interval spin. */
                wait_interval(forever ? TX_REPEAT_RETRY_DELAY_US : interval);
                continue;
            }

            consecutive_failures = 0;
            if (!forever)
                remaining--;

            if (atomic_load(&g_tx.stop_requested))
                break;

            wait_dma_done();
            if (forever || remaining > 0) {
                if (!atomic_load(&g_tx.stop_requested))
                    wait_interval(interval);
            }
        }

        if (atomic_load(&g_tx.stop_requested))
            dma_tx_stop();
    }

done:
    pthread_mutex_lock(&g_tx.lock);
    free(g_tx.real);
    free(g_tx.imag);
    g_tx.real = NULL;
    g_tx.imag = NULL;
    g_tx.running = false;
    atomic_store(&g_tx.stop_requested, false);
    pthread_mutex_unlock(&g_tx.lock);

    return NULL;
}

/* ------------------------------------------------------------------ */

/* Snapshot the FPGA counters into the atomics the poll loop reads.  Called
 * only from the TX thread: dma_tx_stream_depth() advances a monotonic
 * accumulator, so two threads calling it would corrupt each other. */
static void stream_publish(uint64_t fed, uint64_t trigger_us,
                           uint32_t *underruns, uint32_t *max_depth)
{
    int32_t depth = dma_tx_stream_depth();
    if (depth < 0) {
        (*underruns)++;
        depth = 0;
    } else if ((uint32_t)depth > *max_depth) {
        *max_depth = (uint32_t)depth;
    }

    atomic_store_explicit(&g_tx.st_depth, depth, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_underruns, *underruns,
                          memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_max_depth, *max_depth,
                          memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_fed, fed, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_emitted, dma_tx_stream_emitted(),
                          memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_elapsed_us,
                          trigger_us ? now_us() - trigger_us : 0,
                          memory_order_relaxed);
}

static void *tx_scheduler_stream_thread(void *arg)
{
    (void)arg;
    thread_stats_name_self("tx-stream");

    pthread_mutex_lock(&g_tx.lock);
    tx_stream_producer produce = g_tx.stream_produce;
    tx_stream_cleanup cleanup = g_tx.stream_cleanup;
    void *user = g_tx.stream_user;
    uint32_t dac_rate = g_tx.stream_dac_rate;
    pthread_mutex_unlock(&g_tx.lock);

    /*
     * Arm without triggering.  Triggering an empty ring is a trap: the
     * drain FSM starts immediately and runs ahead of the write cursor, so
     * the ARM begins the race already behind and every feed afterwards is
     * chasing a read pointer in front of it.  Same two-phase discipline
     * dma_tx_load()/dma_tx_trigger() uses for one-shot TX.
     *
     * The result is published before prefill, which can wait indefinitely
     * for the host.  CMD_TX must be able to tell the client a start
     * failed without waiting for data that will never come.
     */
    int arm_rc = dma_tx_stream_arm();

    pthread_mutex_lock(&g_tx.lock);
    g_tx.arm_rc = arm_rc;
    g_tx.arm_done = true;
    pthread_cond_signal(&g_tx.arm_cond);
    pthread_mutex_unlock(&g_tx.lock);

    if (arm_rc != 0) {
        if (cleanup) cleanup(user);
        goto done;
    }

    uint32_t high_water = (uint32_t)((uint64_t)TX_STREAM_HIGH_WATER_MS
                                     * dac_rate / 1000ULL);
    if (high_water < DMA_TX_STREAM_CHUNK * 4)
        high_water = DMA_TX_STREAM_CHUNK * 4;
    if (high_water > DMA_TX_MAX_SAMPLES / 2)
        high_water = DMA_TX_MAX_SAMPLES / 2;

    atomic_store_explicit(&g_tx.st_high_water, high_water,
                          memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_dac_rate, dac_rate, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_active, true, memory_order_relaxed);

    {
        float chunk_real[DMA_TX_STREAM_CHUNK];
        float chunk_imag[DMA_TX_STREAM_CHUNK];

        uint64_t fed = 0;
        uint32_t underruns = 0, max_depth = 0;
        bool eos = false;

        /* ---- prefill ----
         * Fill to the latency bound before the engine starts, so the drain
         * FSM never begins ahead of the write cursor. */
        while (fed + DMA_TX_STREAM_CHUNK <= high_water) {
            if (atomic_load(&g_tx.stop_requested))
                goto stop_dma;
            ssize_t n = produce(chunk_real, chunk_imag,
                                DMA_TX_STREAM_CHUNK, user);
            if (n < 0) {
                /* Producer has nothing yet but the stream is still open.
                 * Wait for the host; do not fabricate samples. */
                usleep(500);
                continue;
            }
            if (n == 0) {
                eos = true;
                break;
            }
            if (dma_tx_stream_feed_fixed(chunk_real, chunk_imag, (size_t)n,
                                         TX_IQ_FULL_SCALE) != 0)
                goto stop_dma;
            fed += (uint64_t)n;
        }

        if (dma_tx_stream_trigger() != 0)
            goto stop_dma;

        uint64_t trigger_us = now_us();
        stream_publish(fed, trigger_us, &underruns, &max_depth);

        /* ---- steady state ---- */
        while (!eos && !atomic_load(&g_tx.stop_requested)) {
            stream_publish(fed, trigger_us, &underruns, &max_depth);

            /*
             * Flow control on signed depth, never on a "space free"
             * figure.  A position pair is ambiguous between "ARM ahead by
             * D" and "FPGA ahead by BUF-D", and the old
             * dma_tx_stream_available() reported a small value in both
             * cases — so a feeder waiting on it throttles itself into a
             * permanent underrun it can never exit.
             */
            int32_t depth = atomic_load_explicit(&g_tx.st_depth,
                                                 memory_order_relaxed);
            if ((uint32_t)depth + DMA_TX_STREAM_CHUNK > high_water) {
                usleep(200);
                continue;
            }

            ssize_t n = produce(chunk_real, chunk_imag,
                                DMA_TX_STREAM_CHUNK, user);
            if (n < 0) {
                /* Starved: the host has not caught up.  Wait rather than
                 * pad a finite vector with silence. */
                usleep(500);
                continue;
            }
            if (n == 0)
                break;

            if (dma_tx_stream_feed_fixed(chunk_real, chunk_imag, (size_t)n,
                                         TX_IQ_FULL_SCALE) != 0)
                break;
            fed += (uint64_t)n;
        }

        stream_publish(fed, trigger_us, &underruns, &max_depth);
    }

stop_dma:
    dma_tx_stream_drain(dac_rate);
    dma_tx_stream_stop();
    atomic_store_explicit(&g_tx.st_active, false, memory_order_relaxed);

    if (cleanup)
        cleanup(user);

done:
    pthread_mutex_lock(&g_tx.lock);
    g_tx.running = false;
    atomic_store(&g_tx.stop_requested, false);
    g_tx.is_stream = false;
    g_tx.stream_produce = NULL;
    g_tx.stream_cleanup = NULL;
    g_tx.stream_user = NULL;
    pthread_mutex_unlock(&g_tx.lock);

    return NULL;
}

void tx_scheduler_stream_stats(tx_stream_stats_t *out)
{
    out->active     = atomic_load_explicit(&g_tx.st_active,
                                           memory_order_relaxed);
    out->dac_rate   = atomic_load_explicit(&g_tx.st_dac_rate,
                                           memory_order_relaxed);
    out->fed        = atomic_load_explicit(&g_tx.st_fed,
                                           memory_order_relaxed);
    out->emitted    = atomic_load_explicit(&g_tx.st_emitted,
                                           memory_order_relaxed);
    out->elapsed_us = atomic_load_explicit(&g_tx.st_elapsed_us,
                                           memory_order_relaxed);
    out->depth      = atomic_load_explicit(&g_tx.st_depth,
                                           memory_order_relaxed);
    out->max_depth  = atomic_load_explicit(&g_tx.st_max_depth,
                                           memory_order_relaxed);
    out->high_water = atomic_load_explicit(&g_tx.st_high_water,
                                           memory_order_relaxed);
    out->underruns  = atomic_load_explicit(&g_tx.st_underruns,
                                           memory_order_relaxed);
}

/* ------------------------------------------------------------------ */

int tx_scheduler_init(void)
{
    memset(&g_tx, 0, sizeof(g_tx));
    atomic_init(&g_tx.stop_requested, false);
    atomic_init(&g_tx.start_failed, false);
    if (pthread_mutex_init(&g_tx.lock, NULL) != 0) {
        return -1;
    }
    if (pthread_cond_init(&g_tx.arm_cond, NULL) != 0) {
        pthread_mutex_destroy(&g_tx.lock);
        return -1;
    }
    return 0;
}

void tx_scheduler_cleanup(void)
{
    tx_scheduler_stop();
    pthread_cond_destroy(&g_tx.arm_cond);
    pthread_mutex_destroy(&g_tx.lock);
}

/* Clear published transport telemetry between transmissions.  The client
 * uses these counters to place the visualiser.  Without this, a new stream
 * reports the previous stream's fed/emitted during prefill, before the DMA
 * has triggered. */
static void reset_telemetry_locked(void)
{
    atomic_store_explicit(&g_tx.st_active, false, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_dac_rate, 0, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_fed, 0, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_emitted, 0, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_trigger_us, 0, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_elapsed_us, 0, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_depth, 0, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_max_depth, 0, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_high_water, 0, memory_order_relaxed);
    atomic_store_explicit(&g_tx.st_underruns, 0, memory_order_relaxed);
}

int tx_scheduler_start(const float *real, const float *imag,
                       size_t n_samples, uint8_t flags,
                       uint16_t repeat, uint32_t interval_us)
{
    /* Clear before any early return, so a rejected start never reports the
     * previous transmission's failure. */
    atomic_store(&g_tx.start_failed, false);

    if (!real || !imag || n_samples == 0)
        return -1;

    float *r = malloc(n_samples * sizeof(float));
    float *im = malloc(n_samples * sizeof(float));
    if (!r || !im) {
        free(r); free(im);
        return -1;
    }
    memcpy(r, real, n_samples * sizeof(float));
    memcpy(im, imag, n_samples * sizeof(float));

    tx_scheduler_stop();

    pthread_mutex_lock(&g_tx.lock);
    g_tx.real = r;
    g_tx.imag = im;
    g_tx.n_samples = n_samples;
    g_tx.flags = flags;
    g_tx.repeat = repeat;
    g_tx.interval_us = interval_us;
    g_tx.running = true;
    g_tx.thread_live = true;
    atomic_store(&g_tx.stop_requested, false);
    atomic_store(&g_tx.start_failed, false);
    reset_telemetry_locked();
    pthread_mutex_unlock(&g_tx.lock);

    if (pthread_create(&g_tx.thread, NULL, tx_scheduler_one_shot_thread, NULL) != 0) {
        pthread_mutex_lock(&g_tx.lock);
        free(g_tx.real); g_tx.real = NULL;
        free(g_tx.imag); g_tx.imag = NULL;
        g_tx.running = false;
        g_tx.thread_live = false;
        pthread_mutex_unlock(&g_tx.lock);
        return -1;
    }

    return 0;
}

int tx_scheduler_start_stream(tx_stream_producer produce,
                              tx_stream_cleanup cleanup,
                              void *user, uint32_t dac_rate)
{
    /* Clear before any early return, so a rejected start never reports the
     * previous transmission's failure. */
    atomic_store(&g_tx.start_failed, false);

    if (!produce || dac_rate == 0)
        return -1;

    tx_scheduler_stop();

    pthread_mutex_lock(&g_tx.lock);
    atomic_store(&g_tx.start_failed, false);
    g_tx.is_stream = true;
    g_tx.stream_produce = produce;
    g_tx.stream_cleanup = cleanup;
    g_tx.stream_user = user;
    g_tx.stream_dac_rate = dac_rate;
    g_tx.running = true;
    g_tx.thread_live = true;
    g_tx.arm_done = false;
    g_tx.arm_rc = -1;
    atomic_store(&g_tx.stop_requested, false);
    reset_telemetry_locked();
    pthread_mutex_unlock(&g_tx.lock);

    if (pthread_create(&g_tx.thread, NULL,
                       tx_scheduler_stream_thread, NULL) != 0) {
        pthread_mutex_lock(&g_tx.lock);
        g_tx.running = false;
        g_tx.thread_live = false;
        g_tx.is_stream = false;
        if (cleanup) cleanup(user);
        pthread_mutex_unlock(&g_tx.lock);
        return -1;
    }

    /* Do not acknowledge the start until the DMA arm result is known.
     * Prefill may block waiting for the host, so the arm result must be
     * published before that loop (see tx_scheduler_stream_thread). */
    pthread_mutex_lock(&g_tx.lock);
    while (!g_tx.arm_done)
        pthread_cond_wait(&g_tx.arm_cond, &g_tx.lock);
    int arm_rc = g_tx.arm_rc;
    pthread_mutex_unlock(&g_tx.lock);

    if (arm_rc != 0) {
        tx_scheduler_stop();
        return -1;
    }

    return 0;
}

void tx_scheduler_request_stop(void)
{
    pthread_mutex_lock(&g_tx.lock);
    if (g_tx.thread_live)
        atomic_store(&g_tx.stop_requested, true);
    pthread_mutex_unlock(&g_tx.lock);
}

void tx_scheduler_join(void)
{
    pthread_mutex_lock(&g_tx.lock);
    if (!g_tx.thread_live) {
        pthread_mutex_unlock(&g_tx.lock);
        return;
    }
    pthread_t thread = g_tx.thread;
    pthread_mutex_unlock(&g_tx.lock);

    /* Join even when `running` is already false: a naturally finished
     * thread still owns its stack, and reusing g_tx.thread without joining
     * leaks it. */
    pthread_join(thread, NULL);

    pthread_mutex_lock(&g_tx.lock);
    g_tx.thread_live = false;
    g_tx.running = false;
    pthread_mutex_unlock(&g_tx.lock);
}

void tx_scheduler_stop(void)
{
    tx_scheduler_request_stop();
    tx_scheduler_join();
}

bool tx_scheduler_busy(void)
{
    pthread_mutex_lock(&g_tx.lock);
    bool busy = g_tx.running;
    pthread_mutex_unlock(&g_tx.lock);
    return busy;
}

bool tx_scheduler_start_failed(void)
{
    return atomic_load(&g_tx.start_failed);
}
