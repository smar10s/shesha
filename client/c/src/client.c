/* C TCP client for the shesha daemon TLV protocol. */

#include "shesha/client.h"
#include "shesha/iq.h"

#include "protocol_internal.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

#define SHE_RX_CHUNK 65536

struct shesha_client {
    int      fd;
    double   timeout_s;

    uint8_t *rx;
    size_t   rx_len;
    size_t   rx_cap;

    uint8_t *tx;
    size_t   tx_cap;

    uint32_t last_seq;
    bool     have_last_seq;
    uint64_t gaps;
    uint64_t dropped;
};

/* ------------------------------------------------------------------ */
/* small helpers                                                      */
/* ------------------------------------------------------------------ */

static int64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void msleep(int ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

static int set_nonblocking(int fd)
{
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl < 0) return -1;
    return fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

/* Returns 1 ready, 0 timeout, -1 error. */
static int wait_fd(int fd, short events, int timeout_ms)
{
    struct pollfd pfd;
    pfd.fd = fd;
    pfd.events = events;
    pfd.revents = 0;
    int r;
    do {
        r = poll(&pfd, 1, timeout_ms);
    } while (r < 0 && errno == EINTR);
    if (r < 0) return -1;
    if (r == 0) return 0;
    if (pfd.revents & (POLLERR | POLLNVAL)) return -1;
    return 1;
}

static int alloc_grow(uint8_t **buf, size_t *cap, size_t need)
{
    if (*cap >= need) return 0;
    size_t c = *cap ? *cap : 1024;
    while (c < need) c <<= 1;
    uint8_t *p = realloc(*buf, c);
    if (!p) return -1;
    *buf = p;
    *cap = c;
    return 0;
}

static void close_fd(shesha_client_t *c)
{
    if (c->fd >= 0) {
        close(c->fd);
        c->fd = -1;
    }
}

/* ------------------------------------------------------------------ */
/* socket I/O                                                         */
/* ------------------------------------------------------------------ */

static int send_all(shesha_client_t *c, const void *buf, size_t len)
{
    if (c->fd < 0) return SHESHA_ERR_STATE;
    const uint8_t *p = buf;
    size_t off = 0;
    while (off < len) {
        ssize_t n = send(c->fd, p + off, len - off, MSG_NOSIGNAL);
        if (n > 0) {
            off += (size_t)n;
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            int w = wait_fd(c->fd, POLLOUT, (int)(c->timeout_s * 1000.0));
            if (w == 0) return SHESHA_ERR_TIMEOUT;
            if (w < 0) return SHESHA_ERR_SEND;
            continue;
        }
        return SHESHA_ERR_SEND;
    }
    return SHESHA_OK;
}

/* Reads at most one recv into the rx buffer.  Returns 1 on data, 0 on
 * timeout/would-block, -1 on close/error (and closes the fd). */
static int rx_fill(shesha_client_t *c, int timeout_ms)
{
    int w = wait_fd(c->fd, POLLIN, timeout_ms);
    if (w == 0) return 0;
    if (w < 0) {
        close_fd(c);
        return -1;
    }
    for (;;) {
        if (alloc_grow(&c->rx, &c->rx_cap, c->rx_len + SHE_RX_CHUNK) != 0) {
            close_fd(c);
            return -1;
        }
        ssize_t n = recv(c->fd, c->rx + c->rx_len, c->rx_cap - c->rx_len, 0);
        if (n > 0) {
            c->rx_len += (size_t)n;
            return 1;
        }
        if (n == 0) {
            close_fd(c);
            return -1;
        }
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;
        close_fd(c);
        return -1;
    }
}

/* ------------------------------------------------------------------ */
/* framing                                                            */
/* ------------------------------------------------------------------ */

/* Per-frame callback.  Return 0 to continue, 1 to stop now (leaving any
 * further buffered frames for later), 2 to match and consume the rest of
 * the currently buffered batch. */
typedef int (*frame_fn)(void *user, uint8_t type, const uint8_t *payload,
                        uint32_t len);

static int parse_buffered(shesha_client_t *c, frame_fn cb, void *user)
{
    size_t off = 0;
    int stopped = 0;
    int swallow = 0;

    while (c->rx_len - off >= SHE_FRAME_HEADER_SIZE) {
        const uint8_t *p = c->rx + off;
        uint8_t type = p[0];
        uint32_t plen = she_le32(p + 1);
        uint32_t seq = she_le32(p + 5);
        size_t fsz = SHE_FRAME_HEADER_SIZE + (size_t)plen;
        if (c->rx_len - off < fsz) break;

        if (seq != 0) {
            if (c->have_last_seq
                && (int64_t)seq > (int64_t)c->last_seq + 1) {
                c->gaps++;
                c->dropped += (uint64_t)((int64_t)seq - (int64_t)c->last_seq - 1);
            }
            c->last_seq = seq;
            c->have_last_seq = true;
        }

        if (!swallow) {
            int r = cb(user, type, p + SHE_FRAME_HEADER_SIZE, plen);
            if (r == 1) {
                off += fsz;
                stopped = 1;
                break;
            }
            if (r == 2) {
                /* Match: stop after consuming the rest of this batch. */
                swallow = 1;
                stopped = 1;
            }
        }
        off += fsz;
    }

    if (off) {
        memmove(c->rx, c->rx + off, c->rx_len - off);
        c->rx_len -= off;
    }
    return stopped;
}

/* Pump until cb stops (1), the deadline passes (0), or the link fails
 * (-1).  Drains already-buffered frames before polling. */
static int read_until(shesha_client_t *c, int timeout_ms, frame_fn cb, void *user)
{
    if (parse_buffered(c, cb, user)) return 1;

    int64_t deadline = now_ms() + (timeout_ms < 0 ? 0 : timeout_ms);
    for (;;) {
        int64_t remaining = deadline - now_ms();
        if (remaining <= 0) return 0;
        int f = rx_fill(c, (int)remaining);
        if (f < 0) return -1;
        if (f == 0) continue;
        if (parse_buffered(c, cb, user)) return 1;
    }
}

static int send_frame(shesha_client_t *c, uint8_t type, const void *payload,
                      uint32_t plen)
{
    if (alloc_grow(&c->tx, &c->tx_cap,
                   SHE_FRAME_HEADER_SIZE + (size_t)plen) != 0)
        return SHESHA_ERR_NOMEM;
    uint8_t *b = c->tx;
    b[0] = type;
    she_put_le32(b + 1, plen);
    she_put_le32(b + 5, 0);
    if (plen && payload) memcpy(b + SHE_FRAME_HEADER_SIZE, payload, plen);
    return send_all(c, b, SHE_FRAME_HEADER_SIZE + (size_t)plen);
}

/* ------------------------------------------------------------------ */
/* request / response                                                 */
/* ------------------------------------------------------------------ */

struct rr {
    uint8_t  want;
    uint8_t  also_a;
    uint8_t  also_b;
    int      got;
    uint8_t  rtype;
    uint8_t *buf;
    uint32_t cap;
    uint32_t len;
};

static int rr_cb(void *u, uint8_t type, const uint8_t *payload, uint32_t len)
{
    struct rr *x = (struct rr *)u;
    if (type == x->want || type == SHE_RESP_ERROR
        || (x->also_a && type == x->also_a)
        || (x->also_b && type == x->also_b)) {
        x->got = 1;
        x->rtype = type;
        x->len = len < x->cap ? len : x->cap;
        if (x->buf && x->len) memcpy(x->buf, payload, x->len);
        return 2;
    }
    return 0;
}

static int exchange(shesha_client_t *c, uint8_t type, const void *payload,
                    uint32_t plen, uint8_t want, uint8_t also_a, uint8_t also_b,
                    int timeout_ms, uint8_t *out, uint32_t cap,
                    uint32_t *out_len, uint8_t *out_type)
{
    int rc = send_frame(c, type, payload, plen);
    if (rc != SHESHA_OK) return rc;

    struct rr x;
    memset(&x, 0, sizeof x);
    x.want = want;
    x.also_a = also_a;
    x.also_b = also_b;
    x.buf = out;
    x.cap = cap;

    int r = read_until(c, timeout_ms, rr_cb, &x);
    if (r < 0) return SHESHA_ERR_CLOSED;
    if (r == 0 || !x.got) return SHESHA_ERR_TIMEOUT;

    if (out_len) *out_len = x.len;
    if (out_type) *out_type = x.rtype;
    return SHESHA_OK;
}

static int config_cmd_to(shesha_client_t *c, uint8_t type, const void *payload,
                         uint32_t plen, int timeout_ms)
{
    uint8_t buf[8];
    uint32_t len = 0;
    uint8_t t = 0;
    int r = exchange(c, type, payload, plen, SHE_RESP_OK, 0, 0, timeout_ms,
                     buf, sizeof buf, &len, &t);
    if (r != SHESHA_OK) return r;
    if (t == SHE_RESP_ERROR) return SHESHA_ERR_DAEMON;
    if (t != SHE_RESP_OK) return SHESHA_ERR_PROTOCOL;
    return SHESHA_OK;
}

static int config_cmd(shesha_client_t *c, uint8_t type, const void *payload,
                      uint32_t plen)
{
    return config_cmd_to(c, type, payload, plen, 2000);
}

/* ------------------------------------------------------------------ */
/* lifecycle                                                          */
/* ------------------------------------------------------------------ */

shesha_client_t *shesha_client_new(void)
{
    shesha_client_t *c = calloc(1, sizeof *c);
    if (!c) return NULL;
    c->fd = -1;
    c->timeout_s = 5.0;
    return c;
}

void shesha_client_disconnect(shesha_client_t *c)
{
    if (!c) return;
    close_fd(c);
    c->rx_len = 0;
}

void shesha_client_free(shesha_client_t *c)
{
    if (!c) return;
    shesha_client_disconnect(c);
    free(c->rx);
    free(c->tx);
    free(c);
}

int shesha_client_connect(shesha_client_t *c, const char *host, uint16_t port,
                          double timeout_s)
{
    if (!c || !host) return SHESHA_ERR_PARAM;
    shesha_client_disconnect(c);

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return SHESHA_ERR_SOCKET;
    if (set_nonblocking(fd) != 0) {
        close(fd);
        return SHESHA_ERR_SOCKET;
    }
    {
        int one = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
#ifdef SO_NOSIGPIPE
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
    }

    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons(port);
    if (inet_pton(AF_INET, host, &sa.sin_addr) != 1) {
        close(fd);
        return SHESHA_ERR_PARAM;
    }

    int rc = connect(fd, (struct sockaddr *)&sa, sizeof sa);
    if (rc < 0 && errno != EINPROGRESS) {
        close(fd);
        return SHESHA_ERR_CONNECT;
    }
    if (rc < 0) {
        int w = wait_fd(fd, POLLOUT, (int)((timeout_s > 0 ? timeout_s : 5.0)
                                            * 1000.0));
        if (w <= 0) {
            close(fd);
            return w == 0 ? SHESHA_ERR_TIMEOUT : SHESHA_ERR_CONNECT;
        }
        int err = 0;
        socklen_t el = sizeof err;
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &el) != 0 || err) {
            close(fd);
            return SHESHA_ERR_CONNECT;
        }
    }

    c->fd = fd;
    c->timeout_s = timeout_s > 0 ? timeout_s : 5.0;
    c->rx_len = 0;
    c->have_last_seq = false;
    c->last_seq = 0;
    c->gaps = 0;
    c->dropped = 0;
    return SHESHA_OK;
}

