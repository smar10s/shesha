#include "fir.h"
#include <stdio.h>
#include <string.h>

int fir_init(fir_t *f, const float *taps, uint16_t n, uint16_t dec)
{
    if (!f || !taps || n == 0 || n > FIR_MAX_TAPS || dec == 0) {
        fprintf(stderr, "fir: invalid n=%u dec=%u (max taps %u)\n",
                (unsigned)n, (unsigned)dec, (unsigned)FIR_MAX_TAPS);
        return -1;
    }
    for (uint16_t i = 0; i < n / 2u; i++) {
        if (taps[i] != taps[n - 1u - i]) {
            fprintf(stderr, "fir: taps not symmetric at %u\n", (unsigned)i);
            return -1;
        }
    }
    f->taps = taps;
    f->n_taps = n;
    f->decim = dec;
    f->phase = 0;
    f->wr = 0;
    memset(f->hist, 0, sizeof(f->hist));
    return 0;
}

__attribute__((noinline))
float fir_dot(const float *h, const float *xs, int n)
{
    int half = n / 2;
    float acc = 0.0f;
    if (n & 1)
        acc = h[half] * xs[half];
    for (int j = 0; j < half; j++)
        acc += h[j] * (xs[j] + xs[n - 1 - j]);
    return acc;
}
