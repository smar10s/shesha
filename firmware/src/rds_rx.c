#include "rds_rx.h"

#include <string.h>

/*
 * RDS block CRC and offset words (IEC 62106).
 *
 * A 26-bit block is a shortened cyclic code: 16 information bits MSB first,
 * followed by 10 check bits that are the remainder of info(x)*x^10 modulo
 * g(x), XORed with a 10-bit offset word identifying the block position.
 * Added as polynomials, the offset makes a valid block's remainder equal
 * the offset word, so the receiver only has to compare a syndrome.
 */
#define RDS_CRC_POLY   0x5B9u   /* x^10+x^8+x^7+x^5+x^4+x^3+1 */
#define RDS_OFFSET_A   0x0FCu
#define RDS_OFFSET_B   0x198u
#define RDS_OFFSET_C   0x168u
#define RDS_OFFSET_C1  0x350u
#define RDS_OFFSET_D   0x1B4u

/*
 * The 26-bit window has the oldest received bit at bit 25, so the value is
 * already the polynomial in transmission order. Plain long division; 16
 * iterations per block, and blocks are only 45/s per decoder, so a table
 * would save nothing measurable.
 */
static uint16_t rds_syndrome(uint32_t w)
{
    uint32_t reg = w & 0x3FFFFFFu;
    for (int i = 25; i >= 10; i--) {
        if (reg & (1u << i))
            reg ^= (uint32_t)RDS_CRC_POLY << (i - 10);
    }
    return (uint16_t)(reg & 0x3FFu);
}

/* Block index (0=A, 1=B, 2=C or C', 3=D) for a syndrome, or -1. */
static int rds_offset_index(uint16_t syn)
{
    switch (syn) {
    case RDS_OFFSET_A:  return 0;
    case RDS_OFFSET_B:  return 1;
    case RDS_OFFSET_C:  return 2;
    case RDS_OFFSET_C1: return 2;
    case RDS_OFFSET_D:  return 3;
    default:            return -1;
    }
}

/* --- station state bookkeeping ---------------------------------------- */

static void rds_reset_station(rds_rx_t *r)
{
    rds_state_t *s = &r->state;

    s->ps_mask = 0;
    memset(s->ps, 0, sizeof(s->ps));

    s->rt_mask = 0;
    s->rt_len = 0;
    s->rt_valid = 0;
    s->rt_ab = 0;
    s->rt_version_b = 0;
    memset(s->rt, 0, sizeof(s->rt));

    s->ptyn_mask = 0;
    s->ptyn_ab = 0;
    memset(s->ptyn, 0, sizeof(s->ptyn));

    s->af_count = 0;
    s->ct_valid = 0;
    s->oda_count = 0;
    s->ta = 0;
    s->ms = 0;
    s->di = 0;

    s->pi = 0;
    s->pi_valid = 0;
    s->synced = 0;

    r->rt_started = 0;
    r->ptyn_started = 0;
    r->oda_current = -1;
    r->pi_cand = 0;
    r->pi_cand_hits = 0;
}

/* Any field that identifies a station on screen. */
static int rds_station_present(const rds_state_t *s)
{
    return s->pi_valid || s->ps_mask || s->rt_mask || s->rt_valid ||
           s->ptyn_mask || s->af_count || s->ct_valid || s->oda_count;
}

static void rds_note_pi(rds_rx_t *r, uint16_t pi)
{
    rds_state_t *s = &r->state;

    if (s->pi_valid && s->pi == pi) {
        r->pi_cand_hits = 0;
        return;
    }

    if (r->pi_cand_hits && r->pi_cand == pi) {
        if (++r->pi_cand_hits < RDS_PI_CONFIRM)
            return;
        if (s->pi_valid)
            rds_reset_station(r);
        s->pi = pi;
        s->pi_valid = 1;
        r->pi_cand_hits = 0;
        return;
    }

    r->pi_cand = pi;
    r->pi_cand_hits = 1;
}