bool shesha_client_connected(const shesha_client_t *c)
{
    return c && c->fd >= 0;
}

uint64_t shesha_client_gaps(const shesha_client_t *c)
{
    return c ? c->gaps : 0;
}

uint64_t shesha_client_dropped(const shesha_client_t *c)
{
    return c ? c->dropped : 0;
}

/* ------------------------------------------------------------------ */
/* queries                                                            */
/* ------------------------------------------------------------------ */

int shesha_client_status(shesha_client_t *c, shesha_status_t *out)
{
    if (!c || !out) return SHESHA_ERR_PARAM;
    uint8_t buf[SHE_STATUS_WIRE_SIZE + 8];
    uint32_t len = 0;
    uint8_t t = 0;
    int r = exchange(c, SHE_CMD_STATUS, NULL, 0, SHE_RESP_STATUS, 0, 0, 2000,
                     buf, sizeof buf, &len, &t);
    if (r != SHESHA_OK) return r;
    if (t == SHE_RESP_ERROR) return SHESHA_ERR_DAEMON;
    if (t != SHE_RESP_STATUS) return SHESHA_ERR_PROTOCOL;
    return shesha__parse_status(buf, len, out);
}

int shesha_client_tx_stats(shesha_client_t *c, shesha_tx_stats_t *out)
{
    if (!c || !out) return SHESHA_ERR_PARAM;
    uint8_t buf[SHE_TX_STATS_WIRE_SIZE + 8];
    uint32_t len = 0;
    uint8_t t = 0;
    int r = exchange(c, SHE_CMD_TX_STATS, NULL, 0, SHE_RESP_TX_STATS, 0, 0,
                     2000, buf, sizeof buf, &len, &t);
    if (r != SHESHA_OK) return r;
    if (t == SHE_RESP_ERROR) return SHESHA_ERR_DAEMON;
    if (t != SHE_RESP_TX_STATS) return SHESHA_ERR_PROTOCOL;
    return shesha__parse_tx_stats(buf, len, out);
}

