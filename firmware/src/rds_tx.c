#include "rds_tx.h"

#include <string.h>

/*
 * RDS block CRC and offset words (IEC 62106) — the transmit half of the
 * pair in rds_rx.c.  Encoding is the reference residue-table form (also
 * used by tests and tools), so the two directions never share a bug.
 */
#define RDS_CRC_RES_COUNT 16

static const uint16_t rds_crc_res[RDS_CRC_RES_COUNT] = {
    0x1B9, 0x372, 0x35D, 0x303, 0x3BF, 0x2C7, 0x037, 0x06E,
    0x0DC, 0x1B8, 0x370, 0x359, 0x30B, 0x3AF, 0x2E7, 0x077,
};

#define OFF_A 0x0FCu
#define OFF_B 0x198u
#define OFF_C 0x168u
#define OFF_D 0x1B4u

static uint16_t rds_enc_crc(uint16_t info)
{
    uint16_t rem = 0;
    for (int i = 0; i < RDS_CRC_RES_COUNT; i++)
        if ((info >> i) & 1u)
            rem ^= rds_crc_res[i];
    return rem;
}

static uint32_t rds_enc_block(uint16_t info, uint16_t offset)
{
    return ((uint32_t)info << 10) | (uint16_t)(rds_enc_crc(info) ^ offset);
}

/*
 * Serialise one group (4 information words) into the differential bit
 * stream.  `enc` carries the running differential state across groups, so
 * the pattern is one continuous stream rather than 20 independent ones.
 */
static void put_group(uint8_t *bits, size_t *k, uint8_t *enc,
                      const uint16_t w[4])
{
    static const uint16_t offs[4] = { OFF_A, OFF_B, OFF_C, OFF_D };

    for (int b = 0; b < 4; b++) {
        uint32_t word = rds_enc_block(w[b], offs[b]);
        for (int i = 25; i >= 0; i--) {
            *enc ^= (uint8_t)((word >> i) & 1u);
            bits[(*k)++] = *enc;
        }
    }
}

/* Two continuous rotations of 4 PS groups then 16 RT groups. */
static void build_pattern(rds_tx_t *t, const rds_tx_config_t *cfg,
                           uint8_t rt_ab)
{
    uint8_t cur = atomic_load_explicit(&t->active, memory_order_relaxed);
    uint8_t *bits = t->bits[cur ^ 1u];

    size_t k = 0;
    uint8_t enc = 0;
    uint16_t tp = cfg->tp ? 1u : 0u;
    uint16_t pty = (uint16_t)(cfg->pty & 0x1Fu);

    for (unsigned cycle = 0; cycle < RDS_TX_CYCLES; cycle++) {
        for (unsigned a = 0; a < 4; a++) {
            uint16_t w[4];
            w[0] = cfg->pi;
            w[1] = (uint16_t)((0u << 12) | (tp << 10) | (pty << 5) | a);
            w[2] = 0;
            w[3] = (uint16_t)(((uint8_t)cfg->ps[2 * a] << 8) |
                              (uint8_t)cfg->ps[2 * a + 1]);
            put_group(bits, &k, &enc, w);
        }

        for (unsigned a = 0; a < 16; a++) {
            uint16_t w[4];
            w[0] = cfg->pi;
            w[1] = (uint16_t)((2u << 12) | (tp << 10) | (pty << 5) |
                              ((uint16_t)rt_ab << 4) | a);
            w[2] = (uint16_t)(((uint8_t)cfg->rt[4 * a] << 8) |
                              (uint8_t)cfg->rt[4 * a + 1]);
            w[3] = (uint16_t)(((uint8_t)cfg->rt[4 * a + 2] << 8) |
                              (uint8_t)cfg->rt[4 * a + 3]);
            put_group(bits, &k, &enc, w);
        }
    }
}

void rds_tx_init(rds_tx_t *t, uint32_t dac_rate)
{
    t->dac_rate = dac_rate;
    t->bit_step = (dac_rate > 0)
                    ? RDS_TX_BITRATE_HZ / (double)dac_rate
                    : 0.0;
    t->bit_pos = 0.0;
    memset(t->bits[0], 0, sizeof(t->bits[0]));
    memset(t->bits[1], 0, sizeof(t->bits[1]));
    t->rt_ab = 0;
    t->have_rt = false;
    memset(t->last_rt, 0, sizeof(t->last_rt));
    atomic_store_explicit(&t->enabled, false, memory_order_relaxed);
    atomic_store_explicit(&t->active, 0, memory_order_relaxed);
}

void rds_tx_set_config(rds_tx_t *t, const rds_tx_config_t *cfg)
{
    uint8_t ab = t->rt_ab;
    if (t->have_rt && memcmp(t->last_rt, cfg->rt, sizeof(t->last_rt)) != 0)
        ab ^= 1u;
    build_pattern(t, cfg, ab);
    t->rt_ab = ab;
    memcpy(t->last_rt, cfg->rt, sizeof(t->last_rt));
    t->have_rt = true;

    /* Publish: the relaxed enabled store is ordered before the release
     * store of active, so a reader that acquires the new active also sees
     * the new enabled. */
    atomic_store_explicit(&t->enabled, cfg->enabled, memory_order_relaxed);
    uint8_t cur = atomic_load_explicit(&t->active, memory_order_relaxed);
    atomic_store_explicit(&t->active, (uint8_t)(cur ^ 1u),
                          memory_order_release);
}
