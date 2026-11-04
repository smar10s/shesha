/* Focused tests for the inbound frame-size bound.
 *
 * proto_frame_size() is the only place that turns a wire payload length
 * into an allocation size.  It must reject anything over PROTO_MAX_PAYLOAD
 * before the addition can wrap, including lengths that would wrap to a
 * plausible small size on 32-bit.
 */
#include "../src/protocol.h"

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

static void test_zero_payload(void)
{
    size_t size = 1;

    CHECK(proto_frame_size(0, &size));
    CHECK(size == FRAME_HEADER_SIZE);
}

static void test_small_payloads(void)
{
    size_t size = 0;

    CHECK(proto_frame_size(1, &size));
    CHECK(size == FRAME_HEADER_SIZE + 1);
    CHECK(proto_frame_size(2048, &size));
    CHECK(size == FRAME_HEADER_SIZE + 2048);
}

static void test_max_payload_accepted(void)
{
    size_t size = 0;

    CHECK(proto_frame_size(PROTO_MAX_PAYLOAD, &size));
    CHECK(size == FRAME_HEADER_SIZE + (size_t)PROTO_MAX_PAYLOAD);
}

static void test_above_max_rejected(void)
{
    size_t size = 0;

    CHECK(!proto_frame_size(PROTO_MAX_PAYLOAD + 1, &size));
}

static void test_wrapping_lengths_rejected(void)
{
    size_t size = 0;

    /* 9 + 0xFFFFFFF7 wraps to zero in 32-bit arithmetic. */
    CHECK(!proto_frame_size(0xFFFFFFF7u, &size));
    CHECK(!proto_frame_size(UINT32_MAX, &size));
}

/*
 * Mirrors the receive loop in process_client_data(): parse a header,
 * bound its size, stop on rejection.  The malformed frame sits between a
 * valid frame and another valid frame; the loop must stop at it, never
 * reach the trailing frame, and never spin.
 */
static void test_receive_loop_stops_at_malformed_frame(void)
{
    uint8_t buf[64];
    size_t off = 0;
    uint32_t bad_len = 0xFFFFFFF7u;
    uint32_t seq = 0;
    uint8_t payload = 0;
    uint8_t type = 0;
    uint8_t last_processed = 0;
    size_t pos = 0;
    size_t first_frame_size;
    int processed = 0;
    int iterations = 0;

    /* A complete header-only frame. */
    off += proto_build_frame(buf + off, CMD_STATUS, NULL, 0);
    first_frame_size = off;

    /* A header whose length would wrap header plus payload to zero. */
    buf[off] = CMD_SET_FPS;
    memcpy(buf + off + 1, &bad_len, sizeof(bad_len));
    memcpy(buf + off + 5, &seq, sizeof(seq));
    off += FRAME_HEADER_SIZE;

    /* A valid trailing frame the loop must never process. */
    off += proto_build_frame(buf + off, CMD_SET_FREQ, &payload, 1);

    while (pos + FRAME_HEADER_SIZE <= off) {
        uint32_t payload_len;
        size_t frame_size;

        if (++iterations > 16)
            break;                  /* a rejected frame must not spin */

        payload_len = proto_parse_header(buf + pos, &type, NULL);
        if (!proto_frame_size(payload_len, &frame_size))
            break;

        last_processed = type;
        pos += frame_size;
        processed++;
    }

    CHECK(processed == 1);
    CHECK(iterations == 2);
    CHECK(pos == first_frame_size);
    CHECK(last_processed == CMD_STATUS);
}

int main(void)
{
    TEST(test_zero_payload);
    TEST(test_small_payloads);
    TEST(test_max_payload_accepted);
    TEST(test_above_max_rejected);
    TEST(test_wrapping_lengths_rejected);
    TEST(test_receive_loop_stops_at_malformed_frame);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
