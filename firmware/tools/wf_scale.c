// SPDX-License-Identifier: MIT
/*
 * wf_scale — does the waterfall producer's per-frame compute scale across the
 * two Cortex-A9 cores?
 *
 * wf_bench measures C(N) on one core; this runs the same production pipeline
 * (fused convert+window, split FFT, magnitude) on `cores` worker threads,
 * pinned one per CPU, each consuming a disjoint block of the live DMA stream.
 * If the compute is parallel, two workers should roughly double the aggregate
 * sample rate.  If DDR bandwidth or a shared unit is the ceiling, they will
 * not — and that, not the single-core C(N), is what decides whether a
 * second-core redesign of the daemon is worth it.
 *
 * It owns the AD9361 and RX DMA, so the daemon must be stopped and it needs
 * the Pluto and root:
 *
 *   killall shesha-daemon
 *   /tmp/wf_scale [seconds-per-run]
 *
 * The workers claim sample blocks from a shared atomic counter and wait for
 * the ADC to have produced them (WR_PTR polling, same usleep(50) as the
 * producer).  Both workers track "produced since start" independently from
 * the same initial WR_PTR, which is monotonic modulo the 2^25-sample ring, so
 * no shared progress state is needed.  No frame is sent anywhere: this is a
 * compute-throughput measurement, not the daemon.
 */
#define _GNU_SOURCE
#include "hal.h"
#include "dma_rx.h"
#include "radio.h"
#include "window.h"
#include "wf_convert.h"
#include "wf_mag.h"

#include "wf_fft.h"

#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

typedef struct {
    size_t n;
    const float *win;
    float norm;
    int cpu;
    wf_fft_plan *plan;    /* created by main: FFTW planning is not
                                 * thread-safe, so no worker makes its own */
    uint32_t wr0;               /* WR_PTR snapshot when the run started */
    _Atomic uint64_t *claim;    /* next sample offset to hand out */
    _Atomic bool *running;
    uint64_t frames;
    double elapsed;
    int rc;
} worker_t;

static void *worker(void *arg)
{
    worker_t *w = arg;

    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(w->cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0)
        w->rc = -1;

    size_t n = w->n;
    wf_fft_plan *plan = w->plan;
    float *ir   = malloc(n * sizeof(float));
    float *ii   = malloc(n * sizeof(float));
    float *fr   = malloc(n * sizeof(float));
    float *fi   = malloc(n * sizeof(float));
    float *bins = malloc(n * sizeof(float));
    if (!ir || !ii || !fr || !fi || !bins) {
        w->rc = -1;
        goto done;
    }

    uint32_t prev = w->wr0;
    uint64_t produced = 0;

    double t0 = now_s();
    while (atomic_load(w->running)) {
        /* Claim the next disjoint n-sample block (monotonic offset). */
        uint64_t off = atomic_fetch_add(w->claim, n);
        uint64_t target = off + n;

        /* Wait for the ADC to have produced it. */
        while (produced < target && atomic_load(w->running)) {
            uint32_t cur = dma_rx_wr_ptr();
            produced += (cur >= prev) ? (cur - prev)
                                      : (DMA_RX_BUF_SAMPLES - prev) + cur;
            prev = cur;
            usleep(50);
        }
        if (produced < target)
            break;   /* stopping mid-wait: drop the claimed block */

        uint32_t ring = (w->wr0 + (uint32_t)off) & (DMA_RX_BUF_SAMPLES - 1);

        __sync_synchronize();
        const volatile uint32_t *rx = hal_ddr_rx_buf();
        if (ring + (uint32_t)n <= DMA_RX_BUF_SAMPLES) {
            wf_convert_windowed(&rx[ring], n, w->win, ir, ii, NULL);
        } else {
            size_t first = DMA_RX_BUF_SAMPLES - ring;
            size_t second = n - first;
            wf_convert_windowed(&rx[ring], first, w->win, ir, ii, NULL);
            wf_convert_windowed(&rx[0], second, w->win + first,
                                &ir[first], &ii[first], NULL);
        }

        wf_fft_forward_split(plan, ir, ii, fr, fi);
        wf_magnitude_db(fr, fi, n, w->norm, bins);

        w->frames++;
    }
    w->elapsed = now_s() - t0;

done:
    free(ir); free(ii); free(fr); free(fi); free(bins);
    return NULL;
}

