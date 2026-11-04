/*
 * Host test for wf_magnitude_db (firmware/src/wf_mag.h).
 *
 * The waterfall's magnitude/dB stage is the dominant cost in the PSD
 * producer, so it is a target for micro-optimisation.  This pins its
 * behaviour against a literal copy of the pre-optimisation loop so a
 * faster variant can be checked for equivalence, and separately checks
 * the fftshift mapping (which bin lands at which output index).
 *
 * Pure computation: built by `make test-firmware`, no hardware, and the
 * host's libm, not the target's.
 */
#include "wf_mag.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static const float FLOOR_DB = 10.0f * -18.0f;  /* 10*log10(1e-18) */

/* The loop as it was before optimisation: sqrtf, 20*log10, and a per-bin
 * modulo for the fftshift. */
static void reference(const float *re, const float *im, size_t n, float norm,
                      float *out)
{
    size_t half = n / 2;
    for (size_t i = 0; i < n; i++) {
        size_t src = (i + half) % n;
        float r = re[src];
        float m = im[src];
        float mag = sqrtf(r * r + m * m) * norm;
        if (mag < 1e-9f)
            mag = 1e-9f;
        out[i] = 20.0f * log10f(mag);
    }
}

static uint64_t rng = 12345;
static float frand(void)
{
    rng = rng * 6364136223846793005ULL + 1442695040888963407ULL;
    return (float)((rng >> 33) & 0xFFFFFF) / 0x7FFFFF - 1.0f;
}

static int test_values(void)
{
    const size_t sizes[] = { 8, 64, 256, 1024 };
    int fails = 0;
    for (size_t s = 0; s < sizeof(sizes) / sizeof(sizes[0]); s++) {
        size_t n = sizes[s];
        float *re = malloc(n * sizeof(float));
        float *im = malloc(n * sizeof(float));
        float *got = malloc(n * sizeof(float));
        float *ref = malloc(n * sizeof(float));
        float cg = 0.5f;  /* any coherent gain */
        float norm = 1.0f / ((float)n * cg);
        for (size_t i = 0; i < n; i++) {
            re[i] = frand();
            im[i] = frand();
        }
        wf_magnitude_db(re, im, n, norm, got);
        reference(re, im, n, norm, ref);
        double maxdiff = 0.0;
        for (size_t i = 0; i < n; i++) {
            double d = fabs((double)got[i] - (double)ref[i]);
            if (d > maxdiff)
                maxdiff = d;
        }
        printf("  bins-vs-libm n=%-5zu maxdiff=%.3g dB\n", n, maxdiff);
        if (maxdiff > 1e-4)
            fails++;
        free(re); free(im); free(got); free(ref);
    }
    return fails;
}

static size_t argmax(const float *a, size_t n)
{
    size_t best = 0;
    for (size_t i = 1; i < n; i++)
        if (a[i] > a[best])
            best = i;
    return best;
}

static int test_shift(void)
{
    /* A spike at FFT bin k must land at output index (k - n/2 + n) % n. */
    const size_t n = 8, half = n / 2;
    int fails = 0;
    for (size_t k = 0; k < n; k++) {
        float re[8] = { 0 }, im[8] = { 0 }, out[8];
        im[k] = 1.0f;
        wf_magnitude_db(re, im, n, 1.0f / (float)n, out);
        size_t expect = (k + n - half) % n;
        size_t got = argmax(out, n);
        printf("  shift  bin %zu -> out %zu (want %zu)\n", k, got, expect);
        if (got != expect)
            fails++;
    }
    return fails;
}

static int test_zero(void)
{
    float re[16] = { 0 }, im[16] = { 0 }, out[16];
    wf_magnitude_db(re, im, 16, 1.0f / 16.0f, out);
    int fails = 0;
    for (int i = 0; i < 16; i++) {
        if (fabsf(out[i] - FLOOR_DB) > 1e-3f) {
            printf("  zero  out[%d]=%.3f, want %.3f\n", i, out[i], FLOOR_DB);
            fails++;
        }
    }
    if (!fails)
        printf("  zero  all bins at floor (%.1f dB)\n", FLOOR_DB);
    return fails;
}

/*
 * The producer/drain split: wf_magnitude_power + wf_power_to_db must equal
 * the fused wf_magnitude_db, and the accumulation modes must behave in the
 * power domain.  These pin the contract the daemon relies on, since the
 * daemon no longer calls the fused form at all.
 */
