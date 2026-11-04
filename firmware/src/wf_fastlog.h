#pragma once
#include <stdint.h>

/*
 * Fast log10 for a positive normal float, for the waterfall magnitude stage.
 *
 * Range-reduce x = 2^e * m with m in [1,2), then approximate log2(m) =
 * log2(1+f), f = m-1 in [0,1), with a degree-6 Chebyshev fit and scale by
 * log10(2).  No libm call and no division.
 *
 * The fit's error depends only on the mantissa, not the exponent, so the
 * worst case is exactly bounded by an exhaustive sweep of all 2^23 mantissas
 * (firmware/tests/test_wf_fastlog.c): max ~4e-5 dB over the whole normal
 * range.  That is ~4 orders below the ADC noise floor and ~1/8000 of one
 * waterfall colour step at an 80 dB span / 256 levels.  Higher degrees buy
 * accuracy at ~2 cycles each; degree 6 is already far past what is visible.
 *
 * Inputs must be > 0 (the magnitude path floors at 1e-18 first).
 */
static inline float wf_log10_fast(float x)
{
    union { float f; uint32_t u; } v = { x };

    int e = (int)((v.u >> 23) & 0xFFu) - 127;
    v.u = (v.u & 0x007FFFFFu) | 0x3F800000u;  /* mantissa in [1,2) */
    float f = v.f - 1.0f;

    float p = -2.4568534745e-02f;
    p = p * f + 1.1761308407e-01f;
    p = p * f - 2.7269756485e-01f;
    p = p * f + 4.5450849220e-01f;
    p = p * f - 7.1731278026e-01f;
    p = p * f + 1.4424535262e+00f;
    p = p * f + 2.4434387204e-06f;

    return ((float)e + p) * 0.30102999566398120f;
}
