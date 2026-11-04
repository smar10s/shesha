/*
 * Tests for the FM modulator (firmware/src/fm_mod.c).
 *
 * Two jobs:
 *
 *   1. Pin the disabled-path output.  Subcarriers (19 kHz pilot, 57 kHz RDS)
 *      are an addition, not a rewrite: with them off the IQ must be the same
 *      numbers as before they existed.  A handful of samples from a fixed
 *      input is enough, in the style of test_fm_rx.c's golden test.
 *   2. Prove the composite stays inside full deviation.  Recover the
 *      instantaneous modulation index from the phase difference and assert
 *      pilot + RDS + programme audio never exceeds 1.0.
 *
 * Re-baseline the golden with `./build/test_fm_mod --dump-golden`.
 *
 * Build and run:  make test-firmware
 */
#include "../src/fm_mod.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define AUDIO_RATE 48000
#define DAC_RATE   2100000
#define DEVIATION  75000
#define PHASE_STEP (2.0 * M_PI * (double)DEVIATION / (double)DAC_RATE)

static int failures;

#define CHECK(cond) do {                                                  \
    if (!(cond)) {                                                        \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
        failures++;                                                       \
    }                                                                     \
} while (0)

#define CHECK_NEAR(a, b, tol) do {                                        \
    double _d = (double)(a) - (double)(b);                                \
    if (!(_d < (tol) && _d > -(tol))) {                                   \
        printf("  FAIL %s:%d: %s=%.9g vs %.9g (tol %g)\n",                \
               __FILE__, __LINE__, #a, (double)(a), (double)(b),          \
               (double)(tol));                                            \
        failures++;                                                       \
    }                                                                     \
} while (0)

#define RUN(fn) do { printf("%s\n", #fn); fn(); } while (0)

#define N_OUT 4096
#define N_AUDIO 512

static void make_audio(float *audio)
{
    for (int i = 0; i < N_AUDIO; i++) {
        double t = (double)i / (double)AUDIO_RATE;
        audio[i] = (float)(0.7 * sin(2.0 * M_PI * 1000.0 * t)
                         + 0.2 * sin(2.0 * M_PI * 3000.0 * t));
    }
}

static void make_rds(rds_tx_config_t *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->enabled = true;
    cfg->pi = 0x8203;
    cfg->pty = 10;
    cfg->tp = true;
    memcpy(cfg->ps, "TESTFM  ", 8);
    memset(cfg->rt, ' ', 64);
    const char *m = "SHESHA RDS";
    memcpy(cfg->rt, m, strlen(m));
}

static void test_disabled_matches_golden(void)
{
    float audio[N_AUDIO];
    make_audio(audio);

    struct { size_t idx; double re, im; } gl[] = {
        {0,    1.0,                    0.0},
        {1,    0.99999964237213135,    0.00086120667401701212},
        {17,   0.99133157730102539,    0.13138368725776672},
        {255, -0.70183432102203369,   -0.71234023571014404},
        {256, -0.59399276971817017,   -0.80447036027908325},
        {1000, 0.84197998046875,       0.53950870037078857},
        {2048, 0.41766107082366943,    0.90860289335250854},
        {4095,-0.1529296487569809,    -0.98823708295822144},
    };
    struct { size_t idx; double re, im; } gn[] = {
        {0,    1.0,                    0.0},
        {1,    0.99999934434890747,    0.00086120638297870755},
        {17,   0.99133145809173584,    0.13138367235660553},
        {255, -0.70183467864990234,   -0.71233958005905151},
        {256, -0.59399241209030151,   -0.80447018146514893},
        {1000, 0.84198039770126343,    0.53950756788253784},
        {2048, 0.41766101121902466,    0.90860283374786377},
        {4095,-0.1529289036989212,    -0.98823708295822144},
    };

    float re[N_OUT], im[N_OUT];
    fm_mod_t m;
    fm_mod_lut_init();

    fm_mod_init(&m, AUDIO_RATE, DEVIATION, DAC_RATE, FM_GEN_LIBM);
    CHECK(fm_mod_generate(&m, audio, N_AUDIO, true, re, im, N_OUT) == N_OUT);
    for (unsigned k = 0; k < sizeof(gl) / sizeof(gl[0]); k++) {
        CHECK_NEAR(re[gl[k].idx], gl[k].re, 1e-6);
        CHECK_NEAR(im[gl[k].idx], gl[k].im, 1e-6);
    }

    fm_mod_init(&m, AUDIO_RATE, DEVIATION, DAC_RATE, FM_GEN_NCO);
    CHECK(fm_mod_generate(&m, audio, N_AUDIO, true, re, im, N_OUT) == N_OUT);
    for (unsigned k = 0; k < sizeof(gn) / sizeof(gn[0]); k++) {
        CHECK_NEAR(re[gn[k].idx], gn[k].re, 1e-6);
        CHECK_NEAR(im[gn[k].idx], gn[k].im, 1e-6);
    }
}

