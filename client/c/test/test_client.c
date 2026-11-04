/* Offline tests for the shesha C client.
 *
 * Each test spins a fake one-shot daemon on 127.0.0.1 that speaks the TLV
 * protocol, then drives the real client against it.  No hardware. */

#include "shesha/client.h"
#include "test_util.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

/* command / response types */
#define CT_SET_FREQ          0x01
#define CT_SET_GAIN          0x02
#define CT_SET_BW            0x03
#define CT_SET_AGC           0x07
#define CT_SET_SAMPLE_RATE   0x08
#define CT_SET_MODE          0x0A
#define CT_SET_TX_ATTEN      0x0B
#define CT_TX_STATS          0x17
#define CT_SET_IQ_BITS       0x1C
#define CT_IQ_TX_STREAM_DATA 0x1D
#define CT_IQ_TX_STREAM_END  0x1E
#define CT_RESP_OK           0x10
#define CT_RESP_ERROR        0x11
#define CT_RESP_STATUS       0x12
#define CT_RESP_TX_STATS     0x18
#define CT_RESP_RETRY        0x19
#define CT_DATA_IQ           0x23
#define CT_CMD_STATUS        0x30
#define CT_CMD_SUBSCRIBE     0x33

/* ------------------------------------------------------------------ */
/* fake server                                                        */
/* ------------------------------------------------------------------ */

typedef struct {
    int      listen_fd;
    int      client_fd;
    uint16_t port;
    pthread_t th;
    void   (*fn)(int fd, void *arg);
    void    *arg;
} srv_t;

static ssize_t read_full(int fd, void *buf, size_t n)
{
    uint8_t *p = buf;
    size_t off = 0;
    while (off < n) {
        ssize_t r = read(fd, p + off, n - off);
        if (r > 0) off += (size_t)r;
        else if (r < 0 && errno == EINTR) continue;
        else return -1;
    }
    return (ssize_t)off;
}

static int write_full(int fd, const void *buf, size_t n)
{
    const uint8_t *p = buf;
    size_t off = 0;
    while (off < n) {
        ssize_t r = write(fd, p + off, n - off);
        if (r > 0) off += (size_t)r;
        else if (r < 0 && errno == EINTR) continue;
        else return -1;
    }
    return 0;
}

static int srv_read_frame(int fd, uint8_t *type, uint8_t *buf, size_t cap,
                          uint32_t *len)
{
    uint8_t hdr[9];
    if (read_full(fd, hdr, 9) != 9) return -1;
    *type = hdr[0];
    uint32_t plen = t_le32(hdr + 1);
    if (plen > cap) return -2;
    if (plen && read_full(fd, buf, plen) != (ssize_t)plen) return -1;
    *len = plen;
    return 0;
}

static void srv_send(int fd, uint8_t type, const void *payload, uint32_t len,
                     uint32_t seq)
{
    uint8_t hdr[9];
    hdr[0] = type;
    t_put32(hdr + 1, len);
    t_put32(hdr + 5, seq);
    write_full(fd, hdr, 9);
    if (len) write_full(fd, payload, len);
}

static void srv_ok(int fd, uint8_t cmd)
{
    srv_send(fd, CT_RESP_OK, &cmd, 1, 0);
}

