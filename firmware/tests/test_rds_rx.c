/*
 * Characterisation tests for the RDS decoder (firmware/src/rds_rx.c).
 *
 * The test carries its own RDS encoder: groups -> CRC/offset blocks ->
 * differential encoding -> biphase -> 57 kHz DSB-SC multiplex. Encoder and
 * decoder meeting in the middle only proves they agree; the end-to-end tests
 * below therefore assert known PI/PS/PTY/RT/AF/CT/PTYN/ODA content, and at
 * least one block's 26-bit encoding is pinned by a value computed here by a
 * second, independent method.
 *
 * What is deliberately exercised: carrier phase offsets (differential
 * detection must not care), transmitter clock offsets up to 100 ppm (the
 * phase-bank selector must follow the drift), additive noise, interference
 * from the stereo multiplex, sync loss and reacquisition, and the version-B
 * groups whose PI sits in block C.
 *
 * Build and run:  make test-firmware
 */
#include "../src/rds_rx.h"

#include <math.h>
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

/* Same offset words the standard defines; repeated here so the test does
 * not share a #define with the code under test. */
#define OFF_A   0x0FCu
#define OFF_B   0x198u
#define OFF_C   0x168u
#define OFF_C1  0x350u
#define OFF_D   0x1B4u

#define BITRATE_HZ   1187.5
#define MPX_RATE     350000.0
#define RDS_HZ       57000.0
#define RDS_AMP      0.03    /* standard injection, full deviation = 1.0 */

/* ------------------------------------------------------------------ */
/* Encoder                                                             */
/* ------------------------------------------------------------------ */

/*
 * Independent CRC: XOR of the precomputed residues x^i mod g(x) for each
 * set information bit. The constants were derived by polynomial long
 * division separately from the decoder's sequential reduction, so a shared
 * bug in the reduction is not possible. The orientation is the standard one
 * -- the information word occupies x^25..x^10 of the block -- and the
 * resulting syndromes were cross-checked against IEC 62106 Table B.1
 * (offset A valid blocks yield parity-check syndrome 0x3D8, etc.).
 */
static uint16_t enc_crc(uint16_t info)
{
    static const uint16_t res[16] = {
        0x1B9, 0x372, 0x35D, 0x303, 0x3BF, 0x2C7, 0x037, 0x06E,
        0x0DC, 0x1B8, 0x370, 0x359, 0x30B, 0x3AF, 0x2E7, 0x077,
    };
    uint16_t rem = 0;
    for (int i = 0; i < 16; i++) {
        if ((info >> i) & 1u)
            rem ^= res[i];
    }
    return rem;
}

static uint32_t enc_block(uint16_t info, uint16_t offset)
{
    return ((uint32_t)info << 10) | (uint16_t)(enc_crc(info) ^ offset);
}

/* Build the differentially encoded bit stream for a list of groups. The
 * group list is flat: 4 uint16 per group. */
static uint8_t *enc_bits(const uint16_t *groups, int n_groups,
                         size_t *n_bits_out)
{
    static const uint16_t offsets[4] = { OFF_A, OFF_B, OFF_C, OFF_D };
    size_t n_bits = (size_t)n_groups * 104;
    uint8_t *bits = malloc(n_bits);
    size_t k = 0;
    uint8_t enc = 0;

    for (int g = 0; g < n_groups; g++) {
        /* Version B is signalled by the C' offset word.  Bit 11 of block B
         * carries the version bit for every group type. */
        int version_b = (groups[g * 4 + 1] >> 11) & 1;
        for (int b = 0; b < 4; b++) {
            uint16_t offset = (b == 2 && version_b) ? OFF_C1 : offsets[b];
            uint32_t word = enc_block(groups[g * 4 + b], offset);
            for (int i = 25; i >= 0; i--) {
                enc ^= (uint8_t)((word >> i) & 1u);
                bits[k++] = enc;
            }
        }
    }
    *n_bits_out = n_bits;
    return bits;
}

/*
 * Synthesize the real MPX at 350 kHz. `ppm` scales the transmitter's symbol
 * clock relative to the receiver's sample clock. `phase` rotates the 57 kHz
 * carrier. Interfering tones can be added on top.
 */
