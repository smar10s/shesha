/*
 * Guards the AD9361 RX FIR configuration in firmware/src/fm_rx_fir.h.
 *
 * The coefficients are Q1.15, so their sum is the filter's DC gain.  The
 * header asks for GAIN 0, which is only correct if the taps sum to 32768
 * (unity).  A larger sum saturated the FIR at high RX gain and killed FM
 * decode (the original taps summed to 327724, +20 dB), which is the exact
 * regression this test exists to catch.  It parses the same string the
 * daemon loads, so the table and the test cannot drift apart.
 */
#include "fm_rx_fir.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EXPECTED_TAPS 96

static int fail(const char *msg, const char *arg)
{
    fprintf(stderr, "FAIL: %s%s%s\n", msg, arg ? ": " : "", arg ? arg : "");
    return 1;
}

int main(void)
{
    char copy[sizeof(FM_RX_FIR_CONFIG)];
    memcpy(copy, FM_RX_FIR_CONFIG, sizeof(copy));

    long sum = 0;
    int line_no = 0;
    int coef_lines = 0;
    char *line = strtok(copy, "\n");
    while (line) {
        if (line_no == 0 && strcmp(line, "RX 3 GAIN 0 DEC 2") != 0)
            return fail("first header line is not 'RX 3 GAIN 0 DEC 2'", line);
        if (line_no == 1 && strcmp(line, "TX 3 GAIN 0 INT 1") != 0)
            return fail("second header line is not 'TX 3 GAIN 0 INT 1'", line);

        if (strchr(line, ',')) {
            int i_tap, q_tap;
            if (sscanf(line, "%d,%d", &i_tap, &q_tap) != 2)
                return fail("unparsable coefficient line", line);
            if (i_tap != q_tap)
                return fail("I and Q taps differ", line);
            if (i_tap < -32768 || i_tap > 32767)
                return fail("coefficient does not fit int16", line);
            sum += i_tap;
            coef_lines++;
        }
        line_no++;
        line = strtok(NULL, "\n");
    }

    if (coef_lines != EXPECTED_TAPS) {
        fprintf(stderr, "FAIL: %d tap lines, expected %d\n",
                coef_lines, EXPECTED_TAPS);
        return 1;
    }
    if (sum != 32768) {
        double gain_db = 20.0 * (double)sum / 32768.0;
        fprintf(stderr,
                "FAIL: taps sum to %ld, not 32768 (unity Q1.15); "
                "DC gain is %.2f dB\n", sum, gain_db);
        return 1;
    }

    printf("ok: %d taps, sum %ld (unity DC gain)\n", coef_lines, sum);
    return 0;
}