static void *srv_main(void *p)
{
    srv_t *s = (srv_t *)p;
    s->client_fd = accept(s->listen_fd, NULL, NULL);
    if (s->client_fd >= 0) {
        struct timeval tv = { 3, 0 };
        setsockopt(s->client_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
#ifdef SO_NOSIGPIPE
        int one = 1;
        setsockopt(s->client_fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
        s->fn(s->client_fd, s->arg);
        close(s->client_fd);
        s->client_fd = -1;
    }
    return NULL;
}

static int srv_start(srv_t *s, void (*fn)(int, void *), void *arg)
{
    memset(s, 0, sizeof *s);
    s->fn = fn;
    s->arg = arg;
    s->client_fd = -1;

    s->listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (s->listen_fd < 0) return -1;
    int one = 1;
    setsockopt(s->listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);

    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    a.sin_port = 0;
    if (bind(s->listen_fd, (struct sockaddr *)&a, sizeof a) != 0) return -1;
    if (listen(s->listen_fd, 1) != 0) return -1;

    socklen_t al = sizeof a;
    getsockname(s->listen_fd, (struct sockaddr *)&a, &al);
    s->port = ntohs(a.sin_port);
    return pthread_create(&s->th, NULL, srv_main, s);
}

static void srv_stop(srv_t *s)
{
    pthread_join(s->th, NULL);
    close(s->listen_fd);
}

static shesha_client_t *connect_to(srv_t *s)
{
    shesha_client_t *c = shesha_client_new();
    if (!c) return NULL;
    if (shesha_client_connect(c, "127.0.0.1", s->port, 2.0) != SHESHA_OK) {
        shesha_client_free(c);
        return NULL;
    }
    return c;
}

/* ------------------------------------------------------------------ */
/* payload builders                                                   */
/* ------------------------------------------------------------------ */

static float rd_f(const uint8_t *b)
{
    uint32_t u = t_le32(b);
    float f;
    memcpy(&f, &u, sizeof f);
    return f;
}

static void build_status(uint8_t p[44], uint64_t freq, uint32_t rate,
                         uint32_t bw, uint8_t mode, uint8_t bits)
{
    memset(p, 0, 44);
    t_put64(p, freq);
    t_put32(p + 8, rate);
    t_put32(p + 12, bw);
    t_put32(p + 16, 4096);
    t_putf(p + 20, 12.5f);
    p[24] = 0;
    p[25] = 1;
    p[26] = 50;
    p[27] = mode;
    t_putf(p + 28, 10.0f);
    t_put16(p + 35, 1);
    p[41] = 1;
    p[42] = 1;
    p[43] = bits;
}

static uint32_t build_iq(uint8_t *p, double ts, float freq, uint32_t rate,
                         uint8_t bits, uint16_t n, const uint8_t *samples)
{
    uint64_t u;
    memcpy(&u, &ts, 8);
    t_put64(p, u);
    t_putf(p + 8, freq);
    t_put32(p + 12, rate);
    p[16] = bits;
    p[17] = 0;
    t_put16(p + 18, n);
    uint32_t bps = bits == 8 ? 2 : 3;
    memcpy(p + 20, samples, (size_t)n * bps);
    return 20 + n * bps;
}

static void build_tx_stats(uint8_t p[96])
{
    memset(p, 0, 96);
    t_put64(p + 0, 111);          /* fed */
    t_put64(p + 8, 222);          /* emitted */
    t_put64(p + 16, 1000000);     /* elapsed_us */
    t_put32(p + 40, 2100000);     /* dac_rate */
    t_put32(p + 44, (uint32_t)-5);/* depth */
    t_put32(p + 72, 3);           /* push_rejects */
    p[76] = 1;                    /* tx_active */
    t_put32(p + 88, 7);           /* iq_starved */
    p[92] = 1;                    /* iq_active */
    p[93] = 1;                    /* dma_start_failed */
}

/* ------------------------------------------------------------------ */
/* tests                                                              */
/* ------------------------------------------------------------------ */

struct flag { int ok; };

static void status_handler(int fd, void *arg)
{
    struct flag *f = (struct flag *)arg;
    uint8_t type, buf[256];
    uint32_t len;
    if (srv_read_frame(fd, &type, buf, sizeof buf, &len) == 0
        && type == CT_CMD_STATUS)
        f->ok = 1;
    uint8_t p[44];
    build_status(p, 915000000ULL, 2500000, 200000, 2, 12);
    srv_send(fd, CT_RESP_STATUS, p, 44, 0);
}

static void test_status(void)
{
    TEST_BEGIN("status query + parse");
    struct flag f = { 0 };
    srv_t s;
    if (srv_start(&s, status_handler, &f) != 0) {
        TEST_CHECK_MSG(0, "server start failed");
        return;
    }
    shesha_client_t *c = connect_to(&s);
    TEST_CHECK(c != NULL);
    if (c) {
        shesha_status_t st;
        TEST_CHECK(shesha_client_status(c, &st) == SHESHA_OK);
        TEST_EQ_I64(st.freq_hz, 915000000LL);
        TEST_EQ_I64(st.sample_rate_hz, 2500000);
        TEST_EQ_I64(st.bw_hz, 200000);
        TEST_EQ_I64(st.daemon_mode, 2);
        TEST_EQ_I64(st.iq_bits, 12);
        TEST_EQ_I64(st.rx_active, 1);
        TEST_CHECK(f.ok == 1);
        shesha_client_free(c);
    }
    srv_stop(&s);
}

static void config_handler(int fd, void *arg)
{
    struct flag *f = (struct flag *)arg;
    uint8_t type, b[256];
    uint32_t len;

#define EXPECT(t, cond)                                                        \
    do {                                                                       \
        if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0 || type != (t)   \
            || !(cond)) {                                                      \
            f->ok = -1;                                                        \
            return;                                                            \
        }                                                                      \
    } while (0)

    EXPECT(CT_SET_MODE, len == 1 && b[0] == 2);
    srv_ok(fd, type);

    EXPECT(CT_SET_FREQ, len == 8 && t_le64(b) == 915000000ULL);
    srv_ok(fd, type);

    EXPECT(CT_SET_BW, len == 4 && t_le32(b) == 200000);
    srv_ok(fd, type);

    EXPECT(CT_SET_GAIN, len == 4 && rd_f(b) == 30.0f);
    srv_ok(fd, type);

    EXPECT(CT_SET_AGC, len == 1 && b[0] == 0);
    srv_ok(fd, type);

    EXPECT(CT_SET_SAMPLE_RATE, len == 4 && t_le32(b) == 2500000);
    srv_ok(fd, type);

    EXPECT(CT_SET_IQ_BITS, len == 1 && b[0] == 12);
    srv_ok(fd, type);

    EXPECT(CT_SET_TX_ATTEN, len == 4 && rd_f(b) == 20.0f);
    srv_ok(fd, type);

#undef EXPECT
    f->ok = 1;
}

static void test_config(void)
{
    TEST_BEGIN("config command encoding");
    struct flag f = { 0 };
    srv_t s;
    if (srv_start(&s, config_handler, &f) != 0) {
        TEST_CHECK_MSG(0, "server start failed");
        return;
    }
    shesha_client_t *c = connect_to(&s);
    TEST_CHECK(c != NULL);
    if (c) {
        TEST_CHECK(shesha_client_set_mode(c, SHESHA_MODE_IQ) == SHESHA_OK);
        TEST_CHECK(shesha_client_set_freq(c, 915000000ULL) == SHESHA_OK);
        TEST_CHECK(shesha_client_set_bw(c, 200000) == SHESHA_OK);
        TEST_CHECK(shesha_client_set_gain(c, 30.0f) == SHESHA_OK);
        TEST_CHECK(shesha_client_set_agc(c, 0) == SHESHA_OK);
        TEST_CHECK(shesha_client_set_sample_rate(c, 2500000) == SHESHA_OK);
        TEST_CHECK(shesha_client_set_iq_bits(c, 12) == SHESHA_OK);
        TEST_CHECK(shesha_client_set_tx_atten(c, 20.0f) == SHESHA_OK);
        shesha_client_free(c);
    }
    TEST_CHECK_MSG(f.ok == 1, "server saw unexpected command bytes (ok=%d)", f.ok);
    srv_stop(&s);
}

/* A mutating command's RESP_OK is followed by an unsolicited RESP_STATUS
 * in the same write.  The client must consume that trailing status so the
 * next status query does not read a stale one. */
static void stale_status_handler(int fd, void *arg)
{
    struct flag *f = (struct flag *)arg;
    uint8_t type, b[256];
    uint32_t len;

    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0 || type != CT_SET_MODE)
        return;
    uint8_t ok = CT_SET_MODE;
    uint8_t old[44];
    build_status(old, 111ULL, 1000000, 100000, 0, 8);
    /* OK frame + stale status frame in a single write. */
    {
        uint8_t buf[9 + 1 + 9 + 44];
        size_t o = 0;
        buf[o++] = CT_RESP_OK; t_put32(buf + o, 1); o += 4; t_put32(buf + o, 0); o += 4;
        buf[o++] = ok;
        buf[o++] = CT_RESP_STATUS; t_put32(buf + o, 44); o += 4; t_put32(buf + o, 0); o += 4;
        memcpy(buf + o, old, 44); o += 44;
        write_full(fd, buf, o);
    }

    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0 || type != CT_CMD_STATUS)
        return;
    uint8_t fresh[44];
    build_status(fresh, 222ULL, 2500000, 200000, 2, 12);
    srv_send(fd, CT_RESP_STATUS, fresh, 44, 0);
    f->ok = 1;
}

