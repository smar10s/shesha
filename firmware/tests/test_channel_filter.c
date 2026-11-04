/*
 * Guards the offline channel filter used by rds_offline to make an IQ-mode
 * capture equivalent to the live FM path.
 *
 * IQ mode does not enable the AD9361 RX FIR, so a raw 2.1 MSPS capture
 * carries adjacent-channel noise and the offline decoder fails 100% of block
 * CRCs (STATUS Next Step 1).  The live FM path has a +/-200 kHz filter in
 * hardware; this replicates it on the host.  The test pins the properties
 * that matter: unity DC gain, symmetry (fir_dot assumes it), passband across
 * the +/-100 kHz FM occupied bandwidth, and deep rejection of the adjacent
 * channel.
 */
#include "channel_filter.h"

#include <math.h>
#include <stdio.h>

#define NTAPS 129
#define FS_HZ 2100000.0
#define CUTOFF_HZ 200000.0

static int fail(const char *msg, double got)
{
    fprintf(stderr, "FAIL: %s (got %.6f)\n", msg, got);
    return 1;
}

int main(void)
{
    float h[NTAPS];
    if (channel_filter_design(h, NTAPS, CUTOFF_HZ, FS_HZ) != 0) {
        fprintf(stderr, "FAIL: design returned non-zero\n");
        return 1;
    }

    double sum = 0.0;
    for (int i = 0; i < NTAPS; i++)
        sum += h[i];
    if (fabs(sum - 1.0) > 1e-4)
        return fail("taps do not sum to unity", sum);

    for (int i = 0; i < NTAPS / 2; i++) {
        if (h[i] != h[NTAPS - 1 - i]) {
            fprintf(stderr, "FAIL: taps not symmetric at %d\n", i);
            return 1;
        }
    }

    double dc = channel_filter_response(h, NTAPS, 0.0, FS_HZ);
    double pass = channel_filter_response(h, NTAPS, 100e3, FS_HZ);
    double stop500 = channel_filter_response(h, NTAPS, 500e3, FS_HZ);
    double stop1m = channel_filter_response(h, NTAPS, 1000e3, FS_HZ);

    printf("resp: DC=%.4f 100k=%.4f 500k=%.6f 1M=%.6f\n",
           dc, pass, stop500, stop1m);

    if (dc < 0.99 || dc > 1.01)
        return fail("DC gain not unity", dc);
    if (pass < 0.95)
        return fail("100 kHz (FM occupied bandwidth) not passed", pass);
    if (stop500 > 0.02)
        return fail("500 kHz (adjacent channel) not rejected", stop500);
    if (stop1m > 0.02)
        return fail("1 MHz not rejected", stop1m);

    printf("ok: %d taps, DC %.4f, 100k %.4f, 500k %.6f\n",
           NTAPS, dc, pass, stop500);
    return 0;
}
