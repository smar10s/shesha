/*
 * Microbenchmark for the FM RX decimator FIR inner loop, on the real target.
 *
 * Exists because guessing was expensive: each guess costs a cross-compile,
 * an scp, a daemon restart and a 30 s measurement, and the first guess
 * (four accumulators to break a presumed vmla latency chain) made things
 * 1.9 points *worse*. This measures every variant in one run so the choice is
 * made from numbers.
 *
 * Cross-compile with exactly the daemon's flags and run on the Pluto:
 *   docker run --rm -v $PWD:/src:ro -v /tmp:/out styx-firmware \
 *     arm-linux-gnueabihf-gcc -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard \
 *     -O3 -funsafe-math-optimizations -o /out/bench_fir /src/firmware/tests/bench_fir.c -lm
 *
 * Reports ns per output sample and the implied % of one core for the real
 * 6x4x2 schedule at 2.1 MSPS.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAXT 81

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

/* --- variant A: single accumulator, runtime tap count, descending walk --- */
static float fir_a(const float *h, const float *hist, int w, int n)
{
    const float *newest = &hist[w + n];
    float acc = 0.0f;
    for (int j = 0; j < n; j++)
        acc += h[j] * newest[-j];
    return acc;
}

/* --- variant B: four accumulators, runtime tap count, ascending --- */
static float fir_b(const float *h, const float *hist, int w, int n)
{
    const float *xs = &hist[w + 1];
    float a0 = 0, a1 = 0, a2 = 0, a3 = 0;
    int j = 0;
    for (; j + 4 <= n; j += 4) {
        a0 += h[j + 0] * xs[j + 0];
        a1 += h[j + 1] * xs[j + 1];
        a2 += h[j + 2] * xs[j + 2];
        a3 += h[j + 3] * xs[j + 3];
    }
    float acc = (a0 + a1) + (a2 + a3);
    for (; j < n; j++)
        acc += h[j] * xs[j];
    return acc;
}

/* --- variant C: single accumulator, COMPILE-TIME tap count, ascending --- */
#define DEF_C(NAME, N)                                          \
static float NAME(const float *h, const float *hist, int w)     \
{                                                               \
    const float *xs = &hist[w + 1];                             \
    float acc = 0.0f;                                           \
    for (int j = 0; j < (N); j++)                               \
        acc += h[j] * xs[j];                                    \
    return acc;                                                 \
}
DEF_C(fir_c33, 33)
DEF_C(fir_c23, 23)
DEF_C(fir_c81, 81)

/* --- variant D: four accumulators, COMPILE-TIME tap count, ascending --- */
#define DEF_D(NAME, N)                                          \
static float NAME(const float *h, const float *hist, int w)     \
{                                                               \
    const float *xs = &hist[w + 1];                             \
    float a0 = 0, a1 = 0, a2 = 0, a3 = 0;                       \
    int j = 0;                                                  \
    for (; j + 4 <= (N); j += 4) {                              \
        a0 += h[j + 0] * xs[j + 0];                             \
        a1 += h[j + 1] * xs[j + 1];                             \
        a2 += h[j + 2] * xs[j + 2];                             \
        a3 += h[j + 3] * xs[j + 3];                             \
    }                                                           \
    float acc = (a0 + a1) + (a2 + a3);                          \
    for (; j < (N); j++)                                        \
        acc += h[j] * xs[j];                                    \
    return acc;                                                 \
}
DEF_D(fir_d33, 33)
DEF_D(fir_d23, 23)
DEF_D(fir_d81, 81)

/* --- variant E: symmetric folding, COMPILE-TIME tap count ---
 * Halves the multiplies. On a machine with fused multiply-add this should be
 * a wash (the pair-sum adds replace the saved multiplies), which is worth
 * confirming rather than assuming. */
#define DEF_E(NAME, N)                                          \
static float NAME(const float *h, const float *hist, int w)     \
{                                                               \
    const float *xs = &hist[w + 1];                             \
    const int half = (N) / 2;                                   \
    float acc = h[half] * xs[half];                             \
    for (int j = 0; j < half; j++)                              \
        acc += h[j] * (xs[j] + xs[(N) - 1 - j]);                \
    return acc;                                                 \
}
DEF_E(fir_e33, 33)
DEF_E(fir_e23, 23)
DEF_E(fir_e81, 81)

/* --- variant F: four accumulators with uint16_t loop counters, the old
 * deployed shape. This is variant D plus a uxth per iteration, kept to show
 * that the 16-bit arithmetic, not the accumulator count, was what made the
 * old version slower than the original. --- */
