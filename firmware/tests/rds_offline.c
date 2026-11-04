/*
 * Host tool: decode a raw interleaved float32 IQ capture (2.1 MSPS) through
 * the exact firmware FM demod + RDS decoder.
 *
 * This is how a hardware failure is separated from a signal failure: the
 * daemon and this tool run the same fm_demod_process, so if a capture
 * decodes here but not live, the difference is the live RF/rate path.
 *
 * An IQ-mode capture has the AD9361 RX FIR disabled, so it carries the
 * adjacent channel and will not decode raw (STATUS Next Step 1).  Pass
 * --bandlimit 200 to apply the same +/-200 kHz channel filter the live FM
 * path gets in hardware.  Live FM captures need no band-limit.
 *
 * Build:
 *   make rds-offline
 * Run:
 *   ./build/rds_offline capture.f32                    # human, per-second
 *   ./build/rds_offline --bandlimit 200 cap.f32        # IQ-mode capture
 *   ./build/rds_offline --bandlimit 200 --json cap.f32 # machine-readable
 *
 * The IQ file must be interleaved (I,Q,I,Q...); iq rx writes 12-bit packed
 * samples, so unpack those first (scripts/rds_quality.py does this, or see
 * the client's iq_pack layout).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "fm_rx.h"
#include "fir.h"
#include "channel_filter.h"

#define CHANNEL_TAPS 129
#define CHUNK 49152

static void json_escape(const uint8_t *s, int n, char *out, size_t outsz)
{
    size_t o = 0;
    for (int i = 0; i < n && o + 2 < outsz; i++) {
        unsigned char c = s[i];
        if (c == '"' || c == '\\') {
            out[o++] = '\\';
            out[o++] = (char)c;
        } else if (c >= 0x20 && c < 0x7f) {
            out[o++] = (char)c;
        } else {
            out[o++] = ' ';
        }
    }
    out[o] = '\0';
}

int main(int argc, char **argv)
{
    const char *path = NULL;
    double bandlimit_hz = 0.0;
    int json = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--bandlimit") == 0 && i + 1 < argc) {
            bandlimit_hz = atof(argv[++i]) * 1000.0;
        } else if (strcmp(argv[i], "--json") == 0) {
            json = 1;
        } else if (argv[i][0] == '-' && argv[i][1] != '\0') {
            fprintf(stderr, "unknown option: %s\n", argv[i]);
            return 1;
        } else {
            path = argv[i];
        }
    }
    if (!path) {
        fprintf(stderr,
                "usage: %s [--bandlimit KHZ] [--json] file.f32\n", argv[0]);
        return 1;
    }

    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); return 1; }

    fm_demod_t d;
    if (fm_demod_init(&d, FM_DEFAULT_IQ_RATE, FM_DEFAULT_DEVIATION,
                      FM_DEFAULT_DEEMPHASIS) != 0)
        return 1;

    float taps[CHANNEL_TAPS];
    fir_t fi, fq;
    int bandlimit = bandlimit_hz > 0.0;
    if (bandlimit) {
        if (channel_filter_design(taps, CHANNEL_TAPS, bandlimit_hz,
                                  (double)FM_DEFAULT_IQ_RATE) != 0) {
            fprintf(stderr, "bad --bandlimit: %.0f Hz\n", bandlimit_hz);
            return 1;
        }
        if (fir_init(&fi, taps, CHANNEL_TAPS, 1) != 0 ||
            fir_init(&fq, taps, CHANNEL_TAPS, 1) != 0)
            return 1;
    }

    float *ir = malloc(CHUNK * sizeof(float));
    float *ii = malloc(CHUNK * sizeof(float));
    float *irf = malloc(CHUNK * sizeof(float));
    float *iif = malloc(CHUNK * sizeof(float));
    float *iq = malloc(2 * CHUNK * sizeof(float));
    float audio[2048];
    if (!ir || !ii || !irf || !iif || !iq)
        return 1;

    size_t total = 0;
    double sum_sq = 0.0;
    int last_sec = -1;

    for (;;) {
        size_t got = fread(iq, sizeof(float), 2 * CHUNK, f);
        got /= 2;
        if (!got)
            break;
        for (size_t k = 0; k < got; k++) {
            ir[k] = iq[2 * k];
            ii[k] = iq[2 * k + 1];
            sum_sq += (double)ir[k] * ir[k] + (double)ii[k] * ii[k];
        }

        const float *pin_r = ir, *pin_i = ii;
        if (bandlimit) {
            for (size_t k = 0; k < got; k++) {
                float a, b;
                fir_push(&fi, ir[k], &a);
                fir_push(&fq, ii[k], &b);
                irf[k] = a;
                iif[k] = b;
            }
            pin_r = irf;
            pin_i = iif;
        }

        size_t na = 2048;
        fm_demod_process(&d, pin_r, pin_i, got, audio, &na);
        total += got;

        if (!json) {
            int sec = (int)(total / FM_DEFAULT_IQ_RATE);
            if (sec != last_sec) {
                last_sec = sec;
                const rds_state_t *s = rds_rx_state(&d.rds);
                printf("t=%2ds sync=%d pi=%04X pty=%2u ps=%.8s rt=%.20s "
                       "g=%u b=%u err=%u loss=%u\n",
                       sec, s->synced, s->pi, s->pty, (const char *)s->ps,
                       (const char *)s->rt, s->groups, s->blocks,
                       s->block_errors, s->sync_losses);
                fflush(stdout);
            }
        }
    }
    fclose(f);

    const rds_state_t *s = rds_rx_state(&d.rds);
    double rms = total ? sqrt(sum_sq / total) : 0.0;

    if (json) {
        char ps[24], rt[80], ptyn[24];
        json_escape(s->ps, 8, ps, sizeof(ps));
        json_escape(s->rt, s->rt_len, rt, sizeof(rt));
        json_escape(s->ptyn, 8, ptyn, sizeof(ptyn));
        printf("{\n");
        printf("  \"file\": \"%s\",\n", path);
        printf("  \"n_samples\": %zu,\n", total);
        printf("  \"seconds\": %.3f,\n", (double)total / FM_DEFAULT_IQ_RATE);
        printf("  \"rms\": %.6f,\n", rms);
        printf("  \"bandlimit_hz\": %.0f,\n", bandlimit_hz);
        printf("  \"synced\": %d,\n", s->synced);
        printf("  \"pi\": \"%04X\",\n", s->pi);
        printf("  \"pi_valid\": %d,\n", s->pi_valid);
        printf("  \"pty\": %u,\n", s->pty);
        printf("  \"ps\": \"%s\",\n", ps);
        printf("  \"ps_valid\": %d,\n", rds_ps_valid(s));
        printf("  \"rt\": \"%s\",\n", rt);
        printf("  \"rt_valid\": %d,\n", s->rt_valid);
        printf("  \"ptyn\": \"%s\",\n", ptyn);
        printf("  \"groups\": %u,\n", s->groups);
        printf("  \"blocks\": %u,\n", s->blocks);
        printf("  \"block_errors\": %u,\n", s->block_errors);
        printf("  \"sync_losses\": %u\n", s->sync_losses);
        printf("}\n");
    } else {
        printf("total %zu samples (%.2f s) rms=%.6f  groups=%u blocks=%u "
               "err=%u loss=%u pi=%04X ps=%.8s\n",
               total, (double)total / FM_DEFAULT_IQ_RATE, rms,
               s->groups, s->blocks, s->block_errors, s->sync_losses,
               s->pi, (const char *)s->ps);
    }
    return 0;
}
