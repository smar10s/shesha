// SPDX-License-Identifier: MIT
/*
 * fm_stream_test — isolate FM TX streaming throughput from the daemon.
 *
 * Single-threaded.  No TCP, no mutexes, no producer thread, no ring
 * buffer handoff.  Follows the canonical styx streaming pattern from
 * styxsdr's pluto_stream_test.c: check available space, generate a
 * chunk, feed it.
 *
 * Purpose: answer one question with no confounds — can the ARM sustain
 * FM modulation at the DAC rate indefinitely?
 *
 * The ground-truth instrument is REG_IQ_DMA_TX_RD_PTR.  It only advances
 * when the FPGA drain FSM actually emits a sample; when the FSM starves
 * it parks in D_WAIT and rd_ptr freezes.  So
 *
 *     actual_dac_rate = total_consumed / elapsed
 *
 * is the true output rate.  Any deficit versus nominal is exactly the
 * time-stretch a receiver hears as "the audio slows down" — FM carries
 * its timeline in the phase trajectory, so a stalled DAC does not glitch,
 * it drags.
 *
 * Modes:
 *   --bench   pure generation throughput, no radio and no DMA.  Runs
 *             anywhere, including the host.  Cheapest possible answer to
 *             "is the ARM fast enough?"
 *   (default) full streaming TX with bounded buffer depth.
 *
 * Generators (-g) exist so the scalar-libm hypothesis can be A/B tested
 * rather than argued about.
 */

#include "hal.h"
#include "dma_tx.h"
#include "convert.h"
#include "fm_mod.h"

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define DEFAULT_DAC_RATE     2100000u
#define DEFAULT_AUDIO_RATE      44100u
#define DEFAULT_DEVIATION       75000u
#define DEFAULT_TONE_HZ          1000u
#define DEFAULT_DURATION_SEC       30
#define DEFAULT_HIGH_WATER_MS     300
#define DEFAULT_FREQ_MHZ        100.1
#define DEFAULT_TX_ATTEN         10.0

#define PREEMPHASIS_US             75

/* Pass threshold: sustained output rate as a fraction of nominal. */
#define RATE_PASS_FRACTION      0.999

/* ==================================================================
 * Timing
 * ================================================================== */

static double now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* ==================================================================
 * Generator under test
 *
 * The modulator itself lives in firmware/src/fm_mod.c, shared verbatim
 * with the daemon's fm_tx.c.  That is the whole point of this tool: a
 * private copy here would certify code that never reaches the air.
 *
 * The tool loops a finite tone buffer, so it generates with wrap = true.
 * ================================================================== */

typedef struct {
    fm_mod_t     mod;
    const float *audio;
    uint32_t     n_audio;
} tone_gen_t;

static inline size_t gen(tone_gen_t *g, float *re, float *im, size_t n)
{
    return fm_mod_generate(&g->mod, g->audio, g->n_audio, true, re, im, n);
}

/* Both feed variants behind one flag.  Fixed scale is the default because
 * |IQ| = 1 by construction for FM: auto-scale's peak-finding pass is pure
 * waste, and it renormalises every chunk independently, which steps the
 * transmitted amplitude at chunk boundaries. */
static inline int stream_feed(const float *re, const float *im, size_t n,
                             bool fixed_scale)
{
    return fixed_scale
        ? dma_tx_stream_feed_fixed(re, im, n, 2047.0f)
        : dma_tx_stream_feed(re, im, n);
}


/* ==================================================================
 * Audio source: one second of tone, pre-emphasised once.
 *
 * A 1-second buffer at an integer tone frequency loops seamlessly, so
 * the modulator never sees a discontinuity — unlike fm_tx.c, which
 * re-runs pre-emphasis (with its own peak normalisation) per chunk.
 * ================================================================== */