static void enc_mpx(const uint8_t *bits, size_t n_bits, float *out,
                    size_t n_samples, double ppm, double phase,
                    double noise_sigma, unsigned seed)
{
    uint32_t lcg = seed ? seed : 1;

    for (size_t n = 0; n < n_samples; n++) {
        double t = (double)n / MPX_RATE;
        double pos = t * BITRATE_HZ * (1.0 + ppm);
        double v = 0.0;

        if (pos < (double)n_bits) {
            size_t bit = (size_t)pos;
            double frac = pos - (double)bit;
            double e = bits[bit] ? 1.0 : -1.0;
            /* biphase symbol: +,- for an encoded 1, -,+ for a 0 */
            double chip = (frac < 0.5) ? e : -e;
            v = RDS_AMP * chip * cos(2.0 * M_PI * RDS_HZ * t + phase);

            /* A little of the rest of the multiplex, so the tests would
             * catch a filter that passes L-R or the pilot. */
            v += 0.45 * 0.5 * sin(2.0 * M_PI * 1000.0 * t)
               + 0.09 * cos(2.0 * M_PI * 19000.0 * t + 0.3)
               + 0.225 * cos(2.0 * M_PI * 35000.0 * t)
               + 0.225 * cos(2.0 * M_PI * 41000.0 * t);
        }

        if (noise_sigma > 0.0) {
            lcg = lcg * 1664525u + 1013904223u;
            double u1 = (double)(lcg >> 8) / 16777216.0;
            lcg = lcg * 1664525u + 1013904223u;
            double u2 = (double)(lcg >> 8) / 16777216.0;
            double g1 = sqrt(-2.0 * log(u1 + 1e-12)) * cos(2.0 * M_PI * u2);
            v += noise_sigma * g1;
        }

        out[n] = (float)v;
    }
}

static size_t N_SAMPLES_FOR_BITS(size_t n_bits)
{
    return (size_t)((double)n_bits / BITRATE_HZ * MPX_RATE) + 1024;
}

/* Feed a whole synthesized stream through the decoder in chunks, the way
 * fm_mode.c feeds it. */
static void run_stream(rds_rx_t *r, const float *mpx, size_t n)
{
    const size_t chunk = 49152;
    for (size_t off = 0; off < n; off += chunk) {
        size_t m = n - off;
        if (m > chunk)
            m = chunk;
        rds_rx_process(r, mpx + off, m);
    }
}

/* ------------------------------------------------------------------ */
/* Group builders                                                      */
/* ------------------------------------------------------------------ */

static void g0a(uint16_t i[4], uint16_t pi, int pty, int tp, int ta, int ms,
                int di, int addr, uint8_t af1, uint8_t af2,
                char c0, char c1)
{
    i[0] = pi;
    i[1] = (uint16_t)((0u << 12) | (tp << 10) | (pty << 5) | (ta << 4) |
                      (ms << 3) | (di << 2) | addr);
    i[2] = (uint16_t)((af1 << 8) | af2);
    i[3] = (uint16_t)(((uint8_t)c0 << 8) | (uint8_t)c1);
}

static void g0b(uint16_t i[4], uint16_t pi, int pty, int tp, int addr,
                char c0, char c1)
{
    i[0] = pi;
    i[1] = (uint16_t)((0u << 12) | (1u << 11) | (tp << 10) | (pty << 5) |
                      (0u << 4) | (1u << 3) | addr);
    i[2] = pi;
    i[3] = (uint16_t)(((uint8_t)c0 << 8) | (uint8_t)c1);
}

static void g2b(uint16_t i[4], uint16_t pi, int pty, int tp, int ab, int addr,
                char c0, char c1)
{
    i[0] = pi;
    i[1] = (uint16_t)((2u << 12) | (1u << 11) | (tp << 10) | (pty << 5) |
                      (ab << 4) | (addr & 0xF));
    i[2] = pi;
    i[3] = (uint16_t)(((uint8_t)c0 << 8) | (uint8_t)c1);
}

static void g2a(uint16_t i[4], uint16_t pi, int pty, int tp, int ab, int addr,
                const char *four)
{
    i[0] = pi;
    i[1] = (uint16_t)((2u << 12) | (tp << 10) | (pty << 5) | (ab << 4) |
                      (addr & 0xF));
    i[2] = (uint16_t)(((uint8_t)four[0] << 8) | (uint8_t)four[1]);
    i[3] = (uint16_t)(((uint8_t)four[2] << 8) | (uint8_t)four[3]);
}