static void rds_decode_af(rds_state_t *s, uint8_t code)
{
    if (code >= 1 && code <= 204) {
        for (int i = 0; i < s->af_count; i++) {
            if (s->af[i] == code)
                return;
        }
        if (s->af_count < RDS_MAX_AF)
            s->af[s->af_count++] = code;
    } else if (code == 224) {
        /* "No AF exists" */
        s->af_count = 0;
    }
}

static void rds_ps_seg(rds_state_t *s, unsigned addr, uint16_t w)
{
    s->ps[addr * 2]     = (uint8_t)(w >> 8);
    s->ps[addr * 2 + 1] = (uint8_t)w;
    s->ps_mask |= (uint8_t)(3u << (addr * 2));
}

/*
 * One radio-text character. A 0x0D terminator locks the length from then
 * on; characters after it are stale text from a longer previous message and
 * are ignored. Validity is recomputed from the mask so a segment received
 * once and then lost cannot pass.
 */
static void rds_rt_put(rds_state_t *s, unsigned pos, uint8_t c)
{
    if (pos >= RDS_RT_LEN)
        return;
    if (c == 0x0D) {
        if (s->rt_len == 0 || pos < s->rt_len)
            s->rt_len = (uint8_t)pos;
        return;
    }
    if (s->rt_len != 0 && pos >= s->rt_len)
        return;
    s->rt[pos] = c;
    s->rt_mask |= 1ull << pos;
}

static void rds_rt_validity(rds_state_t *s)
{
    /* Version B has half the payload (one character per address), so a
     * full message with no terminator is 32 characters, not 64. */
    uint64_t full = s->rt_version_b ? 0xFFFFFFFFull : ~0ull;

    if (s->rt_len == 0) {
        if ((s->rt_mask & full) == full) {
            s->rt_len = s->rt_version_b ? 32 : RDS_RT_LEN;
            s->rt_valid = 1;
        } else {
            s->rt_valid = 0;
        }
        return;
    }
    uint64_t need = (s->rt_len >= 64) ? ~0ull
                                      : ((1ull << s->rt_len) - 1ull);
    need &= full;
    s->rt_valid = (s->rt_mask & need) == need;
}

static void rds_rt_seg(rds_rx_t *r, int version_b, unsigned addr, int ab,
                       uint16_t c, uint16_t d)
{
    rds_state_t *s = &r->state;

    if (!r->rt_started || ab != s->rt_ab
        || version_b != s->rt_version_b) {
        s->rt_mask = 0;
        s->rt_len = 0;
        s->rt_valid = 0;
        s->rt_ab = (uint8_t)ab;
        s->rt_version_b = (uint8_t)version_b;
        memset(s->rt, 0, sizeof(s->rt));
        r->rt_started = 1;
    }

    if (!version_b) {
        unsigned base = addr * 4;
        rds_rt_put(s, base + 0, (uint8_t)(c >> 8));
        rds_rt_put(s, base + 1, (uint8_t)c);
        rds_rt_put(s, base + 2, (uint8_t)(d >> 8));
        rds_rt_put(s, base + 3, (uint8_t)d);
    } else {
        unsigned base = addr * 2;
        rds_rt_put(s, base + 0, (uint8_t)(d >> 8));
        rds_rt_put(s, base + 1, (uint8_t)d);
    }
    rds_rt_validity(s);
}

static void rds_ptyn_seg(rds_rx_t *r, unsigned app, uint16_t c, uint16_t d)
{
    rds_state_t *s = &r->state;
    int ab = (app >> 4) & 1;
    unsigned addr = app & 1u;

    if (!r->ptyn_started || ab != s->ptyn_ab) {
        s->ptyn_mask = 0;
        s->ptyn_ab = (uint8_t)ab;
        memset(s->ptyn, 0, sizeof(s->ptyn));
        r->ptyn_started = 1;
    }

    unsigned base = addr * 4;
    s->ptyn[base + 0] = (uint8_t)(c >> 8);
    s->ptyn[base + 1] = (uint8_t)c;
    s->ptyn[base + 2] = (uint8_t)(d >> 8);
    s->ptyn[base + 3] = (uint8_t)d;
    s->ptyn_mask |= (uint8_t)(0xFu << base);
}