static float *make_tone(uint32_t audio_rate, uint32_t tone_hz,
                        float amp, bool preemph)
{
    float *buf = malloc((size_t)audio_rate * sizeof(float));
    if (!buf)
        return NULL;

    for (uint32_t i = 0; i < audio_rate; i++)
        buf[i] = amp * sinf(2.0f * (float)M_PI * (float)tone_hz
                            * (float)i / (float)audio_rate);

    if (preemph) {
        double dt    = 1.0 / (double)audio_rate;
        double tau   = (double)PREEMPHASIS_US * 1e-6;
        double alpha = dt / (dt + tau);
        double gain  = 1.0 / alpha;
        double fb    = (1.0 - alpha) / alpha;

        /* Seed from the last sample so the filter state is continuous
         * across the loop point. */
        float prev = buf[audio_rate - 1];
        float peak = 0.0f;
        for (uint32_t i = 0; i < audio_rate; i++) {
            float x = buf[i];
            float y = (float)(gain * (double)x - fb * (double)prev);
            buf[i] = y;
            prev = x;
            if (fabsf(y) > peak)
                peak = fabsf(y);
        }
        if (peak > 1.0f) {
            float s = 1.0f / peak;
            for (uint32_t i = 0; i < audio_rate; i++)
                buf[i] *= s;
        }
    }

    return buf;
}

/* ==================================================================
 * --verify: is the selected generator numerically equivalent to libm?
 *
 * A fast-but-wrong modulator is worse than a slow one.  Run both
 * generators from identical state and compare.  Error is reported in
 * DAC LSBs (12-bit, full scale 2047) because that is the only precision
 * that reaches the air.
 * ================================================================== */

static int run_verify(const tone_gen_t *proto, const char *gen_name)
{
    static float re_a[DMA_TX_STREAM_CHUNK], im_a[DMA_TX_STREAM_CHUNK];
    static float re_b[DMA_TX_STREAM_CHUNK], im_b[DMA_TX_STREAM_CHUNK];

    /* libm at double-accumulated phase is the reference.  Note the NCO
     * keeps phase in an integer, so the two accumulators drift apart by
     * design; comparing them is still valid because both represent the
     * same instantaneous frequency and the LUT error is what we are
     * bounding.  Any real divergence shows up immediately as a large
     * error, as the NEON polynomial does. */
    tone_gen_t a = *proto; a.mod.kind = FM_GEN_LIBM;
    tone_gen_t b = *proto;

    double worst = 0.0;
    /* 2000 chunks ~ 8.2 M samples: enough for the phase accumulator to
     * traverse many audio cycles and exercise range reduction. */
    for (int c = 0; c < 2000; c++) {
        gen(&a, re_a, im_a, DMA_TX_STREAM_CHUNK);
        gen(&b, re_b, im_b, DMA_TX_STREAM_CHUNK);
        for (size_t i = 0; i < DMA_TX_STREAM_CHUNK; i++) {
            double dr = fabs((double)re_a[i] - (double)re_b[i]);
            double di = fabs((double)im_a[i] - (double)im_b[i]);
            if (dr > worst) worst = dr;
            if (di > worst) worst = di;
        }
    }

    double lsb = worst * 2047.0;
    bool ok = lsb < 1.0;

    fprintf(stderr, "fm_stream_test --verify (%s vs libm)\n", gen_name);
    fprintf(stderr, "  max abs error: %.3e (%.4f DAC LSB of 2047)\n",
            worst, lsb);
    fprintf(stderr, "\n  RESULT: %s\n",
            ok ? "PASS — below 1 LSB, indistinguishable on air" : "FAIL");

    printf("{\"mode\":\"verify\",\"generator\":\"%s\",\"passed\":%s,"
           "\"max_abs_err\":%.6e,\"max_err_lsb\":%.4f}\n",
           gen_name, ok ? "true" : "false", worst, lsb);
    return ok ? 0 : 1;
}

/* ==================================================================
 * --bench: generation throughput only.  No radio, no DMA, no RF.
 * ================================================================== */

