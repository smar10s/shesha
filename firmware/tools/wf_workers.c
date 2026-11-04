// SPDX-License-Identifier: MIT
/*
 * wf_workers — is a second producer core worth it, and at which sample rates?
 *
 * wf_scale answered a narrower question: does the per-frame compute parallelise
 * at all?  It does, 1.92-2.02x.  But it measured peak throughput with no
 * latency cap and no notion of coverage, so it cannot say whether a second
 * worker buys anything at a given (fs, N) — and below fs_max it buys nothing,
 * because one core already transforms every sample.
 *
 * This tool measures the number the decision actually needs: coverage and duty
 * for 1 vs 2 workers at the same operating point.  Coverage is the benefit,
 * duty is the cost, and the interesting cell is where coverage stops rising
 * while duty keeps climbing.
 *
 *   killall shesha-daemon
 *   /tmp/wf_workers [seconds-per-point]
 *
 * It owns the AD9361 and the RX DMA, so the daemon must be stopped.
 *
 * ---- What it prototypes ----
 *
 * Two workers cannot each keep a private read pointer the way waterfall.c
 * does: both would latch the same WR_PTR and transform the same samples, for
 * twice the CPU and no extra coverage.  So the read position becomes shared --
 * one atomic that hands out disjoint N-sample blocks -- and that is the part
 * of the production design this tool exists to exercise.
 *
 * The hard case is the latency cap.  A single producer that falls behind just
 * moves its own pointer forward (waterfall.c).  With a shared claim, a worker
 * that finds its block stale must abandon it and drag the shared claim forward
 * too, or the other worker keeps grinding through old samples.  That skip is a
 * CAS, not a store, because both workers can notice staleness at once and only
 * the furthest jump should win.
 *
 * Everything else is the production pipeline: the same fused convert+window,
 * the same split FFT, the same magnitude, the same WF_MAX_LAG_FRAMES.  Frames
 * are computed and dropped; this measures compute and coverage, not delivery.
 */
#define _GNU_SOURCE
#include "hal.h"
#include "dma_rx.h"
#include "radio.h"
#include "window.h"
#include "wf_convert.h"
#include "wf_mag.h"
#include "waterfall.h"   /* WF_MAX_LAG_FRAMES: match the production producer */

#include "wf_fft.h"

#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_WORKERS 2

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* Per-thread CPU time, so a worker that mostly waits can be told apart from
 * one that mostly computes.  That distinction is the whole question below
 * fs_max, where the second worker should be idle rather than useful. */
static double thread_cpu_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* State shared by every worker at one measurement point. */
typedef struct {
    _Atomic uint64_t claim;      /* next sample offset to hand out */
    _Atomic bool     running;
    uint32_t         wr0;        /* WR_PTR when the point started */
    size_t           n;
    const float     *win;
    float            norm;
    uint32_t         max_lag;
} shared_t;

typedef struct {
    shared_t          *sh;
    int                cpu;
    wf_fft_plan      *plan;      /* built by main: FFTW's planner is not
                                  * thread-safe, so no worker makes its own */
    uint64_t           frames;
    uint64_t           claims_abandoned;
    double             cpu_time;
    int                rc;
} worker_t;

/*
 * Drag the shared claim forward to the newest whole frame.
 *
 * Only an advance is published, and only by CAS: both workers can notice
 * staleness in the same instant, and the furthest jump must win.  A plain
 * store would let a worker that had already skipped get pulled backwards by a
 * slower one, re-reading samples it had deliberately given up on.
 */
static void claim_skip_forward(shared_t *sh, uint64_t produced)
{
    size_t n = sh->n;
    if (produced < n)
        return;
    uint64_t newbase = produced - n;   /* start of the newest whole frame */

    uint64_t expect = atomic_load(&sh->claim);
    while (expect < newbase) {
        if (atomic_compare_exchange_weak(&sh->claim, &expect, newbase))
            return;
        /* expect was refreshed by the failed exchange; retry or fall out. */
    }
}

