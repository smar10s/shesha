/*
 * Tests for the RDS transmit group encoder (firmware/src/rds_tx.c).
 *
 * Two oracles:
 *   1. The encoded bit pattern is compared against a second, local encoder
 *      (residue-table CRC, same as test_rds_rx.c), so a wrong offset word,
 *      CRC, group type or PS/RT mapping shows up as a bit mismatch.
 *   2. The real biphase chip stream is synthesized into a 350 kHz MPX and
 *      fed through rds_rx_process, so encoder and decoder are pinned against
 *      each other end to end (the decoder's CRC is an independent
 *      formulation, per test_rds_rx.c).
 *
 * Build and run:  make test-firmware
 */
#include "../src/rds_tx.h"
#include "../src/rds_rx.h"

#include <math.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures;

#define CHECK(cond) do {                                                  \
    if (!(cond)) {                                                        \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
        failures++;                                                       \
    }                                                                     \
} while (0)

#define RUN(fn) do { printf("%s\n", #fn); fn(); } while (0)

#define MPX_RATE 350000.0
#define RDS_HZ   57000.0
#define RDS_AMP  0.03

#define OFF_A 0x0FCu
#define OFF_B 0x198u
#define OFF_C 0x168u
#define OFF_D 0x1B4u

/* --- independent encoder (same formulation as test_rds_rx.c) ---------- */

static uint16_t t_crc(uint16_t info)
{
    static const uint16_t res[16] = {
        0x1B9, 0x372, 0x35D, 0x303, 0x3BF, 0x2C7, 0x037, 0x06E,
        0x0DC, 0x1B8, 0x370, 0x359, 0x30B, 0x3AF, 0x2E7, 0x077,
    };
    uint16_t rem = 0;
    for (int i = 0; i < 16; i++)
        if ((info >> i) & 1u)
            rem ^= res[i];
    return rem;
}

static uint32_t t_block(uint16_t info, uint16_t offset)
{
    return ((uint32_t)info << 10) | (uint16_t)(t_crc(info) ^ offset);
}

/* Build the 20-group PS-then-RT cycle exactly as the encoder must. */
static void build_groups(uint16_t g[RDS_TX_GROUPS][4],
                         const rds_tx_config_t *c, unsigned rt_ab)
{
    int k = 0;
    for (int a = 0; a < 4; a++, k++) {
        g[k][0] = c->pi;
        g[k][1] = (uint16_t)((0u << 12) | ((c->tp ? 1u : 0u) << 10) |
                             ((c->pty & 0x1Fu) << 5) | (unsigned)a);
        g[k][2] = 0;
        g[k][3] = (uint16_t)(((uint8_t)c->ps[2 * a] << 8) |
                             (uint8_t)c->ps[2 * a + 1]);
    }
    for (int a = 0; a < 16; a++, k++) {
        g[k][0] = c->pi;
        g[k][1] = (uint16_t)((2u << 12) | ((c->tp ? 1u : 0u) << 10) |
                             ((c->pty & 0x1Fu) << 5) | (rt_ab << 4) |
                             (unsigned)a);
        g[k][2] = (uint16_t)(((uint8_t)c->rt[4 * a] << 8) |
                             (uint8_t)c->rt[4 * a + 1]);
        g[k][3] = (uint16_t)(((uint8_t)c->rt[4 * a + 2] << 8) |
                             (uint8_t)c->rt[4 * a + 3]);
    }
}

static void make_config(rds_tx_config_t *c)
{
    memset(c, 0, sizeof(*c));
    c->enabled = true;
    c->pi = 0x8203;
    c->pty = 10;
    c->tp = true;
    memcpy(c->ps, "TESTFM  ", 8);
    memset(c->rt, ' ', 64);
    const char *m = "SHESHA RDS TEST";
    memcpy(c->rt, m, strlen(m));
    c->rt[strlen(m)] = 0x0D;
}

/* --- tests ----------------------------------------------------------- */