/* ------------------------------------------------------------------ */
/* config                                                             */
/* ------------------------------------------------------------------ */

int shesha_client_set_mode(shesha_client_t *c, shesha_mode_t mode)
{
    uint8_t p = (uint8_t)mode;
    return config_cmd(c, SHE_CMD_SET_MODE, &p, 1);
}

int shesha_client_set_freq(shesha_client_t *c, uint64_t freq_hz)
{
    uint8_t p[8];
    she_put_le64(p, freq_hz);
    return config_cmd(c, SHE_CMD_SET_FREQ, p, sizeof p);
}

int shesha_client_set_bw(shesha_client_t *c, uint32_t bw_hz)
{
    uint8_t p[4];
    she_put_le32(p, bw_hz);
    return config_cmd(c, SHE_CMD_SET_BW, p, sizeof p);
}

int shesha_client_set_gain(shesha_client_t *c, float gain_db)
{
    uint8_t p[4];
    she_put_lef32(p, gain_db);
    return config_cmd(c, SHE_CMD_SET_GAIN, p, sizeof p);
}

int shesha_client_set_agc(shesha_client_t *c, uint8_t mode)
{
    return config_cmd(c, SHE_CMD_SET_AGC, &mode, 1);
}

int shesha_client_set_sample_rate(shesha_client_t *c, uint32_t rate_hz)
{
    uint8_t p[4];
    she_put_le32(p, rate_hz);
    return config_cmd(c, SHE_CMD_SET_SAMPLE_RATE, p, sizeof p);
}

