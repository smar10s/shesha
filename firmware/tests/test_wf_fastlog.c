/*
 * Exhaustive accuracy test for wf_log10_fast (firmware/src/wf_fastlog.h).
 *
 * A fast log2 is `exponent + poly(mantissa)`, so its error is periodic in the
 * mantissa and independent of the exponent.  That makes the worst case
 * exactly computable: sweep every one of the 2^23 mantissas once and the
 * bound holds for the entire normal-float range.  Repeated at several
 * exponents to demonstrate the independence.
 *
 * Reports the error against float log10f (isolates the approximation) and
 * against double log10 (total error), in dB as the magnitude stage would see
 * it (10*log10).
 */
#include "wf_fastlog.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>

#define MANTISSA  0x800000u  /* 2^23 */

static int sweep(int e, double *max_f, double *max_d, double *sumsq, size_t *n,
                 double *baseline)
{
    int exp_field = (e + 127) << 23;
    for (uint32_t m = 0; m < MANTISSA; m++) {
        union { uint32_t u; float f; } v;
        v.u = (uint32_t)exp_field | m;
        float x = v.f;
        float fast = wf_log10_fast(x);
        float lf = log10f(x);
        double ld = log10((double)x);

        double ef = fabs((double)fast - (double)lf) * 10.0;  /* fast vs float exact */
        double ed = fabs((double)fast - ld) * 10.0;          /* fast vs double */
        double eb = fabs((double)lf - ld) * 10.0;            /* float exact vs double */

        if (ef > *max_f) *max_f = ef;
        if (ed > *max_d) *max_d = ed;
        if (eb > *baseline) *baseline = eb;
        *sumsq += ed * ed;
        (*n)++;
    }
    return 0;
}

int main(void)
{
    const int exps[] = { -60, -40, -20, 0, 2 };
    double worst_total = 0.0, worst_approx = 0.0, worst_baseline = 0.0;
    double all_sumsq = 0.0;
    size_t all_n = 0;

    printf("test_wf_fastlog — exhaustive mantissa sweep (2^23 per exponent)\n");
    for (size_t k = 0; k < sizeof(exps) / sizeof(exps[0]); k++) {
        double mf = 0, md = 0, ss = 0, base = 0;
        size_t n = 0;
        sweep(exps[k], &mf, &md, &ss, &n, &base);
        double rms = sqrt(ss / (double)n);
        printf("  e=%+3d  fast-vs-float max %.3e dB   fast-vs-double max %.3e "
               "rms %.3e   float-vs-double max %.3e\n",
               exps[k], mf, md, rms, base);
        if (md > worst_total) worst_total = md;
        if (mf > worst_approx) worst_approx = mf;
        if (base > worst_baseline) worst_baseline = base;
        all_sumsq += ss;
        all_n += n;
    }

    double all_rms = sqrt(all_sumsq / (double)all_n);
    printf("  worst total %.3e dB   worst approx %.3e dB   float baseline "
           "%.3e dB   overall rms %.3e dB\n",
           worst_total, worst_approx, worst_baseline, all_rms);

    if (worst_total > 1e-4) {
        printf("FAIL: fast log10 exceeds 1e-4 dB (%.3e)\n", worst_total);
        return 1;
    }
    printf("all checks passed\n");
    return 0;
}