static uint8_t expected_bits(const rds_tx_config_t *cfg, unsigned rt_ab,
                             uint8_t out[RDS_TX_BITS])
{
    uint16_t groups[RDS_TX_GROUPS][4];
    build_groups(groups, cfg, rt_ab);

    static const uint16_t offs[4] = { OFF_A, OFF_B, OFF_C, OFF_D };
    size_t k = 0;
    uint8_t enc = 0;
    for (int cycle = 0; cycle < RDS_TX_CYCLES; cycle++) {
        for (int g = 0; g < RDS_TX_GROUPS; g++) {
            for (int b = 0; b < 4; b++) {
                uint32_t w = t_block(groups[g][b], offs[b]);
                for (int i = 25; i >= 0; i--) {
                    enc ^= (uint8_t)((w >> i) & 1u);
                    out[k++] = enc;
                }
            }
        }
    }
    return enc;
}

static void test_bits_match_independent_encoder(void)
{
    rds_tx_config_t cfg;
    make_config(&cfg);

    rds_tx_t tx;
    rds_tx_init(&tx, (uint32_t)MPX_RATE);
    rds_tx_set_config(&tx, &cfg);

    uint8_t exp[RDS_TX_BITS];
    CHECK(expected_bits(&cfg, 0, exp) == 0);  /* pattern wraps at state 0 */

    const uint8_t *got = tx.bits[atomic_load_explicit(&tx.active,
                                                      memory_order_relaxed)];
    CHECK(memcmp(got, exp, RDS_TX_BITS) == 0);
}

static void test_back_to_back_configs_preserve_published_pattern(void)
{
    rds_tx_config_t first, second;
    make_config(&first);
    second = first;
    second.pi = 0x4321;
    second.pty = 5;
    second.tp = false;
    memcpy(second.ps, "SECOND  ", 8);
    memcpy(second.rt, "SECOND CONFIG", 13);

    rds_tx_t tx;
    rds_tx_init(&tx, (uint32_t)MPX_RATE);

    rds_tx_set_config(&tx, &first);
    uint8_t first_active = atomic_load_explicit(&tx.active,
                                                memory_order_relaxed);
    uint8_t exp_first[RDS_TX_BITS];
    CHECK(expected_bits(&first, 0, exp_first) == 0);
    CHECK(memcmp(tx.bits[first_active], exp_first, RDS_TX_BITS) == 0);

    /* No read between the two publishes: the second build must target the
     * inactive buffer, leaving the first pattern intact. */
    rds_tx_set_config(&tx, &second);
    uint8_t second_active = atomic_load_explicit(&tx.active,
                                                 memory_order_relaxed);
    CHECK(second_active != first_active);
    CHECK(memcmp(tx.bits[first_active], exp_first, RDS_TX_BITS) == 0);

    uint8_t exp_second[RDS_TX_BITS];
    CHECK(expected_bits(&second, 1, exp_second) == 0);
    CHECK(memcmp(tx.bits[second_active], exp_second, RDS_TX_BITS) == 0);
}

static uint16_t published_word(rds_tx_t *tx, int group, int block)
{
    const uint8_t *bits = tx->bits[atomic_load_explicit(
        &tx->active, memory_order_relaxed)];
    size_t start = (size_t)(group * 4 + block) * 26;
    uint8_t enc = 0;
    uint16_t word = 0;
    for (int i = 0; i < 26; i++) {
        enc ^= bits[start + i];
        word = (uint16_t)((word << 1) | enc);
    }
    return word;
}

static void test_rt_change_toggles_ab_but_ps_does_not(void)
{
    rds_tx_config_t cfg;
    make_config(&cfg);

    rds_tx_t tx;
    rds_tx_init(&tx, (uint32_t)MPX_RATE);
    rds_tx_set_config(&tx, &cfg);

    /* First 2A group is group index 4 (after four 0A groups). */
    int ab = (published_word(&tx, 4, 1) >> 4) & 1;

    memcpy(cfg.ps, "OTHER   ", 8);
    rds_tx_set_config(&tx, &cfg);
    CHECK((((published_word(&tx, 4, 1) >> 4) & 1)) == ab);

    cfg.rt[0] = 'X';
    rds_tx_set_config(&tx, &cfg);
    CHECK((((published_word(&tx, 4, 1) >> 4) & 1)) != ab);
}

