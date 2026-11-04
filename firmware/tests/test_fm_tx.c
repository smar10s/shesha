/* fm_tx stream flush: the producer requests a discard, the consumer applies
 * it.  Including the implementation gives the test the ring cursors and the
 * consumer entry point, so a lost race can be recreated deterministically
 * instead of relying on thread timing.
 *
 * The scheduler is faked: the test drives the consumer itself.
 */
#include "../src/fm_tx.c"

#include <stdio.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* --- fake scheduler ----------------------------------------------------- */

static tx_stream_producer g_produce;
static tx_stream_cleanup g_cleanup;
static void *g_user;

int tx_scheduler_start_stream(tx_stream_producer produce,
                              tx_stream_cleanup cleanup,
                              void *user, uint32_t dac_rate)
{
    (void)dac_rate;
    g_produce = produce;
    g_cleanup = cleanup;
    g_user = user;
    return 0;
}

void tx_scheduler_stop(void)
{
}

/* --- helpers ------------------------------------------------------------ */

static void start_stream(uint32_t n_audio)
{
    float *audio = calloc(n_audio, sizeof(*audio));

    CHECK(audio != NULL);
    for (uint32_t i = 0; i < n_audio; i++)
        audio[i] = (float)i / (float)n_audio;
    CHECK(fm_tx_stream_start(audio, n_audio, 44100, 75000, 2100000, NULL) == 0);
    free(audio);
}

static ssize_t run_consumer(size_t max_chunk)
{
    static float real[DMA_TX_STREAM_CHUNK];
    static float imag[DMA_TX_STREAM_CHUNK];

    return fm_stream_produce(real, imag, max_chunk, g_fm_stream);
}

static void drain_until_empty(void)
{
    for (int i = 0; i < 32; i++) {
        fm_tx_stream_stats_t st;
        fm_tx_stream_stats(&st);
        if (st.ring_fill == 0)
            return;
        CHECK(run_consumer(DMA_TX_STREAM_CHUNK) > 0);
    }
}

/* A consumer that read the old tail just before the flush can publish its
 * commit just after.  The flush must still win: the ring empties and the
 * next consumer pass must not see a huge wrapped "available" count. */
static void test_flush_beats_stale_consumer_commit(void)
{
    start_stream(100);

    fm_stream_t *s = g_fm_stream;
    CHECK(s != NULL);
    CHECK(fm_tx_stream_flush() == 0);

    /* The in-flight consumer had consumed 10 samples before the flush. */
    atomic_store_explicit(&s->tail, 10, memory_order_release);

    run_consumer(64);

    fm_tx_stream_stats_t st;
    fm_tx_stream_stats(&st);
    CHECK(st.ring_fill == 0);
    CHECK(s->tail == s->head);

    fm_tx_stream_release();
}

/* The same race, but the client pushed new audio after the flush.  Those
 * samples are part of the new timeline and must not be discarded. */
static void test_flush_keeps_audio_pushed_after_it(void)
{
    start_stream(50);

    fm_stream_t *s = g_fm_stream;
    CHECK(s != NULL);
    CHECK(fm_tx_stream_flush() == 0);

    atomic_store_explicit(&s->tail, 10, memory_order_release);

    float more[100];
    for (int i = 0; i < 100; i++)
        more[i] = 0.1f;
    CHECK(fm_tx_stream_push(more, 100) == FM_TX_PUSH_OK);

    drain_until_empty();

    fm_tx_stream_stats_t st;
    fm_tx_stream_stats(&st);
    /* Interpolation needs a pair, so the last sample or two stay queued.
     * The point is that the count is bounded, not the wrapped ~4G the old
     * flush produced. */
    CHECK(st.ring_fill <= 2);
    CHECK(st.audio_consumed >= 148);
    CHECK(s->tail <= s->head);

    fm_tx_stream_release();
}

/* END closes the stream.  A push after it must fail: accepting audio would
 * keep the TX thread feeding the DAC long after the client asked it to
 * stop, because the thread only exits once the ring drains. */
static void test_push_after_end_is_rejected(void)
{
    start_stream(100);

    fm_tx_stream_end();

    float more[16];
    for (int i = 0; i < 16; i++)
        more[i] = 0.1f;
    CHECK(fm_tx_stream_push(more, 16) == FM_TX_PUSH_ERR);

    fm_tx_stream_release();
}

int main(void)
{
    TEST(test_flush_beats_stale_consumer_commit);
    TEST(test_flush_keeps_audio_pushed_after_it);
    TEST(test_push_after_end_is_rejected);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
