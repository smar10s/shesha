// SPDX-License-Identifier: MIT
/*
 * wf_burst — does coverage predict what the waterfall actually catches?
 *
 * wf_cover measures coverage as a ratio of sample counters, which is exact but
 * indirect: it says what fraction of the stream was transformed, not what
 * fraction of real signals were seen.  This closes that loop empirically.
 *
 * Method: transmit a cyclic on/off tone burst into the RX path (attenuated
 * loopback cable), run the production waterfall pipeline, and count how many
 * bursts land in a frame.  The burst is a tone at a known offset from centre,
 * so detection is "is the bin at that offset above the floor" -- a threshold
 * on one bin, not a judgement about an image.
 *
 * For a burst of length T in a period P, with the producer covering a fraction
 * c of the stream in contiguous frames, the expected detection rate is ~c
 * (each burst is seen if a frame overlaps it).  Comparing measured detections
 * against coverage is the check: if they agree, coverage is a sufficient
 * statistic for "what the waterfall catches" and can be the documented number.
 *
 * Needs TX and RX at the same rate so the producer is not suspended, which is
 * the FM TX+RX concurrency rule (tx_session.c).  Everything here runs at one
 * rate, so TX and RX coexist.
 *
 *   killall shesha-daemon
 *   /tmp/wf_burst [-n N] [-t burst_us] [-p period_us] [-s seconds]
 *
 * ATTENUATED LOOPBACK ONLY.  It transmits continuously for the run.
 */
#define _GNU_SOURCE
#include "hal.h"
#include "dma_rx.h"
#include "dma_tx.h"
#include "radio.h"
#include "window.h"
#include "wf_convert.h"
#include "wf_mag.h"
#include "waterfall.h"   /* WF_MAX_LAG_FRAMES: match the producer */

#include "wf_fft.h"

#include <getopt.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define SAMPLE_RATE_HZ  20000000u
#define CENTRE_HZ       2450000000ull
/* Tone offset from centre.  Far enough from DC to clear the AD9361's LO
 * leakage, and not a harmonic of the burst period. */
#define TONE_OFFSET_HZ  3000000.0

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static void usage(const char *p)
{
    printf("usage: %s [-n fft_size] [-t burst_us] [-p period_us] "
           "[-s seconds] [-m margin_db]\n", p);
}