static void test_disabled_gives_no_chip(void)
{
    rds_tx_config_t cfg;
    make_config(&cfg);
    cfg.enabled = false;

    rds_tx_t tx;
    rds_tx_init(&tx, (uint32_t)MPX_RATE);
    rds_tx_set_config(&tx, &cfg);

    for (int i = 0; i < 1000; i++)
        CHECK(rds_tx_chip(&tx) == 0.0f);
}

static void test_roundtrip_through_decoder(void)
{
    rds_tx_config_t cfg;
    make_config(&cfg);

    rds_tx_t tx;
    rds_tx_init(&tx, (uint32_t)MPX_RATE);
    rds_tx_set_config(&tx, &cfg);

    /* ~4.5 s of MPX: 2A cycles (1.75 s) plus acquisition.  A 1 kHz
     * programme tone + 19 kHz pilot, as a real broadcast, so the decoder's
     * filters see the same composite as test_rds_rx.c. */
    size_t n = (size_t)(4.5 * MPX_RATE);
    float *mpx = malloc(n * sizeof(float));
    for (size_t i = 0; i < n; i++) {
        double t = (double)i / MPX_RATE;
        double v = 0.45 * sin(2.0 * M_PI * 1000.0 * t)
                 + 0.09 * sin(2.0 * M_PI * 19000.0 * t);
        v += RDS_AMP * (double)rds_tx_chip(&tx)
           * cos(2.0 * M_PI * RDS_HZ * t);
        mpx[i] = (float)v;
    }

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    rds_rx_process(&r, mpx, n);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->pi_valid && s->pi == 0x8203);
    CHECK(s->pty == 10);
    CHECK(s->tp == 1);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "TESTFM  ", 8) == 0);
    CHECK(rds_rt_complete(s));

    free(mpx);
}

static void test_ps_change_survives_cycle_wrap(void)
{
    rds_tx_config_t cfg;
    make_config(&cfg);
    memcpy(cfg.ps, "OLDPS   ", 8);

    rds_tx_t tx;
    rds_tx_init(&tx, (uint32_t)MPX_RATE);
    rds_tx_set_config(&tx, &cfg);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);

    /* Acquire, then change PS and decode two full pattern repeats. */
    size_t pre = (size_t)(3.0 * MPX_RATE);
    size_t total = (size_t)(10.0 * MPX_RATE);
    float *mpx = malloc(total * sizeof(float));
    for (size_t i = 0; i < total; i++) {
        if (i == pre) {
            memcpy(cfg.ps, "NEWPS   ", 8);
            rds_tx_set_config(&tx, &cfg);
        }
        double t = (double)i / MPX_RATE;
        double v = 0.45 * sin(2.0 * M_PI * 1000.0 * t)
                 + 0.09 * sin(2.0 * M_PI * 19000.0 * t);
        v += RDS_AMP * (double)rds_tx_chip(&tx)
           * cos(2.0 * M_PI * RDS_HZ * t);
        mpx[i] = (float)v;
    }

    rds_rx_process(&r, mpx, total);
    const rds_state_t *s = rds_rx_state(&r);
    CHECK(rds_ps_valid(s));
    if (memcmp(s->ps, "NEWPS   ", 8) != 0)
        printf("  decoded PS: %.8s\n", s->ps);
    CHECK(memcmp(s->ps, "NEWPS   ", 8) == 0);
    free(mpx);
}

int main(void)
{
    RUN(test_bits_match_independent_encoder);
    RUN(test_back_to_back_configs_preserve_published_pattern);
    RUN(test_rt_change_toggles_ab_but_ps_does_not);
    RUN(test_disabled_gives_no_chip);
    RUN(test_roundtrip_through_decoder);
    RUN(test_ps_change_survives_cycle_wrap);

    if (failures) {
        printf("\n%d check(s) FAILED\n", failures);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