static void *worker(void *arg)
{
    worker_t *w = arg;
    shared_t *sh = w->sh;
    size_t n = sh->n;

    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(w->cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0)
        w->rc = -1;

    float *ir   = malloc(n * sizeof(float));
    float *ii   = malloc(n * sizeof(float));
    float *fr   = malloc(n * sizeof(float));
    float *fi   = malloc(n * sizeof(float));
    float *pw   = malloc(n * sizeof(float));
    if (!ir || !ii || !fr || !fi || !pw) {
        w->rc = -1;
        goto done;
    }

    /* ADC progress is tracked per worker from the same starting WR_PTR.  The
     * pointer is monotonic modulo the ring, so independent delta sums agree
     * without any shared progress state (the wf_scale argument). */
    uint32_t prev = sh->wr0;
    uint64_t produced = 0;
    double c0 = thread_cpu_s();

    while (atomic_load(&sh->running)) {
        uint64_t off = atomic_fetch_add(&sh->claim, n);
        uint64_t target = off + n;
        bool stale = false;

        /* Wait for the ADC to produce the claimed block, unless it has
         * already run so far ahead that the block is stale. */
        while (atomic_load(&sh->running)) {
            uint32_t cur = dma_rx_wr_ptr();
            produced += (cur >= prev) ? (cur - prev)
                                      : (DMA_RX_BUF_SAMPLES - prev) + cur;
            prev = cur;

            if (produced > target + sh->max_lag) {
                stale = true;
                break;
            }
            if (produced >= target)
                break;
            usleep(50);
        }

        if (stale) {
            /* Abandon the block and pull everyone forward, then re-claim. */
            claim_skip_forward(sh, produced);
            w->claims_abandoned++;
            continue;
        }
        if (produced < target)
            break;   /* stopping mid-wait: drop the claimed block */

        uint32_t ring = (uint32_t)((sh->wr0 + off) & (DMA_RX_BUF_SAMPLES - 1));

        __sync_synchronize();
        const volatile uint32_t *rx = hal_ddr_rx_buf();
        if (ring + (uint32_t)n <= DMA_RX_BUF_SAMPLES) {
            wf_convert_windowed(&rx[ring], n, sh->win, ir, ii, NULL);
        } else {
            size_t head = DMA_RX_BUF_SAMPLES - ring;
            wf_convert_windowed(&rx[ring], head, sh->win, ir, ii, NULL);
            wf_convert_windowed(&rx[0], n - head, sh->win + head,
                                &ir[head], &ii[head], NULL);
        }
        wf_fft_forward_split(w->plan, ir, ii, fr, fi);
        wf_magnitude_power(fr, fi, n, sh->norm, pw);

        w->frames++;
    }
    w->cpu_time = thread_cpu_s() - c0;

done:
    free(ir); free(ii); free(fr); free(fi); free(pw);
    return NULL;
}

typedef struct {
    uint64_t frames;
    uint64_t abandoned;
    uint64_t produced;
    double   wall;
    double   cpu;                   /* summed across workers */
    double   cpu_each[MAX_WORKERS];
} point_t;

static int run_point(size_t n, int workers, double seconds,
                     const float *win, float norm,
                     wf_fft_plan **plans, point_t *out)
{
    memset(out, 0, sizeof(*out));

    shared_t sh;
    atomic_store(&sh.claim, 0);
    atomic_store(&sh.running, true);
    sh.n = n;
    sh.win = win;
    sh.norm = norm;
    sh.max_lag = (uint32_t)n * WF_MAX_LAG_FRAMES;
    if (sh.max_lag > DMA_RX_BUF_SAMPLES / 2)
        sh.max_lag = DMA_RX_BUF_SAMPLES / 2;

    uint32_t prev = dma_rx_wr_ptr();
    sh.wr0 = prev;

    worker_t w[MAX_WORKERS];
    memset(w, 0, sizeof(w));
    for (int i = 0; i < workers; i++) {
        w[i].sh = &sh;
        w[i].cpu = i;
        w[i].plan = plans[i];
    }

    pthread_t th[MAX_WORKERS];
    double t0 = now_s();
    for (int i = 0; i < workers; i++) {
        if (pthread_create(&th[i], NULL, worker, &w[i]) != 0)
            return -1;
    }

    /* Track ADC progress from the main thread while the workers run: WR_PTR
     * holds 25 bits of sample index, so it wraps every 1.7 s at 20 MSPS and a
     * single before/after read would undercount. */
    uint64_t produced = 0;
    while (now_s() - t0 < seconds) {
        usleep(20000);
        uint32_t cur = dma_rx_wr_ptr();
        produced += (cur >= prev) ? (cur - prev)
                                  : (DMA_RX_BUF_SAMPLES - prev) + cur;
        prev = cur;
    }
    atomic_store(&sh.running, false);
    for (int i = 0; i < workers; i++)
        pthread_join(th[i], NULL);
    out->wall = now_s() - t0;

    uint32_t cur = dma_rx_wr_ptr();
    produced += (cur >= prev) ? (cur - prev)
                              : (DMA_RX_BUF_SAMPLES - prev) + cur;
    out->produced = produced;

    for (int i = 0; i < workers; i++) {
        if (w[i].rc == -1)
            return -1;
        out->frames    += w[i].frames;
        out->abandoned += w[i].claims_abandoned;
        out->cpu       += w[i].cpu_time;
        out->cpu_each[i] = w[i].cpu_time;
    }
    return 0;
}

static const size_t SIZES[] = { 1024, 4096, 8192 };
#define NSIZES (sizeof(SIZES) / sizeof(SIZES[0]))

