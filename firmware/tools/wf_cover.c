// SPDX-License-Identifier: MIT
/*
 * wf_cover — measure the waterfall's spectral coverage, and what it costs.
 *
 * `C(N)` (wf_bench) says how long a frame takes and `duty` says what share of
 * a core that is.  Neither says what you get for it.  The missing number is
 * coverage: the fraction of the incoming sample stream that actually passes
 * through an FFT.  The producer is un-paced and re-latches WR_PTR after every
 * frame, so at high sample rates most samples are never transformed at all --
 * the gap between frames is dead air, and nothing in the daemon reports it.
 *
 * Coverage is exactly measurable without RF, by construction: the producer
 * consumes exactly N samples per frame, and the ADC produces exactly fs
 * samples per second.  Reading WR_PTR before and after a run gives the
 * denominator (samples produced) and counting frames gives the numerator
 * (samples transformed, N each).  No signal, no detector, no threshold -- the
 * ratio is arithmetic on two counters.
 *
 * coverage = frames * N / samples_produced
 *
 * Run it per (fs, N), optionally paced, to get the operating envelope:
 *
 *   killall shesha-daemon
 *   /tmp/wf_cover [seconds-per-point]
 *
 * It owns the AD9361 and the RX DMA, so the daemon must be stopped.
 *
 * Coverage > 100% would mean overlapping FFTs (hop < N); the producer has no
 * overlap, so it is bounded by 100% and reached only when compute outruns the
 * ADC (fs < fs_max).  The paced columns show what a demand-rate producer
 * would cover instead, which is the pacing trade in one table.
 */
#define _GNU_SOURCE
#include "hal.h"
#include "dma_rx.h"
#include "radio.h"
#include "window.h"
#include "wf_convert.h"
#include "wf_mag.h"
#include "waterfall.h"   /* WF_MAX_LAG_FRAMES: match the producer */

#include "wf_fft.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static double cpu_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* One measurement point.  `pace_fps` of 0 means free-running (the production
 * producer); non-zero paces frames to that rate, which is what a
 * pace-to-demand producer would do. */
typedef struct {
    uint64_t frames;
    uint64_t skipped;      /* samples jumped over after falling behind */
    uint64_t produced;     /* samples the ADC wrote during the run */
    double   wall;
    double   cpu;
} point_t;

static int run_point(size_t n, double seconds, double pace_fps,
                     const float *win, float norm,
                     wf_fft_plan *plan, point_t *out)
{
    float *ir   = malloc(n * sizeof(float));
    float *ii   = malloc(n * sizeof(float));
    float *fr   = malloc(n * sizeof(float));
    float *fi   = malloc(n * sizeof(float));
    float *pw   = malloc(n * sizeof(float));
    if (!ir || !ii || !fr || !fi || !pw) {
        free(ir); free(ii); free(fr); free(fi); free(pw);
        return -1;
    }

    memset(out, 0, sizeof(*out));
    uint32_t prev = dma_rx_wr_ptr();
    uint32_t read_ptr = prev;     /* persistent, like the production producer */
    uint64_t produced = 0;
    double t0 = now_s(), c0 = cpu_s();
    double next_frame = t0;

    while (now_s() - t0 < seconds) {
        if (pace_fps > 0.0) {
            /* Sleep until this frame is due, so the producer runs at the
             * demand rate instead of as fast as it can. */
            double due = next_frame;
            double slack = due - now_s();
            if (slack > 0)
                usleep((useconds_t)(slack * 1e6));
            next_frame = due + 1.0 / pace_fps;
        }

        /* Same wait contract as the production producer: a persistent read
         * pointer, so frames are contiguous (hop == N), skipping forward only
         * when the DMA has lapped us. */
        for (;;) {
            uint32_t cur = dma_rx_wr_ptr();
            uint32_t avail = (cur >= read_ptr)
                ? cur - read_ptr
                : (DMA_RX_BUF_SAMPLES - read_ptr) + cur;
            /* Same latency cap as the production producer (waterfall.c). */
            if (avail > (uint32_t)n * WF_MAX_LAG_FRAMES) {
                uint32_t newptr = (cur + DMA_RX_BUF_SAMPLES - (uint32_t)n)
                                  & (DMA_RX_BUF_SAMPLES - 1);
                uint32_t lost = (newptr >= read_ptr)
                    ? newptr - read_ptr
                    : (DMA_RX_BUF_SAMPLES - read_ptr) + newptr;
                out->skipped += lost;
                read_ptr = newptr;
                break;
            }
            if (avail >= (uint32_t)n)
                break;
            usleep(50);
        }
        uint32_t start = read_ptr;

        __sync_synchronize();
        const volatile uint32_t *rx = hal_ddr_rx_buf();
        if (start + (uint32_t)n <= DMA_RX_BUF_SAMPLES) {
            wf_convert_windowed(&rx[start], n, win, ir, ii, NULL);
        } else {
            size_t head = DMA_RX_BUF_SAMPLES - start;
            wf_convert_windowed(&rx[start], head, win, ir, ii, NULL);
            wf_convert_windowed(&rx[0], n - head, win + head,
                                &ir[head], &ii[head], NULL);
        }
        wf_fft_forward_split(plan, ir, ii, fr, fi);
        wf_magnitude_power(fr, fi, n, norm, pw);
        read_ptr = (start + (uint32_t)n) & (DMA_RX_BUF_SAMPLES - 1);
        out->frames++;

        /* Track ADC progress continuously: WR_PTR is only 25 bits of sample
         * index, so it wraps every 33.5 M samples (1.7 s at 20 MSPS) and a
         * single before/after read would undercount. */
        uint32_t cur = dma_rx_wr_ptr();
        produced += (cur >= prev) ? (cur - prev)
                                  : (DMA_RX_BUF_SAMPLES - prev) + cur;
        prev = cur;
    }
    out->wall = now_s() - t0;
    out->cpu  = cpu_s() - c0;

    uint32_t cur = dma_rx_wr_ptr();
    produced += (cur >= prev) ? (cur - prev)
                              : (DMA_RX_BUF_SAMPLES - prev) + cur;
    out->produced = produced;

    free(ir); free(ii); free(fr); free(fi); free(pw);
    return 0;
}

