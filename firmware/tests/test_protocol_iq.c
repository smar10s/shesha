#include "../src/protocol.h"
#include "../src/session.h"
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

static void test_proto_build_iq_12bit_layout(void)
{
    uint8_t buf[4096];
    uint8_t packed[6] = {0x23, 0xC1, 0xAB, 0xFF, 0x0F, 0x80};
    double ts = 123.5;
    float freq = 100.1f;

    size_t len = proto_build_iq(buf, 7, ts, freq, 2000000, 12, 2, packed);

    CHECK(buf[0] == DATA_IQ);
    uint32_t plen;
    memcpy(&plen, buf + 1, 4);
    CHECK(plen == 20 + 6);            /* header + 2 samples * 3 bytes */
    uint32_t seq;
    memcpy(&seq, buf + 5, 4);
    CHECK(seq == 7);
    CHECK(len == FRAME_HEADER_SIZE + plen);

    const uint8_t *p = buf + FRAME_HEADER_SIZE;
    double got_ts;
    memcpy(&got_ts, p, 8);
    CHECK(got_ts == ts);
    float got_f;
    memcpy(&got_f, p + 8, 4);
    CHECK(got_f == freq);
    uint32_t sr;
    memcpy(&sr, p + 12, 4);
    CHECK(sr == 2000000);
    CHECK(p[16] == 12);
    CHECK(p[17] == 0);                /* pad */
    uint16_t n;
    memcpy(&n, p + 18, 2);
    CHECK(n == 2);
    CHECK(memcmp(p + 20, packed, 6) == 0);
}

static void test_proto_build_iq_8bit_layout(void)
{
    uint8_t buf[4096];
    uint8_t packed[4] = {0x12, 0xAB, 0x34, 0xCD};

    size_t len = proto_build_iq(buf, 1, 0.0, 915.0f, 2000000, 8, 2, packed);

    uint32_t plen;
    memcpy(&plen, buf + 1, 4);
    CHECK(plen == 20 + 4);            /* 2 samples * 2 bytes */
    CHECK(len == FRAME_HEADER_SIZE + plen);
    const uint8_t *p = buf + FRAME_HEADER_SIZE;
    CHECK(p[16] == 8);
    CHECK(memcmp(p + 20, packed, 4) == 0);
}

static void test_ids_are_distinct(void)
{
    CHECK(DATA_IQ == 0x23);
    CHECK(CMD_SET_IQ_BITS == 0x1C);
    CHECK(CMD_IQ_TX_STREAM_DATA == 0x1D);
    CHECK(CMD_IQ_TX_STREAM_END == 0x1E);
    CHECK(DAEMON_MODE_IQ == 2);
    CHECK(SESSION_DATA_IQ == (1u << 3));
    CHECK(DATA_IQ != DATA_PSD && DATA_IQ != DATA_WIFI && DATA_IQ != DATA_FM);
    CHECK(CMD_SET_IQ_BITS != CMD_TX_STREAM_DATA);
}

int main(void)
{
    TEST(test_proto_build_iq_12bit_layout);
    TEST(test_proto_build_iq_8bit_layout);
    TEST(test_ids_are_distinct);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
