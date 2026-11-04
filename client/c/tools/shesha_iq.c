/* shesha-iq — capture (rx) or stream (tx) IQ through the shesha daemon.
 *
 *   shesha-iq rx  --freq 915000000 --lo 915250000 --bw 200000 --rate 2500000 \
 *                 --gain 30 --bits 12 --seconds 5 -o capture.bin
 *   shesha-iq tx  --freq 915000000 --bw 200000 --rate 2500000 --atten 20 \
 *                 --bits 12 -i waveform.bin
 *   shesha-iq status
 *
 * Files are raw packed wire bytes (2 bytes/sample at 8-bit, 3 at 12-bit);
 * use shesha_iq_unpack() to get interleaved signed 12-bit samples. */

#include "shesha/client.h"
#include "shesha/iq.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void usage(FILE *out)
{
    fprintf(out,
        "usage: shesha-iq <rx|tx|status> [options]\n"
        "\n"
        "common:  --host H (default 192.168.2.1)  --port P (default 5555)\n"
        "\n"
        "rx:      --freq HZ  [--lo HZ]  [--bw HZ]  [--rate HZ]  [--gain DB]\n"
        "         [--agc N]  [--bits 8|12]  --seconds S  -o FILE\n"
        "tx:      --freq HZ  [--bw HZ]  [--rate HZ]  [--atten DB]  [--bits 8|12]\n"
        "         -i FILE\n"
        "stats:   (no extra options)\n"
        "status:  (no extra options)\n");
}

static long long parse_ll(const char *s, long long dflt)
{
    if (!s) return dflt;
    return strtoll(s, NULL, 0);
}

static double parse_d(const char *s, double dflt)
{
    if (!s) return dflt;
    return strtod(s, NULL);
}

static int read_file(const char *path, uint8_t **out, size_t *out_len)
{
    FILE *fp = fopen(path, "rb");
    if (!fp) {
        fprintf(stderr, "cannot open %s\n", path);
        return -1;
    }
    if (fseek(fp, 0, SEEK_END) != 0) { fclose(fp); return -1; }
    long n = ftell(fp);
    if (n < 0) { fclose(fp); return -1; }
    rewind(fp);
    uint8_t *buf = malloc((size_t)n ? (size_t)n : 1);
    if (!buf) { fclose(fp); return -1; }
    if (n > 0 && fread(buf, 1, (size_t)n, fp) != (size_t)n) {
        free(buf);
        fclose(fp);
        return -1;
    }
    fclose(fp);
    *out = buf;
    *out_len = (size_t)n;
    return 0;
}

static int write_file(const char *path, const uint8_t *p, size_t n)
{
    FILE *fp = fopen(path, "wb");
    if (!fp) {
        fprintf(stderr, "cannot create %s\n", path);
        return -1;
    }
    if (n && fwrite(p, 1, n, fp) != n) {
        fclose(fp);
        return -1;
    }
    fclose(fp);
    return 0;
}