static void test_stale_status_swallowed(void)
{
    TEST_BEGIN("unsolicited RESP_STATUS does not desync a later query");
    struct flag f = { 0 };
    srv_t s;
    if (srv_start(&s, stale_status_handler, &f) != 0) {
        TEST_CHECK_MSG(0, "server start failed");
        return;
    }
    shesha_client_t *c = connect_to(&s);
    TEST_CHECK(c != NULL);
    if (c) {
        TEST_CHECK(shesha_client_set_mode(c, SHESHA_MODE_IQ) == SHESHA_OK);
        shesha_status_t st;
        TEST_CHECK(shesha_client_status(c, &st) == SHESHA_OK);
        TEST_EQ_I64(st.freq_hz, 222LL);   /* not the stale 111 */
        shesha_client_free(c);
    }
    TEST_CHECK(f.ok == 1);
    srv_stop(&s);
}

static void iq_handler(int fd, void *arg)
{
    struct flag *f = (struct flag *)arg;
    uint8_t type, b[256];
    uint32_t len;
    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
        || type != CT_CMD_SUBSCRIBE || len != 1 || b[0] != 0x08)
        return;
    srv_ok(fd, type);

    usleep(20000);  /* keep frames in separate segments from the OK */

    uint8_t samples[4 * 3];
    for (int i = 0; i < 12; i++) samples[i] = (uint8_t)(0x10 + i);
    uint8_t p[20 + 12];
    uint32_t n = build_iq(p, 1.5, 915.25f, 2500000, 12, 4, samples);
    srv_send(fd, CT_DATA_IQ, p, n, 1);
    n = build_iq(p, 2.5, 915.25f, 2500000, 12, 4, samples);
    srv_send(fd, CT_DATA_IQ, p, n, 2);
    n = build_iq(p, 3.5, 915.25f, 2500000, 12, 4, samples);
    srv_send(fd, CT_DATA_IQ, p, n, 4);   /* seq gap: one dropped */
    f->ok = 1;
}