static const size_t SIZES[] = { 256, 1024, 4096, 8192 };
#define NSIZES (sizeof(SIZES) / sizeof(SIZES[0]))

/* FM (2.1), a USB2-practical ceiling (5), and the waterfall default (20). */
static const uint32_t RATES[] = { 2100000, 5000000, 20000000 };
#define NRATES (sizeof(RATES) / sizeof(RATES[0]))

#define DEMAND_FPS 50.0

int main(int argc, char **argv)
{
    double seconds = (argc > 1) ? strtod(argv[1], NULL) : 2.0;
    if (seconds <= 0.0)
        seconds = 2.0;

    radio_state_t radio;
    if (radio_init(&radio) != 0) {
        fprintf(stderr, "wf_cover: radio_init failed "
                        "(is the daemon stopped and the Pluto reachable?)\n");
        return 1;
    }

    printf("waterfall coverage and duty, Cortex-A9, %.1f s/point\n", seconds);
    printf("coverage = frames*N / samples the ADC produced, measured from "
           "WR_PTR\n");
    printf("free-run = the production producer; paced = %.0f fps "
           "(demand rate)\n\n", DEMAND_FPS);

    printf("%9s %6s | %8s %8s %7s | %8s %8s %7s | %9s\n",
           "fs(MSPS)", "N",
           "fps", "cover", "duty",
           "fps", "cover", "duty", "cover x");
    printf("%9s %6s | %8s %8s %7s | %8s %8s %7s | %9s\n",
           "", "", "free", "free", "free", "paced", "paced", "paced", "ratio");

    for (size_t r = 0; r < NRATES; r++) {
        uint32_t actual = 0;
        if (radio_set_sample_rate(&radio, RATES[r], &actual) != 0) {
            fprintf(stderr, "wf_cover: could not set %u Hz\n", RATES[r]);
            continue;
        }
        /* The AD9361 needs the DMA restarted after a rate change (styx
         * discipline), and the analog path needs a moment to settle. */
        dma_rx_stop();
        usleep(50000);
        if (dma_rx_start() != 0) {
            fprintf(stderr, "wf_cover: dma_rx_start failed at %u Hz\n", actual);
            continue;
        }
        usleep(100000);

        for (size_t s = 0; s < NSIZES; s++) {
            size_t n = SIZES[s];
            float *win = malloc(n * sizeof(float));
            if (!win) return 1;
            float cg = window_generate(WINDOW_HANN, n, win);
            float norm = 1.0f / ((float)n * cg);
            wf_fft_plan *plan = wf_fft_plan_create_n(n);
            if (!plan) {
                fprintf(stderr, "wf_cover: fft plan failed at N=%zu\n", n);
                return 1;
            }

            point_t free_run, paced;
            if (run_point(n, seconds, 0.0, win, norm, plan, &free_run) != 0 ||
                run_point(n, seconds, DEMAND_FPS, win, norm, plan, &paced) != 0) {
                fprintf(stderr, "wf_cover: alloc failed at N=%zu\n", n);
                return 1;
            }

            double cf = free_run.produced
                ? (double)free_run.frames * n / (double)free_run.produced : 0.0;
            double cp = paced.produced
                ? (double)paced.frames * n / (double)paced.produced : 0.0;

            printf("%9.1f %6zu | %8.0f %7.1f%% %6.1f%% | %8.0f %7.2f%% %6.1f%% | %8.1fx\n",
                   actual / 1e6, n,
                   free_run.frames / free_run.wall, cf * 100.0,
                   free_run.cpu / free_run.wall * 100.0,
                   paced.frames / paced.wall, cp * 100.0,
                   paced.cpu / paced.wall * 100.0,
                   cp > 0 ? cf / cp : 0.0);
            fflush(stdout);

            wf_fft_plan_destroy(plan);
            free(win);
        }
    }

    printf("\n  coverage is the fraction of the sample stream that reached an\n");
    printf("  FFT.  The producer has no overlap (hop == N), so 100%% is the\n");
    printf("  ceiling and is reached only when fs < fs_max.  'duty' is the\n");
    printf("  measured CPU share of one core, including the WR_PTR poll.\n");
    printf("  'cover ratio' is what free-running buys over pacing to demand,\n");
    printf("  which is the pacing trade: free-run spends ~80%% of a core to\n");
    printf("  cover ~20-30%% of the stream; paced spends ~5%% to cover ~1%%.\n");
    return 0;
}