static int test_split_matches_fused(void)
{
    const size_t sizes[] = { 8, 64, 256, 1024 };
    int fails = 0;
    for (size_t s = 0; s < sizeof(sizes) / sizeof(sizes[0]); s++) {
        size_t n = sizes[s];
        float *re = malloc(n * sizeof(float));
        float *im = malloc(n * sizeof(float));
        float *fused = malloc(n * sizeof(float));
        float *split = malloc(n * sizeof(float));
        float norm = 1.0f / ((float)n * 0.5f);
        for (size_t i = 0; i < n; i++) {
            re[i] = frand();
            im[i] = frand();
        }
        wf_magnitude_db(re, im, n, norm, fused);
        wf_magnitude_power(re, im, n, norm, split);
        wf_power_to_db(split, n, split);
        double maxdiff = 0.0;
        for (size_t i = 0; i < n; i++) {
            double d = fabs((double)fused[i] - (double)split[i]);
            if (d > maxdiff)
                maxdiff = d;
        }
        printf("  split-vs-fused n=%-5zu maxdiff=%.3g dB\n", n, maxdiff);
        if (maxdiff != 0.0)   /* same ops in the same order: must be exact */
            fails++;
        free(re); free(im); free(fused); free(split);
    }
    return fails;
}

/*
 * ACCUM_MAX is domain-independent (log is monotonic), so peak-hold in power
 * must equal peak-hold in dB.  This is what lets the drain reduce in power
 * without changing peak-hold behaviour.
 */
static int test_accum_max_domain_free(void)
{
    const size_t n = 256;
    const int frames = 23;
    float *pmax = malloc(n * sizeof(float));
    float *dmax = malloc(n * sizeof(float));
    float *p = malloc(n * sizeof(float));
    float *d = malloc(n * sizeof(float));
    for (size_t i = 0; i < n; i++) {
        pmax[i] = 0.0f;
        dmax[i] = -1e30f;
    }
    for (int f = 0; f < frames; f++) {
        for (size_t i = 0; i < n; i++) {
            float u = (frand() + 1.0f) * 0.5f;   /* (0,1] */
            if (u < 1e-9f) u = 1e-9f;
            p[i] = -logf(u);                     /* exponential power */
        }
        wf_power_to_db(p, n, d);
        for (size_t i = 0; i < n; i++) {
            if (p[i] > pmax[i]) pmax[i] = p[i];
            if (d[i] > dmax[i]) dmax[i] = d[i];
        }
    }
    wf_power_to_db(pmax, n, pmax);   /* dB of the power-domain max */
    double maxdiff = 0.0;
    for (size_t i = 0; i < n; i++) {
        double diff = fabs((double)pmax[i] - (double)dmax[i]);
        if (diff > maxdiff)
            maxdiff = diff;
    }
    printf("  accum_max power-vs-dB maxdiff=%.3g dB\n", maxdiff);
    free(pmax); free(dmax); free(p); free(d);
    return maxdiff > 1e-4 ? 1 : 0;
}

/*
 * ACCUM_AVG is NOT domain-independent, and this is the bug the split fixes.
 * A mean of dB values is a geometric mean of powers, which reads low.  Assert
 * that power-domain averaging matches the true power mean, and that the old
 * dB-domain averaging is measurably biased -- so a regression to the old
 * behaviour fails here rather than silently shifting the noise floor.
 */
static int test_accum_avg_needs_power_domain(void)
{
    const size_t n = 4096;
    const int frames = 23;
    double *truth = malloc(n * sizeof(double));
    float *psum = malloc(n * sizeof(float));
    float *dsum = malloc(n * sizeof(float));
    float *p = malloc(n * sizeof(float));
    float *d = malloc(n * sizeof(float));
    for (size_t i = 0; i < n; i++) {
        truth[i] = 0.0;
        psum[i] = 0.0f;
        dsum[i] = 0.0f;
    }
    for (int f = 0; f < frames; f++) {
        for (size_t i = 0; i < n; i++) {
            float u = (frand() + 1.0f) * 0.5f;
            if (u < 1e-9f) u = 1e-9f;
            p[i] = -logf(u);
        }
        wf_power_to_db(p, n, d);
        for (size_t i = 0; i < n; i++) {
            truth[i] += (double)p[i];
            psum[i] += p[i];
            dsum[i] += d[i];
        }
    }

    double worst_power = 0.0, bias_db = 0.0;
    for (size_t i = 0; i < n; i++) {
        float mean_power = psum[i] / (float)frames;
        float got;
        wf_power_to_db(&mean_power, 1, &got);
        double want = 10.0 * log10(truth[i] / frames);
        double err = fabs((double)got - want);
        if (err > worst_power)
            worst_power = err;
        bias_db += (dsum[i] / frames) - want;   /* old behaviour's error */
    }
    bias_db /= (double)n;

    printf("  accum_avg power-domain err=%.3g dB   dB-domain bias=%+.2f dB\n",
           worst_power, bias_db);
    free(truth); free(psum); free(dsum); free(p); free(d);

    int fails = 0;
    if (worst_power > 1e-3)         /* power averaging must be correct */
        fails++;
    if (bias_db > -1.0)             /* and dB averaging must be visibly wrong */
        fails++;
    return fails;
}

int main(void)
{
    printf("test_wf_mag\n");
    int fails = test_values() + test_shift() + test_zero()
              + test_split_matches_fused()
              + test_accum_max_domain_free()
              + test_accum_avg_needs_power_domain();
    if (fails) {
        printf("FAIL: %d checks failed\n", fails);
        return 1;
    }
    printf("all checks passed\n");
    return 0;
}