static void test_iq_rx_gaps(void)
{
    TEST_BEGIN("DATA_IQ reception + sequence-gap accounting");
    struct flag f = { 0 };
    srv_t s;
    if (srv_start(&s, iq_handler, &f) != 0) {
        TEST_CHECK_MSG(0, "server start failed");
        return;
    }
    shesha_client_t *c = connect_to(&s);
    TEST_CHECK(c != NULL);
    if (c) {
        TEST_CHECK(shesha_client_subscribe(c, SHESHA_SUBSCRIBE_IQ) == SHESHA_OK);
        for (int i = 0; i < 3; i++) {
            shesha_iq_frame_t fr;
            int r = shesha_client_iq_rx_next(c, &fr, 1000);
            TEST_CHECK_MSG(r == 1, "frame %d: r=%d", i, r);
            if (r == 1) {
                TEST_EQ_I64(fr.n_samples, 4);
                TEST_EQ_I64(fr.bits, 12);
                TEST_CHECK(fr.sample_rate_hz == 2500000);
                TEST_CHECK_MSG(fr.samples != NULL && fr.samples[0] == 0x10,
                               "sample bytes wrong");
                double want = 1.5 + i;
                TEST_CHECK_MSG(fr.timestamp > want - 1e-6
                                   && fr.timestamp < want + 1e-6,
                               "ts=%f want %f", fr.timestamp, want);
                shesha_iq_frame_free(&fr);
            }
        }
        TEST_EQ_I64(shesha_client_gaps(c), 1);
        TEST_EQ_I64(shesha_client_dropped(c), 1);
        shesha_client_free(c);
    }
    srv_stop(&s);
}

