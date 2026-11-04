/* TX scheduler async start failures.
 *
 * tx_scheduler_start() returns before the TX thread calls dma_tx_start(),
 * so a failed start has to be observable after the command already
 * succeeded.  The fake DMA layer makes that failure controllable and
 * leaves dma_tx_done() false, so a thread that waits on a transfer which
 * never began trips the test deadline instead of passing silently.
 *
 * A start is retried before it counts as failed, and a repeat burst skips
 * an iteration whose retries are exhausted.  The tests below pin down both
 * the retry counts and the decision to keep going.
 */
#include "tx_scheduler.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

extern bool fake_dma_tx_fail_start;
extern bool fake_dma_tx_done_value;
extern unsigned fake_dma_tx_start_calls;
extern unsigned fake_dma_tx_stop_calls;
extern unsigned fake_dma_tx_fail_on_start;
extern unsigned fake_dma_tx_fail_count;
extern unsigned fake_dma_tx_stream_drain_ms;

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* Stub: the scheduler names its thread for /proc telemetry. */
void thread_stats_name_self(const char *name)
{
    (void)name;
}

/* Poll with a deadline.  A start failure must finish the thread, not just
 * flip a flag; if it is still busy when time runs out, report failure. */
static bool wait_not_busy(int timeout_ms)
{
    for (int i = 0; i < timeout_ms; i++) {
        if (!tx_scheduler_busy())
            return true;
        usleep(1000);
    }
    return !tx_scheduler_busy();
}

static void reset_fake(void)
{
    fake_dma_tx_fail_start = false;
    fake_dma_tx_fail_on_start = 0;
    fake_dma_tx_fail_count = 1;
    fake_dma_tx_done_value = true;
    fake_dma_tx_start_calls = 0;
    fake_dma_tx_stop_calls = 0;
    fake_dma_tx_stream_drain_ms = 0;
}

static uint64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)ts.tv_nsec / 1000000ULL;
}

static void start_one_shot(const float *iq)
{
    CHECK(tx_scheduler_start(iq, iq, 8, 0, 1, 0) == 0);
}

/* Every attempt fails, so the scheduler must stop the engine after each
 * one and report the failure once the retries are exhausted. */
static void test_all_attempts_fail_one_shot(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_start = true;
    fake_dma_tx_done_value = false;

    start_one_shot(iq);
    CHECK(wait_not_busy(1000));
    CHECK(tx_scheduler_start_failed());
    CHECK(!tx_scheduler_busy());
    CHECK(fake_dma_tx_start_calls == 3);
    CHECK(fake_dma_tx_stop_calls == 3);

    tx_scheduler_stop();
}

/* The first two calls are refused and the third succeeds: a retry is not a
 * failure, so the flag must stay clear. */
static void test_retry_then_success(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_on_start = 1;
    fake_dma_tx_fail_count = 2;

    start_one_shot(iq);
    CHECK(wait_not_busy(1000));
    CHECK(!tx_scheduler_busy());
    CHECK(!tx_scheduler_start_failed());
    CHECK(fake_dma_tx_start_calls == 3);
    CHECK(fake_dma_tx_stop_calls == 2);

    tx_scheduler_stop();
}

static void test_success_clears_flag_then_failure_sets_it(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_start = true;
    fake_dma_tx_done_value = false;
    start_one_shot(iq);
    CHECK(wait_not_busy(1000));
    CHECK(tx_scheduler_start_failed());
    CHECK(fake_dma_tx_start_calls == 3);
    CHECK(fake_dma_tx_stop_calls == 3);
    tx_scheduler_stop();

    /* A successful start must clear the previous failure. */
    fake_dma_tx_fail_start = false;
    start_one_shot(iq);
    CHECK(wait_not_busy(1000));
    CHECK(!tx_scheduler_busy());
    CHECK(!tx_scheduler_start_failed());
    CHECK(fake_dma_tx_start_calls == 4);
    CHECK(fake_dma_tx_stop_calls == 3);
    tx_scheduler_stop();

    /* And a later failure must set it again. */
    fake_dma_tx_fail_start = true;
    fake_dma_tx_done_value = false;
    start_one_shot(iq);
    CHECK(wait_not_busy(1000));
    CHECK(tx_scheduler_start_failed());
    CHECK(fake_dma_tx_start_calls == 7);
    CHECK(fake_dma_tx_stop_calls == 6);
    tx_scheduler_stop();
}