static int run_bench(tone_gen_t *g, uint32_t dac_rate, int duration_sec,
                     const char *gen_name, bool with_convert,
                     bool fixed_scale)
{
    static float re[DMA_TX_STREAM_CHUNK];
    static float im[DMA_TX_STREAM_CHUNK];
    static uint32_t packed[DMA_TX_STREAM_CHUNK];

    fprintf(stderr, "fm_stream_test --bench\n");
    fprintf(stderr, "  generator:  %s\n", gen_name);
    fprintf(stderr, "  convert:    %s\n",
            !with_convert ? "excluded"
            : fixed_scale ? "included, fixed scale (1 pass)"
                          : "included, auto-scale (2 passes, as feed does)");
    fprintf(stderr, "  nominal:    %u SPS\n", dac_rate);
    fprintf(stderr, "  duration:   %d s\n", duration_sec);

    double t0 = now_sec();
    double deadline = t0 + (double)duration_sec;
    uint64_t total = 0;

    while (now_sec() < deadline) {
        /* 64 chunks between clock reads so clock_gettime overhead does
         * not pollute the measurement. */
        for (int k = 0; k < 64; k++) {
            gen(g, re, im, DMA_TX_STREAM_CHUNK);
            if (with_convert) {
                if (fixed_scale)
                    convert_float_to_tx(re, im, DMA_TX_STREAM_CHUNK,
                                        2047.0f, packed);
                else
                    convert_float_to_tx_auto(re, im, DMA_TX_STREAM_CHUNK,
                                             packed);
            }
            total += DMA_TX_STREAM_CHUNK;
        }
    }

    double elapsed = now_sec() - t0;
    double msps = (double)total / elapsed / 1e6;
    double headroom = (double)total / elapsed / (double)dac_rate;

    fprintf(stderr, "\n  generated:  %llu samples in %.2f s\n",
            (unsigned long long)total, elapsed);
    fprintf(stderr, "  throughput: %.3f MSPS\n", msps);
    fprintf(stderr, "  headroom:   %.2fx nominal (%s)\n", headroom,
            headroom >= 1.0 ? "sufficient" : "INSUFFICIENT");
    fprintf(stderr, "\n  RESULT: %s\n", headroom >= 1.0 ? "PASS" : "FAIL");

    printf("{\"mode\":\"bench\",\"generator\":\"%s\",\"convert\":%s,"
           "\"fixed_scale\":%s,\"passed\":%s,"
           "\"nominal_sps\":%u,\"throughput_sps\":%.0f,\"headroom\":%.4f,"
           "\"elapsed_sec\":%.3f,\"generated\":%llu}\n",
           gen_name, with_convert ? "true" : "false",
           fixed_scale ? "true" : "false",
           headroom >= 1.0 ? "true" : "false",
           dac_rate, (double)total / elapsed, headroom, elapsed,
           (unsigned long long)total);

    return headroom >= 1.0 ? 0 : 1;
}

/* ==================================================================
 * Streaming TX
 * ================================================================== */

/* ==================================================================
 * --probe: raw register evidence.
 *
 * Feeds flat out and dumps the actual FPGA pointers alongside the ARM's
 * own counters, so the relationship between rd_ptr, WR_PTR,
 * dma_tx_stream_available() and the monotonic fed/consumed pair can be
 * read off directly rather than inferred.
 * ================================================================== */