int shesha_client_set_iq_bits(shesha_client_t *c, int bits)
{
    if (bits != SHESHA_IQ_BITS_8 && bits != SHESHA_IQ_BITS_12)
        return SHESHA_ERR_PARAM;
    uint8_t p = (uint8_t)bits;
    return config_cmd(c, SHE_CMD_SET_IQ_BITS, &p, 1);
}

int shesha_client_set_tx_atten(shesha_client_t *c, float db)
{
    uint8_t p[4];
    she_put_lef32(p, db);
    return config_cmd(c, SHE_CMD_SET_TX_ATTEN, p, sizeof p);
}

int shesha_client_subscribe(shesha_client_t *c, uint8_t mask)
{
    return config_cmd(c, SHE_CMD_SUBSCRIBE, &mask, 1);
}

/* ------------------------------------------------------------------ */
/* IQ receive                                                         */
/* ------------------------------------------------------------------ */

struct iq_ctx {
    shesha_iq_frame_t *out;
    int got;
};

static int iq_cb(void *u, uint8_t type, const uint8_t *payload, uint32_t len)
{
    struct iq_ctx *x = (struct iq_ctx *)u;
    if (type == SHE_DATA_IQ && shesha__parse_iq(payload, len, x->out) == SHESHA_OK) {
        x->got = 1;
        return 1;
    }
    return 0;
}

int shesha_client_iq_rx_next(shesha_client_t *c, shesha_iq_frame_t *out,
                             int timeout_ms)
{
    if (!c || !out) return SHESHA_ERR_PARAM;
    memset(out, 0, sizeof *out);

    struct iq_ctx x;
    x.out = out;
    x.got = 0;

    int r = read_until(c, timeout_ms, iq_cb, &x);
    if (r < 0) return SHESHA_ERR_CLOSED;
    if (r == 0) return 0;
    return x.got ? 1 : 0;
}

void shesha_iq_frame_free(shesha_iq_frame_t *f)
{
    if (!f) return;
    free(f->samples);
    memset(f, 0, sizeof *f);
}

/* ------------------------------------------------------------------ */
/* IQ transmit                                                        */
/* ------------------------------------------------------------------ */

