// SPDX-License-Identifier: MIT
/*
 * wf_bench — measure the waterfall producer's per-frame compute cost, by
 * stage, on the real target (Cortex-A9) against the real uncached DDR
 * mapping.
 *
 * Why: the PSD cost is duty(fs,N) = C(N)*fs / (C(N)*fs + N).  C(N) — the
 * per-frame compute, i.e. convert + window + FFT + magnitude — had never
 * been measured; only the aggregate at two sample rates was known from
 * the `shesha debug cpu` command.  This tool times each stage with
 * the production code paths (the fused wf_convert_windowed,
 * wf_fft_forward_split, wf_magnitude_db) so the numbers describe what the
 * daemon runs, not a copy.
 *
 * It A/Bs the two open micro-optimisations within a single run, so the
 * comparison is immune to board drift: convert+window (two-pass vs the fused
 * wf_convert_windowed) and the FFT (interleaved vs split-complex), plus a
 * one-shot split-vs-interleaved correctness check per size.
 *
 * It powers the AD9361 and starts the RX DMA so WR_PTR advances and the DDR
 * holds real samples, so it needs the Pluto and root.  Stop the daemon first
 * (it owns the AD9361 and the DMA, and the point is to measure the compute,
 * not the contention):
 *
 *   killall shesha-daemon
 *   /tmp/wf_bench [iters]
 *
 * The wait for N fresh samples (W = N/fs) is excluded from C — it is the idle
 * half of the duty cycle, not compute — but it is not free: dma_rx_capture
 * polls WR_PTR with usleep(50), so part of the wait burns CPU.  This tool
 * measures that CPU separately (clock_gettime(CLOCK_PROCESS_CPUTIME_ID) around
 * the wait) and reports k = cpu/wall, so duty becomes (C + k*W)/(C + W)
 * instead of the old C/(C + W), which assumed the wait was free.  fs_max is
 * unaffected: it is the rate at which compute alone saturates a core.
 */
#include "hal.h"
#include "dma_rx.h"
#include "radio.h"
#include "window.h"
#include "wf_convert.h"
#include "wf_mag.h"
#include "wf_fft.h"

#include <lib80211/fft.h>

#include <math.h>
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

/* CPU time (not wall): singles out the polling cost inside the capture wait,
 * which is the part of W that actually occupies the core. */
static double cpu_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* Wait until n fresh samples have been written, and return the write pointer
 * snapshot they start at.  Untimed on purpose. */
static uint32_t wait_fresh(size_t n)
{
    uint32_t start = dma_rx_wr_ptr();
    for (;;) {
        uint32_t cur = dma_rx_wr_ptr();
        uint32_t avail = (cur >= start) ? cur - start
                                        : (DMA_RX_BUF_SAMPLES - start) + cur;
        if (avail >= (uint32_t)n)
            return start;
        usleep(50);
    }
}

/* Windowed counterpart of dma_rx_read(), i.e. the production waterfall's
 * fused convert+window (see firmware/src/wf_convert.h).  Wrap-aware and
 * ordered against the WR_PTR check the same way dma_rx_read() is. */
static void read_windowed(uint32_t read_ptr, size_t n, const float *win,
                          float *out_real, float *out_imag)
{
    const volatile uint32_t *rx = hal_ddr_rx_buf();

    __sync_synchronize();

    if (read_ptr + (uint32_t)n <= DMA_RX_BUF_SAMPLES) {
        wf_convert_windowed(&rx[read_ptr], n, win, out_real, out_imag, NULL);
    } else {
        size_t first = DMA_RX_BUF_SAMPLES - read_ptr;
        size_t second = n - first;
        wf_convert_windowed(&rx[read_ptr], first, win, out_real, out_imag, NULL);
        wf_convert_windowed(&rx[0], second, win + first,
                            &out_real[first], &out_imag[first], NULL);
    }
}

static const size_t SIZES[] = { 256, 512, 1024, 2048, 4096, 8192 };
#define NSIZES (sizeof(SIZES) / sizeof(SIZES[0]))

/* Reference rates to express duty at: FM (2.1), a USB2-practical point (5),
 * and the daemon's default waterfall rate (20) MSPS. */
static const double REF_FS[] = { 2.1e6, 5.0e6, 10.0e6, 20.0e6 };
#define NREF (sizeof(REF_FS) / sizeof(REF_FS[0]))