#define DEF_F(NAME, N)                                          \
static float NAME(const float *h, const float *hist, int w)     \
{                                                               \
    const float *xs = &hist[w + 1];                             \
    float a0 = 0, a1 = 0, a2 = 0, a3 = 0;                       \
    uint16_t j = 0;                                             \
    for (; (uint16_t)(j + 4u) <= (N); j += 4u) {                \
        a0 += h[j + 0u] * xs[j + 0u];                           \
        a1 += h[j + 1u] * xs[j + 1u];                           \
        a2 += h[j + 2u] * xs[j + 2u];                           \
        a3 += h[j + 3u] * xs[j + 3u];                           \
    }                                                           \
    float acc = (a0 + a1) + (a2 + a3);                          \
    for (; j < (N); j++)                                        \
        acc += h[j] * xs[j];                                    \
    return acc;                                                 \
}
DEF_F(fir_f33, 33)
DEF_F(fir_f23, 23)
DEF_F(fir_f81, 81)

/* --- variant G: eight accumulators, to see whether four is the plateau --- */
#define DEF_G(NAME, N)                                          \
static float NAME(const float *h, const float *hist, int w)     \
{                                                               \
    const float *xs = &hist[w + 1];                             \
    float a[8] = {0,0,0,0,0,0,0,0};                             \
    int j = 0;                                                  \
    for (; j + 8 <= (N); j += 8)                                \
        for (int k = 0; k < 8; k++)                             \
            a[k] += h[j + k] * xs[j + k];                       \
    float acc = ((a[0]+a[1])+(a[2]+a[3]))+((a[4]+a[5])+(a[6]+a[7])); \
    for (; j < (N); j++)                                        \
        acc += h[j] * xs[j];                                    \
    return acc;                                                 \
}
DEF_G(fir_g33, 33)
DEF_G(fir_g23, 23)
DEF_G(fir_g81, 81)

static float h_[MAXT], hist_[2 * MAXT];
static volatile float sink;

#define ITERS 400000

static double bench_rt(float (*fn)(const float *, const float *, int, int),
                       int n)
{
    double t0 = now_s();
    for (int i = 0; i < ITERS; i++)
        sink = fn(h_, hist_, i % n, n);
    return (now_s() - t0) / ITERS * 1e9;
}

static double bench_ct(float (*fn)(const float *, const float *, int), int n)
{
    double t0 = now_s();
    for (int i = 0; i < ITERS; i++)
        sink = fn(h_, hist_, i % n);
    return (now_s() - t0) / ITERS * 1e9;
}

/* Output rates of the three stages in the deployed 6x4x2 schedule. */
static const double RATE[3] = { 350000.0, 87500.0, 43750.0 };
static const int    TAPS[3] = { 33, 23, 81 };

static void report(const char *name, const double ns[3])
{
    double pct = 0.0;
    for (int k = 0; k < 3; k++)
        pct += ns[k] * 1e-9 * RATE[k] * 100.0;
    printf("  %-34s %7.1f %7.1f %7.1f   %6.1f%%\n",
           name, ns[0], ns[1], ns[2], pct);
}

int main(void)
{
    for (int i = 0; i < MAXT; i++)
        h_[i] = 0.01f * (float)(i + 1);
    for (int i = 0; i < 2 * MAXT; i++)
        hist_[i] = 0.5f - 0.001f * (float)i;

    printf("FIR inner loop, ns per output sample, %d iterations\n", ITERS);
    printf("  %-34s %7s %7s %7s   %7s\n", "variant",
           "33tap", "23tap", "81tap", "core");

    double ns[3];

    ns[0] = bench_rt(fir_a, 33);
    ns[1] = bench_rt(fir_a, 23);
    ns[2] = bench_rt(fir_a, 81);
    report("A single acc, runtime n, desc", ns);

    ns[0] = bench_rt(fir_b, 33);
    ns[1] = bench_rt(fir_b, 23);
    ns[2] = bench_rt(fir_b, 81);
    report("B four acc,   runtime n, asc", ns);

    ns[0] = bench_ct(fir_c33, 33);
    ns[1] = bench_ct(fir_c23, 23);
    ns[2] = bench_ct(fir_c81, 81);
    report("C single acc, const n,   asc", ns);

    ns[0] = bench_ct(fir_d33, 33);
    ns[1] = bench_ct(fir_d23, 23);
    ns[2] = bench_ct(fir_d81, 81);
    report("D four acc,   const n,   asc", ns);

    ns[0] = bench_ct(fir_e33, 33);
    ns[1] = bench_ct(fir_e23, 23);
    ns[2] = bench_ct(fir_e81, 81);
    report("E symmetric,  const n,   asc", ns);

    ns[0] = bench_ct(fir_f33, 33);
    ns[1] = bench_ct(fir_f23, 23);
    ns[2] = bench_ct(fir_f81, 81);
    report("F four acc, uint16_t j", ns);

    ns[0] = bench_ct(fir_g33, 33);
    ns[1] = bench_ct(fir_g23, 23);
    ns[2] = bench_ct(fir_g81, 81);
    report("G eight acc,  const n,   asc", ns);

    printf("\n  'core' is the sum over the three stages of ns * that stage's\n");
    printf("  output rate, i.e. the whole decimator as a share of one core.\n");
    printf("  It excludes the per-input-sample push (two stores at 2.1 MSPS)\n");
    printf("  and the discriminator, which are the same for every variant.\n");
    printf("  sink=%g\n", (double)sink);
    return 0;
}
