/* radio_set_sample_rate: commit and rollback around the RX DMA restart.
 *
 * radio.c reaches the AD9361 and the RX DMA engine through plain functions.
 * These fakes record the calls and let a test fail a chosen start, so the
 * ordering that matters on hardware is checked on the host: the state and
 * the reported actual rate are committed only after DMA is running, and a
 * failed final start puts the hardware back on the old rate.
 */
#include "radio.h"

#include <stdio.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* --- fake AD9361 and RX DMA -------------------------------------------- */

static uint32_t hw_sr;
static uint32_t hw_rx_bw;
static uint32_t hw_tx_bw;
static uint64_t hw_rx_lo;
static uint64_t hw_tx_lo;
static uint64_t readback_lo;          /* 0: report hw_rx_lo */
static uint32_t readback_sr;          /* 0: report hw_sr */
static uint32_t readback_bw;          /* 0: report hw_rx_bw */
static uint32_t last_set_sr;
static uint32_t last_set_tx_bw;
static uint32_t last_set_rx_bw;
static unsigned set_sr_calls;
static unsigned set_tx_bw_calls;
static unsigned set_rx_bw_calls;
static unsigned get_sr_calls;
static unsigned get_rx_bw_calls;
static unsigned rx_start_calls;
static unsigned rx_stop_calls;
static int fail_set_rx_lo;
static int fail_set_tx_lo;
static int fail_get_rx_lo;
static int fail_set_sr;
static int fail_set_sr_after_write;
static int fail_set_tx_bw;
static int fail_set_rx_bw;
static int fail_get_sr;
static int fail_get_rx_bw;
static unsigned rx_start_fail_remaining;

static void reset_fake(uint32_t initial_sr)
{
    hw_sr = initial_sr;
    hw_rx_bw = 20000000;
    hw_tx_bw = 20000000;
    hw_rx_lo = 2457000000ULL;
    hw_tx_lo = 2457000000ULL;
    readback_lo = 0;
    readback_sr = 0;
    readback_bw = 0;
    last_set_sr = 0;
    last_set_tx_bw = 0;
    last_set_rx_bw = 0;
    set_sr_calls = 0;
    set_tx_bw_calls = 0;
    set_rx_bw_calls = 0;
    get_sr_calls = 0;
    get_rx_bw_calls = 0;
    rx_start_calls = 0;
    rx_stop_calls = 0;
    fail_set_rx_lo = 0;
    fail_set_tx_lo = 0;
    fail_get_rx_lo = 0;
    fail_set_sr = 0;
    fail_set_sr_after_write = 0;
    fail_set_tx_bw = 0;
    fail_set_rx_bw = 0;
    fail_get_sr = 0;
    fail_get_rx_bw = 0;
    rx_start_fail_remaining = 0;
}

int hal_init(void) { return 0; }

int hal_ad9361_set_rx_lo(uint64_t freq_hz)
{
    if (fail_set_rx_lo)
        return -1;
    hw_rx_lo = freq_hz;
    return 0;
}

int hal_ad9361_set_tx_lo(uint64_t freq_hz)
{
    if (fail_set_tx_lo)
        return -1;
    hw_tx_lo = freq_hz;
    return 0;
}

int hal_ad9361_set_sample_rate(uint64_t rate_hz)
{
    set_sr_calls++;
    last_set_sr = (uint32_t)rate_hz;
    if (fail_set_sr)
        return -1;
    hw_sr = (uint32_t)rate_hz;
    if (fail_set_sr_after_write) {
        fail_set_sr_after_write = 0;
        return -1;
    }
    return 0;
}

int hal_ad9361_set_rx_bandwidth(uint64_t bw_hz)
{
    set_rx_bw_calls++;
    last_set_rx_bw = (uint32_t)bw_hz;
    if (fail_set_rx_bw)
        return -1;
    hw_rx_bw = (uint32_t)bw_hz;
    return 0;
}