/* Peak |instantaneous modulation index| over the generated IQ. */
static double peak_mod_index(const float *re, const float *im, size_t n)
{
    double worst = 0.0;
    double prev = atan2((double)im[0], (double)re[0]);
    for (size_t i = 1; i < n; i++) {
        double ph = atan2((double)im[i], (double)re[i]);
        double d = ph - prev;
        while (d > M_PI) d -= 2.0 * M_PI;
        while (d < -M_PI) d += 2.0 * M_PI;
        prev = ph;
        double index = d / PHASE_STEP;
        double a = index < 0 ? -index : index;
        if (a > worst)
            worst = a;
    }
    return worst;
}

static void test_composite_peak_within_deviation(void)
{
    /* Programme audio arrives at full scale (the client normalises to 1.0
     * and no longer pre-scales for RDS).  The modulator must apply the
     * headroom itself when the subcarriers are on, so pilot (0.09) + RDS
     * (0.03) + programme (0.88) lands exactly on full deviation.  Without
     * that scaling this peaks at 1.12 and the <= 1.0 check fails. */
    float audio[N_AUDIO];
    for (int i = 0; i < N_AUDIO; i++)
        audio[i] = 1.0f;

    rds_tx_config_t cfg;
    make_rds(&cfg);

    float re[N_OUT], im[N_OUT];
    fm_mod_t m;
    fm_mod_lut_init();
    fm_mod_init(&m, AUDIO_RATE, DEVIATION, DAC_RATE, FM_GEN_LIBM);
    fm_mod_enable_subcarriers(&m, 0.09f, 0.03f);
    fm_mod_set_rds(&m, &cfg);

    CHECK(fm_mod_generate(&m, audio, N_AUDIO, true, re, im, N_OUT) == N_OUT);

    double peak = peak_mod_index(re, im, N_OUT);
    CHECK(peak <= 1.0 + 2e-3);
    /* The subcarriers are actually present: peak must reach near 1.0. */
    CHECK(peak > 0.97);
}

/* Goertzel power of a real signal at frequency f (Hz), sample rate fs. */
static double goertzel_power(const float *x, int n, double f, double fs)
{
    double w = 2.0 * M_PI * f / fs;
    double coeff = 2.0 * cos(w);
    double s0 = 0.0, s1 = 0.0, s2 = 0.0;
    for (int i = 0; i < n; i++) {
        s2 = s1; s1 = s0;
        s0 = (double)x[i] + coeff * s1 - s2;
    }
    return s0 * s0 + s1 * s1 - coeff * s0 * s1;
}

/*
 * The pilot must be a clean 19 kHz tone.  It is generated from the NCO
 * table now rather than a per-sample cosf() (which starved the TX thread),
 * so a wrong phase increment would silently move it off frequency.
 */
/* Hostile deviation and audio must not reach an out-of-range double→int32
 * conversion in the NCO path.  The values are absurd on purpose; the
 * modulator still has to produce a defined unit-magnitude phasor. */
static void test_hostile_inputs_are_defined(void)
{
    float audio[64];
    float re[256], im[256];
    fm_mod_t m;

    for (int i = 0; i < 64; i++)
        audio[i] = 1e30f;

    fm_mod_lut_init();
    fm_mod_init(&m, AUDIO_RATE, UINT32_MAX, DAC_RATE, FM_GEN_NCO);
    size_t n = fm_mod_generate(&m, audio, 64, false, re, im, 256);
    CHECK(n > 0);
    for (size_t i = 0; i < n; i++) {
        CHECK(isfinite(re[i]) && isfinite(im[i]));
        CHECK(fabsf(re[i]) <= 1.0f + 1e-6f);
        CHECK(fabsf(im[i]) <= 1.0f + 1e-6f);
    }

    /* A sane deviation still has to survive a hostile audio sample. */
    fm_mod_init(&m, AUDIO_RATE, DEVIATION, DAC_RATE, FM_GEN_NCO);
    n = fm_mod_generate(&m, audio, 64, false, re, im, 256);
    CHECK(n > 0);
    for (size_t i = 0; i < n; i++) {
        CHECK(isfinite(re[i]) && isfinite(im[i]));
        CHECK(fabsf(re[i]) <= 1.0f + 1e-6f);
        CHECK(fabsf(im[i]) <= 1.0f + 1e-6f);
    }
}

