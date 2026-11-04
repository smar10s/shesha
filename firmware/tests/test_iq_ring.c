#include "../src/iq_ring.h"
#include <stdio.h>
#include <string.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

static void test_push_peek_consume(void)
{
    iq_byte_ring_t r;
    CHECK(iq_byte_ring_init(&r, 1024) == 0);

    uint8_t data[16];
    for (int i = 0; i < 16; i++) data[i] = (uint8_t)i;

    CHECK(iq_byte_ring_push(&r, data, 16) == 0);
    CHECK(iq_byte_ring_avail(&r) == 16);

    uint8_t out[32];
    CHECK(iq_byte_ring_peek(&r, out, 32) == 16);   /* clamped to avail */
    CHECK(memcmp(out, data, 16) == 0);

    iq_byte_ring_consume(&r, 10);
    CHECK(iq_byte_ring_avail(&r) == 6);
    CHECK(iq_byte_ring_peek(&r, out, 32) == 6);
    CHECK(memcmp(out, data + 10, 6) == 0);

    iq_byte_ring_destroy(&r);
}

static void test_push_backpressure(void)
{
    iq_byte_ring_t r;
    CHECK(iq_byte_ring_init(&r, 1024) == 0);

    uint8_t big[1024];
    memset(big, 0xAB, sizeof(big));
    CHECK(iq_byte_ring_push(&r, big, 1024) == 0);      /* exactly fills */
    CHECK(iq_byte_ring_push(&r, big, 1) == 1);         /* full: backpressure */

    CHECK(atomic_load(&r.push_rejects) == 1);
    iq_byte_ring_destroy(&r);
}

static void test_wraparound_linearizes(void)
{
    iq_byte_ring_t r;
    CHECK(iq_byte_ring_init(&r, 1024) == 0);

    uint8_t data[1024];
    for (int i = 0; i < 1024; i++) data[i] = (uint8_t)(i * 7);

    /* Fill to 1000, consume 900 -> tail at 900.  Push 924 bytes: wraps
     * past the end, 800 bytes in the second segment. */
    CHECK(iq_byte_ring_push(&r, data, 1000) == 0);
    iq_byte_ring_consume(&r, 900);
    uint8_t more[924];
    for (int i = 0; i < 924; i++) more[i] = (uint8_t)(255 - i);
    CHECK(iq_byte_ring_push(&r, more, 924) == 0);

    uint8_t out[2048];
    CHECK(iq_byte_ring_peek(&r, out, 2048) == 1024);
    CHECK(memcmp(out, data + 900, 100) == 0);
    CHECK(memcmp(out + 100, more, 924) == 0);

    iq_byte_ring_destroy(&r);
}

static void test_reject_oversize_and_bad_init(void)
{
    /* Zero-init on purpose: the failed-init paths leave the struct
     * untouched, and push() reads r.buf to detect that.  An uninitialized
     * stack struct would make that read UB (and flaky). */
    iq_byte_ring_t r = {0};
    CHECK(iq_byte_ring_init(&r, 1000) == -1);   /* not power of two */
    CHECK(iq_byte_ring_init(&r, 0) == -1);

    uint8_t buf[8];
    CHECK(iq_byte_ring_push(&r, buf, 8) == -1); /* not initialized */
}

static void test_end_of_stream_flag(void)
{
    iq_byte_ring_t r;
    CHECK(iq_byte_ring_init(&r, 256) == 0);
    CHECK(atomic_load(&r.end_of_stream) == false);
    atomic_store(&r.end_of_stream, true);
    CHECK(atomic_load(&r.end_of_stream) == true);
    iq_byte_ring_destroy(&r);
}

int main(void)
{
    TEST(test_push_peek_consume);
    TEST(test_push_backpressure);
    TEST(test_wraparound_linearizes);
    TEST(test_reject_oversize_and_bad_init);
    TEST(test_end_of_stream_flag);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