int hal_ad9361_set_tx_bandwidth(uint64_t bw_hz)
{
    set_tx_bw_calls++;
    last_set_tx_bw = (uint32_t)bw_hz;
    if (fail_set_tx_bw > 0) {
        fail_set_tx_bw--;
        return -1;
    }
    hw_tx_bw = (uint32_t)bw_hz;
    return 0;
}

int hal_ad9361_set_rx_gain(double gain_db) { (void)gain_db; return 0; }
long long hal_ad9361_get_rx_lo(void)
{
    if (fail_get_rx_lo)
        return -1;
    return (long long)(readback_lo ? readback_lo : hw_rx_lo);
}
long long hal_ad9361_get_rx_bandwidth(void)
{
    get_rx_bw_calls++;
    if (fail_get_rx_bw)
        return -1;
    return (long long)(readback_bw ? readback_bw : hw_rx_bw);
}

long long hal_ad9361_get_sample_rate(void)
{
    get_sr_calls++;
    if (fail_get_sr)
        return -1;
    return (long long)(readback_sr ? readback_sr : hw_sr);
}

int hal_ad9361_set_rx_gain_mode(const char *mode) { (void)mode; return 0; }
int hal_ad9361_set_tx_attenuation(double atten_db) { (void)atten_db; return 0; }
uint32_t hal_reg_read(uint32_t addr) { (void)addr; return 0; }

int dma_rx_start(void)
{
    rx_start_calls++;
    if (rx_start_fail_remaining > 0) {
        rx_start_fail_remaining--;
        return -1;
    }
    return 0;
}

void dma_rx_stop(void)
{
    rx_stop_calls++;
}

/* --- tests -------------------------------------------------------------- */

static void test_noop_does_not_restart_dma(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 0;

    reset_fake(20000000);

    CHECK(radio_set_sample_rate(&st, 20000000, &actual) == 0);
    CHECK(actual == 20000000);
    CHECK(st.sample_rate_hz == 20000000);
    CHECK(rx_stop_calls == 0);
    CHECK(rx_start_calls == 0);
    CHECK(set_sr_calls == 0);
}

/* The readback value, not the request, is what the caller and the state
 * see.  Only a successful DMA start commits either. */
static void test_success_commits_actual_after_dma_start(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 0;

    reset_fake(20000000);
    readback_sr = 2400000;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == 0);
    CHECK(actual == 2400000);
    CHECK(st.sample_rate_hz == 2400000);
    CHECK(last_set_sr == 2500000);
    CHECK(last_set_tx_bw == 2500000);
    CHECK(hw_sr == 2500000);
    CHECK(hw_tx_bw == 2500000);
    CHECK(rx_stop_calls == 1);
    CHECK(rx_start_calls == 1);
    CHECK(get_sr_calls == 1);
}

/* The new rate is live but DMA is not.  The hardware must go back to the
 * old rate and the state must stay there, so the caller's rollback logic
 * sees a failure that did not half-apply. */
static void test_final_start_failure_restores_old_rate(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 123;

    reset_fake(20000000);
    rx_start_fail_remaining = 1;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == -1);
    CHECK(st.sample_rate_hz == 20000000);
    CHECK(actual == 123);
    CHECK(last_set_sr == 20000000);
    CHECK(last_set_tx_bw == 20000000);
    CHECK(hw_sr == 20000000);
    CHECK(hw_tx_bw == 20000000);
    CHECK(set_sr_calls == 2);
    CHECK(set_tx_bw_calls == 2);
    CHECK(rx_stop_calls == 2);
    CHECK(rx_start_calls == 2);
}

/* Even when the best-effort restart also fails, the state contract is the
 * same: no new rate is committed.  The code logs this case. */
static void test_restore_start_failure_still_leaves_old_state(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 123;

    reset_fake(20000000);
    rx_start_fail_remaining = 2;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == -1);
    CHECK(st.sample_rate_hz == 20000000);
    CHECK(actual == 123);
    CHECK(hw_sr == 20000000);
    CHECK(rx_stop_calls == 2);
    CHECK(rx_start_calls == 2);
}