/*
 * Clock time (4A). Block B's low two bits are the MJD MSBs, block C the
 * other 15 MJD bits plus the hour MSB, and block D the rest of the hour,
 * the minute and the signed half-hour offset.
 */
static void rds_decode_ct(rds_state_t *s, uint16_t b, uint16_t c, uint16_t d)
{
    uint32_t mjd = ((uint32_t)(b & 0x3u) << 15) | (uint32_t)(c >> 1);
    unsigned hour = (unsigned)(((c & 1u) << 4) | (d >> 12));
    unsigned minute = (unsigned)((d >> 6) & 0x3Fu);
    unsigned off = (unsigned)(d & 0x3Fu);

    if (hour > 23 || minute > 59)
        return;

    int sign = (off & 0x20u) ? -1 : 1;
    int mag = (int)(off & 0x1Fu);

    s->ct_mjd = mjd;
    s->ct_minutes = (uint16_t)(hour * 60 + minute);
    s->ct_offset_half_hours = (int8_t)(sign * mag);
    s->ct_valid = 1;
}

static void rds_decode_oda(rds_rx_t *r, int version_b, unsigned app,
                           uint16_t c, uint16_t d)
{
    rds_state_t *s = &r->state;

    if (!version_b) {
        uint16_t aid = c;
        int idx = -1;
        for (int i = 0; i < s->oda_count; i++) {
            if (s->oda[i].aid == aid) {
                idx = i;
                break;
            }
        }
        if (idx < 0) {
            if (s->oda_count < RDS_MAX_ODA)
                idx = s->oda_count++;
            else
                idx = RDS_MAX_ODA - 1;
            s->oda[idx].aid = aid;
            s->oda[idx].len = 0;
            memset(s->oda[idx].data, 0, RDS_ODA_DATA);
        }
        r->oda_current = (int8_t)idx;
    }
    (void)app;

    int idx = r->oda_current;
    if (idx < 0 || idx >= s->oda_count)
        return;

    rds_oda_t *o = &s->oda[idx];
    if (o->len + 2 <= RDS_ODA_DATA) {
        o->data[o->len] = (uint8_t)(d >> 8);
        o->data[o->len + 1] = (uint8_t)d;
        o->len += 2;
    } else {
        memmove(o->data, o->data + 2, RDS_ODA_DATA - 2);
        o->data[RDS_ODA_DATA - 2] = (uint8_t)(d >> 8);
        o->data[RDS_ODA_DATA - 1] = (uint8_t)d;
    }
}

/* Dispatch one complete, CRC-valid group. */
static void rds_dispatch(rds_rx_t *r, const uint16_t *b, int version_b)
{
    rds_state_t *s = &r->state;
    uint16_t w = b[1];
    unsigned gtype = (w >> 12) & 0xFu;
    unsigned app = w & 0x1Fu;

    r->quiet_samples = 0;

    s->groups++;
    s->tp = (uint8_t)((w >> 10) & 1u);
    s->pty = (uint8_t)((w >> 5) & 0x1Fu);

    switch (gtype) {
    case 0:
        s->ta = (uint8_t)((app >> 4) & 1u);
        s->ms = (uint8_t)((app >> 3) & 1u);
        {
            unsigned addr = app & 3u;
            s->di = (uint8_t)((s->di & ~(1u << addr)) |
                              (((app >> 2) & 1u) << addr));
        }
        if (!version_b) {
            rds_decode_af(s, (uint8_t)(b[2] >> 8));
            rds_decode_af(s, (uint8_t)b[2]);
        }
        rds_ps_seg(s, app & 3u, b[3]);
        break;

    case 2:
        rds_rt_seg(r, version_b, app & 0xFu, (int)((app >> 4) & 1u),
                   b[2], b[3]);
        break;

    case 3:
        rds_decode_oda(r, version_b, app, b[2], b[3]);
        break;

    case 4:
        if (!version_b)
            rds_decode_ct(s, b[1], b[2], b[3]);
        break;

    case 10:
        if (!version_b)
            rds_ptyn_seg(r, app, b[2], b[3]);
        break;

    default:
        break;
    }
}

