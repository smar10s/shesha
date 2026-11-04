/* test_sendq.c — host tests for the control/data outbound queues.
 *
 * Validates: control priority at frame boundaries, frame-boundary
 * switching while a ring is mid-frame, independent ring backpressure,
 * wrap-around (payload and length prefix), zero-length frames, and
 * teardown with bytes still pending.
 */
#include "../src/sendq.h"

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

/* --- creation, empty behavior, and invalid capacities --- */

static void test_create_empty(void)
{
    sendq_t *q = sendq_create(64, 256);
    CHECK(q != NULL);
    CHECK(!sendq_pending(q));

    bool is_control = true;
    const uint8_t *ptr = (const uint8_t *)"stale";
    CHECK(sendq_peek(q, &is_control, &ptr) == 0);
    CHECK(ptr == NULL);
    CHECK(!is_control);

    /* Consuming from empty rings must be harmless. */
    sendq_consume(q, true, 8);
    sendq_consume(q, false, 8);
    CHECK(!sendq_pending(q));

    CHECK(!sendq_push_control(q, NULL, 1));
    CHECK(!sendq_push_data(q, NULL, 1));
    CHECK(!sendq_pending(q));

    sendq_destroy(q);
}

static void test_invalid_capacity(void)
{
    CHECK(sendq_create(0, 8) == NULL);
    CHECK(sendq_create(8, 0) == NULL);
    CHECK(sendq_create(6, 8) == NULL);
    CHECK(sendq_create(8, 3) == NULL);
}

/* --- control is peeked before data, regardless of push order --- */

static void test_control_before_data(void)
{
    sendq_t *q = sendq_create(8, 8);
    const uint8_t data[] = { 'd', 'a', 't', 'a' };
    const uint8_t ctrl[] = { 'c', 't', 'r', 'l' };

    CHECK(sendq_push_data(q, data, sizeof(data)));
    CHECK(sendq_push_control(q, ctrl, sizeof(ctrl)));
    CHECK(sendq_pending(q));

    bool is_control = false;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(ctrl));
    CHECK(is_control);
    CHECK(memcmp(ptr, ctrl, sizeof(ctrl)) == 0);

    sendq_consume(q, is_control, n);
    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(data));
    CHECK(!is_control);
    CHECK(memcmp(ptr, data, sizeof(data)) == 0);

    sendq_consume(q, is_control, n);
    CHECK(!sendq_pending(q));
    sendq_destroy(q);
}

/* --- a full data ring must not block control frames --- */

static void test_data_full_control_still_queued(void)
{
    sendq_t *q = sendq_create(16, 16);
    const uint8_t data[8] = { 0, 1, 2, 3, 4, 5, 6, 7 };
    const uint8_t extra = 99;
    const uint8_t ctrl[] = { 'o', 'k' };

    CHECK(sendq_push_data(q, data, sizeof(data)));
    CHECK(!sendq_push_data(q, &extra, 1));
    CHECK(sendq_push_control(q, ctrl, sizeof(ctrl)));

    bool is_control = false;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(ctrl));
    CHECK(is_control);
    CHECK(memcmp(ptr, ctrl, sizeof(ctrl)) == 0);
    sendq_consume(q, is_control, n);

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(data));
    CHECK(!is_control);
    CHECK(memcmp(ptr, data, sizeof(data)) == 0);
    sendq_consume(q, is_control, n);
    CHECK(!sendq_pending(q));
    sendq_destroy(q);
}

/* --- a full control ring fails without touching the data ring --- */

static void test_control_full_isolated(void)
{
    sendq_t *q = sendq_create(16, 16);
    const uint8_t ctrl[8] = { 'c', 0, 1, 2, 3, 4, 5, 6 };
    const uint8_t overflow = 'x';
    const uint8_t data[] = { 'd', 'a', 't', 'a' };

    CHECK(sendq_push_control(q, ctrl, sizeof(ctrl)));
    CHECK(!sendq_push_control(q, &overflow, 1));
    CHECK(sendq_push_data(q, data, sizeof(data)));

    bool is_control = false;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(ctrl));
    CHECK(is_control);
    CHECK(memcmp(ptr, ctrl, sizeof(ctrl)) == 0);
    sendq_consume(q, is_control, n);

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(data));
    CHECK(!is_control);
    CHECK(memcmp(ptr, data, sizeof(data)) == 0);
    sendq_consume(q, is_control, n);
    CHECK(!sendq_pending(q));
    sendq_destroy(q);
}

