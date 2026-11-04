/* Fake DMA TX layer for host tests of the scheduler's failure paths.
 *
 * The real engine only refuses to start when the radio is in a bad state,
 * which a unit test cannot arrange.  These switches let a test fail every
 * start or one window of calls, and keep dma_tx_done() false after a
 * failure so a thread that wrongly waits on a transfer that never began is
 * caught by the test deadline.
 */
#include "dma_tx.h"

#include <unistd.h>

bool fake_dma_tx_fail_start;
bool fake_dma_tx_done_value = true;
unsigned fake_dma_tx_start_calls;
unsigned fake_dma_tx_stop_calls;
/* 0 disables; N makes the Nth dma_tx_start() call the first failure in a
 * run of fake_dma_tx_fail_count calls.  Count 0 means a single call. */
unsigned fake_dma_tx_fail_on_start;
unsigned fake_dma_tx_fail_count = 1;
/* Models the DDR drain: the real one sleeps for the queued audio.  A
 * request-stop test needs that wait to be long enough to observe. */
unsigned fake_dma_tx_stream_drain_ms;

int dma_tx_start(const float *in_real, const float *in_imag,
                 size_t n_samples, bool cyclic)
{
    (void)in_real;
    (void)in_imag;
    (void)n_samples;
    (void)cyclic;

    fake_dma_tx_start_calls++;

    bool fail = fake_dma_tx_fail_start;
    if (!fail && fake_dma_tx_fail_on_start != 0) {
        unsigned count = fake_dma_tx_fail_count ? fake_dma_tx_fail_count : 1;
        unsigned last = fake_dma_tx_fail_on_start + count;
        fail = fake_dma_tx_start_calls >= fake_dma_tx_fail_on_start
            && fake_dma_tx_start_calls < last;
    }
    if (fail) {
        /* A refused start leaves no transfer running.  If the scheduler
         * wrongly waits for one, the test deadline catches it. */
        fake_dma_tx_done_value = false;
        return -1;
    }
    fake_dma_tx_done_value = true;
    return 0;
}

void dma_tx_stop(void)
{
    fake_dma_tx_stop_calls++;
}

bool dma_tx_done(void)
{
    return fake_dma_tx_done_value;
}

int dma_tx_stream_arm(void)
{
    return 0;
}

int dma_tx_stream_trigger(void)
{
    return 0;
}

int32_t dma_tx_stream_depth(void)
{
    return 0;
}

uint64_t dma_tx_stream_emitted(void)
{
    return 0;
}

int dma_tx_stream_feed_fixed(const float *in_real, const float *in_imag,
                             size_t n_samples, float peak_scale)
{
    (void)in_real;
    (void)in_imag;
    (void)n_samples;
    (void)peak_scale;
    return 0;
}

void dma_tx_stream_drain(uint32_t sample_rate_hz)
{
    (void)sample_rate_hz;
    if (fake_dma_tx_stream_drain_ms)
        usleep(fake_dma_tx_stream_drain_ms * 1000u);
}

void dma_tx_stream_stop(void)
{
}
