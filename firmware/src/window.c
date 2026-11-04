#include "window.h"
#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

float window_generate(window_type_t type, size_t n, float *out)
{
    double sum = 0.0;
    for (size_t i = 0; i < n; i++) {
        double x;
        switch (type) {
        case WINDOW_HANN:
            x = 0.5 * (1.0 - cos(2.0 * M_PI * i / (n - 1)));
            break;
        case WINDOW_BLACKMAN_HARRIS:
            x = 0.35875
              - 0.48829 * cos(2.0 * M_PI * i / (n - 1))
              + 0.14128 * cos(4.0 * M_PI * i / (n - 1))
              - 0.01168 * cos(6.0 * M_PI * i / (n - 1));
            break;
        case WINDOW_FLAT_TOP:
            x = 0.21557895
              - 0.41663158 * cos(2.0 * M_PI * i / (n - 1))
              + 0.27726316 * cos(4.0 * M_PI * i / (n - 1))
              - 0.08357895 * cos(6.0 * M_PI * i / (n - 1))
              + 0.00694737 * cos(8.0 * M_PI * i / (n - 1));
            break;
        case WINDOW_RECT:
        default:
            x = 1.0;
            break;
        }
        out[i] = (float)x;
        sum += x;
    }
    return (float)(sum / n);
}

void window_apply(const float *win, size_t n, float *real, float *imag)
{
    for (size_t i = 0; i < n; i++) {
        real[i] *= win[i];
        imag[i] *= win[i];
    }
}