/* --- block sync -------------------------------------------------------- */

static void rds_blk_feed(rds_rx_t *r, int idx, int bit)
{
    rds_blk_t *b = &r->blk[idx];
    const int selected = (idx == r->selected);

    b->sr = ((b->sr << 1) | (uint32_t)(bit & 1)) & 0x3FFFFFFu;
    b->nbits++;

    if (!b->in_sync) {
        /* Slide one bit at a time until a syndrome matches. Stepping a
         * whole block instead would never find sync if the first bit of
         * the stream is a partial window or a bit slip has occurred. */
        if (b->nbits < 26)
            return;
        uint16_t syn = rds_syndrome(b->sr);
        int oi = rds_offset_index(syn);
        b->score -= b->score >> 4;
        if (oi < 0)
            return;
        b->in_sync = 1;
        b->nbits = 0;
        b->errs = 0;
        b->version_b = (uint8_t)(syn == RDS_OFFSET_C1);
        b->group_pos = (uint8_t)((oi + 1) & 3);
        b->block[oi] = (uint16_t)((b->sr >> 10) & 0xFFFFu);
        b->bad = (uint8_t)(oi != 0);
        b->score += 8;
        if (selected && oi == 0)
            rds_note_pi(r, b->block[0]);
        return;
    }

    if (b->nbits < 26)
        return;
    b->nbits = 0;

    uint16_t syn = rds_syndrome(b->sr);
    if (selected)
        r->state.blocks++;

    b->score -= b->score >> 4;

    int pos = b->group_pos;
    int ok = 0;
    if (pos == 2) {
        if (syn == RDS_OFFSET_C) {
            ok = 1;
            b->version_b = 0;
        } else if (syn == RDS_OFFSET_C1) {
            ok = 1;
            b->version_b = 1;
        }
    } else if ((pos == 0 && syn == RDS_OFFSET_A) ||
               (pos == 1 && syn == RDS_OFFSET_B) ||
               (pos == 3 && syn == RDS_OFFSET_D)) {
        ok = 1;
    }

    if (!ok) {
        b->bad = 1;
        if (selected)
            r->state.block_errors++;
        if (++b->errs >= 4) {
            if (selected)
                r->state.sync_losses++;
            b->in_sync = 0;
        }
    } else {
        b->errs = 0;
        b->score += 16;
        b->block[pos] = (uint16_t)((b->sr >> 10) & 0xFFFFu);
        if (pos == 0) {
            b->bad = 0;
            if (selected)
                rds_note_pi(r, b->block[0]);
        } else if (pos == 3) {
            if (!b->bad && selected) {
                /* PI is confirmed on A blocks only (rds_note_pi), so the
                 * D block's copy of it is deliberately not re-noted: doing
                 * so would let one group confirm itself. */
                rds_dispatch(r, b->block, b->version_b);
            }
            b->bad = 1;   /* next group starts at the next A */
        }
    }
    b->group_pos = (uint8_t)((pos + 1) & 3);
}

/*
 * The 16 decoders are all decoding the same air, so the one that is
 * bit-aligned and polarity-correct is the one whose blocks pass CRC. A
 * leaky count of valid blocks is enough to find it; the margin avoids
 * flapping between two adjacent phases.
 */