/* A failed change leaves the state on the old rate, so asking for the same
 * new rate again must not hit the no-op guard. */
static void test_retry_reapplies_rate_after_failed_start(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 0;

    reset_fake(20000000);
    rx_start_fail_remaining = 1;
    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == -1);
    CHECK(st.sample_rate_hz == 20000000);

    actual = 0;
    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == 0);
    CHECK(actual == 2500000);
    CHECK(st.sample_rate_hz == 2500000);
    CHECK(last_set_sr == 2500000);
    CHECK(rx_stop_calls == 3);
    CHECK(rx_start_calls == 3);
}

static void test_early_set_failure_keeps_old_state(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 123;

    reset_fake(20000000);
    fail_set_sr = 1;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == -1);
    CHECK(st.sample_rate_hz == 20000000);
    CHECK(actual == 123);
    CHECK(hw_sr == 20000000);
    CHECK(hw_tx_bw == 20000000);
    CHECK(last_set_sr == 20000000);
    CHECK(last_set_tx_bw == 20000000);
    CHECK(set_sr_calls == 2);
    CHECK(set_tx_bw_calls == 1);
    CHECK(rx_stop_calls == 2);
    CHECK(rx_start_calls == 1);
}

/* A write that lands and then reports failure is indistinguishable from
 * one that never landed, so the rollback must restore both the rate and
 * the TX bandwidth the write moved with it. */
static void test_set_failure_after_write_restores_old_rate_and_bw(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 123;

    reset_fake(20000000);
    fail_set_sr_after_write = 1;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == -1);
    CHECK(st.sample_rate_hz == 20000000);
    CHECK(actual == 123);
    CHECK(hw_sr == 20000000);
    CHECK(hw_tx_bw == 20000000);
    CHECK(last_set_sr == 20000000);
    CHECK(last_set_tx_bw == 20000000);
    CHECK(set_sr_calls == 2);
    CHECK(set_tx_bw_calls == 1);
    CHECK(rx_stop_calls == 2);
    CHECK(rx_start_calls == 1);
}

static void test_tx_bandwidth_failure_restores_old_rate_and_bw(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 123;

    reset_fake(20000000);
    fail_set_tx_bw = 1;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == -1);
    CHECK(st.sample_rate_hz == 20000000);
    CHECK(actual == 123);
    CHECK(hw_sr == 20000000);
    CHECK(hw_tx_bw == 20000000);
    CHECK(last_set_sr == 20000000);
    CHECK(last_set_tx_bw == 20000000);
    CHECK(set_sr_calls == 2);
    CHECK(set_tx_bw_calls == 2);
    CHECK(get_sr_calls == 0);
    CHECK(rx_stop_calls == 2);
    CHECK(rx_start_calls == 1);
}

static void test_readback_failure_restores_old_rate_and_bw(void)
{
    radio_state_t st = { .sample_rate_hz = 20000000 };
    uint32_t actual = 123;

    reset_fake(20000000);
    fail_get_sr = 1;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == -1);
    CHECK(st.sample_rate_hz == 20000000);
    CHECK(actual == 123);
    CHECK(hw_sr == 20000000);
    CHECK(hw_tx_bw == 20000000);
    CHECK(last_set_sr == 20000000);
    CHECK(last_set_tx_bw == 20000000);
    CHECK(set_sr_calls == 2);
    CHECK(set_tx_bw_calls == 2);
    CHECK(get_sr_calls == 1);
    CHECK(rx_stop_calls == 2);
    CHECK(rx_start_calls == 1);
}

static void test_set_bw_readback_failure_restores_old_bw(void)
{
    radio_state_t st = {
        .sample_rate_hz = 20000000,
        .bw_hz = 20000000,
    };
    uint32_t actual = 123;

    reset_fake(20000000);
    fail_get_rx_bw = 1;

    CHECK(radio_set_bw(&st, 5000000, &actual) == -1);
    CHECK(st.bw_hz == 20000000);
    CHECK(actual == 123);
    CHECK(hw_rx_bw == 20000000);
    CHECK(last_set_rx_bw == 20000000);
    CHECK(set_rx_bw_calls == 2);
    CHECK(get_rx_bw_calls == 1);
}

