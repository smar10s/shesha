/*
 * Capture the already-configured RX stream and report the magnitude of one
 * complex tone.  Unlike the other tools this deliberately does NOT call
 * radio_init(): the point is to observe the radio exactly as the daemon
 * left it, without a sample-clock or LO change that could hide a held DAC
 * word.
 *
 * Usage: rx_probe <offset_hz> <sample_rate_hz> [samples]
 */
#include "dma_rx.h"
#include "hal.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <offset_hz> <sample_rate_hz> [samples]\n",
                argv[0]);
        return 2;
    }

    double offset_hz = strtod(argv[1], NULL);
    double sample_rate_hz = strtod(argv[2], NULL);
    size_t n = (argc > 3) ? (size_t)strtoul(argv[3], NULL, 0) : 16384;

    if (sample_rate_hz <= 0.0 || n == 0 || n > DMA_RX_BUF_SAMPLES) {
        fprintf(stderr, "rx_probe: invalid offset/rate/count\n");
        return 2;
    }

    if (hal_init() != 0) {
        fprintf(stderr, "rx_probe: hal_init failed\n");
        return 1;
    }

    float *real = malloc(n * sizeof(*real));
    float *imag = malloc(n * sizeof(*imag));
    if (!real || !imag) {
        fprintf(stderr, "rx_probe: allocation failed\n");
        free(real);
        free(imag);
        return 1;
    }

    if (dma_rx_start() != 0) {
        fprintf(stderr, "rx_probe: RX DMA start failed\n");
        free(real);
        free(imag);
        return 1;
    }

    if (dma_rx_capture(n, real, imag, 2000) != (int)n) {
        fprintf(stderr, "rx_probe: capture failed\n");
        free(real);
        free(imag);
        return 1;
    }

    double w = 2.0 * M_PI * offset_hz / sample_rate_hz;
    double acc_r = 0.0, acc_i = 0.0;
    for (size_t k = 0; k < n; k++) {
        double c = cos(w * (double)k);
        double s = sin(w * (double)k);
        acc_r += (double)real[k] * c + (double)imag[k] * s;
        acc_i += (double)imag[k] * c - (double)real[k] * s;
    }

    double mag = sqrt(acc_r * acc_r + acc_i * acc_i) / (double)n;
    printf("%.6f\n", mag);

    free(real);
    free(imag);
    return 0;
}