static void tx_handler(int fd, void *arg)
{
    struct flag *f = (struct flag *)arg;
    uint8_t type, b[256];
    uint32_t len;

    /* configure (order matches shesha_client_iq_transmit) */
    const uint8_t order[] = { CT_SET_MODE, CT_SET_IQ_BITS, CT_SET_SAMPLE_RATE,
                              CT_SET_BW, CT_SET_FREQ, CT_SET_TX_ATTEN };
    for (size_t i = 0; i < sizeof order; i++) {
        if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0 || type != order[i])
            return;
        srv_ok(fd, type);
    }

    /* first stream chunk: backpressure */
    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
        || type != CT_IQ_TX_STREAM_DATA)
        return;
    if (len != 4 + 6 || t_le32(b) != 6 || b[4] != 0xAA || b[9] != 0xBB)
        return;
    srv_send(fd, CT_RESP_RETRY, &type, 1, 0);

    /* same chunk resent after RETRY: accepted */
    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
        || type != CT_IQ_TX_STREAM_DATA
        || len != 4 + 6 || t_le32(b) != 6)
        return;
    srv_ok(fd, type);

    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
        || type != CT_IQ_TX_STREAM_END)
        return;
    srv_ok(fd, type);

    /* iq_transmit now waits for the stream to go idle via tx_stats. */
    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
        || type != CT_TX_STATS)
        return;
    uint8_t st[96];
    build_tx_stats(st);
    st[76] = 0;   /* tx_active */
    st[92] = 0;   /* iq_active */
    srv_send(fd, CT_RESP_TX_STATS, st, 96, 0);
    f->ok = 1;
}

static void test_iq_tx_transmit(void)
{
    TEST_BEGIN("iq_transmit: config, RETRY backpressure, end");
    struct flag f = { 0 };
    srv_t s;
    if (srv_start(&s, tx_handler, &f) != 0) {
        TEST_CHECK_MSG(0, "server start failed");
        return;
    }
    shesha_client_t *c = connect_to(&s);
    TEST_CHECK(c != NULL);
    if (c) {
        uint8_t packed[6] = { 0xAA, 1, 2, 3, 4, 0xBB };
        shesha_transmit_opts_t o;
        memset(&o, 0, sizeof o);
        o.bits = 12;
        o.sample_rate_hz = 2500000;
        o.bw_hz = 200000;
        o.freq_hz = 915250000ULL;
        o.atten_db = 20.0f;
        int64_t pushed = shesha_client_iq_transmit(c, packed, sizeof packed, &o);
        TEST_EQ_I64(pushed, 2);   /* 6 bytes / 3 bytes-per-sample */
        shesha_client_free(c);
    }
    TEST_CHECK_MSG(f.ok == 1, "server protocol mismatch (ok=%d)", f.ok);
    srv_stop(&s);
}