static int send_iq_stream_data(shesha_client_t *c, const void *data, size_t len)
{
    if (c->fd < 0) return SHESHA_ERR_STATE;
    uint32_t plen = 4 + (uint32_t)len;
    if (alloc_grow(&c->tx, &c->tx_cap,
                   SHE_FRAME_HEADER_SIZE + (size_t)plen) != 0)
        return SHESHA_ERR_NOMEM;
    uint8_t *b = c->tx;
    b[0] = SHE_CMD_IQ_TX_STREAM_DATA;
    she_put_le32(b + 1, plen);
    she_put_le32(b + 5, 0);
    she_put_le32(b + 9, (uint32_t)len);
    if (len) memcpy(b + 13, data, len);
    return send_all(c, b, 13 + len);
}

shesha_push_result_t shesha_client_iq_tx_stream_data(shesha_client_t *c,
                                                     const void *data,
                                                     size_t len,
                                                     int timeout_ms)
{
    if (!c || !data || len == 0) return SHESHA_PUSH_ERROR;
    if (send_iq_stream_data(c, data, len) != SHESHA_OK)
        return SHESHA_PUSH_ERROR;

    struct rr x;
    memset(&x, 0, sizeof x);
    x.want = SHE_RESP_OK;
    x.also_a = SHE_RESP_RETRY;

    int r = read_until(c, timeout_ms, rr_cb, &x);
    if (r <= 0 || !x.got) return SHESHA_PUSH_ERROR;
    if (x.rtype == SHE_RESP_OK) return SHESHA_PUSH_OK;
    if (x.rtype == SHE_RESP_RETRY) return SHESHA_PUSH_RETRY;
    return SHESHA_PUSH_ERROR;
}

int shesha_client_iq_tx_stream_end(shesha_client_t *c)
{
    return config_cmd_to(c, SHE_CMD_IQ_TX_STREAM_END, NULL, 0, 5000);
}

int shesha_client_tx_wait(shesha_client_t *c, int timeout_ms)
{
    if (!c) return SHESHA_ERR_PARAM;
    int64_t deadline = now_ms() + (timeout_ms < 0 ? 0 : timeout_ms);
    for (;;) {
        shesha_tx_stats_t ts;
        int r = shesha_client_tx_stats(c, &ts);
        if (r != SHESHA_OK) return r;
        if (!ts.tx_active && !ts.iq_active) return SHESHA_OK;
        if (now_ms() >= deadline) return SHESHA_ERR_TIMEOUT;
        msleep(20);
    }
}

/* ------------------------------------------------------------------ */
/* convenience: capture / transmit                                    */
/* ------------------------------------------------------------------ */