/* --- control payload wraps and partial consume preserves order --- */

static void test_control_wrap_partial(void)
{
    sendq_t *q = sendq_create(16, 16);
    const uint8_t first[] = { 'a', 'b', 'c', 'd', 'e', 'f' };
    const uint8_t second[] = { 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n' };
    const uint8_t overflow = 'z';

    CHECK(sendq_push_control(q, first, sizeof(first)));

    bool is_control = false;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(first));
    CHECK(is_control);
    CHECK(memcmp(ptr, first, sizeof(first)) == 0);
    sendq_consume(q, is_control, n);

    /* The next payload starts near the ring end and wraps. */
    CHECK(sendq_push_control(q, second, sizeof(second)));
    CHECK(!sendq_push_control(q, &overflow, 1));

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == 2);
    CHECK(is_control);
    CHECK(memcmp(ptr, "gh", 2) == 0);
    sendq_consume(q, is_control, n);

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == 6);
    CHECK(is_control);
    CHECK(memcmp(ptr, "ijklmn", 6) == 0);
    sendq_consume(q, is_control, n);

    CHECK(!sendq_pending(q));
    sendq_destroy(q);
}

/* --- data payload wraps and partial consume preserves order --- */

static void test_data_wrap_partial(void)
{
    sendq_t *q = sendq_create(16, 16);
    const uint8_t first[] = { 'a', 'b', 'c', 'd', 'e', 'f' };
    const uint8_t second[] = { 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n' };
    const uint8_t overflow = 'z';

    CHECK(sendq_push_data(q, first, sizeof(first)));

    bool is_control = true;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(first));
    CHECK(!is_control);
    CHECK(memcmp(ptr, first, sizeof(first)) == 0);
    sendq_consume(q, is_control, n);

    CHECK(sendq_push_data(q, second, sizeof(second)));
    CHECK(!sendq_push_data(q, &overflow, 1));

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == 2);
    CHECK(!is_control);
    CHECK(memcmp(ptr, "gh", 2) == 0);
    sendq_consume(q, is_control, n);

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == 6);
    CHECK(!is_control);
    CHECK(memcmp(ptr, "ijklmn", 6) == 0);
    sendq_consume(q, is_control, n);

    CHECK(!sendq_pending(q));
    sendq_destroy(q);
}

/* --- a length prefix that wraps across the ring end is decoded --- */

static void test_length_prefix_wrap(void)
{
    sendq_t *q = sendq_create(8, 8);
    const uint8_t first[] = { 'a', 'b' };
    const uint8_t second[] = { 'c', 'd', 'e', 'f' };
    const uint8_t overflow = 'z';

    CHECK(sendq_push_control(q, first, sizeof(first)));

    bool is_control = false;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(first));
    CHECK(is_control);
    CHECK(memcmp(ptr, first, sizeof(first)) == 0);
    sendq_consume(q, is_control, n);

    /* tail sits at offset 6, so the four-byte prefix spans the end. */
    CHECK(sendq_push_control(q, second, sizeof(second)));
    CHECK(!sendq_push_control(q, &overflow, 1));

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(second));
    CHECK(is_control);
    CHECK(memcmp(ptr, second, sizeof(second)) == 0);
    sendq_consume(q, is_control, 2);

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == 2);
    CHECK(is_control);
    CHECK(memcmp(ptr, "ef", 2) == 0);
    sendq_consume(q, is_control, n);

    CHECK(!sendq_pending(q));
    sendq_destroy(q);
}

/* --- control cannot interleave into a partially sent data frame --- */

static void test_frame_boundary_priority(void)
{
    sendq_t *q = sendq_create(64, 64);
    const uint8_t data[] = { 'd', 'a', 't', 'a', '-', 'f', 'r', 'a', 'm', 'e' };
    const uint8_t ctrl[] = { 'c', 't', 'r', 'l' };

    CHECK(sendq_push_data(q, data, sizeof(data)));

    bool is_control = true;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(data));
    CHECK(!is_control);
    CHECK(memcmp(ptr, data, sizeof(data)) == 0);
    sendq_consume(q, is_control, 4);

    CHECK(sendq_push_control(q, ctrl, sizeof(ctrl)));

    /* Mid-frame data keeps priority over the complete control frame. */
    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == 6);
    CHECK(!is_control);
    CHECK(memcmp(ptr, data + 4, 6) == 0);
    sendq_consume(q, is_control, n);

    /* Only now, at the boundary, does control take over. */
    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(ctrl));
    CHECK(is_control);
    CHECK(memcmp(ptr, ctrl, sizeof(ctrl)) == 0);
    sendq_consume(q, is_control, n);

    CHECK(!sendq_pending(q));
    sendq_destroy(q);
}