static void test_failing_cyclic_start_finishes(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_start = true;
    fake_dma_tx_done_value = false;

    CHECK(tx_scheduler_start(iq, iq, 8, TX_FLAG_CYCLIC, 0, 0) == 0);
    CHECK(wait_not_busy(1000));
    CHECK(tx_scheduler_start_failed());
    CHECK(fake_dma_tx_start_calls == 3);
    CHECK(fake_dma_tx_stop_calls == 3);

    fake_dma_tx_fail_start = false;
    tx_scheduler_stop();
}

/* The middle iteration exhausts its retries.  The repeat loop must skip
 * that iteration and still run the remaining one instead of unwinding. */
static void test_repeat_skips_failed_iteration(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_on_start = 2;
    fake_dma_tx_fail_count = 3;

    CHECK(tx_scheduler_start(iq, iq, 8, 0, 3, 0) == 0);
    CHECK(wait_not_busy(1000));
    CHECK(!tx_scheduler_busy());
    CHECK(tx_scheduler_start_failed());
    CHECK(fake_dma_tx_start_calls == 5);
    CHECK(fake_dma_tx_stop_calls == 3);

    tx_scheduler_stop();
}

/* A failed final iteration leaves no transfer to wait for, and the burst is
 * already over, so the thread must not spend the configured interval.  The
 * interval is long enough that an unnecessary wait misses the deadline. */
static void test_repeat_final_failure_finishes(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_on_start = 2;
    fake_dma_tx_fail_count = 3;

    CHECK(tx_scheduler_start(iq, iq, 8, 0, 2, 1000000) == 0);
    CHECK(wait_not_busy(1500));
    CHECK(!tx_scheduler_busy());
    CHECK(tx_scheduler_start_failed());
    CHECK(fake_dma_tx_start_calls == 4);
    CHECK(fake_dma_tx_stop_calls == 3);

    tx_scheduler_stop();
}

/* An endless repeat cannot retry a dead engine forever.  Three consecutive
 * failed iterations are enough to give up. */
static void test_repeat_forever_caps_consecutive_failures(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_start = true;
    fake_dma_tx_done_value = false;

    CHECK(tx_scheduler_start(iq, iq, 8, 0, TX_REPEAT_FOREVER, 0) == 0);
    CHECK(wait_not_busy(2000));
    CHECK(!tx_scheduler_busy());
    CHECK(tx_scheduler_start_failed());
    CHECK(fake_dma_tx_start_calls == 9);
    CHECK(fake_dma_tx_stop_calls == 9);

    tx_scheduler_stop();
}

/* An argument or allocation failure at entry must not expose the previous
 * transmission's failure through tx_scheduler_start_failed(). */
static void test_early_failures_clear_stale_flag(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_start = true;
    fake_dma_tx_done_value = false;
    start_one_shot(iq);
    CHECK(wait_not_busy(1000));
    CHECK(tx_scheduler_start_failed());
    tx_scheduler_stop();

    CHECK(tx_scheduler_start(NULL, iq, 8, 0, 1, 0) == -1);
    CHECK(!tx_scheduler_start_failed());

    fake_dma_tx_fail_start = true;
    fake_dma_tx_done_value = false;
    start_one_shot(iq);
    CHECK(wait_not_busy(1000));
    CHECK(tx_scheduler_start_failed());
    tx_scheduler_stop();

    CHECK(tx_scheduler_start_stream(NULL, NULL, NULL, 1000) == -1);
    CHECK(!tx_scheduler_start_failed());
}

static ssize_t stream_empty(float *real, float *imag, size_t max_chunk,
                            void *user)
{
    (void)real;
    (void)imag;
    (void)max_chunk;
    (void)user;
    return 0;
}