static void drain_handler(int fd, void *arg)
{
    struct flag *f = (struct flag *)arg;
    uint8_t type, b[256];
    uint32_t len;

    const uint8_t order[] = { CT_SET_MODE, CT_SET_IQ_BITS, CT_SET_SAMPLE_RATE,
                              CT_SET_BW, CT_SET_FREQ, CT_SET_TX_ATTEN };
    for (size_t i = 0; i < sizeof order; i++) {
        if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
            || type != order[i])
            return;
        srv_ok(fd, type);
    }
    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
        || type != CT_IQ_TX_STREAM_DATA)
        return;
    srv_ok(fd, type);
    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
        || type != CT_IQ_TX_STREAM_END)
        return;
    srv_ok(fd, type);

    /* Report the stream active for two polls, then idle.  iq_transmit must
     * not return until it has seen the idle poll. */
    for (int k = 0; k < 3; k++) {
        if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
            || type != CT_TX_STATS)
            return;
        uint8_t st[96];
        build_tx_stats(st);
        int active = k < 2;
        st[76] = (uint8_t)active;   /* tx_active */
        st[92] = (uint8_t)active;   /* iq_active */
        srv_send(fd, CT_RESP_TX_STATS, st, 96, 0);
        f->ok = k + 1;
    }
}

static void test_tx_drain_wait(void)
{
    TEST_BEGIN("iq_transmit waits for the TX stream to drain");
    struct flag f = { 0 };
    srv_t s;
    if (srv_start(&s, drain_handler, &f) != 0) {
        TEST_CHECK_MSG(0, "server start failed");
        return;
    }
    shesha_client_t *c = connect_to(&s);
    TEST_CHECK(c != NULL);
    if (c) {
        uint8_t packed[6] = { 1, 2, 3, 4, 5, 6 };
        shesha_transmit_opts_t o;
        memset(&o, 0, sizeof o);
        o.bits = 12;
        o.sample_rate_hz = 2500000;
        o.bw_hz = 200000;
        o.freq_hz = 915250000ULL;
        o.atten_db = 20.0f;
        int64_t pushed = shesha_client_iq_transmit(c, packed, sizeof packed, &o);
        TEST_EQ_I64(pushed, 2);
        shesha_client_free(c);
    }
    TEST_CHECK_MSG(f.ok == 3, "expected 3 tx_stats polls, got %d", f.ok);
    srv_stop(&s);
}

static void tx_stats_handler(int fd, void *arg)
{
    struct flag *f = (struct flag *)arg;
    uint8_t type, b[256];
    uint32_t len;
    if (srv_read_frame(fd, &type, b, sizeof b, &len) != 0
        || type != CT_TX_STATS)
        return;
    uint8_t p[96];
    build_tx_stats(p);
    srv_send(fd, CT_RESP_TX_STATS, p, 96, 0);
    f->ok = 1;
}

static void test_tx_stats(void)
{
    TEST_BEGIN("tx_stats query + parse");
    struct flag f = { 0 };
    srv_t s;
    if (srv_start(&s, tx_stats_handler, &f) != 0) {
        TEST_CHECK_MSG(0, "server start failed");
        return;
    }
    shesha_client_t *c = connect_to(&s);
    TEST_CHECK(c != NULL);
    if (c) {
        shesha_tx_stats_t ts;
        TEST_CHECK(shesha_client_tx_stats(c, &ts) == SHESHA_OK);
        TEST_EQ_I64(ts.fed, 111);
        TEST_EQ_I64(ts.emitted, 222);
        TEST_EQ_I64(ts.dac_rate, 2100000);
        TEST_EQ_I64(ts.depth, -5);
        TEST_EQ_I64(ts.push_rejects, 3);
        TEST_EQ_I64(ts.iq_starved, 7);
        TEST_EQ_I64(ts.tx_active, 1);
        TEST_EQ_I64(ts.iq_active, 1);
        TEST_EQ_I64(ts.dma_start_failed, 1);
        shesha_client_free(c);
    }
    TEST_CHECK(f.ok == 1);
    srv_stop(&s);
}

int main(void)
{
    signal(SIGPIPE, SIG_IGN);
    test_status();
    test_config();
    test_stale_status_swallowed();
    test_iq_rx_gaps();
    test_iq_tx_transmit();
    test_tx_drain_wait();
    test_tx_stats();
    TEST_SUMMARY();
    return TEST_EXIT();
}
