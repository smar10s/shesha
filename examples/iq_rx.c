/* Minimal C IQ receiver: connect, select IQ mode, tune, read one frame.
 *
 * Build with `make examples`, then run:
 *
 *     build/examples/iq_rx 192.168.2.1 90.1
 *
 * This is the same protocol path as the Python example, without the
 * interpreter. See client/c/tools/shesha_iq.c for a full capture tool.
 */

#include "shesha/client.h"

#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    const char *host = argc > 1 ? argv[1] : SHESHA_DEFAULT_HOST;
    double freq_mhz = argc > 2 ? strtod(argv[2], NULL) : 90.1;

    shesha_client_t *client = shesha_client_new();
    if (!client) {
        fprintf(stderr, "out of memory\n");
        return 1;
    }

    int rc = shesha_client_connect(client, host, SHESHA_DEFAULT_PORT, 2.0);
    if (rc != SHESHA_OK) {
        fprintf(stderr, "connect to %s failed: %d\n", host, rc);
        shesha_client_free(client);
        return 1;
    }

    shesha_client_set_mode(client, SHESHA_MODE_IQ);
    shesha_client_set_freq(client, (uint64_t)(freq_mhz * 1e6));
    shesha_client_set_sample_rate(client, 2100000);
    shesha_client_subscribe(client, SHESHA_SUBSCRIBE_IQ);

    shesha_iq_frame_t frame;
    int got = shesha_client_iq_rx_next(client, &frame, 1000);
    if (got == 1) {
        printf("%.3fs  %.3f MHz  %.3f MSPS  %d-bit  %u samples\n",
               frame.timestamp, frame.freq_mhz,
               frame.sample_rate_hz / 1e6, frame.bits,
               (unsigned)frame.n_samples);
        shesha_iq_frame_free(&frame);
    } else {
        fprintf(stderr, "no IQ frame (rc=%d)\n", got);
    }

    shesha_client_disconnect(client);
    shesha_client_free(client);
    return got == 1 ? 0 : 1;
}
