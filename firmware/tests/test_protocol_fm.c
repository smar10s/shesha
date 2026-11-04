/*
 * Layout test for DATA_FM. The client parses this frame byte for byte, and
 * a firmware/client disagreement yields plausible garbage rather than an
 * error, so the offsets are pinned here and mirrored in
 * client/tests/test_protocol_fm.py.
 *
 * The RDS state block sits before the variable-length audio so every RDS
 * offset is constant.
 */
#include "../src/fm_protocol.h"

#include <stdio.h>
#include <string.h>

static int fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

static void test_rds_block_layout(void)
{
    rds_state_t rds;
    memset(&rds, 0, sizeof(rds));
    rds.synced = 1;
    rds.pi_valid = 1;
    rds.tp = 1;
    rds.ta = 1;
    rds.ms = 1;
    rds.pty = 10;
    rds.di = 0x5;
    rds.pi = 0x8203;
    memcpy(rds.ps, "TESTFM  ", 8);
    rds.ps_mask = 0xFF;
    memcpy(rds.rt, "Hello world", 11);
    rds.rt_len = 11;
    rds.rt_mask = 0x7FF;
    rds.rt_ab = 1;
    rds.rt_valid = 1;
    memcpy(rds.ptyn, "ROCK ON ", 8);
    rds.ptyn_mask = 0xFF;
    rds.af[0] = 126;
    rds.af[1] = 92;
    rds.af_count = 2;
    rds.ct_mjd = 53142;
    rds.ct_minutes = 12 * 60 + 34;
    rds.ct_offset_half_hours = 3;
    rds.ct_valid = 1;
    rds.oda_count = 1;
    rds.oda[0].aid = 0x4BD7;
    rds.oda[0].len = 3;
    rds.oda[0].data[0] = 0x12;
    rds.oda[0].data[1] = 0x34;
    rds.oda[0].data[2] = 0x56;
    rds.groups = 7;
    rds.blocks = 28;
    rds.block_errors = 1;
    rds.sync_losses = 2;

    static const float audio[4] = { 0.1f, -0.2f, 0.3f, -0.4f };
    uint8_t buf[1024];
    size_t len = proto_build_fm(buf, 42, 1.5, 43750, 4, audio, &rds);

    CHECK(PROTO_FM_HEADER_SIZE == 16);
    CHECK(PROTO_RDS_WIRE_SIZE == 190);
    CHECK(buf[0] == DATA_FM);

    uint32_t plen;
    memcpy(&plen, buf + 1, 4);
    CHECK(plen == 16 + 190 + 4 * 4);
    CHECK(len == FRAME_HEADER_SIZE + plen);

    uint32_t seq;
    memcpy(&seq, buf + 5, 4);
    CHECK(seq == 42);

    const uint8_t *p = buf + FRAME_HEADER_SIZE;
    double ts;
    memcpy(&ts, p + 0, 8);
    CHECK(ts == 1.5);
    uint32_t rate, n;
    memcpy(&rate, p + 8, 4);
    memcpy(&n, p + 12, 4);
    CHECK(rate == 43750);
    CHECK(n == 4);

    const uint8_t *m = p + PROTO_FM_HEADER_SIZE;
    /* synced, pi_valid, ps_valid, rt_valid, ptyn_valid, ct_valid, tp, ta */
    CHECK(m[0] == 0xFF);
    /* ms, rt_ab, ptyn_ab=0, di=0x5<<3 */
    CHECK(m[1] == 0x2B);

    uint16_t pi;
    memcpy(&pi, m + 2, 2);
    CHECK(pi == 0x8203);
    CHECK(m[4] == 10);
    CHECK(memcmp(m + 5, "TESTFM  ", 8) == 0);
    CHECK(m[13] == 0xFF);
    CHECK(m[14] == 11);
    CHECK(memcmp(m + 15, "Hello world", 11) == 0);
    uint64_t rtmask;
    memcpy(&rtmask, m + 79, 8);
    CHECK(rtmask == 0x7FF);
    CHECK(m[87] == 0xFF);
    CHECK(memcmp(m + 88, "ROCK ON ", 8) == 0);
    CHECK(m[96] == 2);
    CHECK(m[97] == 126 && m[98] == 92);

    uint32_t mjd;
    memcpy(&mjd, m + 122, 4);
    CHECK(mjd == 53142);
    uint16_t minutes;
    memcpy(&minutes, m + 126, 2);
    CHECK(minutes == 754);
    CHECK((int8_t)m[128] == 3);
    CHECK(m[129] == 1);
    uint16_t aid;
    memcpy(&aid, m + 130, 2);
    CHECK(aid == 0x4BD7);
    CHECK(m[132] == 3);
    CHECK(m[133] == 0x12 && m[134] == 0x34 && m[135] == 0x56);

    uint32_t v;
    memcpy(&v, m + 174, 4);
    CHECK(v == 7);
    memcpy(&v, m + 178, 4);
    CHECK(v == 28);
    memcpy(&v, m + 182, 4);
    CHECK(v == 1);
    memcpy(&v, m + 186, 4);
    CHECK(v == 2);

    float got;
    memcpy(&got, m + 190, 4);
    CHECK(got == 0.1f);
    memcpy(&got, m + 190 + 12, 4);
    CHECK(got == -0.4f);
}