static int run_probe(tone_gen_t *g, uint32_t dac_rate, int duration_sec,
                    bool fixed_scale)
{
    static float re[DMA_TX_STREAM_CHUNK];
    static float im[DMA_TX_STREAM_CHUNK];

    fprintf(stderr, "fm_stream_test --probe  (nominal %u SPS, "
            "ring %u samples = %.2f s)\n\n",
            dac_rate, (unsigned)DMA_TX_MAX_SAMPLES,
            (double)DMA_TX_MAX_SAMPLES / (double)dac_rate);

    fprintf(stderr, "  before start: rd_ptr=%u STATUS=0x%08x\n",
            dma_tx_stream_rd_ptr(),
            hal_reg_read(REG_IQ_DMA_TX_STATUS));

    if (dma_tx_stream_arm() != 0) {
        fprintf(stderr, "dma_tx_stream_arm failed\n");
        return 1;
    }

    /* Pre-fill before trigger so the drain FSM never starts ahead of the
     * write cursor. */
    uint64_t fed = 0;
    uint32_t prefill = (uint32_t)((uint64_t)DEFAULT_HIGH_WATER_MS
                                  * dac_rate / 1000ULL);
    while (fed < prefill) {
        gen(g, re, im, DMA_TX_STREAM_CHUNK);
        if (stream_feed(re, im, DMA_TX_STREAM_CHUNK, fixed_scale) != 0)
            break;
        fed += DMA_TX_STREAM_CHUNK;
    }
    fprintf(stderr, "  prefilled %llu samples (%.0f ms), depth=%d\n",
            (unsigned long long)fed,
            (double)fed * 1000.0 / (double)dac_rate,
            (int)dma_tx_stream_depth());

    if (dma_tx_stream_trigger() != 0) {
        fprintf(stderr, "dma_tx_stream_trigger failed\n");
        return 1;
    }

    fprintf(stderr, "  after trigger: rd_ptr=%u STATUS=0x%08x depth=%d\n\n",
            dma_tx_stream_rd_ptr(),
            hal_reg_read(REG_IQ_DMA_TX_STATUS),
            (int)dma_tx_stream_depth());

    fprintf(stderr, "  %7s %10s %10s %9s %12s %12s %12s\n",
            "t_ms", "rd_ptr", "WR_PTR", "depth", "fed", "consumed",
            "fed-cons");
    double t0 = now_sec();
    double next = t0;
    double deadline = t0 + (double)duration_sec;

    while (now_sec() < deadline) {
        double now = now_sec();
        if (now >= next) {
            fprintf(stderr, "  %7.0f %10u %10u %9d %12llu %12llu %12lld\n",
                    (now - t0) * 1000.0,
                    dma_tx_stream_rd_ptr(),
                    hal_reg_read(REG_IQ_DMA_TX_WR_PTR),
                    (int)dma_tx_stream_depth(),
                    (unsigned long long)fed,
                    (unsigned long long)dma_tx_stream_emitted(),
                    (long long)((int64_t)fed
                        - (int64_t)dma_tx_stream_emitted()));
            next = now + 0.5;
        }

        if (dma_tx_stream_available() < DMA_TX_STREAM_CHUNK) {
            usleep(200);
            continue;
        }
        gen(g, re, im, DMA_TX_STREAM_CHUNK);
        if (stream_feed(re, im, DMA_TX_STREAM_CHUNK, fixed_scale) != 0) {
            fprintf(stderr, "  feed failed at fed=%llu\n",
                    (unsigned long long)fed);
            break;
        }
        fed += DMA_TX_STREAM_CHUNK;
    }

    dma_tx_stream_stop();
    uint64_t emitted = dma_tx_stream_emitted();
    fprintf(stderr, "\n  final: fed=%llu consumed=%llu diff=%lld\n",
            (unsigned long long)fed, (unsigned long long)emitted,
            (long long)((int64_t)fed - (int64_t)emitted));
    return 0;
}

