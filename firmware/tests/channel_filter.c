#include "channel_filter.h"

#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

int channel_filter_design(float *taps, int n_taps, double cutoff_hz, double fs_hz)
{
    if (n_taps < 3 || (n_taps & 1) == 0)
        return -1;
    if (fs_hz <= 0.0 || cutoff_hz <= 0.0 || cutoff_hz >= fs_hz * 0.5)
        return -1;

    const double fc = cutoff_hz / fs_hz;   /* cycles/sample */
    const int m = n_taps - 1;
    double sum = 0.0;

    for (int i = 0; i < n_taps; i++) {
        const int k = i - m / 2;
        double sinc;
        if (k == 0)
            sinc = 2.0 * fc;
        else
            sinc = sin(2.0 * M_PI * fc * (double)k) / (M_PI * (double)k);

        /* Blackman window: ~74 dB sidelobes, one octave wider transition
         * than Hamming. The extra width keeps the stopband well clear of the
         * adjacent channel at 500 kHz. */
        const double w = 0.42
                       - 0.5 * cos(2.0 * M_PI * (double)i / (double)m)
                       + 0.08 * cos(4.0 * M_PI * (double)i / (double)m);

        double v = sinc * w;
        taps[i] = (float)v;
        sum += v;
    }

    for (int i = 0; i < n_taps; i++)
        taps[i] = (float)((double)taps[i] / sum);

    return 0;
}

double channel_filter_response(const float *taps, int n_taps,
                               double f_hz, double fs_hz)
{
    double re = 0.0, im = 0.0;
    const double w = 2.0 * M_PI * f_hz / fs_hz;
    for (int i = 0; i < n_taps; i++) {
        re += (double)taps[i] * cos(w * (double)i);
        im -= (double)taps[i] * sin(w * (double)i);
    }
    return sqrt(re * re + im * im);
}