int main(int argc, char **argv)
{
    size_t n = 4096;
    double burst_us = 500.0;
    double period_us = 20000.0;    /* one burst per 50 fps broadcast window */
    double seconds = 5.0;
    double margin_db = 6.0;
    int opt;

    while ((opt = getopt(argc, argv, "n:t:p:s:m:h")) != -1) {
        switch (opt) {
        case 'n': n = (size_t)strtoul(optarg, NULL, 0); break;
        case 't': burst_us = strtod(optarg, NULL); break;
        case 'p': period_us = strtod(optarg, NULL); break;
        case 's': seconds = strtod(optarg, NULL); break;
        case 'm': margin_db = strtod(optarg, NULL); break;
        case 'h': usage(argv[0]); return 0;
        default:  usage(argv[0]); return 1;
        }
    }
    if (n < 256 || n > 8192 || (n & (n - 1)) != 0) {
        fprintf(stderr, "wf_burst: fft size must be a power of 2 in 256..8192\n");
        return 1;
    }
    if (burst_us >= period_us) {
        fprintf(stderr, "wf_burst: burst must be shorter than the period\n");
        return 1;
    }

    radio_state_t radio;
    if (radio_init(&radio) != 0) {
        fprintf(stderr, "wf_burst: radio_init failed "
                        "(is the daemon stopped?)\n");
        return 1;
    }
    uint32_t actual = 0;
    radio_set_sample_rate(&radio, SAMPLE_RATE_HZ, &actual);
    uint64_t fa = 0;
    radio_set_freq(&radio, CENTRE_HZ, &fa);
    /* radio_set_bw writes the readback unconditionally, so it needs a real
     * destination even when the value is not wanted (same idiom as main.c). */
    radio_set_bw(&radio, SAMPLE_RATE_HZ, &(uint32_t){0});
    radio_set_agc(&radio, 0);               /* manual: a moving gain would
                                             * move the detection threshold */
    radio_set_gain(&radio, 30.0);
    radio_set_tx_atten(&radio, 20.0);       /* loopback: keep the front end
                                             * well clear of saturation */

    /* One period of the burst waveform, played cyclically by the DMA so the
     * ARM does not have to feed it and the timing is exact. */
    size_t period_n = (size_t)(period_us * 1e-6 * actual);
    size_t burst_n  = (size_t)(burst_us  * 1e-6 * actual);
    if (period_n > DMA_TX_MAX_SAMPLES) {
        fprintf(stderr, "wf_burst: period too long for the TX buffer\n");
        return 1;
    }
    float *tx_re = calloc(period_n, sizeof(float));
    float *tx_im = calloc(period_n, sizeof(float));
    if (!tx_re || !tx_im) return 1;
    double dphi = 2.0 * M_PI * TONE_OFFSET_HZ / (double)actual;
    for (size_t i = 0; i < burst_n; i++) {
        tx_re[i] = 0.7f * (float)cos(dphi * (double)i);
        tx_im[i] = 0.7f * (float)sin(dphi * (double)i);
    }

    printf("wf_burst: N=%zu  burst=%.0f us  period=%.0f us  duty=%.2f%%  "
           "%.1f s\n", n, burst_us, period_us,
           burst_us / period_us * 100.0, seconds);
    printf("  fs=%.3f MSPS  centre=%.3f MHz  tone=+%.3f MHz  "
           "threshold=floor+%.0f dB\n",
           actual / 1e6, fa / 1e6, TONE_OFFSET_HZ / 1e6, margin_db);
    printf("  frame covers %.1f us; expected bursts in %.1f s: %.0f\n\n",
           (double)n / actual * 1e6, seconds, seconds / (period_us * 1e-6));

    if (dma_tx_load(tx_re, tx_im, period_n, true) != 0) {
        fprintf(stderr, "wf_burst: dma_tx_load failed\n");
        return 1;
    }
    if (dma_rx_start() != 0) {
        fprintf(stderr, "wf_burst: dma_rx_start failed\n");
        return 1;
    }
    if (dma_tx_trigger() != 0) {
        fprintf(stderr, "wf_burst: dma_tx_trigger failed\n");
        dma_rx_stop();
        return 1;
    }
    usleep(200000);   /* let the loop settle before measuring */

    float *win = malloc(n * sizeof(float));
    float cg = window_generate(WINDOW_HANN, n, win);
    float norm = 1.0f / ((float)n * cg);
    wf_fft_plan *plan = wf_fft_plan_create_n(n);
    float *ir = malloc(n * sizeof(float)), *ii = malloc(n * sizeof(float));
    float *fr = malloc(n * sizeof(float)), *fi = malloc(n * sizeof(float));
    float *pw = malloc(n * sizeof(float));
    if (!win || !plan || !ir || !ii || !fr || !fi || !pw) return 1;

    /* The tone's bin after the fftshift wf_magnitude_power applies. */
    long tone_bin = (long)(TONE_OFFSET_HZ / (double)actual * (double)n)
                    + (long)(n / 2);

    uint32_t read_ptr = dma_rx_wr_ptr();
    uint64_t frames = 0, skipped = 0, hits = 0;
    /* Runs of consecutive hit-frames collapse to one burst: a 500 us burst
     * at N=4096 spans more than one 205 us frame. */
    bool in_run = false;
    uint64_t runs = 0;
    double floor_acc = 0.0, peak_acc = 0.0;

    double t0 = now_s();
    while (now_s() - t0 < seconds) {
        for (;;) {
            uint32_t cur = dma_rx_wr_ptr();
            uint32_t avail = (cur >= read_ptr)
                ? cur - read_ptr : (DMA_RX_BUF_SAMPLES - read_ptr) + cur;
            /* Same latency cap as the production producer (waterfall.c). */
            if (avail > (uint32_t)n * WF_MAX_LAG_FRAMES) {
                uint32_t np = (cur + DMA_RX_BUF_SAMPLES - (uint32_t)n)
                              & (DMA_RX_BUF_SAMPLES - 1);
                skipped += (np >= read_ptr) ? np - read_ptr
                         : (DMA_RX_BUF_SAMPLES - read_ptr) + np;
                read_ptr = np;
                break;
            }
            if (avail >= (uint32_t)n) break;
            usleep(50);
        }
        __sync_synchronize();
        const volatile uint32_t *rx = hal_ddr_rx_buf();
        if (read_ptr + (uint32_t)n <= DMA_RX_BUF_SAMPLES) {
            wf_convert_windowed(&rx[read_ptr], n, win, ir, ii, NULL);
        } else {
            size_t head = DMA_RX_BUF_SAMPLES - read_ptr;
            wf_convert_windowed(&rx[read_ptr], head, win, ir, ii, NULL);
            wf_convert_windowed(&rx[0], n - head, win + head,
                                &ir[head], &ii[head], NULL);
        }
        wf_fft_forward_split(plan, ir, ii, fr, fi);
        wf_magnitude_power(fr, fi, n, norm, pw);
        read_ptr = (read_ptr + (uint32_t)n) & (DMA_RX_BUF_SAMPLES - 1);
        frames++;

        /* Median-ish floor from a band well away from the tone, so a present
         * burst cannot raise its own threshold. */
        double sum = 0.0;
        size_t lo = n / 8, hi = n / 4, cnt = 0;
        for (size_t i = lo; i < hi; i++) { sum += pw[i]; cnt++; }
        float floor_p = (float)(sum / (double)cnt);

        /* Peak over a few bins: the tone drifts by the LO offset error. */
        float peak = 0.0f;
        for (long b = tone_bin - 2; b <= tone_bin + 2; b++)
            if (b >= 0 && b < (long)n && pw[b] > peak)
                peak = pw[b];

        float fdb, pdb;
        wf_power_to_db(&floor_p, 1, &fdb);
        wf_power_to_db(&peak, 1, &pdb);
        floor_acc += fdb;
        peak_acc  += pdb;

        bool hit = (pdb - fdb) > margin_db;
        if (hit) {
            hits++;
            if (!in_run) { runs++; in_run = true; }
        } else {
            in_run = false;
        }
    }
    double wall = now_s() - t0;

    dma_tx_stop();
    dma_rx_stop();

    double expected_bursts = wall / (period_us * 1e-6);
    double coverage = (frames * (double)n) / (frames * (double)n + (double)skipped);

    printf("  frames %llu   skipped %llu samples   coverage %.1f%%\n",
           (unsigned long long)frames, (unsigned long long)skipped,
           coverage * 100.0);
    printf("  mean floor %.1f dB   mean tone-band peak %.1f dB\n",
           floor_acc / frames, peak_acc / frames);
    printf("  hit frames %llu/%llu (%.1f%% of frames)\n",
           (unsigned long long)hits, (unsigned long long)frames,
           (double)hits / frames * 100.0);
    printf("  distinct bursts detected %llu / %.0f expected  -> %.1f%%\n",
           (unsigned long long)runs, expected_bursts,
           expected_bursts > 0 ? runs / expected_bursts * 100.0 : 0.0);
    printf("\n  Detection should track coverage: a burst is caught when a\n");
    printf("  frame overlaps it, and the producer covers %.1f%% of the\n",
           coverage * 100.0);
    printf("  stream.  A detection rate far below coverage means the burst\n");
    printf("  is shorter than one frame and is being diluted by the window;\n");
    printf("  far above means the burst is longer than the frame gap.\n");

    wf_fft_plan_destroy(plan);
    free(win); free(ir); free(ii); free(fr); free(fi); free(pw);
    free(tx_re); free(tx_im);
    return 0;
}