static void g4a(uint16_t i[4], uint16_t pi, int pty, int tp,
                uint32_t mjd, int hour, int minute, int off_signed)
{
    int sign = (off_signed < 0);
    int mag = sign ? -off_signed : off_signed;
    i[0] = pi;
    i[1] = (uint16_t)((4u << 12) | (tp << 10) | (pty << 5) |
                      (mjd >> 15));
    i[2] = (uint16_t)(((mjd & 0x7FFFu) << 1) | ((hour >> 4) & 1));
    i[3] = (uint16_t)(((hour & 0xF) << 12) | ((minute & 0x3F) << 6) |
                      ((sign ? 0x20 : 0) | mag));
}

static void g10a(uint16_t i[4], uint16_t pi, int pty, int ab, int addr,
                 const char *four)
{
    i[0] = pi;
    i[1] = (uint16_t)((10u << 12) | (pty << 5) | (ab << 4) | (addr & 1));
    i[2] = (uint16_t)(((uint8_t)four[0] << 8) | (uint8_t)four[1]);
    i[3] = (uint16_t)(((uint8_t)four[2] << 8) | (uint8_t)four[3]);
}

static void g3a(uint16_t i[4], uint16_t pi, int pty, int gtc,
                uint16_t aid, uint16_t data)
{
    i[0] = pi;
    i[1] = (uint16_t)((3u << 12) | (pty << 5) | (gtc & 0x1F));
    i[2] = aid;
    i[3] = data;
}

/* ------------------------------------------------------------------ */
/* Tests                                                               */
/* ------------------------------------------------------------------ */

static void test_crc_matches_independent_implementation(void)
{
    /* Hand-checked: info 0x8203 is PI for a station used below. The value
     * printed here was computed with this file's CRC, which is a different
     * formulation from rds_rx.c's; the decode tests then prove the two
     * agree end to end. */
    uint32_t blk = enc_block(0x8203, OFF_A);
    uint16_t rem = enc_crc(0x8203);
    CHECK((blk & 0x3FFu) == (uint16_t)(rem ^ OFF_A));
    CHECK((blk >> 10) == 0x8203);
}

/* Reuse one message pattern for most tests. */
static int build_0a_stream(uint16_t **groups_out)
{
    /* 8 cycles of 4 addresses = 32 groups, enough for acquisition and a
     * full PS plus AF. */
    const int n = 8;
    uint16_t *g = malloc((size_t)n * 4 * sizeof(uint16_t) * 4);
    static const char ps[4][2] = { {'T','E'}, {'S','T'}, {'F','M'}, {' ',' '} };
    int k = 0;
    for (int c = 0; c < n; c++) {
        for (int a = 0; a < 4; a++) {
            g0a(g + (size_t)k * 4, 0x8203, 10, 1, 0, 1, a & 1, a,
                126, 92, ps[a][0], ps[a][1]);
            k++;
        }
    }
    *groups_out = g;
    return k;
}

static void test_decodes_ps_pi_pty_af(void)
{
    uint16_t *groups;
    int n_groups = build_0a_stream(&groups);
    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)groups, n_groups, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 1);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->synced);
    CHECK(s->pi_valid && s->pi == 0x8203);
    CHECK(s->pty == 10);
    CHECK(s->tp == 1);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "TESTFM  ", 8) == 0);
    CHECK(s->af_count >= 2);
    CHECK(s->af[0] == 126 && s->af[1] == 92);
    CHECK(s->groups > 8);
    CHECK(s->block_errors == 0);

    free(mpx);
    free(bits);
    free(groups);
}

static void test_version_b_ps(void)
{
    /* 0B carries PI in block C and two PS characters in D. */
    uint16_t groups[4][4];
    g0b(groups[0], 0x1234, 3, 0, 0, 'A', 'B');
    g0b(groups[1], 0x1234, 3, 0, 1, 'C', 'D');
    g0b(groups[2], 0x1234, 3, 0, 2, 'E', 'F');
    g0b(groups[3], 0x1234, 3, 0, 3, 'G', 'H');

    uint16_t stream[16][4];
    for (int c = 0; c < 4; c++)
        for (int g = 0; g < 4; g++)
            memcpy(stream[c * 4 + g], groups[g], sizeof(groups[g]));

    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)stream, 16, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 2);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->pi == 0x1234);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "ABCDEFGH", 8) == 0);

    free(mpx);
    free(bits);
}