static void test_deviation_bound(void)
{
    CHECK(fm_mod_deviation_valid(DEVIATION, DAC_RATE));
    CHECK(fm_mod_deviation_valid(0, DAC_RATE));
    CHECK(!fm_mod_deviation_valid((uint32_t)DAC_RATE / 2, DAC_RATE));
    CHECK(!fm_mod_deviation_valid(UINT32_MAX, DAC_RATE));
    CHECK(!fm_mod_deviation_valid(DEVIATION, 0));
}

static void test_pilot_is_19khz(void)
{
    static float re[N_OUT], im[N_OUT], dev[N_OUT];
    float audio[N_AUDIO];
    for (int i = 0; i < N_AUDIO; i++)
        audio[i] = 0.0f;

    fm_mod_t m;
    fm_mod_init(&m, AUDIO_RATE, DEVIATION, DAC_RATE, FM_GEN_NCO);
    fm_mod_enable_subcarriers(&m, 0.09f, 0.0f);   /* pilot only */

    CHECK(fm_mod_generate(&m, audio, N_AUDIO, true, re, im, N_OUT) == N_OUT);

    /* Instantaneous frequency is the phase difference between samples. */
    for (int i = 0; i < N_OUT; i++) {
        double a = atan2((double)im[i], (double)re[i]);
        double b = atan2((double)im[(i + 1) % N_OUT],
                         (double)re[(i + 1) % N_OUT]);
        double d = b - a;
        while (d > M_PI) d -= 2.0 * M_PI;
        while (d < -M_PI) d += 2.0 * M_PI;
        dev[i] = (float)d;
    }

    double at19k = goertzel_power(dev, N_OUT, 19000.0, DAC_RATE);
    double at1k  = goertzel_power(dev, N_OUT, 1000.0, DAC_RATE);
    double at30k = goertzel_power(dev, N_OUT, 30000.0, DAC_RATE);
    CHECK(at19k > 100.0 * at1k);
    CHECK(at19k > 100.0 * at30k);
}

static void dump_golden(void)
{
    float audio[N_AUDIO];
    make_audio(audio);

    static const size_t idx[] = {0, 1, 17, 255, 256, 1000, 2048, 4095};
    float re[N_OUT], im[N_OUT];
    fm_mod_t m;
    fm_mod_lut_init();

    fm_mod_init(&m, AUDIO_RATE, DEVIATION, DAC_RATE, FM_GEN_LIBM);
    fm_mod_generate(&m, audio, N_AUDIO, true, re, im, N_OUT);
    printf("LIBM:\n");
    for (unsigned k = 0; k < sizeof(idx) / sizeof(idx[0]); k++)
        printf("  {%zu, %.17g, %.17g},\n", idx[k],
               (double)re[idx[k]], (double)im[idx[k]]);

    fm_mod_init(&m, AUDIO_RATE, DEVIATION, DAC_RATE, FM_GEN_NCO);
    fm_mod_generate(&m, audio, N_AUDIO, true, re, im, N_OUT);
    printf("NCO:\n");
    for (unsigned k = 0; k < sizeof(idx) / sizeof(idx[0]); k++)
        printf("  {%zu, %.17g, %.17g},\n", idx[k],
               (double)re[idx[k]], (double)im[idx[k]]);
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--dump-golden") == 0) {
        dump_golden();
        return 0;
    }

    RUN(test_disabled_matches_golden);
    RUN(test_composite_peak_within_deviation);
    RUN(test_pilot_is_19khz);
    RUN(test_hostile_inputs_are_defined);
    RUN(test_deviation_bound);

    if (failures) {
        printf("\n%d check(s) FAILED\n", failures);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