/* Below fs_max (2.1), at it (5), and above it (10, 20).  fs_max at N=4096 is
 * 5.34 MSPS on one core, so these straddle the point where a second worker
 * could start to matter. */
static const uint32_t RATES[] = { 2100000, 5000000, 10000000, 20000000 };
#define NRATES (sizeof(RATES) / sizeof(RATES[0]))

int main(int argc, char **argv)
{
    double seconds = (argc > 1) ? strtod(argv[1], NULL) : 2.0;
    if (seconds <= 0.0)
        seconds = 2.0;

    radio_state_t radio;
    if (radio_init(&radio) != 0) {
        fprintf(stderr, "wf_workers: radio_init failed "
                        "(is the daemon stopped and the Pluto reachable?)\n");
        return 1;
    }

    printf("producer worker scaling: coverage and duty, Cortex-A9, "
           "%.1f s/point\n", seconds);
    printf("coverage = frames*N / samples the ADC produced; duty is CPU as a "
           "share of ONE core\n");
    printf("workers claim disjoint blocks from one atomic, lag capped at "
           "%d frames\n\n", WF_MAX_LAG_FRAMES);

    printf("%9s %6s | %7s %7s %7s | %7s %7s %7s %7s | %7s %7s\n",
           "fs(MSPS)", "N",
           "cover", "duty", "fps",
           "cover", "duty", "w0", "w1",
           "cover", "duty");
    printf("%9s %6s | %7s %7s %7s | %7s %7s %7s %7s | %7s %7s\n",
           "", "", "1 wkr", "1 wkr", "1 wkr",
           "2 wkr", "2 wkr", "duty", "duty",
           "gain", "cost");

    for (size_t r = 0; r < NRATES; r++) {
        uint32_t actual = 0;
        if (radio_set_sample_rate(&radio, RATES[r], &actual) != 0) {
            fprintf(stderr, "wf_workers: could not set %u Hz\n", RATES[r]);
            continue;
        }
        /* The AD9361 needs the DMA restarted after a rate change (styx
         * discipline), and the analog path needs a moment to settle. */
        dma_rx_stop();
        usleep(50000);
        if (dma_rx_start() != 0) {
            fprintf(stderr, "wf_workers: dma_rx_start failed at %u Hz\n",
                    actual);
            continue;
        }
        usleep(100000);

        for (size_t s = 0; s < NSIZES; s++) {
            size_t n = SIZES[s];
            float *win = malloc(n * sizeof(float));
            if (!win) return 1;
            float cg = window_generate(WINDOW_HANN, n, win);
            float norm = 1.0f / ((float)n * cg);

            wf_fft_plan *plans[MAX_WORKERS] = { NULL, NULL };
            for (int i = 0; i < MAX_WORKERS; i++) {
                plans[i] = wf_fft_plan_create_n(n);
                if (!plans[i]) {
                    fprintf(stderr, "wf_workers: fft plan failed at N=%zu\n", n);
                    return 1;
                }
            }

            point_t one, two;
            if (run_point(n, 1, seconds, win, norm, plans, &one) != 0 ||
                run_point(n, 2, seconds, win, norm, plans, &two) != 0) {
                fprintf(stderr, "wf_workers: run failed at N=%zu\n", n);
                return 1;
            }

            double c1 = one.produced
                ? (double)one.frames * n / (double)one.produced : 0.0;
            double c2 = two.produced
                ? (double)two.frames * n / (double)two.produced : 0.0;
            double d1 = one.cpu / one.wall * 100.0;
            double d2 = two.cpu / two.wall * 100.0;

            printf("%9.1f %6zu | %6.1f%% %6.1f%% %7.0f | "
                   "%6.1f%% %6.1f%% %6.1f%% %6.1f%% | %6.2fx %6.2fx\n",
                   actual / 1e6, n,
                   c1 * 100.0, d1, one.frames / one.wall,
                   c2 * 100.0, d2,
                   two.cpu_each[0] / two.wall * 100.0,
                   two.cpu_each[1] / two.wall * 100.0,
                   c1 > 0 ? c2 / c1 : 0.0,
                   d1 > 0 ? d2 / d1 : 0.0);
            fflush(stdout);

            for (int i = 0; i < MAX_WORKERS; i++)
                wf_fft_plan_destroy(plans[i]);
            free(win);
        }
    }

    printf("\n  'cover gain' is what the second worker buys and 'duty cost' is\n");
    printf("  what it costs.  A gain near 1.00x means the second core is pure\n");
    printf("  tax: one core already transformed every sample, so the extra\n");
    printf("  worker only waits.  The per-worker duty columns show that\n");
    printf("  directly -- a waiting worker still burns the WR_PTR poll.\n");
    printf("  Coverage cannot exceed 100%%: hop == N, so there is no overlap.\n");
    return 0;
}