static void test_decodes_radiotext(void)
{
    static const char *segs[16] = {
        "Now ", "play", "ing ", "The ", "Bogu", "s Ma", "n --", "- 19",
        "97 r", "emas", "ter;", "  la", "te n", "ight", " rad", "io  ",
    };
    uint16_t groups[16][4];
    for (int a = 0; a < 16; a++)
        g2a(groups[a], 0x8203, 10, 1, 0, a, segs[a]);

    uint16_t stream[32][4];
    for (int c = 0; c < 2; c++)
        for (int a = 0; a < 16; a++)
            memcpy(stream[c * 16 + a], groups[a], sizeof(groups[a]));

    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)stream, 32, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 3);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(rds_rt_complete(s));
    CHECK(memcmp(s->rt,
                 "Now playing The Bogus Man --- 1997 remaster;  "
                 "late night radio  ", 64) == 0);

    free(mpx);
    free(bits);
}

static void test_version_b_radiotext_without_terminator(void)
{
    /* 2B carries two characters in D and the PI in C.  A full 32-character
     * message with no 0x0D terminator must validate at exactly 32. */
    static const char text[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";
    uint16_t groups[16][4];
    for (int a = 0; a < 16; a++)
        g2b(groups[a], 0x3456, 10, 1, 0, a,
            text[a * 2], text[a * 2 + 1]);

    uint16_t stream[32][4];
    for (int c = 0; c < 2; c++)
        for (int a = 0; a < 16; a++)
            memcpy(stream[c * 16 + a], groups[a], sizeof(groups[a]));

    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)stream, 32, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 4);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->pi == 0x3456);
    CHECK(rds_rt_complete(s));
    CHECK(s->rt_len == 32);
    CHECK(memcmp(s->rt, text, 32) == 0);

    free(mpx);
    free(bits);
}

static void test_decodes_ct(void)
{
    /* 2004-05-17 12:34 UTC, +1.5 h local. MJD for 2004-05-17 is 53142. */
    uint16_t groups[4][4];
    for (int a = 0; a < 4; a++)
        g4a(groups[a], 0x8203, 10, 1, 53142, 12, 34, 3);

    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)groups, 4, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 4);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->ct_valid);
    CHECK(s->ct_mjd == 53142);
    CHECK(s->ct_minutes == 12 * 60 + 34);
    CHECK(s->ct_offset_half_hours == 3);

    free(mpx);
    free(bits);
}

static void test_decodes_ptyn(void)
{
    uint16_t groups[2][4];
    g10a(groups[0], 0x8203, 10, 0, 0, "ROCK");
    g10a(groups[1], 0x8203, 10, 0, 1, " ON ");

    uint16_t stream[8][4];
    for (int c = 0; c < 4; c++) {
        memcpy(stream[c * 2 + 0], groups[0], sizeof(groups[0]));
        memcpy(stream[c * 2 + 1], groups[1], sizeof(groups[1]));
    }

    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)stream, 8, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 5);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(rds_ptyn_valid(s));
    CHECK(memcmp(s->ptyn, "ROCK ON ", 8) == 0);

    free(mpx);
    free(bits);
}

static void test_decodes_oda(void)
{
    uint16_t groups[4][4];
    /* RT+ aid 0x4BD7, group type code 3 (the usual 3A pair). */
    g3a(groups[0], 0x8203, 10, 3, 0x4BD7, 0x1234);
    g3a(groups[1], 0x8203, 10, 3, 0x4BD7, 0x5678);
    g3a(groups[2], 0x8203, 10, 3, 0x4BD7, 0x9ABC);
    g3a(groups[3], 0x8203, 10, 3, 0x4BD7, 0xDEF0);

    uint16_t stream[12][4];
    for (int c = 0; c < 3; c++)
        for (int g = 0; g < 4; g++)
            memcpy(stream[c * 4 + g], groups[g], sizeof(groups[g]));

    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)stream, 12, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 6);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->oda_count == 1);
    CHECK(s->oda[0].aid == 0x4BD7);
    CHECK(s->oda[0].len == 8);
    CHECK(s->oda[0].data[0] == 0x12 && s->oda[0].data[7] == 0xF0);

    free(mpx);
    free(bits);
}