static void test_set_bw_failure_keeps_old_state(void)
{
    radio_state_t st = {
        .sample_rate_hz = 20000000,
        .bw_hz = 20000000,
    };
    uint32_t actual = 123;

    reset_fake(20000000);
    fail_set_rx_bw = 1;

    CHECK(radio_set_bw(&st, 5000000, &actual) == -1);
    CHECK(st.bw_hz == 20000000);
    CHECK(actual == 123);
    CHECK(hw_rx_bw == 20000000);
    CHECK(set_rx_bw_calls == 1);
    CHECK(get_rx_bw_calls == 0);
}

static void test_init_fails_when_dma_start_fails(void)
{
    radio_state_t st;

    reset_fake(20000000);
    rx_start_fail_remaining = 1;

    CHECK(radio_init(&st) == -1);
    CHECK(rx_start_calls == 1);
}

/* The RX LO moved but the TX LO write failed.  Leave neither changed and
 * report failure, so hardware and state agree. */
static void test_set_freq_tx_lo_failure_restores_rx_lo(void)
{
    radio_state_t st = {
        .freq_hz = 2457000000ULL,
        .sample_rate_hz = 20000000,
    };
    uint64_t actual = 123;

    reset_fake(20000000);
    fail_set_tx_lo = 1;

    CHECK(radio_set_freq(&st, 2437000000ULL, &actual) == -1);
    CHECK(st.freq_hz == 2457000000ULL);
    CHECK(actual == 123);
    CHECK(hw_rx_lo == 2457000000ULL);
    CHECK(hw_tx_lo == 2457000000ULL);
}

/* Both LOs landed, then the readback failed.  Both must go back. */
static void test_set_freq_readback_failure_restores_both_los(void)
{
    radio_state_t st = {
        .freq_hz = 2457000000ULL,
        .sample_rate_hz = 20000000,
    };
    uint64_t actual = 123;

    reset_fake(20000000);
    fail_get_rx_lo = 1;

    CHECK(radio_set_freq(&st, 2437000000ULL, &actual) == -1);
    CHECK(st.freq_hz == 2457000000ULL);
    CHECK(actual == 123);
    CHECK(hw_rx_lo == 2457000000ULL);
    CHECK(hw_tx_lo == 2457000000ULL);
}

static void test_capture_publish_and_snapshot(void)
{
    radio_state_t st = {
        .freq_hz = 1234567890ULL,
        .sample_rate_hz = 4800000,
    };
    uint64_t freq = 0;
    uint32_t rate = 0;

    radio_capture_publish(&st);
    radio_capture_snapshot(&freq, &rate);
    CHECK(freq == 1234567890ULL);
    CHECK(rate == 4800000);

    st.freq_hz = 2400000000ULL;
    st.sample_rate_hz = 20000000;
    radio_capture_publish(&st);
    radio_capture_snapshot(&freq, &rate);
    CHECK(freq == 2400000000ULL);
    CHECK(rate == 20000000);

    /* A producer that only needs one field must not have to invent a
     * scratch variable for the other. */
    freq = 0;
    rate = 0;
    radio_capture_snapshot(&freq, NULL);
    CHECK(freq == 2400000000ULL);
    radio_capture_snapshot(NULL, &rate);
    CHECK(rate == 20000000);
}

static void test_init_publishes_defaults(void)
{
    radio_state_t st;
    uint64_t freq = 0;
    uint32_t rate = 0;

    reset_fake(20000000);
    CHECK(radio_init(&st) == 0);
    radio_capture_snapshot(&freq, &rate);
    CHECK(freq == 2457000000ULL);
    CHECK(rate == 20000000);
    CHECK(st.freq_hz == freq);
    CHECK(st.sample_rate_hz == rate);
}