int main(int argc, char **argv)
{
    const char *cmd = NULL;
    const char *host = SHESHA_DEFAULT_HOST;
    long long port = SHESHA_DEFAULT_PORT;
    const char *in_path = NULL;
    const char *out_path = NULL;
    long long freq = 0, lo = -1, bw = 200000, rate = 2500000;
    long long bits = 12, agc = 0;
    double gain = 30.0, atten = 20.0, seconds = 0.0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        int has = (i + 1 < argc);
        if (!strcmp(a, "--host") && has) host = argv[++i];
        else if (!strcmp(a, "--port") && has) port = parse_ll(argv[++i], port);
        else if (!strcmp(a, "--freq") && has) freq = parse_ll(argv[++i], freq);
        else if (!strcmp(a, "--lo") && has) lo = parse_ll(argv[++i], lo);
        else if (!strcmp(a, "--bw") && has) bw = parse_ll(argv[++i], bw);
        else if (!strcmp(a, "--rate") && has) rate = parse_ll(argv[++i], rate);
        else if (!strcmp(a, "--gain") && has) gain = parse_d(argv[++i], gain);
        else if (!strcmp(a, "--agc") && has) agc = parse_ll(argv[++i], agc);
        else if (!strcmp(a, "--bits") && has) bits = parse_ll(argv[++i], bits);
        else if (!strcmp(a, "--atten") && has) atten = parse_d(argv[++i], atten);
        else if (!strcmp(a, "--seconds") && has) seconds = parse_d(argv[++i], seconds);
        else if (!strcmp(a, "-o") && has) out_path = argv[++i];
        else if (!strcmp(a, "-i") && has) in_path = argv[++i];
        else if (a[0] == '-') {
            fprintf(stderr, "unknown option: %s\n", a);
            usage(stderr);
            return 2;
        } else if (!cmd) {
            cmd = a;
        } else {
            fprintf(stderr, "unexpected argument: %s\n", a);
            usage(stderr);
            return 2;
        }
    }

    if (!cmd) {
        usage(stderr);
        return 2;
    }
    if (bits != 8 && bits != 12) {
        fprintf(stderr, "--bits must be 8 or 12\n");
        return 2;
    }

    shesha_client_t *c = shesha_client_new();
    if (!c) {
        fprintf(stderr, "out of memory\n");
        return 1;
    }
    if (shesha_client_connect(c, host, (uint16_t)port, 5.0) != SHESHA_OK) {
        fprintf(stderr, "connect to %s:%lld failed\n", host, port);
        shesha_client_free(c);
        return 1;
    }

    int rc = 0;

    if (!strcmp(cmd, "status")) {
        shesha_status_t st;
        if (shesha_client_status(c, &st) != SHESHA_OK) {
            fprintf(stderr, "status failed\n");
            rc = 1;
        } else {
            printf("mode=%u freq=%llu Hz rate=%u Hz bw=%u Hz gain=%.1f dB "
                   "agc=%u iq_bits=%u fpga=%d rx_active=%d\n",
                   st.daemon_mode, (unsigned long long)st.freq_hz,
                   st.sample_rate_hz, st.bw_hz, st.gain_db, st.agc_mode,
                   st.iq_bits, st.fpga_active, st.rx_active);
        }
    } else if (!strcmp(cmd, "stats")) {
        shesha_tx_stats_t ts;
        if (shesha_client_tx_stats(c, &ts) != SHESHA_OK) {
            fprintf(stderr, "tx_stats failed\n");
            rc = 1;
        } else {
            printf("tx_active=%d fm_active=%d iq_active=%d eos=%d "
                   "dac_rate=%u fed=%llu emitted=%llu depth=%d underruns=%u "
                   "iq_ring_fill=%u iq_push_rejects=%u iq_starved=%u\n",
                   ts.tx_active, ts.fm_active, ts.iq_active, ts.end_of_stream,
                   ts.dac_rate, (unsigned long long)ts.fed,
                   (unsigned long long)ts.emitted, ts.depth, ts.underruns,
                   ts.iq_ring_fill, ts.iq_push_rejects, ts.iq_starved);
        }
    } else if (!strcmp(cmd, "rx")) {
        if (!out_path || seconds <= 0 || freq <= 0) {
            fprintf(stderr, "rx needs --freq, --seconds and -o\n");
            usage(stderr);
            rc = 2;
        } else {
            size_t bps = bits == 8 ? 2 : 3;
            size_t cap = (size_t)(seconds * (double)rate) * bps + bps * 65536;
            uint8_t *buf = malloc(cap);
            if (!buf) {
                fprintf(stderr, "out of memory\n");
                rc = 1;
            } else {
                shesha_capture_opts_t o;
                memset(&o, 0, sizeof o);
                o.bits = (int)bits;
                o.sample_rate_hz = (uint32_t)rate;
                o.bw_hz = (uint32_t)bw;
                o.lo_hz = (uint64_t)(lo >= 0 ? lo : freq);
                o.gain_db = (float)gain;
                o.agc_mode = (uint8_t)agc;
                o.duration_s = seconds;

                shesha_capture_info_t info;
                size_t got = 0;
                int r = shesha_client_iq_capture(c, &o, buf, cap, &got, &info);
                if (r != SHESHA_OK) {
                    fprintf(stderr, "capture failed (%d)\n", r);
                    rc = 1;
                } else if (write_file(out_path, buf, got) != 0) {
                    rc = 1;
                } else {
                    printf("captured %u samples (%zu bytes, %d-bit) "
                           "rate=%u Hz lo=%llu Hz gain=%.1f dB -> %s\n",
                           info.n_samples, got, info.bits, info.sample_rate_hz,
                           (unsigned long long)info.lo_hz, info.gain_db, out_path);
                }
                free(buf);
            }
        }
    } else if (!strcmp(cmd, "tx")) {
        if (!in_path || freq <= 0) {
            fprintf(stderr, "tx needs --freq and -i\n");
            usage(stderr);
            rc = 2;
        } else {
            uint8_t *buf = NULL;
            size_t n = 0;
            if (read_file(in_path, &buf, &n) != 0) {
                rc = 1;
            } else {
                shesha_transmit_opts_t o;
                memset(&o, 0, sizeof o);
                o.bits = (int)bits;
                o.sample_rate_hz = (uint32_t)rate;
                o.bw_hz = (uint32_t)bw;
                o.freq_hz = (uint64_t)freq;
                o.atten_db = (float)atten;
                o.chunk_samples = 4096;

                int64_t pushed =
                    shesha_client_iq_transmit(c, buf, n, &o);
                if (pushed < 0) {
                    fprintf(stderr, "transmit failed (%lld)\n",
                            (long long)pushed);
                    rc = 1;
                } else {
                    printf("pushed %lld samples from %s\n",
                           (long long)pushed, in_path);
                    shesha_tx_stats_t ts;
                    if (shesha_client_tx_stats(c, &ts) == SHESHA_OK)
                        printf("tx stats: emitted=%llu underruns=%u "
                               "iq_starved=%u iq_push_rejects=%u\n",
                               (unsigned long long)ts.emitted, ts.underruns,
                               ts.iq_starved, ts.iq_push_rejects);
                }
                free(buf);
            }
        }
    } else {
        fprintf(stderr, "unknown command: %s\n", cmd);
        usage(stderr);
        rc = 2;
    }

    shesha_client_free(c);
    return rc;
}
