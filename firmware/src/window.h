#pragma once
#include <stddef.h>

typedef enum {
    WINDOW_RECT = 0,
    WINDOW_HANN = 1,
    WINDOW_BLACKMAN_HARRIS = 2,
    WINDOW_FLAT_TOP = 3,
} window_type_t;

// Fills `out` with `n` window coefficients. Returns coherent gain (sum/n).
float window_generate(window_type_t type, size_t n, float *out);

// Apply window in-place to real/imag arrays
void window_apply(const float *win, size_t n, float *real, float *imag);