/* --- zero-length frames are skipped without wedging the queue --- */

static void test_zero_length_frames(void)
{
    sendq_t *q = sendq_create(16, 16);
    const uint8_t ctrl[] = { 'o', 'k' };
    const uint8_t data[] = { 'd' };

    CHECK(sendq_push_control(q, NULL, 0));
    CHECK(sendq_push_data(q, NULL, 0));
    CHECK(sendq_push_control(q, ctrl, sizeof(ctrl)));
    CHECK(sendq_push_control(q, NULL, 0));
    CHECK(sendq_push_data(q, data, sizeof(data)));

    bool is_control = false;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(ctrl));
    CHECK(is_control);
    CHECK(memcmp(ptr, ctrl, sizeof(ctrl)) == 0);
    sendq_consume(q, is_control, n);

    n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(data));
    CHECK(!is_control);
    CHECK(memcmp(ptr, data, sizeof(data)) == 0);
    sendq_consume(q, is_control, n);

    CHECK(!sendq_pending(q));
    sendq_destroy(q);

    sendq_t *empty = sendq_create(8, 8);
    CHECK(sendq_push_control(empty, NULL, 0));
    CHECK(sendq_push_data(empty, NULL, 0));
    CHECK(sendq_pending(empty));

    is_control = true;
    ptr = (const uint8_t *)"stale";
    CHECK(sendq_peek(empty, &is_control, &ptr) == 0);
    CHECK(ptr == NULL);
    CHECK(!is_control);
    CHECK(!sendq_pending(empty));
    sendq_destroy(empty);
}

/* --- teardown with bytes pending in both rings --- */

static void test_destroy_pending(void)
{
    sendq_t *q = sendq_create(16, 16);
    const uint8_t ctrl[] = { 1, 2, 3 };
    const uint8_t data[] = { 4, 5, 6, 7, 8 };

    CHECK(sendq_push_control(q, ctrl, sizeof(ctrl)));
    CHECK(sendq_push_data(q, data, sizeof(data)));
    CHECK(sendq_pending(q));
    sendq_destroy(q);
}

/* --- consume past the available bytes is clamped --- */

static void test_consume_clamps(void)
{
    sendq_t *q = sendq_create(8, 8);
    const uint8_t ctrl[] = { 'a', 'b', 'c' };
    const uint8_t data[] = { 'x', 'y' };

    CHECK(sendq_push_control(q, ctrl, sizeof(ctrl)));
    CHECK(sendq_push_data(q, data, sizeof(data)));

    sendq_consume(q, true, 100);
    CHECK(sendq_pending(q));
    sendq_consume(q, false, 100);
    CHECK(!sendq_pending(q));

    /* Both rings are empty and usable again after the clamped consume. */
    CHECK(sendq_push_control(q, ctrl, sizeof(ctrl)));
    bool is_control = false;
    const uint8_t *ptr = NULL;
    size_t n = sendq_peek(q, &is_control, &ptr);
    CHECK(n == sizeof(ctrl));
    CHECK(is_control);
    CHECK(memcmp(ptr, ctrl, sizeof(ctrl)) == 0);
    sendq_destroy(q);
}

int main(void)
{
    TEST(test_create_empty);
    TEST(test_invalid_capacity);
    TEST(test_control_before_data);
    TEST(test_data_full_control_still_queued);
    TEST(test_control_full_isolated);
    TEST(test_control_wrap_partial);
    TEST(test_data_wrap_partial);
    TEST(test_length_prefix_wrap);
    TEST(test_frame_boundary_priority);
    TEST(test_zero_length_frames);
    TEST(test_destroy_pending);
    TEST(test_consume_clamps);

    printf("\n%s (%d failures)\n", g_fails ? "FAILED" : "all checks passed",
           g_fails);
    return g_fails ? 1 : 0;
}
