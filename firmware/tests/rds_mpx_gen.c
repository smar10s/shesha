/*
 * Host tool: generate an FM stereo multiplex carrying real RDS groups, as
 * packed 12-bit IQ at 2.1 MSPS, for a TX -> attenuator -> RX loopback.
 *
 * A loopback that carries RDS only will not decode through the daemon: the
 * FM path's DC block subtracts the signal's own mean and a narrowband
 * (single-subcarrier) signal is distorted by it (see STATUS Known Issues).
 * So the multiplex always includes a 1 kHz programme tone and the 19 kHz
 * pilot, as a real broadcast does.
 *
 * The RDS groups are the same shape the decoder is tested against: PI 8203,
 * PS "TESTFM", PTY 10, and a RadioText segment set.  Groups are repeated in
 * a PS-then-RT cycle so both PS and RT complete within a few seconds.
 *
 * Build:
 *   make rds-mpx
 * Run:
 *   ./build/rds_mpx_gen --out /tmp/mpx.f32q --seconds 12
 * Stream it:
 *   shesha iq tx --file /tmp/mpx.f32q --freq 90.1 --tx-atten 20
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdint.h>

#define FS_HZ       2100000.0
#define DEVIATION   75000.0
#define BITRATE_HZ  1187.5
#define RDS_HZ      57000.0
#define RDS_AMP     0.03

#define OFF_A 0x0FCu
#define OFF_B 0x198u
#define OFF_C 0x168u
#define OFF_D 0x1B4u

/* --- RDS encoder (independent of rds_rx.c) ----------------------------- */

static uint16_t enc_crc(uint16_t info)
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

static uint32_t enc_block(uint16_t info, uint16_t offset)
{
    return ((uint32_t)info << 10) | (uint16_t)(enc_crc(info) ^ offset);
}

/* groups is flat: 4 uint16 per group.  Returns a malloc'd bit array. */
static uint8_t *enc_bits(const uint16_t *groups, int n_groups, size_t *n_bits_out)
{
    static const uint16_t offsets[4] = { OFF_A, OFF_B, OFF_C, OFF_D };
    size_t n_bits = (size_t)n_groups * 104;
    uint8_t *bits = malloc(n_bits);
    size_t k = 0;
    uint8_t enc = 0;
    for (int g = 0; g < n_groups; g++) {
        for (int b = 0; b < 4; b++) {
            uint32_t word = enc_block(groups[g * 4 + b], offsets[b]);
            for (int i = 25; i >= 0; i--) {
                enc ^= (uint8_t)((word >> i) & 1u);
                bits[k++] = enc;
            }
        }
    }
    *n_bits_out = n_bits;
    return bits;
}

static void g0a(uint16_t i[4], uint16_t pi, int pty, int tp, int addr,
                char c0, char c1)
{
    i[0] = pi;
    i[1] = (uint16_t)((0u << 12) | (tp << 10) | (pty << 5) | addr);
    i[2] = 0;
    i[3] = (uint16_t)(((uint8_t)c0 << 8) | (uint8_t)c1);
}

static void g2a(uint16_t i[4], uint16_t pi, int pty, int tp, int addr,
                const char *four)
{
    i[0] = pi;
    i[1] = (uint16_t)((2u << 12) | (tp << 10) | (pty << 5) | (addr & 0xF));
    i[2] = (uint16_t)(((uint8_t)four[0] << 8) | (uint8_t)four[1]);
    i[3] = (uint16_t)(((uint8_t)four[2] << 8) | (uint8_t)four[3]);
}

/* --- 12-bit packing (firmware/src/iq_pack.h layout) -------------------- */

static void pack12(FILE *f, double re, double im)
{
    int r = (int)lrint(re * 2047.0);
    int q = (int)lrint(im * 2047.0);
    if (r > 2047) r = 2047; if (r < -2048) r = -2048;
    if (q > 2047) q = 2047; if (q < -2048) q = -2048;
    unsigned ri = (unsigned)(r & 0xFFF);
    unsigned qi = (unsigned)(q & 0xFFF);
    uint8_t b[3] = {
        (uint8_t)(ri & 0xFF),
        (uint8_t)(((ri >> 8) & 0xF) | ((qi & 0xF) << 4)),
        (uint8_t)((qi >> 4) & 0xFF),
    };
    fwrite(b, 1, 3, f);
}

int main(int argc, char **argv)
{
    const char *out = NULL;
    double seconds = 12.0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--out") == 0 && i + 1 < argc)
            out = argv[++i];
        else if (strcmp(argv[i], "--seconds") == 0 && i + 1 < argc)
            seconds = atof(argv[++i]);
        else {
            fprintf(stderr, "usage: %s --out FILE [--seconds S]\n", argv[0]);
            return 1;
        }
    }
    if (!out) { fprintf(stderr, "--out is required\n"); return 1; }

    const uint16_t PI = 0x8203;
    const int PTY = 10, TP = 1;

    char rt[64];
    memset(rt, ' ', sizeof(rt));
    const char *text = "SHESHA RDS TEST";
    memcpy(rt, text, strlen(text));
    rt[strlen(text)] = 0x0D;

    /* Groups per PS-then-RT cycle: 4 (PS 0A addr 0..3) + 16 (RT 2A seg 0..15) */
    const int per_cycle = 20;
    int cycles = (int)(seconds * BITRATE_HZ / 104.0 / per_cycle) + 1;
    if (cycles < 2)
        cycles = 2;
    int n_groups = cycles * per_cycle;

    uint16_t *groups = malloc((size_t)n_groups * 4 * sizeof(uint16_t));
    int gi = 0;
    const char *ps = "TESTFM  ";
    for (int c = 0; c < cycles; c++) {
        for (int a = 0; a < 4; a++)
            g0a(&groups[gi++ * 4], PI, PTY, TP, a, ps[2 * a], ps[2 * a + 1]);
        for (int a = 0; a < 16; a++)
            g2a(&groups[gi++ * 4], PI, PTY, TP, a, &rt[4 * a]);
    }

    size_t n_bits = 0;
    uint8_t *bits = enc_bits(groups, n_groups, &n_bits);

    size_t n_samples = (size_t)((double)n_bits / BITRATE_HZ * FS_HZ) + 8192;

    FILE *f = fopen(out, "wb");
    if (!f) { perror(out); return 1; }

    double two_pi = 2.0 * M_PI;
    double phase = 0.0;
    for (size_t n = 0; n < n_samples; n++) {
        double t = (double)n / FS_HZ;
        double v = 0.45 * sin(two_pi * 1000.0 * t)
                 + 0.09 * sin(two_pi * 19000.0 * t);

        double pos = t * BITRATE_HZ;
        if (pos < (double)n_bits) {
            size_t bit = (size_t)pos;
            double frac = pos - (double)bit;
            double e = bits[bit] ? 1.0 : -1.0;
            double chip = (frac < 0.5) ? e : -e;
            v += RDS_AMP * chip * cos(two_pi * RDS_HZ * t);
        }

        phase += two_pi * DEVIATION * v / FS_HZ;
        if (phase > M_PI) phase -= two_pi;
        else if (phase < -M_PI) phase += two_pi;
        pack12(f, cos(phase), sin(phase));
    }
    fclose(f);

    printf("wrote %s: %d groups, %.2f s, %zu samples (packed 12-bit)\n",
           out, n_groups, (double)n_samples / FS_HZ, n_samples);
    free(bits);
    free(groups);
    return 0;
}