int main(int argc, char **argv)
{
    uint32_t dac_rate     = DEFAULT_DAC_RATE;
    uint32_t audio_rate   = DEFAULT_AUDIO_RATE;
    uint32_t deviation    = DEFAULT_DEVIATION;
    uint32_t tone_hz      = DEFAULT_TONE_HZ;
    int      duration_sec = DEFAULT_DURATION_SEC;
    int      high_water_ms = DEFAULT_HIGH_WATER_MS;
    double   freq_mhz     = DEFAULT_FREQ_MHZ;
    double   tx_atten     = DEFAULT_TX_ATTEN;
    bool     bench        = false;
    bool     bench_convert = true;
    bool     fixed_scale   = true;
    bool     verify        = false;
    bool     probe         = false;
    bool     preemph      = true;
    bool     verbose      = false;
    fm_gen_kind_t kind    = FM_GEN_LIBM;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        bool has_next = (i + 1 < argc);
        if (!strcmp(a, "--bench")) {
            bench = true;
        } else if (!strcmp(a, "--no-convert")) {
            bench_convert = false;
        } else if (!strcmp(a, "--fixed-scale")) {
            fixed_scale = true;
        } else if (!strcmp(a, "--auto-scale")) {
            fixed_scale = false;
        } else if (!strcmp(a, "--verify")) {
            verify = true;
        } else if (!strcmp(a, "--probe")) {
            probe = true;
        } else if (!strcmp(a, "-v")) {
            verbose = true;
        } else if (!strcmp(a, "--no-preemph")) {
            preemph = false;
        } else if (!strcmp(a, "-g") && has_next) {
            const char *v = argv[++i];
            if (!strcmp(v, "libm")) {
                kind = FM_GEN_LIBM;
            } else if (!strcmp(v, "nco")) {
                kind = FM_GEN_NCO;
            } else if (!strcmp(v, "neon")) {
#ifdef __ARM_NEON
                kind = FM_GEN_NEON;
#else
                fprintf(stderr, "built without NEON; -g neon unavailable\n");
                return 2;
#endif
            } else {
                fprintf(stderr, "unknown generator '%s'\n", v);
                return 2;
            }
        } else if (!strcmp(a, "-r") && has_next) {
            dac_rate = (uint32_t)strtoul(argv[++i], NULL, 10);
        } else if (!strcmp(a, "-d") && has_next) {
            duration_sec = atoi(argv[++i]);
        } else if (!strcmp(a, "-w") && has_next) {
            high_water_ms = atoi(argv[++i]);
        } else if (!strcmp(a, "-t") && has_next) {
            tone_hz = (uint32_t)strtoul(argv[++i], NULL, 10);
        } else if (!strcmp(a, "-f") && has_next) {
            freq_mhz = atof(argv[++i]);
        } else if (!strcmp(a, "-a") && has_next) {
            tx_atten = atof(argv[++i]);
        } else if (!strcmp(a, "--audio-rate") && has_next) {
            audio_rate = (uint32_t)strtoul(argv[++i], NULL, 10);
        } else if (!strcmp(a, "--deviation") && has_next) {
            deviation = (uint32_t)strtoul(argv[++i], NULL, 10);
        } else if (!strcmp(a, "-h") || !strcmp(a, "--help")) {
            fprintf(stderr,
                "usage: fm_stream_test [options]\n"
                "  --bench            generation throughput only "
                                     "(no radio, no DMA)\n"
                "  --no-convert       bench without the float->int12 "
                                     "pack step\n"
                "  --auto-scale       pack with peak-finding auto-scale "
                                     "(2 passes) not a fixed 2047\n"
                "  --probe            dump raw FPGA pointers "
                                     "while feeding\n"
                "  --verify           check -g generator against libm, "
                                     "error in DAC LSB\n"
                "  -g libm|nco|neon   sin/cos implementation "
                                     "(default libm)\n"
                "  -r HZ              DAC sample rate (default %u)\n"
                "  -d SEC             duration (default %d)\n"
                "  -w MS              max buffered depth, "
                                     "bounds TX latency (default %d)\n"
                "  -t HZ              audio tone (default %u)\n"
                "  -f MHZ             carrier (default %.1f)\n"
                "  -a DB              TX attenuation (default %.1f)\n"
                "  --audio-rate HZ    (default %u)\n"
                "  --deviation HZ     (default %u)\n"
                "  --no-preemph       skip 75 us pre-emphasis\n"
                "  -v                 per-second progress\n",
                DEFAULT_DAC_RATE, DEFAULT_DURATION_SEC,
                DEFAULT_HIGH_WATER_MS, DEFAULT_TONE_HZ,
                DEFAULT_FREQ_MHZ, DEFAULT_TX_ATTEN,
                DEFAULT_AUDIO_RATE, DEFAULT_DEVIATION);
            return 0;
        } else {
            fprintf(stderr, "unknown argument '%s' (try --help)\n", a);
            return 2;
        }
    }

    if (dac_rate == 0 || audio_rate == 0 || duration_sec <= 0) {
        fprintf(stderr, "invalid rate or duration\n");
        return 2;
    }
    if (tone_hz * 2 > audio_rate) {
        fprintf(stderr, "tone %u Hz exceeds Nyquist for %u Hz audio\n",
                tone_hz, audio_rate);
        return 2;
    }

    const char *gen_name = (kind == FM_GEN_NEON) ? "neon"
                         : (kind == FM_GEN_NCO)  ? "nco" : "libm";

    float *tone = make_tone(audio_rate, tone_hz, 0.8f, preemph);
    if (!tone) {
        fprintf(stderr, "tone alloc failed\n");
        return 1;
    }

    fm_mod_lut_init();

    tone_gen_t tg = { .audio = tone, .n_audio = audio_rate };
    fm_mod_init(&tg.mod, audio_rate, deviation, dac_rate, kind);

    if (verify) {
        int rc = run_verify(&tg, gen_name);
        free(tone);
        return rc;
    }

    if (probe) {
        if (hal_init() != 0) {
            fprintf(stderr, "hal_init failed\n");
            free(tone);
            return 1;
        }
        hal_ad9361_set_sample_rate(dac_rate);
        hal_ad9361_set_tx_attenuation(tx_atten);
        usleep(100000);
        int rc = run_probe(&tg, dac_rate, duration_sec, fixed_scale);
        hal_cleanup();
        free(tone);
        return rc;
    }

    if (bench) {
        int rc = run_bench(&tg, dac_rate, duration_sec, gen_name,
                           bench_convert, fixed_scale);
        free(tone);
        return rc;
    }

    /* ---- streaming TX ---- */

    if (hal_init() != 0) {
        fprintf(stderr, "hal_init failed\n");
        free(tone);
        return 1;
    }

    uint64_t freq_hz = (uint64_t)(freq_mhz * 1e6);
    hal_ad9361_set_sample_rate(dac_rate);
    hal_ad9361_set_tx_lo(freq_hz);
    hal_ad9361_set_tx_bandwidth(dac_rate);
    hal_ad9361_set_tx_attenuation(tx_atten);
    usleep(100000);

    uint32_t high_water = (uint32_t)((uint64_t)high_water_ms
                                     * dac_rate / 1000ULL);
    if (high_water < DMA_TX_STREAM_CHUNK * 4)
        high_water = DMA_TX_STREAM_CHUNK * 4;
    if (high_water > DMA_TX_MAX_SAMPLES / 2)
        high_water = DMA_TX_MAX_SAMPLES / 2;

    fprintf(stderr, "fm_stream_test\n");
    fprintf(stderr, "  generator:   %s\n", gen_name);
    fprintf(stderr, "  carrier:     %.3f MHz  atten %.1f dB\n",
            freq_mhz, tx_atten);
    fprintf(stderr, "  dac rate:    %u SPS\n", dac_rate);
    fprintf(stderr, "  audio:       %u Hz tone, %u Hz rate, "
            "%u Hz deviation, preemph %s\n",
            tone_hz, audio_rate, deviation, preemph ? "on" : "off");
    fprintf(stderr, "  audio_step:  %.6f (%.3f DAC samples per audio "
            "sample)\n", tg.mod.audio_step, 1.0 / tg.mod.audio_step);
    fprintf(stderr, "  high water:  %u samples (%.1f ms)\n",
            high_water, (double)high_water * 1000.0 / (double)dac_rate);
    fprintf(stderr, "  duration:    %d s\n\n", duration_sec);

    if (dma_tx_stream_arm() != 0) {
        fprintf(stderr, "dma_tx_stream_arm failed\n");
        hal_cleanup();
        free(tone);
        return 1;
    }

    static float chunk_re[DMA_TX_STREAM_CHUNK];
    static float chunk_im[DMA_TX_STREAM_CHUNK];

    uint64_t total_fed = 0;
    uint64_t feed_cycles = 0, wait_cycles = 0;
    uint64_t min_depth = UINT64_MAX, max_depth = 0;
    uint64_t underruns = 0;
    bool     feed_error = false;

    /* ---- prime ----
     * Fill to the high-water mark BEFORE triggering the engine.  Two
     * reasons: the drain FSM must never start ahead of the write cursor,
     * and timing the measurement window from t=0 would otherwise charge
     * the fill ramp against the output rate. */
    double prime_t0 = now_sec();
    bool prime_timeout = false;
    while (total_fed + DMA_TX_STREAM_CHUNK <= high_water) {
        gen(&tg, chunk_re, chunk_im, DMA_TX_STREAM_CHUNK);
        if (stream_feed(chunk_re, chunk_im,
                        DMA_TX_STREAM_CHUNK, fixed_scale) != 0) {
            fprintf(stderr, "feed failed during prime\n");
            dma_tx_stream_stop();
            hal_cleanup();
            free(tone);
            return 1;
        }
        total_fed += DMA_TX_STREAM_CHUNK;
        feed_cycles++;
    }
    double prime_sec = now_sec() - prime_t0;
    fprintf(stderr, "  primed %llu samples (%.1f ms) in %.0f ms, depth=%d\n",
            (unsigned long long)total_fed,
            (double)total_fed * 1000.0 / (double)dac_rate,
            prime_sec * 1000.0, (int)dma_tx_stream_depth());

    if (dma_tx_stream_trigger() != 0) {
        fprintf(stderr, "dma_tx_stream_trigger failed\n");
        dma_tx_stream_stop();
        hal_cleanup();
        free(tone);
        return 1;
    }
    fprintf(stderr, "  triggered\n\n");


    /* ---- measure ----
     * Rate is measured over the steady-state window only, so the counters
     * are rebased here. */
    uint64_t fed_at_start = total_fed;
    uint64_t consumed_at_start = dma_tx_stream_emitted();

    double t0 = now_sec();
    double deadline = t0 + (double)duration_sec;
    double next_report = t0 + 1.0;
    uint64_t last_consumed = dma_tx_stream_emitted();
    double   last_report_t = t0;
    double   worst_window_frac = 1e9;

    while (1) {
        double now = now_sec();
        if (now >= deadline)
            break;

        /* Hardware depth: signed, so an underrun reads negative instead of
         * masquerading as a nearly-full ring. */
        int32_t hw_depth = dma_tx_stream_depth();
        if (hw_depth < 0) {
            underruns++;
            hw_depth = 0;
        }
        uint64_t depth = (uint64_t)hw_depth;
        if (depth < min_depth) min_depth = depth;
        if (depth > max_depth) max_depth = depth;

        if (verbose && now >= next_report) {
            double win = now - last_report_t;
            uint64_t emit_now = dma_tx_stream_emitted();
            uint64_t d = emit_now - last_consumed;
            double frac = (double)d / win / (double)dac_rate;
            if (frac < worst_window_frac)
                worst_window_frac = frac;
            fprintf(stderr, "  t=%5.1fs  out=%.3f MSPS (%.1f%% nominal)  "
                    "depth=%.0f ms  fed=%.1f Ms\n",
                    now - t0, (double)d / win / 1e6, frac * 100.0,
                    (double)depth * 1000.0 / (double)dac_rate,
                    (double)total_fed / 1e6);
            last_consumed = emit_now;
            last_report_t = now;
            next_report = now + 1.0;
        }

        /* Bounded latency: never run further ahead than high_water. */
        if (depth + DMA_TX_STREAM_CHUNK > high_water) {
            wait_cycles++;
            usleep(200);
            continue;
        }

        /* Space check is separate from the depth clamp: the clamp bounds
         * latency, this guards the ring. */
        if (dma_tx_stream_available() < DMA_TX_STREAM_CHUNK) {
            wait_cycles++;
            usleep(200);
            continue;
        }

        gen(&tg, chunk_re, chunk_im, DMA_TX_STREAM_CHUNK);

        if (stream_feed(chunk_re, chunk_im,
                        DMA_TX_STREAM_CHUNK, fixed_scale) != 0) {
            fprintf(stderr, "feed failed after %llu samples\n",
                    (unsigned long long)total_fed);
            feed_error = true;
            break;
        }

        total_fed += DMA_TX_STREAM_CHUNK;
        feed_cycles++;
    }

    double elapsed = now_sec() - t0;
    if (min_depth == UINT64_MAX)
        min_depth = 0;

    uint64_t window_fed      = total_fed - fed_at_start;
    uint64_t window_consumed = dma_tx_stream_emitted() - consumed_at_start;

    /* Drain only what is actually buffered — with a bounded depth this is
     * milliseconds, not the ~4 s a full ring would need. */
    int32_t resid_signed = dma_tx_stream_depth();
    uint64_t residual = resid_signed > 0 ? (uint64_t)resid_signed : 0;
    usleep((unsigned int)(residual * 1000000ULL / dac_rate) + 20000);
    dma_tx_stream_stop();

    double actual_rate = (double)window_consumed / elapsed;
    double rate_frac   = actual_rate / (double)dac_rate;
    double max_latency_ms = (double)max_depth * 1000.0 / (double)dac_rate;

    /*
     * Cross-check: over the measurement window the FPGA cannot legitimately
     * emit more than the ARM fed, give or take the change in buffer depth.
     * If it does, it is replaying stale ring contents — which shows up as a
     * flawless output rate while transmitting nothing.  Guard against
     * trusting the rate metric in that case.
     */
    double emit_excess = (double)window_consumed - (double)window_fed;
    double emit_excess_frac = (window_fed > 0)
        ? emit_excess / (double)window_fed : 1.0;
    bool feed_consistent = emit_excess_frac < 0.02;

    bool rate_ok    = rate_frac >= RATE_PASS_FRACTION;
    bool latency_ok = max_depth <= high_water;
    bool starved    = (underruns > 0);
    bool passed     = rate_ok && latency_ok && feed_consistent
                      && !starved && !feed_error && !prime_timeout;

    fprintf(stderr, "\n  elapsed:      %.2f s\n", elapsed);
    fprintf(stderr, "  fed:          %llu samples (%.3f MSPS)\n",
            (unsigned long long)window_fed,
            (double)window_fed / elapsed / 1e6);
    fprintf(stderr, "  DAC emitted:  %llu samples (%.3f MSPS)\n",
            (unsigned long long)window_consumed, actual_rate / 1e6);
    fprintf(stderr, "  output rate:  %.3f%% of nominal  [%s]\n",
            rate_frac * 100.0, rate_ok ? "ok" : "DEFICIT -> audio drags");
    fprintf(stderr, "  fed vs emit:  %+.2f%%  [%s]\n",
            emit_excess_frac * 100.0,
            feed_consistent ? "consistent"
                            : "EMITTING MORE THAN FED -> replaying stale ring");
    fprintf(stderr, "  depth:        min %.1f ms, max %.1f ms  [%s]\n",
            (double)min_depth * 1000.0 / (double)dac_rate, max_latency_ms,
            latency_ok ? "bounded" : "EXCEEDED HIGH WATER");
    if (starved)
        fprintf(stderr, "  UNDERRUNS:    %llu polls saw RD_PTR ahead of the "
                "write cursor\n", (unsigned long long)underruns);
    if (prime_timeout)
        fprintf(stderr, "  WARNING:      generation slower than DAC rate\n");
    fprintf(stderr, "  cycles:       %llu feed, %llu wait\n",
            (unsigned long long)feed_cycles,
            (unsigned long long)wait_cycles);
    if (verbose && worst_window_frac < 1e8)
        fprintf(stderr, "  worst 1s window: %.1f%% nominal\n",
                worst_window_frac * 100.0);
    fprintf(stderr, "\n  RESULT: %s\n", passed ? "PASS" : "FAIL");

    printf("{\"mode\":\"stream\",\"generator\":\"%s\",\"passed\":%s,"
           "\"nominal_sps\":%u,\"actual_sps\":%.0f,\"rate_frac\":%.5f,"
           "\"elapsed_sec\":%.3f,\"fed\":%llu,\"emitted\":%llu,"
           "\"emit_excess_frac\":%.5f,\"feed_consistent\":%s,"
           "\"feed_cycles\":%llu,\"wait_cycles\":%llu,"
           "\"prime_ms\":%.0f,\"prime_timeout\":%s,"
           "\"underrun_polls\":%llu,"
           "\"min_depth_ms\":%.2f,\"max_depth_ms\":%.2f,"
           "\"high_water_ms\":%.2f,\"starved\":%s,\"feed_error\":%s}\n",
           gen_name, passed ? "true" : "false",
           dac_rate, actual_rate, rate_frac, elapsed,
           (unsigned long long)window_fed,
           (unsigned long long)window_consumed,
           emit_excess_frac, feed_consistent ? "true" : "false",
           (unsigned long long)feed_cycles,
           (unsigned long long)wait_cycles,
           prime_sec * 1000.0, prime_timeout ? "true" : "false",
           (unsigned long long)underruns,
           (double)min_depth * 1000.0 / (double)dac_rate, max_latency_ms,
           (double)high_water * 1000.0 / (double)dac_rate,
           starved ? "true" : "false",
           feed_error ? "true" : "false");

    hal_cleanup();
    free(tone);
    return passed ? 0 : 1;
}
