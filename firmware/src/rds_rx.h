#pragma once
#include <stdint.h>
#include <stddef.h>
#include "fir.h"
#include "rds_state.h"
#include "rds_decim_taps.h"

/*
 * RDS (IEC 62106) subcarrier decoder.
 *
 * Input is the FM multiplex at 350 kHz, the stage-1 output of the audio
 * decimation chain (fm_rx.c). The chain is:
 *
 *   mix 57 kHz (exact 350-sample NCO)
 *   -> decimate 16 (350 kHz -> 21875 Hz)
 *   -> decimate 2  (21875 -> 10937.5 Hz, complex baseband)
 *   -> resample to 19000 Hz = 16 samples per 1187.5 bps data bit
 *   -> biphase bank, one hypothesis per sample of the bit period
 *   -> differential BPSK bits
 *   -> 26-bit block sync / CRC
 *   -> group decode into rds_state_t
 *
 * Why a phase bank instead of a timing PLL: the sample clock and the
 * transmitter's symbol clock are independent, so the symbol phase can be
 * anywhere and drifts by ~1 sample per 5 s at 10 ppm. The hypotheses cover
 * the phase axis at full sample resolution; each runs its own CRC, and the
 * selector simply follows whichever is producing valid blocks. There is no
 * loop to tune and no lock acquisition dynamics, and sample-clock drift
 * walks the symbol phase slowly across the grid where the selector picks it
 * up. The differential detector removes carrier phase entirely.
 */

/* MPX tap rate and the baseband rate it decimates to. */
#define RDS_MPX_RATE        350000
#define RDS_BASEBAND_HZ     10937.5f

/* Resampled rate: exactly 16 samples per data bit, 8 per biphase chip. */
#define RDS_BITRATE_HZ      1187.5f
#define RDS_CHIP_HZ         2375.0f
#define RDS_SAMPLES_PER_BIT 16
/*
 * One timing hypothesis per sample of the 16-sample bit period. Spacing
 * them 2 samples apart (8 phases) would leave the worst case a whole
 * sample off, and the biphase matched filter integrates only 8 samples per
 * half, so that is a real loss. The cost of the extra decoders is a few
 * hundred bytes and a few thousand operations a second.
 */
#define RDS_PHASES          RDS_SAMPLES_PER_BIT

/* --- internal --- */

/* One decoder per timing phase and polarity convention. */
#define RDS_DECODERS (RDS_PHASES * 2)

/*
 * Consecutive A blocks that must carry the same PI before it is committed to
 * the wire state. A random 26-bit window matches an offset syndrome about
 * once per 1024 bit positions per phase, so a single match can be noise; two
 * in a row from different groups cannot realistically be. This is what stops
 * a marginal station from showing random PI values while unsynced.
 */
#define RDS_PI_CONFIRM 2

/*
 * With no dispatched group for this many MPX samples, the decoded station is
 * dropped: the signal is gone (retune, out of range, or dead air), so the
 * last station's PI/PS/RT must not stay on the wire. 2 s rides out a short
 * fade but clears the display promptly.
 */
#define RDS_STATE_TIMEOUT_SAMPLES (RDS_MPX_RATE * 2)

typedef struct {
    float    acc_i[2];       /* biphase matched accumulator, two halves */
    float    acc_q[2];
    float    prev_i;         /* previous bit's matched value */
    float    prev_q;
} rds_phase_t;

typedef struct {
    uint32_t sr;             /* last 26 bits, newest at bit 0 */
    uint8_t  nbits;
    uint8_t  in_sync;
    uint8_t  group_pos;      /* 0..3, next expected block index */
    uint8_t  version_b;
    uint8_t  bad;            /* current group incomplete/invalid */
    uint8_t  errs;           /* consecutive bad blocks while in sync */
    uint16_t block[4];       /* information words of the current group */
    uint32_t score;          /* leaky valid-block count, for selection */
} rds_blk_t;

typedef struct {
    /* mixer */
    uint16_t mix_idx;

    /* decimation: two fir_t per stage, one each for I and Q */
    fir_t s1_i, s1_q, s2_i, s2_q;

    /* cubic resampler 10937.5 -> 19000 Hz */
    float    rb_i[4], rb_q[4];
    uint64_t n_in;           /* input samples seen, for the history ring */
    double   t_out;          /* next output position, input-sample units */
    uint32_t n_out;

    /* biphase bank */
    rds_phase_t phase[RDS_PHASES];
    rds_blk_t   blk[RDS_DECODERS];
    int         selected;

    /* group-decode bookkeeping that is not part of the wire state */
    uint8_t     rt_started;
    uint8_t     ptyn_started;
    int8_t      oda_current;

    /*
     * PI confirmation across A blocks. Once the committed PI is confirmed,
     * a different PI must be seen RDS_PI_CONFIRM times before it replaces
     * the station and resets its PS/RT.
     */
    uint16_t    pi_cand;
    uint8_t     pi_cand_hits;

    /* MPX samples since the last dispatched group; station clear on expiry. */
    uint32_t    quiet_samples;

    rds_state_t state;
} rds_rx_t;

/*
 * Arm a decoder. The tap tables are rate-locked, like the audio chain: they
 * are designed for 350000 Hz in and 10937.5 Hz out only. Returns 0, or -1
 * if a table fails its symmetry check.
 */
int rds_rx_init(rds_rx_t *r);

/* Drop all signal state and station state, as at init. */
void rds_rx_reset(rds_rx_t *r);

/*
 * Feed `n` real MPX samples at RDS_MPX_RATE. Output is latched into
 * rds_rx_state(); nothing is returned.
 */
void rds_rx_process(rds_rx_t *r, const float *mpx, size_t n);

static inline const rds_state_t *rds_rx_state(const rds_rx_t *r)
{
    return &r->state;
}