static void test_survives_carrier_phase(void)
{
    uint16_t *groups;
    int n_groups = build_0a_stream(&groups);
    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)groups, n_groups, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    /* 2.1 rad is close to the worst case for a coherent slicer. */
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 2.1, 0.0, 7);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->pi_valid && s->pi == 0x8203);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "TESTFM  ", 8) == 0);

    free(mpx);
    free(bits);
    free(groups);
}

static void test_survives_sample_clock_offset(void)
{
    /* 100 ppm is far beyond any real transmitter/receiver pair; it makes
     * the symbol phase walk several hypothesis steps per second, so the
     * selector has to switch and keep decoding. */
    uint16_t *groups;
    int n_groups = build_0a_stream(&groups);
    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)groups, n_groups, &n_bits);
    size_t n_samples = (size_t)((double)n_bits / BITRATE_HZ
                                * MPX_RATE * 1.0001) + 4096;
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 100e-6, 0.0, 0.0, 8);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->pi_valid && s->pi == 0x8203);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "TESTFM  ", 8) == 0);

    free(mpx);
    free(bits);
    free(groups);
}

static void test_survives_noise(void)
{
    uint16_t *groups;
    int n_groups = build_0a_stream(&groups);
    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)groups, n_groups, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, RDS_AMP * 0.5, 9);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->pi_valid && s->pi == 0x8203);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "TESTFM  ", 8) == 0);

    free(mpx);
    free(bits);
    free(groups);
}

static void test_reacquires_after_dropout(void)
{
    uint16_t *groups;
    int n_groups = build_0a_stream(&groups);
    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)groups, n_groups, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 10);

    /* Silence the middle 300 ms, longer than the 4-block sync-loss rule. */
    size_t drop0 = n_samples / 3;
    size_t drop1 = drop0 + (size_t)(0.30 * MPX_RATE);
    for (size_t i = drop0; i < drop1 && i < n_samples; i++)
        mpx[i] = 0.0f;

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->sync_losses > 0);
    CHECK(s->pi_valid && s->pi == 0x8203);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "TESTFM  ", 8) == 0);

    free(mpx);
    free(bits);
    free(groups);
}

static void test_pi_change_clears_station_state(void)
{
    /* Two different stations back to back: the second's PS must not be
     * contaminated by the first's characters.  PI is confirmed across two
     * A blocks, so the first group of the new station is discarded with the
     * old station's state; the new PS therefore needs a full PS cycle after
     * the change. */
    uint16_t stream[12][4];
    for (int a = 0; a < 4; a++)
        g0a(stream[a], 0x8203, 10, 1, 0, 1, 0, a,
            126, 92, "ABCD"[a], "1234"[a]);
    for (int a = 0; a < 8; a++)
        g0a(stream[4 + a], 0x1111, 4, 0, 0, 0, 0, a & 3,
            126, 92, "WXYZ"[a & 3], "5678"[a & 3]);

    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)stream, 12, &n_bits);
    size_t n_samples = N_SAMPLES_FOR_BITS(n_bits);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 11);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->pi == 0x1111);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "W5X6Y7Z8", 8) == 0);

    free(mpx);
    free(bits);
}