static void rds_select(rds_rx_t *r)
{
    int best = 0;
    uint32_t best_score = r->blk[0].score;
    for (int i = 1; i < RDS_DECODERS; i++) {
        if (r->blk[i].score > best_score) {
            best_score = r->blk[i].score;
            best = i;
        }
    }
    if (best != r->selected &&
        best_score > r->blk[r->selected].score +
                     (r->blk[r->selected].score >> 1) + 4) {
        r->selected = best;
    }
    r->state.synced = r->blk[r->selected].in_sync;
}

/* --- physical layer ---------------------------------------------------- */

/*
 * Catmull-Rom cubic. The input is oversampled (10.9 kHz for a 2.4 kHz
 * signal), but linear interpolation still bends the band edge by several
 * percent at the worst phase; four points cost almost nothing at 19 kHz.
 * ym1/y0/y1/y2 are the samples around y0, and mu is the position between
 * y0 and y1.
 */
static inline float rds_cubic(float ym1, float y0, float y1, float y2,
                              float mu)
{
    float a = 0.5f * (y1 - ym1);
    float b = ym1 - 2.5f * y0 + 2.0f * y1 - 0.5f * y2;
    float c = 0.5f * (y2 - ym1) + 1.5f * (y0 - y1);
    return y0 + mu * (a + mu * (b + mu * c));
}

/*
 * Feed one phase's bit to both polarity conventions. The timing phase
 * ambiguity is real; the polarity one is the receiver's choice of which
 * differential transition means "1", and the CRC decides.
 */
static void rds_bank_bit(rds_rx_t *r, int phase, int neg)
{
    rds_blk_feed(r, phase * 2 + 0, neg);
    rds_blk_feed(r, phase * 2 + 1, !neg);
}

/*
 * One sample of the 19000 Hz stream. Each timing hypothesis h integrates a
 * different 16-sample window; the window is two biphase chips, and the
 * matched value is the first half minus the second half, which is +/-A for
 * a biphase symbol (a plain sum would cancel).
 */
static void rds_bank_push(rds_rx_t *r, float yi, float yq)
{
    uint32_t n = r->n_out++;

    for (int h = 0; h < RDS_PHASES; h++) {
        if (n < (uint32_t)h)
            continue;
        uint32_t rel = (n - (uint32_t)h) & 15u;
        rds_phase_t *p = &r->phase[h];
        int half = (rel >= 8);

        p->acc_i[half] += yi;
        p->acc_q[half] += yq;

        if (rel == 15) {
            float di = p->acc_i[0] - p->acc_i[1];
            float dq = p->acc_q[0] - p->acc_q[1];
            /* Re{diff * conj(prev)} cancels the carrier phase. */
            float re = di * p->prev_i + dq * p->prev_q;
            p->prev_i = di;
            p->prev_q = dq;
            p->acc_i[0] = p->acc_i[1] = 0.0f;
            p->acc_q[0] = p->acc_q[1] = 0.0f;
            rds_bank_bit(r, h, re < 0.0f);
        }
    }

    rds_select(r);
}

/*
 * Rational resampler 10937.5 -> 19000 Hz = 16 samples per data bit. The
 * step is 10937.5/19000 = 175/304 input samples per output sample, exact in
 * doubles. Outputs wait until k+2 is in the history ring.
 */
#define RDS_RESAMPLE_STEP (175.0 / 304.0)