static void test_null_rds_zeroes_the_block(void)
{
    static const float audio[2] = { 0.5f, -0.5f };
    uint8_t buf[512];
    size_t len = proto_build_fm(buf, 1, 0.25, 43750, 2, audio, NULL);
    CHECK(len == FRAME_HEADER_SIZE + PROTO_FM_HEADER_SIZE
                + PROTO_RDS_WIRE_SIZE + 8);
    const uint8_t *m = buf + FRAME_HEADER_SIZE + PROTO_FM_HEADER_SIZE;
    for (int i = 0; i < PROTO_RDS_WIRE_SIZE; i++)
        CHECK(m[i] == 0);

    float got;
    memcpy(&got, m + PROTO_RDS_WIRE_SIZE, 4);
    CHECK(got == 0.5f);
}

static void test_rds_tx_block_roundtrip(void)
{
    rds_tx_config_t cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.enabled = true;
    cfg.tp = true;
    cfg.pi = 0x8203;
    cfg.pty = 10;
    memcpy(cfg.ps, "TESTFM  ", 8);
    memset(cfg.rt, ' ', 64);
    memcpy(cfg.rt, "SHESHA RDS", 10);

    uint8_t b[PROTO_RDS_TX_SIZE];
    proto_pack_rds_tx(b, &cfg);

    CHECK(PROTO_RDS_TX_SIZE == 76);
    CHECK(b[0] == 0x03);              /* bit0 enabled, bit1 tp */
    uint16_t pi;
    memcpy(&pi, b + 1, 2);
    CHECK(pi == 0x8203);
    CHECK(b[3] == 10);
    CHECK(memcmp(b + 4, "TESTFM  ", 8) == 0);
    CHECK(memcmp(b + 12, cfg.rt, 64) == 0);

    rds_tx_config_t out;
    proto_unpack_rds_tx(b, &out);
    CHECK(out.enabled && out.tp);
    CHECK(out.pi == 0x8203);
    CHECK(out.pty == 10);
    CHECK(memcmp(out.ps, "TESTFM  ", 8) == 0);
    CHECK(memcmp(out.rt, cfg.rt, 64) == 0);

    /* Disabled and no TP packs to an all-zero first byte. */
    cfg.enabled = false;
    cfg.tp = false;
    proto_pack_rds_tx(b, &cfg);
    CHECK(b[0] == 0x00);
}

int main(void)
{
    TEST(test_rds_block_layout);
    TEST(test_null_rds_zeroes_the_block);
    TEST(test_rds_tx_block_roundtrip);

    if (fails) {
        printf("\n%d check(s) FAILED\n", fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