static const size_t SIZES[] = { 256, 512, 1024, 2048, 4096, 8192 };
#define NSIZES (sizeof(SIZES) / sizeof(SIZES[0]))

int main(int argc, char **argv)
{
    double seconds = (argc > 1) ? strtod(argv[1], NULL) : 2.0;
    if (seconds <= 0.0)
        seconds = 2.0;

    radio_state_t radio;
    if (radio_init(&radio) != 0) {
        fprintf(stderr, "wf_scale: radio_init failed "
                        "(is the daemon stopped and the Pluto reachable?)\n");
        return 1;
    }

    printf("waterfall producer scaling, Cortex-A9, %.1f s/run, Hann window\n",
           seconds);
    printf("pipeline: fused convert+window + split FFT + magnitude, "
           "disjoint DMA blocks\n\n");
    printf("    N  cores  frames  frames/s    Msps  speedup\n");

    for (size_t s = 0; s < NSIZES; s++) {
        size_t n = SIZES[s];
        double msps_1 = 0.0;

        for (int cores = 1; cores <= 2; cores++) {
            float *win = malloc(n * sizeof(float));
            if (!win) return 1;
            float cg = window_generate(WINDOW_HANN, n, win);
            float norm = 1.0f / ((float)n * cg);

            /* Plans are built here, single-threaded: FFTW's planner is not
             * safe to call from two threads at once. */
            wf_fft_plan *plans[2] = { NULL, NULL };
            for (int i = 0; i < cores; i++) {
                plans[i] = wf_fft_plan_create_n(n);
                if (!plans[i]) {
                    fprintf(stderr, "wf_scale: fft plan failed at N=%zu\n", n);
                    return 1;
                }
            }

            worker_t w[2];
            _Atomic uint64_t claim = 0;
            _Atomic bool running = true;
            uint32_t wr0 = dma_rx_wr_ptr();

            for (int i = 0; i < cores; i++) {
                w[i] = (worker_t){
                    .n = n, .win = win, .norm = norm, .cpu = i, .plan = plans[i],
                    .wr0 = wr0, .claim = &claim, .running = &running,
                };
            }

            pthread_t th[2];
            double t0 = now_s();
            for (int i = 0; i < cores; i++)
                pthread_create(&th[i], NULL, worker, &w[i]);
            usleep((useconds_t)(seconds * 1e6));
            atomic_store(&running, false);
            for (int i = 0; i < cores; i++)
                pthread_join(th[i], NULL);
            double wall = now_s() - t0;

            for (int i = 0; i < cores; i++)
                wf_fft_plan_destroy(plans[i]);

            uint64_t frames = 0;
            for (int i = 0; i < cores; i++) {
                frames += w[i].frames;
                if (w[i].rc == -1) {
                    fprintf(stderr, "wf_scale: worker %d failed at N=%zu\n", i, n);
                    return 1;
                }
            }
            double fps = frames / wall;
            double msps = fps * (double)n / 1e6;
            printf("  %5zu  %5d  %6llu  %8.0f  %6.2f", n, cores,
                   (unsigned long long)frames, fps, msps);
            if (cores == 1) {
                msps_1 = msps;
                printf("      -");
            } else {
                printf("   %5.2fx", msps_1 > 0 ? msps / msps_1 : 0.0);
            }
            printf("\n");
            fflush(stdout);

            free(win);
        }
    }

    printf("\n  Msps = aggregate samples consumed per second = the sample rate\n");
    printf("  this many cores could keep up with (equivalently N*C_aggregate).\n");
    printf("  If 2-core Msps ~= 2x the 1-core Msps at a given N, the compute\n");
    printf("  parallelises; a smaller ratio means a shared bottleneck (DDR\n");
    printf("  read bandwidth, memory controller, or scheduler).\n");
    return 0;
}