int shesha_client_iq_capture(shesha_client_t *c,
                             const shesha_capture_opts_t *opts,
                             uint8_t *out, size_t out_capacity,
                             size_t *out_len,
                             shesha_capture_info_t *info)
{
    if (!c || !opts || !out || !out_len) return SHESHA_ERR_PARAM;
    if (opts->bits != SHESHA_IQ_BITS_8 && opts->bits != SHESHA_IQ_BITS_12)
        return SHESHA_ERR_PARAM;
    if (opts->duration_s <= 0) return SHESHA_ERR_PARAM;

    size_t bps = opts->bits == SHESHA_IQ_BITS_8 ? 2 : 3;
    int rc = SHESHA_OK;
    size_t used = 0;
    uint32_t total = 0;

    /* Save what we can restore afterwards. */
    shesha_status_t prev;
    memset(&prev, 0, sizeof prev);
    bool have_prev = shesha_client_status(c, &prev) == SHESHA_OK;

    if ((rc = shesha_client_set_mode(c, SHESHA_MODE_IQ)) != SHESHA_OK) goto restore;
    if ((rc = shesha_client_set_bw(c, opts->bw_hz)) != SHESHA_OK) goto restore;
    if ((rc = shesha_client_set_sample_rate(c, opts->sample_rate_hz)) != SHESHA_OK) goto restore;
    if ((rc = shesha_client_set_iq_bits(c, opts->bits)) != SHESHA_OK) goto restore;
    if ((rc = shesha_client_set_freq(c, opts->lo_hz)) != SHESHA_OK) goto restore;
    if ((rc = shesha_client_set_agc(c, opts->agc_mode)) != SHESHA_OK) goto restore;
    if ((rc = shesha_client_set_gain(c, opts->gain_db)) != SHESHA_OK) goto restore;

    {
        shesha_status_t now;
        if (info) {
            uint32_t rate = opts->sample_rate_hz;
            uint64_t lo = opts->lo_hz;
            float gain = opts->gain_db;
            if (shesha_client_status(c, &now) == SHESHA_OK) {
                rate = now.sample_rate_hz;
                lo = now.freq_hz;
                gain = now.gain_db;
            }
            info->sample_rate_hz = rate;
            info->lo_hz = lo;
            info->gain_db = gain;
            info->n_samples = 0;
            info->bits = opts->bits;
        }
    }

    if ((rc = shesha_client_subscribe(c, SHESHA_SUBSCRIBE_IQ)) != SHESHA_OK) goto restore;

    {
        int64_t deadline = now_ms() + (int64_t)(opts->duration_s * 1000.0 + 0.5);
        while (now_ms() < deadline) {
            int64_t rem = deadline - now_ms();
            int to = rem < 1 ? 1 : (int)rem;
            shesha_iq_frame_t f;
            int r = shesha_client_iq_rx_next(c, &f, to);
            if (r == 1) {
                size_t need = (size_t)f.n_samples * bps;
                if (used + need > out_capacity) {
                    shesha_iq_frame_free(&f);
                    rc = SHESHA_ERR_NOMEM;
                    break;
                }
                memcpy(out + used, f.samples, need);
                used += need;
                total += f.n_samples;
                shesha_iq_frame_free(&f);
            } else if (r == 0) {
                continue;
            } else {
                rc = r;
                break;
            }
        }
    }

restore:
    /* Drop the subscription before restoring state: an active IQ
     * subscription keeps streaming frames that would otherwise collide
     * with the next request/response (mirrors liblora.pluto.capture). */
    shesha_client_subscribe(c, 0);
    if (have_prev) {
        if ((uint8_t)prev.daemon_mode != (uint8_t)SHESHA_MODE_IQ)
            shesha_client_set_mode(c, (shesha_mode_t)prev.daemon_mode);
        if (prev.sample_rate_hz)
            shesha_client_set_sample_rate(c, prev.sample_rate_hz);
        if (prev.bw_hz)
            shesha_client_set_bw(c, prev.bw_hz);
    }

    *out_len = used;
    if (info) info->n_samples = total;
    return rc;
}

int64_t shesha_client_iq_transmit(shesha_client_t *c,
                                  const uint8_t *packed, size_t packed_len,
                                  const shesha_transmit_opts_t *opts)
{
    if (!c || !packed || !opts) return SHESHA_ERR_PARAM;
    if (opts->bits != SHESHA_IQ_BITS_8 && opts->bits != SHESHA_IQ_BITS_12)
        return SHESHA_ERR_PARAM;
    size_t bps = opts->bits == SHESHA_IQ_BITS_8 ? 2 : 3;
    if (packed_len == 0 || packed_len % bps != 0) return SHESHA_ERR_PARAM;

    size_t chunk_samples = opts->chunk_samples ? opts->chunk_samples : 4096;
    size_t chunk_bytes = chunk_samples * bps;

    int rc;
    if ((rc = shesha_client_set_mode(c, SHESHA_MODE_IQ)) != SHESHA_OK) return rc;
    if ((rc = shesha_client_set_iq_bits(c, opts->bits)) != SHESHA_OK) return rc;
    if ((rc = shesha_client_set_sample_rate(c, opts->sample_rate_hz)) != SHESHA_OK) return rc;
    if ((rc = shesha_client_set_bw(c, opts->bw_hz)) != SHESHA_OK) return rc;
    if ((rc = shesha_client_set_freq(c, opts->freq_hz)) != SHESHA_OK) return rc;
    if ((rc = shesha_client_set_tx_atten(c, opts->atten_db)) != SHESHA_OK) return rc;

    int64_t pushed = 0;
    for (size_t off = 0; off < packed_len; off += chunk_bytes) {
        size_t n = packed_len - off < chunk_bytes ? packed_len - off : chunk_bytes;
        for (;;) {
            shesha_push_result_t pr =
                shesha_client_iq_tx_stream_data(c, packed + off, n, 10000);
            if (pr == SHESHA_PUSH_OK) break;
            if (pr == SHESHA_PUSH_RETRY) {
                msleep(5);
                continue;
            }
            return SHESHA_ERR_DAEMON;
        }
        pushed += (int64_t)(n / bps);
    }

    if ((rc = shesha_client_iq_tx_stream_end(c)) != SHESHA_OK) return rc;

    /* The commands above only acknowledge acceptance; wait for the daemon to
     * actually emit the stream.  Disconnecting earlier would stop the TX and
     * truncate the tail (worst for the shortest waveforms). */
    int64_t drain_ms = 5000;
    if (opts->sample_rate_hz && pushed > 0)
        drain_ms = (pushed * 1000) / (int64_t)opts->sample_rate_hz + 5000;
    int w = shesha_client_tx_wait(c, (int)drain_ms);
    if (w != SHESHA_OK) return w;

    return pushed;
}