/* A healthy stream start must clear a failure left by the previous TX. */
static void test_healthy_stream_clears_stale_flag(void)
{
    float iq[8] = {0};

    reset_fake();
    fake_dma_tx_fail_start = true;
    fake_dma_tx_done_value = false;
    start_one_shot(iq);
    CHECK(wait_not_busy(1000));
    CHECK(tx_scheduler_start_failed());
    tx_scheduler_stop();

    fake_dma_tx_fail_start = false;
    CHECK(tx_scheduler_start_stream(stream_empty, NULL, NULL, 1000) == 0);
    CHECK(wait_not_busy(1000));
    CHECK(!tx_scheduler_start_failed());
    tx_scheduler_stop();
}

static ssize_t stream_starved(float *real, float *imag, size_t max_chunk,
                              void *user)
{
    (void)real;
    (void)imag;
    (void)max_chunk;
    (void)user;
    return -1;
}

/*
 * Requesting a stop must return at once; the drain can take a third of a
 * second and belongs to the TX thread.  The fake drain is held open long
 * enough that a synchronous implementation misses the latency check, and
 * busy must still be true afterwards because the thread is draining.
 */
static void test_request_stop_does_not_wait_for_drain(void)
{
    reset_fake();
    fake_dma_tx_stream_drain_ms = 300;

    CHECK(tx_scheduler_start_stream(stream_starved, NULL, NULL, 1000) == 0);
    /* The arm result is published before prefill, so the thread is inside
     * the prefill wait by the time start_stream() returns. */
    usleep(2000);

    uint64_t t0 = now_ms();
    tx_scheduler_request_stop();
    uint64_t elapsed = now_ms() - t0;

    CHECK(elapsed < 100);
    CHECK(tx_scheduler_busy());

    CHECK(wait_not_busy(1000));
    tx_scheduler_stop();
}

static int stream_once_left;

static ssize_t stream_once(float *real, float *imag, size_t max_chunk,
                           void *user)
{
    (void)user;
    if (stream_once_left <= 0)
        return 0;
    stream_once_left--;
    for (size_t i = 0; i < max_chunk; i++) {
        real[i] = 0.0f;
        imag[i] = 0.0f;
    }
    return (ssize_t)max_chunk;
}

/*
 * A new stream must not expose the previous stream's fed/emitted counters
 * while it is still prefilling.  The client uses those counters to place
 * the visualiser, so stale values make it jump ahead of the audio.
 */
static void test_stream_start_clears_published_stats(void)
{
    tx_stream_stats_t st;

    reset_fake();
    stream_once_left = 1;
    CHECK(tx_scheduler_start_stream(stream_once, NULL, NULL, 1000) == 0);
    CHECK(wait_not_busy(1000));
    tx_scheduler_stream_stats(&st);
    CHECK(st.fed > 0);

    CHECK(tx_scheduler_start_stream(stream_starved, NULL, NULL, 1000) == 0);
    tx_scheduler_stream_stats(&st);
    CHECK(st.fed == 0);
    CHECK(st.emitted == 0);
    CHECK(st.elapsed_us == 0);
    tx_scheduler_stop();
}

int main(void)
{
    if (tx_scheduler_init() != 0) {
        printf("tx_scheduler_init failed\n");
        return 1;
    }

    TEST(test_all_attempts_fail_one_shot);
    TEST(test_retry_then_success);
    TEST(test_success_clears_flag_then_failure_sets_it);
    TEST(test_failing_cyclic_start_finishes);
    TEST(test_repeat_skips_failed_iteration);
    TEST(test_repeat_final_failure_finishes);
    TEST(test_repeat_forever_caps_consecutive_failures);
    TEST(test_early_failures_clear_stale_flag);
    TEST(test_healthy_stream_clears_stale_flag);
    TEST(test_request_stop_does_not_wait_for_drain);
    TEST(test_stream_start_clears_published_stats);

    tx_scheduler_cleanup();

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
