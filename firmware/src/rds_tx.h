// SPDX-License-Identifier: MIT
#ifndef SHESHA_RDS_TX_H
#define SHESHA_RDS_TX_H

#include <stdbool.h>
#include <stddef.h>
#include <stdatomic.h>
#include <stdint.h>

/*
 * RDS (IEC 62106) transmit group encoder.
 *
 * The TX thread only reads a published bit pattern; configuration is built
 * off-thread into the inactive half of a double buffer and handed over by
 * flipping an atomic index.  There are no locks and no per-group state
 * machine: one rotation is 4 PS groups (0A) followed by 16 RadioText groups
 * (2A), then the pattern repeats.
 *
 * The chip convention matches the decoder's tested input: bits are
 * differentially encoded, and each bit is two biphase chips (+e, -e), so a
 * bit is the pair (+1, -1) for a 1 and (-1, +1) for a 0.
 */

/* One rotation: 4 PS groups (0A addr 0..3) + 16 RT groups (2A seg 0..15). */
#define RDS_TX_GROUPS 20

/*
 * A rotation usually has odd differential parity, so a single copy cannot
 * repeat without inverting the first bit.  Build two rotations as one
 * continuous stream: two identical rotations always have even parity, so
 * the pattern wraps at state 0.
 */
#define RDS_TX_CYCLES 2
#define RDS_TX_BITS   (RDS_TX_GROUPS * RDS_TX_CYCLES * 104)  /* ~3.5 s */

#define RDS_TX_BITRATE_HZ 1187.5

typedef struct {
    bool     enabled;
    uint16_t pi;
    uint8_t  pty;        /* 0..31 */
    bool     tp;
    char     ps[8];      /* space padded */
    char     rt[64];     /* space padded, 0x0D terminator if short */
} rds_tx_config_t;

typedef struct {
    uint32_t dac_rate;
    double   bit_step;              /* 1187.5 / dac_rate, bits per sample */
    double   bit_pos;               /* position within the active pattern */
    _Atomic bool    enabled;
    _Atomic uint8_t active;         /* which pattern buffer is published */
    uint8_t  rt_ab;                 /* RadioText A/B flag in 2A block B */
    bool     have_rt;
    char     last_rt[64];
    uint8_t  bits[2][RDS_TX_BITS];
} rds_tx_t;

/* Arm the encoder at the DAC sample rate.  Disabled until configured. */
void rds_tx_init(rds_tx_t *t, uint32_t dac_rate);

/* Off-thread: build the inactive buffer, then publish it. */
void rds_tx_set_config(rds_tx_t *t, const rds_tx_config_t *cfg);

/*
 * TX thread: biphase chip (+1/-1) for the current DAC sample, advancing
 * the bit clock by one sample.  Returns 0 while disabled.
 */
static inline float rds_tx_chip(rds_tx_t *t)
{
    uint8_t a = atomic_load_explicit(&t->active, memory_order_acquire);
    if (!atomic_load_explicit(&t->enabled, memory_order_relaxed))
        return 0.0f;

    double pos = t->bit_pos;
    size_t bit = (size_t)pos;
    double frac = pos - (double)bit;
    float e = t->bits[a][bit] ? 1.0f : -1.0f;
    float chip = (frac < 0.5) ? e : -e;

    pos += t->bit_step;
    while (pos >= (double)RDS_TX_BITS)
        pos -= (double)RDS_TX_BITS;
    t->bit_pos = pos;

    return chip;
}

#endif /* SHESHA_RDS_TX_H */
