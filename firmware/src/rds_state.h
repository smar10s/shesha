#pragma once
#include <stdint.h>

/*
 * RDS decoded station state — the part that leaves the daemon.
 *
 * Split from rds_rx.h so protocol.h and the ring slots can carry the state
 * without pulling in the decoder's filter tables. The wire layout in
 * protocol.c mirrors these fields one for one.
 */

#define RDS_PS_LEN   8
#define RDS_RT_LEN   64
#define RDS_MAX_AF   25
#define RDS_MAX_ODA  4
#define RDS_ODA_DATA 8

typedef struct {
    uint16_t aid;
    uint8_t  len;
    uint8_t  data[RDS_ODA_DATA];
} rds_oda_t;

typedef struct {
    uint8_t  synced;
    uint8_t  pi_valid;
    uint8_t  tp;
    uint8_t  ta;
    uint8_t  ms;
    uint8_t  pty;
    uint8_t  di;             /* 3 bits, bit 0 first received */
    uint16_t pi;

    uint8_t  ps[RDS_PS_LEN];
    uint8_t  ps_mask;        /* bit per received character */

    uint8_t  rt[RDS_RT_LEN];
    uint8_t  rt_len;
    uint64_t rt_mask;        /* bit per received character */
    uint8_t  rt_ab;
    uint8_t  rt_version_b;   /* 2B carries 32 characters, not 64 */
    uint8_t  rt_valid;

    uint8_t  ptyn[RDS_PS_LEN];
    uint8_t  ptyn_mask;
    uint8_t  ptyn_ab;

    uint8_t  af[RDS_MAX_AF];
    uint8_t  af_count;

    uint32_t ct_mjd;
    uint16_t ct_minutes;     /* local time, minutes since midnight */
    int8_t   ct_offset_half_hours;
    uint8_t  ct_valid;

    uint8_t  oda_count;
    rds_oda_t oda[RDS_MAX_ODA];

    uint32_t groups;
    uint32_t blocks;
    uint32_t block_errors;
    uint32_t sync_losses;
} rds_state_t;

/* Validity helpers, derived from the segment masks. */
static inline int rds_ps_valid(const rds_state_t *s)
{
    return s->ps_mask == 0xFF;
}

static inline int rds_ptyn_valid(const rds_state_t *s)
{
    return s->ptyn_mask == 0xFF;
}

static inline int rds_rt_complete(const rds_state_t *s)
{
    return s->rt_valid;
}