int main(int argc, char **argv)
{
    size_t iters = (argc > 1) ? (size_t)strtoul(argv[1], NULL, 0) : 500;
    if (iters == 0)
        iters = 1;

    radio_state_t radio;
    if (radio_init(&radio) != 0) {
        fprintf(stderr, "wf_bench: radio_init failed "
                        "(is the daemon stopped and the Pluto reachable?)\n");
        return 1;
    }

    printf("waterfall per-frame compute C(N), Cortex-A9, %zu frames/size, "
           "Hann window\n", iters);
    printf("stages: convert+window (uncached DDR, fused) + FFT (fftw3f, "
           "split) + magnitude/dB\n\n");

    printf("    N   cvt_us  fft_us  mag_us    C_us   fs_max");
    for (size_t r = 0; r < NREF; r++)
        printf("  duty@%g", REF_FS[r] / 1e6);
    printf("\n");

    for (size_t s = 0; s < NSIZES; s++) {
        size_t n = SIZES[s];
        float *win  = malloc(n * sizeof(float));
        float *ir   = malloc(n * sizeof(float));
        float *ii   = malloc(n * sizeof(float));
        float *fr   = malloc(n * sizeof(float));
        float *fi   = malloc(n * sizeof(float));
        float *bins = malloc(n * sizeof(float));
        float *bins2 = malloc(n * sizeof(float));
        float *bins3 = malloc(n * sizeof(float));
        if (!win || !ir || !ii || !fr || !fi || !bins || !bins2 || !bins3) {
            fprintf(stderr, "wf_bench: alloc failed at N=%zu\n", n);
            return 1;
        }
        float cg = window_generate(WINDOW_HANN, n, win);
        float norm = 1.0f / ((float)n * cg);
        lib80211_fft_plan *plan = lib80211_fft_plan_create_n(n);
        if (!plan) {
            fprintf(stderr, "wf_bench: fft plan failed at N=%zu\n", n);
            return 1;
        }
        wf_fft_plan *split = wf_fft_plan_create_n(n);
        if (!split) {
            fprintf(stderr, "wf_bench: split fft plan failed at N=%zu\n", n);
            return 1;
        }

        /*
         * Equivalence check: the split path the production waterfall now uses
         * must match the interleaved reference exactly (FFTW runs the same
         * codelets either way; only the pack/unpack is skipped).  A stride or
         * format mistake shows up here before it can corrupt the waterfall.
         */
        for (size_t i = 0; i < n; i++) {
            ir[i] = sinf(0.017f * (float)i);
            ii[i] = cosf(0.031f * (float)i);
        }
        lib80211_fft_forward(plan, ir, ii, fr, fi);
        wf_fft_forward_split(split, ir, ii, bins, bins2);
        double fft_diff = 0.0;
        for (size_t i = 0; i < n; i++) {
            double dr = fabs((double)fr[i] - (double)bins[i]);
            double di = fabs((double)fi[i] - (double)bins2[i]);
            if (dr > fft_diff) fft_diff = dr;
            if (di > fft_diff) fft_diff = di;
        }
        printf("  %5zu  fft split-vs-interleaved max|diff| = %.3e\n",
               n, fft_diff);

        double sf = 0, sm = 0, sme = 0, sdb = 0;
        double cvt_sep = 0, cvt_fus = 0;
        double fft_i = 0, fft_s = 0;
        size_t nsep = 0, nfus = 0, ni = 0, ns = 0;
        double swait = 0, swait_cpu = 0;
        double dmax = 0.0, dsumsq = 0.0;
        for (size_t i = 0; i < iters; i++) {
            double wa = now_s();
            double ca = cpu_s();
            uint32_t rd = wait_fresh(n);
            double cb = cpu_s();
            double a = now_s();
            swait += a - wa;
            swait_cpu += cb - ca;
            /*
             * Alternate the paths within one run so each A/B is immune to
             * board drift (thermal, governor, cache warmup): both variants
             * see the same fresh input and the same downstream stages.
             */
            if (i & 1u) {
                /* two-pass: convert, then a separate window pass */
                if (dma_rx_read(rd, n, ir, ii) != (int)n) {
                    fprintf(stderr, "wf_bench: dma_rx_read failed at N=%zu\n", n);
                    return 1;
                }
                double b1 = now_s();
                window_apply(win, n, ir, ii);
                double b2 = now_s();
                cvt_sep += (b1 - a) + (b2 - b1); nsep++;
            } else {
                /* production: convert with the window folded into the unpack */
                read_windowed(rd, n, win, ir, ii);
                double b1 = now_s();
                cvt_fus += b1 - a; nfus++;
            }
            double c = now_s();
            /* Independent parity from the convert A/B so the two axes do not
             * correlate their cache state across the run. */
            if ((i >> 1) & 1u) {
                lib80211_fft_forward(plan, ir, ii, fr, fi);
                double dd = now_s();
                fft_i += dd - c; ni++;
            } else {
                wf_fft_forward_split(split, ir, ii, fr, fi);
                double dd = now_s();
                fft_s += dd - c; ns++;
            }
            double d = now_s();
            wf_magnitude_power(fr, fi, n, norm, bins);
            double e = now_s();
            /* The fused power+dB form, i.e. what the producer ran before the
             * split, so the table can show what it stopped paying. */
            wf_magnitude_db(fr, fi, n, norm, bins2);
            double f = now_s();
            /* The drain's one-shot dB conversion, run once per broadcast
             * rather than once per computed frame. */
            double g0 = now_s();
            wf_power_to_db(bins, n, bins3);
            double g1 = now_s();

            sm += e - d;
            sme += f - e;
            sdb += g1 - g0;

            /* Accuracy against a libm oracle, on real frames: the fast log is
             * the only approximation in the chain, and bins3 is the exact
             * output the client receives. */
            for (size_t j = 0; j < n; j++) {
                double want = 10.0 * log10((double)bins[j]);
                double err = fabs((double)bins3[j] - want);
                if (err > dmax)
                    dmax = err;
                dsumsq += err * err;
            }
        }
        double sep = nsep ? cvt_sep / (double)nsep : 0.0;
        double fus = nfus ? cvt_fus / (double)nfus : 0.0;
        fft_i = ni ? fft_i / (double)ni : 0.0;
        fft_s = ns ? fft_s / (double)ns : 0.0;
        sm /= iters; sme /= iters; sdb /= iters;
        swait /= iters; swait_cpu /= iters;
        double sc = fus;          /* production convert (window fused in) */
        sf = fft_s;               /* production FFT (split) */
        double C = sc + sf + sm;  /* production: magnitude is power-only */
        double Cdb = sc + sf + sme;   /* pre-split: dB in the producer */
        double k = swait > 0 ? swait_cpu / swait : 0.0;
        double rms = sqrt(dsumsq / ((double)iters * n));

        printf("        cvt+win A/B: two-pass %7.2f us  fused %7.2f us  "
               "(%.2fx)\n", sep * 1e6, fus * 1e6,
               fus > 0 ? sep / fus : 0.0);
        printf("        fft     A/B: interleaved %7.2f us  split %7.2f us  "
               "(%.2fx)\n", fft_i * 1e6, fft_s * 1e6,
               fft_s > 0 ? fft_i / fft_s : 0.0);
        printf("        mag     A/B: power+dB %7.2f us  power-only %7.2f us  "
               "(%.2fx)  drain dB %6.2f us/broadcast\n",
               sme * 1e6, sm * 1e6, sm > 0 ? sme / sm : 0.0, sdb * 1e6);
        printf("  %5zu  %6.2f  %6.2f  %6.2f  %6.2f  %6.2f",
               n, sc * 1e6, sf * 1e6, sm * 1e6, C * 1e6,
               (double)n / C / 1e6);
        for (size_t r = 0; r < NREF; r++) {
            double fs = REF_FS[r];
            double W = (double)n / fs;
            double duty = (C + k * W) / (C + W) * 100.0;
            printf("  %8.1f%%", duty);
        }
        printf("\n");
        printf("        wait %7.2f us/frame  cpu %6.2f  k=%.3f   "
               "dB-in-producer: C %7.2f (%.2fx)  "
               "fast-log vs libm: max %.2e dB  rms %.2e dB\n",
               swait * 1e6, swait_cpu * 1e6, k,
               Cdb * 1e6, Cdb / C, dmax, rms);

        lib80211_fft_plan_destroy(plan);
        wf_fft_plan_destroy(split);
        free(win); free(ir); free(ii); free(fr); free(fi);
        free(bins); free(bins2); free(bins3);
    }

    printf("\n  'duty@X' = (C + k*W)/(C + W) at X MSPS, W = N/fs: the\n");
    printf("  producer's share of one core including the poll cost k*W, so\n");
    printf("  the wait is no longer assumed free.  fs_max = N/C is where\n");
    printf("  compute alone saturates the core (independent of k).\n");
    printf("  'mag_us' and C are the production path: the producer emits\n");
    printf("  linear power and the drain converts to dB once per broadcast\n");
    printf("  ('drain dB'), not once per computed frame.  'dB-in-producer'\n");
    printf("  is what C was before that split.\n");
    printf("  Cross-check against `shesha debug cpu` while the\n");
    printf("  daemon runs a waterfall: the difference is the N/fs wait plus\n");
    printf("  contention.\n");
    return 0;
}