static void rds_resample(rds_rx_t *r, float xi, float xq)
{
    r->rb_i[r->n_in & 3] = xi;
    r->rb_q[r->n_in & 3] = xq;
    r->n_in++;

    while (r->t_out < (double)r->n_in - 2.0) {
        int64_t k = (int64_t)r->t_out;
        float mu = (float)(r->t_out - (double)k);
        uint32_t m1 = (uint32_t)((uint64_t)k - 1u) & 3u;
        uint32_t p0 = (uint32_t)(uint64_t)k & 3u;
        uint32_t p1 = (uint32_t)((uint64_t)k + 1u) & 3u;
        uint32_t p2 = (uint32_t)((uint64_t)k + 2u) & 3u;

        float yi = rds_cubic(r->rb_i[m1], r->rb_i[p0],
                             r->rb_i[p1], r->rb_i[p2], mu);
        float yq = rds_cubic(r->rb_q[m1], r->rb_q[p0],
                             r->rb_q[p1], r->rb_q[p2], mu);
        rds_bank_push(r, yi, yq);
        r->t_out += RDS_RESAMPLE_STEP;
    }
}

int rds_rx_init(rds_rx_t *r)
{
    if (fir_init(&r->s1_i, rds_s1_taps, RDS_S1_TAPS, RDS_S1_DEC) != 0 ||
        fir_init(&r->s1_q, rds_s1_taps, RDS_S1_TAPS, RDS_S1_DEC) != 0 ||
        fir_init(&r->s2_i, rds_s2_taps, RDS_S2_TAPS, RDS_S2_DEC) != 0 ||
        fir_init(&r->s2_q, rds_s2_taps, RDS_S2_TAPS, RDS_S2_DEC) != 0) {
        return -1;
    }
    rds_rx_reset(r);
    return 0;
}

void rds_rx_reset(rds_rx_t *r)
{
    memset(&r->state, 0, sizeof(r->state));

    r->mix_idx = 0;

    r->s1_i.phase = r->s1_q.phase = 0;
    r->s1_i.wr = r->s1_q.wr = 0;
    memset(r->s1_i.hist, 0, sizeof(r->s1_i.hist));
    memset(r->s1_q.hist, 0, sizeof(r->s1_q.hist));
    r->s2_i.phase = r->s2_q.phase = 0;
    r->s2_i.wr = r->s2_q.wr = 0;
    memset(r->s2_i.hist, 0, sizeof(r->s2_i.hist));
    memset(r->s2_q.hist, 0, sizeof(r->s2_q.hist));

    memset(r->rb_i, 0, sizeof(r->rb_i));
    memset(r->rb_q, 0, sizeof(r->rb_q));
    r->n_in = 0;
    r->t_out = 3.0;
    r->n_out = 0;

    memset(r->phase, 0, sizeof(r->phase));
    memset(r->blk, 0, sizeof(r->blk));
    r->selected = 0;

    r->rt_started = 0;
    r->ptyn_started = 0;
    r->oda_current = -1;

    r->pi_cand = 0;
    r->pi_cand_hits = 0;
    r->quiet_samples = 0;
}

void rds_rx_process(rds_rx_t *r, const float *mpx, size_t n)
{
    r->quiet_samples += (uint32_t)n;

    for (size_t i = 0; i < n; i++) {
        float x = mpx[i];

        uint16_t mi = r->mix_idx;
        float zi = x * rds_mix_i[mi];
        float zq = x * rds_mix_q[mi];
        r->mix_idx = (uint16_t)((mi + 1u == RDS_MIX_PERIOD) ? 0u
                                                            : mi + 1u);

        float y1i = 0.0f, y1q = 0.0f;
        int o1 = fir_push(&r->s1_i, zi, &y1i);
        fir_push(&r->s1_q, zq, &y1q);
        if (!o1)
            continue;

        float y2i = 0.0f, y2q = 0.0f;
        int o2 = fir_push(&r->s2_i, y1i, &y2i);
        fir_push(&r->s2_q, y1q, &y2q);
        if (!o2)
            continue;

        rds_resample(r, y2i, y2q);
    }

    /*
     * No group for too long: the signal is gone, so drop the station rather
     * than keep stamping its identity into every frame. Cumulative
     * telemetry counters are deliberately left alone.
     */
    if (r->quiet_samples >= RDS_STATE_TIMEOUT_SAMPLES &&
        rds_station_present(&r->state))
        rds_reset_station(r);
}