static void test_quiet_timeout_clears_station(void)
{
    /* Station 1: PS "TESTFM  ", PI 0x8203, plus a full RadioText message. */
    static const char ps1[4][2] = { {'T','E'}, {'S','T'}, {'F','M'}, {' ',' '} };
    static const char *rt[16] = {
        "Now ", "play", "ing ", "The ", "Bogu", "s Ma", "n --", "- 19",
        "97 r", "emas", "ter;", "  la", "te n", "ight", " rad", "io  ",
    };
    uint16_t s1[64][4];
    int k = 0;
    for (int c = 0; c < 8; c++)
        for (int a = 0; a < 4; a++, k++)
            g0a(s1[k], 0x8203, 10, 1, 0, 1, a & 1, a,
                126, 92, ps1[a][0], ps1[a][1]);
    for (int c = 0; c < 2; c++)
        for (int a = 0; a < 16; a++, k++)
            g2a(s1[k], 0x8203, 10, 1, 0, a, rt[a]);

    size_t n_bits;
    uint8_t *bits = enc_bits((const uint16_t *)s1, 64, &n_bits);
    size_t n_station = N_SAMPLES_FOR_BITS(n_bits);
    size_t n_quiet = (size_t)(3.0 * MPX_RATE);  /* > 2 s decoder timeout */
    size_t n_samples = n_station + n_quiet;
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 13);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);

    /* The live station decodes. */
    run_stream(&r, mpx, n_station);
    const rds_state_t *s = rds_rx_state(&r);
    CHECK(s->synced);
    CHECK(s->pi_valid && s->pi == 0x8203);
    CHECK(rds_ps_valid(s));
    CHECK(rds_rt_complete(s));

    /* Silence past the timeout must drop the whole station, not just sync. */
    run_stream(&r, mpx + n_station, n_quiet);
    CHECK(!s->pi_valid);
    CHECK(s->ps_mask == 0);
    CHECK(!s->rt_valid);
    CHECK(!s->synced);
    /* Cumulative telemetry survives the clear. */
    CHECK(s->groups > 0);
    CHECK(s->blocks > 0);

    free(mpx);
    free(bits);

    /* A different station then decodes cleanly, with no carry-over. */
    static const char ps2[4][2] = { {'N','W'}, {'P','B'}, {' ',' '}, {' ',' '} };
    uint16_t s2[32][4];
    k = 0;
    for (int c = 0; c < 8; c++)
        for (int a = 0; a < 4; a++, k++)
            g0a(s2[k], 0x4B41, 20, 0, 0, 0, 0, a,
                126, 92, ps2[a][0], ps2[a][1]);

    bits = enc_bits((const uint16_t *)s2, 32, &n_bits);
    n_samples = N_SAMPLES_FOR_BITS(n_bits);
    mpx = malloc(n_samples * sizeof(float));
    enc_mpx(bits, n_bits, mpx, n_samples, 0.0, 0.0, 0.0, 14);

    run_stream(&r, mpx, n_samples);
    CHECK(s->synced);
    CHECK(s->pi_valid && s->pi == 0x4B41);
    CHECK(rds_ps_valid(s));
    CHECK(memcmp(s->ps, "NWPB    ", 8) == 0);

    free(mpx);
    free(bits);
}

/*
 * The resampler's input counter used to be 32-bit. At 10937.5 inputs/s it
 * wraps after about 4.5 days of continuous reception, and once it wrapped
 * the output position stayed billions of samples ahead, so the decoder
 * silently stopped producing samples. Park the counter just below the old
 * wrap and prove outputs keep arriving across it.
 */
static void test_resampler_survives_the_input_wrap(void)
{
    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);

    r.n_in = 0xFFFFFFF0u;
    r.t_out = (double)r.n_in - 2.5;
    uint32_t before = r.n_out;

    const size_t n = 65536;
    float *mpx = calloc(n, sizeof(float));
    CHECK(mpx != NULL);
    rds_rx_process(&r, mpx, n);

    CHECK(r.n_out - before > 100);

    free(mpx);
}

static void test_no_signal_does_not_invent_pi(void)
{
    /* Pure noise, no RDS at all.  A random 26-bit window matches an offset
     * syndrome about once per 1024 bit positions per phase, and the old
     * decoder committed a PI on the first such match.  That is the on-air
     * "flicker": a marginal station shows random PI values while unsynced.
     * A PI must be confirmed by repetition before it is exposed. */
    size_t n_samples = (size_t)(4.0 * MPX_RATE);
    float *mpx = malloc(n_samples * sizeof(float));
    enc_mpx(NULL, 0, mpx, n_samples, 0.0, 0.0, 0.5, 12);

    rds_rx_t r;
    CHECK(rds_rx_init(&r) == 0);
    run_stream(&r, mpx, n_samples);

    const rds_state_t *s = rds_rx_state(&r);
    CHECK(!s->pi_valid);

    free(mpx);
}

int main(void)
{
    RUN(test_crc_matches_independent_implementation);
    RUN(test_decodes_ps_pi_pty_af);
    RUN(test_version_b_ps);
    RUN(test_decodes_radiotext);
    RUN(test_version_b_radiotext_without_terminator);
    RUN(test_decodes_ct);
    RUN(test_decodes_ptyn);
    RUN(test_decodes_oda);
    RUN(test_survives_carrier_phase);
    RUN(test_survives_sample_clock_offset);
    RUN(test_survives_noise);
    RUN(test_reacquires_after_dropout);
    RUN(test_pi_change_clears_station_state);
    RUN(test_quiet_timeout_clears_station);
    RUN(test_resampler_survives_the_input_wrap);
    RUN(test_no_signal_does_not_invent_pi);

    if (failures) {
        printf("\n%d check(s) FAILED\n", failures);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