static void test_set_freq_publishes_readback(void)
{
    radio_state_t st = {
        .freq_hz = 2457000000ULL,
        .sample_rate_hz = 20000000,
    };
    uint64_t actual = 0;
    uint64_t freq = 0;
    uint32_t rate = 0;

    reset_fake(20000000);
    radio_capture_publish(&st);
    readback_lo = 2436000000ULL;

    CHECK(radio_set_freq(&st, 2437000000ULL, &actual) == 0);
    CHECK(actual == 2436000000ULL);
    CHECK(st.freq_hz == 2436000000ULL);
    CHECK(hw_rx_lo == 2437000000ULL);
    CHECK(hw_tx_lo == 2437000000ULL);
    radio_capture_snapshot(&freq, &rate);
    CHECK(freq == 2436000000ULL);
    CHECK(rate == 20000000);
}

static void test_set_freq_failure_does_not_publish(void)
{
    radio_state_t st = {
        .freq_hz = 2457000000ULL,
        .sample_rate_hz = 20000000,
    };
    uint64_t actual = 0;
    uint64_t freq = 0;
    uint32_t rate = 0;

    reset_fake(20000000);
    radio_capture_publish(&st);
    fail_set_rx_lo = 1;

    CHECK(radio_set_freq(&st, 2437000000ULL, &actual) == -1);
    CHECK(st.freq_hz == 2457000000ULL);
    radio_capture_snapshot(&freq, &rate);
    CHECK(freq == 2457000000ULL);
    CHECK(rate == 20000000);
}

static void test_set_sample_rate_publishes_actual(void)
{
    radio_state_t st = {
        .freq_hz = 2457000000ULL,
        .sample_rate_hz = 20000000,
    };
    uint32_t actual = 0;
    uint64_t freq = 0;
    uint32_t rate = 0;

    reset_fake(20000000);
    radio_capture_publish(&st);
    readback_sr = 2400000;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == 0);
    radio_capture_snapshot(&freq, &rate);
    CHECK(freq == 2457000000ULL);
    CHECK(rate == 2400000);
}

static void test_failed_sample_rate_does_not_publish(void)
{
    radio_state_t st = {
        .freq_hz = 2457000000ULL,
        .sample_rate_hz = 20000000,
    };
    uint32_t actual = 0;
    uint64_t freq = 0;
    uint32_t rate = 0;

    reset_fake(20000000);
    radio_capture_publish(&st);
    readback_sr = 2400000;
    rx_start_fail_remaining = 1;

    CHECK(radio_set_sample_rate(&st, 2500000, &actual) == -1);
    radio_capture_snapshot(&freq, &rate);
    CHECK(freq == 2457000000ULL);
    CHECK(rate == 20000000);
}

int main(void)
{
    TEST(test_noop_does_not_restart_dma);
    TEST(test_success_commits_actual_after_dma_start);
    TEST(test_final_start_failure_restores_old_rate);
    TEST(test_restore_start_failure_still_leaves_old_state);
    TEST(test_retry_reapplies_rate_after_failed_start);
    TEST(test_early_set_failure_keeps_old_state);
    TEST(test_set_failure_after_write_restores_old_rate_and_bw);
    TEST(test_tx_bandwidth_failure_restores_old_rate_and_bw);
    TEST(test_readback_failure_restores_old_rate_and_bw);
    TEST(test_set_bw_readback_failure_restores_old_bw);
    TEST(test_set_bw_failure_keeps_old_state);
    TEST(test_capture_publish_and_snapshot);
    TEST(test_init_fails_when_dma_start_fails);
    TEST(test_init_publishes_defaults);
    TEST(test_set_freq_tx_lo_failure_restores_rx_lo);
    TEST(test_set_freq_readback_failure_restores_both_los);
    TEST(test_set_freq_publishes_readback);
    TEST(test_set_freq_failure_does_not_publish);
    TEST(test_set_sample_rate_publishes_actual);
    TEST(test_failed_sample_rate_does_not_publish);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