/* ------------------------------------------------------------------ */
/* payload parsers                                                    */
/* ------------------------------------------------------------------ */

int shesha__parse_status(const uint8_t *p, uint32_t len, shesha_status_t *out)
{
    if (len < SHE_STATUS_WIRE_SIZE) return SHESHA_ERR_PROTOCOL;
    out->freq_hz        = she_le64(p + 0);
    out->sample_rate_hz = she_le32(p + 8);
    out->bw_hz          = she_le32(p + 12);
    out->fft_size       = she_le32(p + 16);
    out->gain_db        = she_lef32(p + 20);
    out->agc_mode       = p[24];
    out->window_type    = p[25];
    out->target_fps     = p[26];
    out->daemon_mode    = p[27];
    out->tx_atten_db    = she_lef32(p + 28);
    out->tx_busy        = p[32];
    out->tx_proto       = p[33];
    out->tx_flags       = p[34];
    out->tx_repeat      = she_le16(p + 35);
    out->tx_interval_us = she_le32(p + 37);
    out->fpga_active    = p[41] != 0;
    out->rx_active      = p[42] != 0;
    out->iq_bits        = p[43];
    return SHESHA_OK;
}

int shesha__parse_tx_stats(const uint8_t *p, uint32_t len,
                           shesha_tx_stats_t *out)
{
    if (len < SHE_TX_STATS_WIRE_SIZE) return SHESHA_ERR_PROTOCOL;
    out->fed             = she_le64(p + 0);
    out->emitted         = she_le64(p + 8);
    out->elapsed_us      = she_le64(p + 16);
    out->audio_consumed  = she_le64(p + 24);
    out->starved_samples = she_le64(p + 32);
    out->dac_rate        = she_le32(p + 40);
    out->depth           = (int32_t)she_le32(p + 44);
    out->max_depth       = she_le32(p + 48);
    out->high_water      = she_le32(p + 52);
    out->underruns       = she_le32(p + 56);
    out->ring_fill       = she_le32(p + 60);
    out->ring_size       = she_le32(p + 64);
    out->audio_rate      = she_le32(p + 68);
    out->push_rejects    = she_le32(p + 72);
    out->tx_active       = p[76] != 0;
    out->fm_active       = p[77] != 0;
    out->end_of_stream   = p[78] != 0;
    out->iq_ring_fill    = she_le32(p + 80);
    out->iq_push_rejects = she_le32(p + 84);
    out->iq_starved      = she_le32(p + 88);
    out->iq_active       = p[92] != 0;
    out->dma_start_failed = p[93] != 0;
    return SHESHA_OK;
}

int shesha__parse_iq(const uint8_t *p, uint32_t len, shesha_iq_frame_t *out)
{
    if (len < SHE_IQ_HEADER_SIZE) return SHESHA_ERR_PROTOCOL;
    uint8_t bits = p[16];
    if (bits != SHESHA_IQ_BITS_8 && bits != SHESHA_IQ_BITS_12)
        return SHESHA_ERR_PROTOCOL;
    size_t bps = bits == SHESHA_IQ_BITS_8 ? 2 : 3;
    uint16_t n = she_le16(p + 18);
    size_t expect = (size_t)n * bps;
    if ((size_t)len < SHE_IQ_HEADER_SIZE + expect) return SHESHA_ERR_PROTOCOL;

    /* Payload holds an IEEE-754 little-endian double. */
    {
        uint64_t tu = she_le64(p + 0);
        memcpy(&out->timestamp, &tu, sizeof out->timestamp);
    }
    out->freq_mhz       = she_lef32(p + 8);
    out->sample_rate_hz = she_le32(p + 12);
    out->bits           = bits;
    out->n_samples      = n;
    out->samples        = NULL;
    if (expect) {
        out->samples = malloc(expect);
        if (!out->samples) return SHESHA_ERR_NOMEM;
        memcpy(out->samples, p + SHE_IQ_HEADER_SIZE, expect);
    }
    return SHESHA_OK;
}
