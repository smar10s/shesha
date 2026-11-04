/*
 * shesha-daemon — capture and transmit daemon for PlutoSDR
 *
 * Modes:
 *   waterfall  — DMA capture → window → FFT → fftshift → dB → ring buffer
 *   wifi       — DMA capture → lib80211 decode → DATA_WIFI frames
 *                (software or FPGA-accelerated)
 *   fm         — DMA capture → FIR decimation → FM demod + RDS decode
 *                → DATA_FM frames
 *   iq         — DMA capture → 8/12-bit packed samples → DATA_IQ frames
 *
 * TX paths:
 *   vector     — WiFi frames through the scheduler, one-shot, cyclic, or
 *                repeat-forever
 *   streaming  — continuous FM (with RDS) or raw IQ, fed by the client and
 *                clocked out by the scheduler
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <pthread.h>
#include <stdatomic.h>
#include <time.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>

#include "protocol.h"
#include "fm_protocol.h"
#include "sendq.h"

/*
 * Firmware build fingerprint (git SHA), injected by CMake via the
 * Makefile.  Reported in RESP_CAPS so the client can tell which daemon
 * binary it is talking to.  Zero means it was built outside the normal
 * flow.
 */
#ifndef SHESHA_FW_BUILD_ID
#define SHESHA_FW_BUILD_ID 0u
#endif

#include "adc_clip.h"
#include "waterfall.h"
#include "waterfall_mode.h"
#include "spsc_ring.h"
#include "ring_slots.h"
#include "radio.h"
#include "window.h"
#include "wifi_rx.h"
#include "deimos_rx.h"
#include "accel.h"
#include "wifi_tx.h"
#include "fm_tx.h"
#include "fm_mod.h"
#include "fm_mode.h"
#include "iq_mode.h"
#include "iq_tx.h"
#include "thread_stats.h"
#include "fm_rx.h"
#include "dma_tx.h"
#include "tx_scheduler.h"
#include "mode_radio.h"
#include "session.h"
#include "tx_session.h"
#include "producer.h"
#include "radio_txn.h"
#include "wifi_fpga.h"
#include "hal.h"

#define DEFAULT_PORT       5555
#define MAX_CLIENTS        4
#define DEFAULT_FPS        50
#define RECV_BUF_SIZE      2048
/*
 * Per-client outbound data capacity.  Sized for the worst-case IQ stream:
 * 12-bit at 6 MSPS is 18 MB/s, and a single stalled USB2 write can easily
 * exceed a few hundred ms.  The old 256 KB absorbed only ~40 ms and
 * dropped frames on every hiccup (12-bit 4 MSPS measured 2.86 MSPS of the
 * 4.00 offered).  4 MB gives ~220 ms of headroom at that rate; the
 * client's sequence numbers still expose any gap, but they should now be
 * rare.  The control ring only carries command responses and status
 * frames, which are small and infrequent.
 */
#define SEND_RING_SIZE     (4 * 1024 * 1024)
#define CONTROL_RING_SIZE  (64 * 1024)
#define SEND_SOCKBUF       (1024 * 1024)  /* requested SO_SNDBUF */

atomic_bool g_running = true;
radio_state_t g_radio;
waterfall_t *g_waterfall;
spsc_ring_t *g_ringbuf;
/* The PSD producers' shared read position.  Owned here because it outlives
 * any individual producer and must be reset exactly once on a size change,
 * not once per producer.  See wf_claim.h. */
wf_claim_t g_wf_claim;
static uint8_t g_target_fps = DEFAULT_FPS;
static accum_mode_t g_accum_mode = ACCUM_MAX;
static struct timespec g_start_time;
static uint16_t g_port = DEFAULT_PORT;

static int g_mode = DAEMON_MODE_WATERFALL;
static const producer_ops_t *g_active_producer = NULL;

spsc_ring_t *g_wifi_ringbuf = NULL;
spsc_ring_t *g_fm_ringbuf = NULL;
spsc_ring_t *g_iq_ringbuf = NULL;
atomic_uint g_iq_bits = 12;   /* wire sample width: 8 or 12 */

/* One record for the TX lifecycle: state, telemetry fields, owner, and the
 * suspended RX producer.  Transitions live in tx_session.c. */
static tx_session_t g_tx_session;

/* Mode enter/exit now dispatched through g_active_producer (producer.h). */

/* Defined with the waterfall config setters; declared here because the
 * sample-rate handler (which may re-cycle the producer) precedes it. */
static bool waterfall_producer_live(void);

/* Defined with the TX commands; the radio transaction needs the same
 * definition of "a TX owns the sample clock". */
static bool tx_holds_clock(void);

typedef struct {
    int fd;
    uint32_t id;         /* stable across remove_client() compaction */
    uint8_t recv_buf[RECV_BUF_SIZE];
    size_t recv_len;
    uint8_t *ovf_buf;
    size_t ovf_total;
    size_t ovf_offset;
    session_client_t session;
    /* Outbound control/data queues.  Data frames are dropped when their
     * ring is full, so a slow client loses frames instead of getting
     * disconnected.  Control frames always win at frame boundaries.
     * Flushed on POLLOUT in the main loop. */
    sendq_t *sendq;
} client_t;

static client_t g_clients[MAX_CLIENTS];
static int g_num_clients = 0;
static uint32_t g_next_client_id = 1;

/* Every subscribed client gets its own copy of DATA_IQ, and each copy
 * costs the same host link.  Used by TX admission and by the RX producer
 * idle path. */
static unsigned iq_rx_subscriber_count(void)
{
    unsigned n = 0;
    for (int i = 0; i < g_num_clients; i++) {
        if (session_client_wants(&g_clients[i].session, DATA_IQ))
            n++;
    }
    return n;
}

/* Flush as much queued data as the socket will accept without blocking.
 * Consume only the bytes send() accepted so a partial write never splits a
 * frame.  An empty peek is not peer loss: use sendq_pending() to decide
 * whether anything is left to send.  Returns false only when the peer is
 * gone (send error other than EAGAIN). */
static bool client_flush(client_t *c)
{
    for (;;) {
        bool is_control;
        const uint8_t *ptr;
        size_t n = sendq_peek(c->sendq, &is_control, &ptr);
        if (n == 0)
            return true;

        ssize_t sent = send(c->fd, ptr, n, MSG_NOSIGNAL);
        if (sent < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                return true;  /* socket full, try again later */
            return false;     /* peer gone */
        }
        if (sent == 0)
            return false;
        sendq_consume(c->sendq, is_control, (size_t)sent);
    }
}

/* Does a connected client still own this id?  Ids survive the compaction
 * remove_client() performs, so this is the only stable identity. */
static bool client_alive_id(uint32_t id)
{
    for (int i = 0; i < g_num_clients; i++) {
        if (g_clients[i].id == id)
            return true;
    }
    return false;
}

/* Mark all clients except the issuer as needing a status notification.
 * Wraps the session logic for the strided client_t array. */
static void notify_others(int issuer_idx)
{
    for (int i = 0; i < g_num_clients; i++) {
        if (i != issuer_idx)
            g_clients[i].session.notify_pending = true;
    }
}

/* Send an unsolicited RESP_STATUS to a single client.  Used for mode-change
 * notification so observers know the radio was reconfigured.  Returns
 * whether the frame was queued. */
static bool send_status_notification(client_t *c);

/* Defined with the TX commands; also used by mode-change cancellation. */
static void tx_flush_zero(void);

static void sighandler(int sig) { (void)sig; atomic_store(&g_running, false); }

double monotonic_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (ts.tv_sec - g_start_time.tv_sec) +
           (ts.tv_nsec - g_start_time.tv_nsec) / 1e9;
}

/* --- Mode switching ---
 *
 * A producer change and any radio change it needs are one transaction:
 * exit the running producer, apply bandwidth/rate, enter the target, and
 * roll both back if the target fails.  The ordering lives in
 * radio_txn.c so host tests can assert it.
 *
 * `force` re-enters even when the target matches g_active_producer.
 * Needed after a TX suspension has torn the producer down: the pointer
 * still names the suspended producer, so the usual no-op shortcut would
 * leave the daemon with no producer running at all.  `prev_running` says
 * whether `prev` still owns the DMA.
 */
static int radio_txn_set_bw(uint32_t bw_hz, uint32_t *actual_hz, void *ctx)
{
    (void)ctx;
    return radio_set_bw(&g_radio, bw_hz, actual_hz);
}

static int radio_txn_set_sr(uint32_t sr_hz, uint32_t *actual_hz, void *ctx)
{
    (void)ctx;
    return radio_set_sample_rate(&g_radio, sr_hz, actual_hz);
}

static const radio_txn_io_t g_radio_txn_io = {
    .set_bw = radio_txn_set_bw,
    .set_sr = radio_txn_set_sr,
};

static int switch_producer(const producer_ops_t *next, bool force,
                           bool prev_running)
{
    if (!next) return -1;
    /* A same-mode request while the producer is not ready (still starting,
     * or stopped) must re-enter: leaving the old thread alive would put two
     * producers on one DMA/ring.  A redundant set against a running producer
     * still short-circuits. */
    bool force_reentry = force
        || (next == g_active_producer && !producer_is_running(next));
    if (next == g_active_producer && !force_reentry && prev_running) return 0;

    radio_txn_req_t req = {
        .kind = RADIO_TXN_ENTRY,
        .prev = g_active_producer,
        .next = next,
        .prev_running = prev_running,
        .force = force_reentry,
        .cur_bw = g_radio.bw_hz,
        .cur_sr = g_radio.sample_rate_hz,
    };
    radio_txn_result_t res;
    radio_txn_status_t st = radio_txn_run(&req, &g_radio_txn_io, NULL, &res);
    if (st != RADIO_TXN_OK) {
        fprintf(stderr, "radio: entry into '%s' failed (status %d)\n",
                next->name, (int)st);
        return -1;
    }

    /* Modes that own the AGC (wifi_fpga: fabric pipeline needs fast_attack)
     * settle g_radio to it on entry, so RESP_STATUS tells the truth about
     * the chip's gain mode. */
    if (next->radio.agc_fixed)
        g_radio.agc_mode = next->radio.agc_fixed;

    g_active_producer = next;
    g_mode = next->mode;
    return 0;
}

/* --- TX exclusivity ---
 *
 * The AD9361 is full-duplex: TX and RX run at the same time, and WiFi TX
 * does exactly that (see test_beacon_loopback, which transmits and receives
 * simultaneously).  What TX and RX cannot do is run at DIFFERENT SAMPLE
 * RATES, because they share one sample clock.  Verified on hardware:
 * writing out_voltage_sampling_frequency also moves
 * in_voltage_sampling_frequency.
 *
 * That is the whole reason FM TX displaces an RX producer: the rate
 * conflict, not CPU cost.  With both Cortex-A9 cores online (required —
 * see enable_dual_core.sh), TX at 61% plus FM RX at 42% fits in 200%.
 *
 * Decision logic and state tracking live in tx_session.h/c
 * (host-testable).  This file owns the side effects: stopping/starting
 * producers and changing the sample rate.
 */
static void tx_suspend_producer(void)
{
    if (tx_session_clock_held(&g_tx_session))
        return;                         /* already suspended */

    tx_session_hold_clock(&g_tx_session, g_mode, g_radio.sample_rate_hz,
                          g_active_producer->requires_fpga);

    if (g_active_producer)
        g_active_producer->exit();

    fprintf(stderr, "tx: suspended producer '%s' (rate %u) "
            "for exclusive TX\n",
            g_active_producer ? g_active_producer->name : "none",
            g_tx_session.saved_rate);
}

static radio_txn_status_t resume_at_rate(const producer_ops_t *target,
                                         uint32_t rate,
                                         radio_txn_result_t *res)
{
    radio_txn_req_t req = {
        .kind = RADIO_TXN_SR,
        .prev = NULL,
        .next = target,
        .prev_running = false,
        .value = rate,
        .cur_bw = g_radio.bw_hz,
        .cur_sr = g_radio.sample_rate_hz,
    };
    return radio_txn_run(&req, &g_radio_txn_io, NULL, res);
}

static void tx_resume_producer(void)
{
    tx_clock_resume_t r = tx_session_release_clock(&g_tx_session);
    if (r.mode < 0)
        return;

    /* Pick the correct producer variant (SW or FPGA) based on what was
     * saved.  If FPGA resume fails, fall back to SW.  The transaction
     * restores the saved rate before the producer starts, so it never
     * sees the dma_rx_stop()/dma_rx_start() a rate change performs. */
    const producer_ops_t *target = r.fpga
        ? producer_fpga_for_mode(r.mode) : NULL;
    radio_txn_result_t res;
    radio_txn_status_t st = RADIO_TXN_INVALID;

    if (target) {
        st = resume_at_rate(target, r.rate, &res);
        if (st != RADIO_TXN_OK) {
            fprintf(stderr, "tx: fpga resume failed, falling back to "
                    "software\n");
            target = NULL;
        }
    }
    if (!target) {
        target = producer_for_mode(r.mode);
        if (target)
            st = resume_at_rate(target, r.rate, &res);
    }

    if (target && st == RADIO_TXN_OK) {
        /* The producer may have been suspended while the client changed
         * AGC/gain.  Reapply the mode's policy so g_radio matches what
         * its enter() just programmed. */
        if (target->radio.agc_fixed)
            g_radio.agc_mode = target->radio.agc_fixed;
        g_active_producer = target;
        g_mode = target->mode;
    }
    fprintf(stderr, "tx: resumed producer '%s' (rate %u)\n",
            (target && st == RADIO_TXN_OK) ? target->name : "none", r.rate);
}

/* Is a TX currently holding the sample clock?
 *
 * This is narrower than tx_session_active(): it is true only while a TX has
 * displaced an RX producer to claim a rate (FM TX).  A WiFi TX burst does
 * not touch the rate, so gating mode changes on it would block operations
 * that have no conflict — and would break the concurrent TX+RX that
 * test_beacon_loopback depends on.
 */
static bool tx_holds_clock(void)
{
    return tx_session_clock_held(&g_tx_session);
}

/* --- Command dispatch --- */

/* RESP_STATUS payload: 47 bytes of state, a 26-byte radio-policy block,
 * two 64-bit ADC rail counters, and the accumulator/running-state bytes.
 * The policy is whatever the active producer enforces, so the client never
 * has to duplicate mode/rate rules. */
#define STATUS_PAYLOAD_BYTES 91

/* Serialize the current daemon state into a RESP_STATUS payload.
 * Returns the number of bytes written (always STATUS_PAYLOAD_BYTES). */
static size_t build_status_payload(uint8_t *sp)
{
    size_t off = 0;
    /*
     * tx_busy is the session, not the scheduler.  The session stays active
     * through the drain and the completion cleanup, so a status poll cannot
     * observe the gap between the thread exiting and the owner being
     * cleared.
     */
    memcpy(sp + off, &g_radio.freq_hz, 8); off += 8;
    memcpy(sp + off, &g_radio.sample_rate_hz, 4); off += 4;
    memcpy(sp + off, &g_radio.bw_hz, 4); off += 4;
    uint32_t ffs = g_waterfall
        ? (uint32_t)waterfall_get_fft_size(g_waterfall)
        : WATERFALL_DEFAULT_FFT_SIZE;
    memcpy(sp + off, &ffs, 4); off += 4;
    memcpy(sp + off, &g_radio.gain_db, 4); off += 4;
    sp[off++] = g_radio.agc_mode;
    sp[off++] = g_waterfall ? (uint8_t)waterfall_get_window(g_waterfall) : 0;
    sp[off++] = g_target_fps;
    sp[off++] = (uint8_t)g_mode;
    memcpy(sp + off, &g_radio.tx_atten_db, 4); off += 4;
    sp[off++] = (uint8_t)tx_session_active(&g_tx_session);
    sp[off++] = g_tx_session.proto;
    sp[off++] = g_tx_session.flags;
    memcpy(sp + off, &g_tx_session.repeat, 2); off += 2;
    memcpy(sp + off, &g_tx_session.interval_us, 4); off += 4;
    sp[off++] = (uint8_t)(g_active_producer && g_active_producer->requires_fpga);
    /* Snapshot once so one frame cannot report rx_active and fpga_running
     * from opposite sides of a producer state change. */
    bool running = producer_is_running(g_active_producer);
    /* Whether the configured mode's producer is actually RUNNING.  The mode
     * remains configured during an exclusive TX even though its producer
     * has been stopped. */
    sp[off++] = (uint8_t)running;
    sp[off++] = (uint8_t)atomic_load(&g_iq_bits);
    /*
     * Coverage: the fraction of the sample stream that reached an FFT, in
     * tenths of a percent.  It is the one number that says whether the
     * display is showing all of the signal or a time-gapped sample of it,
     * and until now nothing reported it -- the counters existed but stopped
     * at the producer.
     *
     * Sent as a scaled integer rather than a float because 0.1% resolution
     * is far finer than the measurement is stable, and it keeps the payload
     * two bytes instead of four.
     */
    uint16_t cover_permille = 0;
    {
        uint64_t transformed = 0, skipped = 0;
        waterfall_mode_get_coverage(&transformed, &skipped);
        uint64_t total = transformed + skipped;
        if (total > 0)
            cover_permille = (uint16_t)((transformed * 1000) / total);
    }
    memcpy(sp + off, &cover_permille, 2); off += 2;
    /*
     * PSD workers actually running (0 while the producer is stopped).  The
     * fs_max clamp is applied on enter(), so this can differ from both the
     * user's request and the target for the next start.
     */
    sp[off++] = (uint8_t)waterfall_mode_worker_count();
    /*
     * Active producer's radio policy.  Fixed values are non-zero; zero
     * means the key is a range.  A non-independent, unfixed rate is derived
     * from bw, so clients must not replay it as a user input.  A non-zero
     * agc_fixed means the mode owns the gain/AGC mode.
     */
    const mode_radio_req_t *r = g_active_producer ? &g_active_producer->radio : NULL;
    uint32_t bw_min = r ? r->bw_min : 0, bw_max = r ? r->bw_max : 0;
    uint32_t bw_fixed = r ? r->bw_fixed : 0;
    uint32_t sr_min = r ? r->sr_min : 0, sr_max = r ? r->sr_max : 0;
    uint32_t sr_fixed = r ? r->sr_fixed : 0;
    uint8_t sr_indep = r ? (uint8_t)r->sr_independent : 0;
    uint8_t agc_fixed = r ? r->agc_fixed : 0;
    memcpy(sp + off, &bw_min, 4); off += 4;
    memcpy(sp + off, &bw_max, 4); off += 4;
    memcpy(sp + off, &bw_fixed, 4); off += 4;
    memcpy(sp + off, &sr_min, 4); off += 4;
    memcpy(sp + off, &sr_max, 4); off += 4;
    memcpy(sp + off, &sr_fixed, 4); off += 4;
    sp[off++] = sr_indep;
    sp[off++] = agc_fixed;

    /* Lifetime ADC rail counts: components at +2047 or -2048, and total
     * components seen.  Clients difference them between status samples. */
    uint64_t clip_count = 0, sample_count = 0;
    adc_clip_get(&clip_count, &sample_count);
    memcpy(sp + off, &clip_count, 8); off += 8;
    memcpy(sp + off, &sample_count, 8); off += 8;

    /* PSD accumulation mode (0=none, 1=max, 2=avg), then whether the
     * configured FPGA producer is actually running (fpga_active above is
     * configuration only). */
    sp[off++] = (uint8_t)g_accum_mode;
    sp[off++] = (uint8_t)(g_active_producer && g_active_producer->requires_fpga
                          && running);
    return off;
}

/* RESP_THREAD_STATS is by far the largest response, and it is built on the
 * stack.  Assert rather than trust: silently overflowing this would be a
 * stack smash, not a truncated frame. */
#define RESP_BUF_BYTES 1024
_Static_assert(THREAD_STATS_MAX_BYTES + FRAME_HEADER_SIZE <= RESP_BUF_BYTES,
               "resp buffer too small for RESP_THREAD_STATS");

/* --- Command handlers (by category) ---
 *
 * Each returns the response frame length written into `resp`, or 0 for no
 * response (which should never happen — every command gets an answer).
 */

/* --- Radio configuration --- */

static size_t cmd_radio_config(client_t *c, uint8_t *resp, uint8_t type,
                               const uint8_t *payload, uint32_t len)
{
    (void)c;
    switch (type) {
    case CMD_SET_FREQ:
        if (len < 8) return proto_build_error(resp, type, "need 8 bytes");
        {
            uint64_t freq, actual = 0;
            memcpy(&freq, payload, 8);
            if (freq < RADIO_FREQ_MIN_HZ || freq > RADIO_FREQ_MAX_HZ)
                return proto_build_error(resp, type,
                    "freq outside 70-6000 MHz");

            /* The fabric holds STF off for the whole transaction so the
             * poll thread cannot read a frame across the LO change. */
            bool fabric = producer_is_fabric_running(g_active_producer);
            if (fabric && deimos_rx_hold() != 0)
                return proto_build_error(resp, type, "fabric hold failed");

            int rc = radio_set_freq(&g_radio, freq, &actual);

            if (fabric) {
                if (rc == 0)
                    deimos_rx_note_channel(
                        wifi_fpga_freq_to_channel(actual));
                if (deimos_rx_release() != 0 && rc == 0)
                    rc = -1;
            }
            if (rc != 0)
                return proto_build_error(resp, type, "freq rejected");
            return proto_build_ok_u64(resp, type, actual);
        }

    case CMD_SET_GAIN:
        if (len < 4) return proto_build_error(resp, type, "need 4 bytes");
        {
            const mode_radio_req_t *req =
                producer_active_radio(g_active_producer);
            if (req && req->agc_fixed)
                return proto_build_error(resp, type,
                    "gain locked by wifi_fpga (agc fast_attack)");
            float gain;
            memcpy(&gain, payload, 4);
            if (!isfinite(gain))
                return proto_build_error(resp, type, "gain must be finite");
            if (g_radio.agc_mode != 0)
                return proto_build_error(resp, type,
                    "gain requires manual agc (current agc is active)");
            /* The fabric policy fixes AGC to fast_attack, so this path is
             * unreachable while the fabric runs. */
            int rc = radio_set_gain(&g_radio, gain);
            if (rc != 0)
                return proto_build_error(resp, type, "gain rejected");
        }
        return proto_build_ok(resp, type);

    case CMD_SET_BW:
        if (len < 4) return proto_build_error(resp, type, "need 4 bytes");
        {
            uint32_t bw_hz;
            memcpy(&bw_hz, payload, 4);

            const mode_radio_req_t *req = &g_active_producer->radio;
            /* A TX owns the shared clock: reject a bandwidth whose derived
             * rate would move it.  Independent-rate modes keep their current
             * rate, so their bandwidth remains settable during a TX. */
            if (tx_session_active(&g_tx_session)
                && mode_radio_bw_sr(req, g_radio.sample_rate_hz, bw_hz)
                       != g_radio.sample_rate_hz)
                return proto_build_error(resp, type, "locked by active tx");

            if (tx_holds_clock())
                return proto_build_error(resp, type, "locked by active tx");

            if (!mode_radio_bw_valid(req, bw_hz)) {
                char msg[96];
                if (req->bw_fixed)
                    snprintf(msg, sizeof(msg),
                             "bw locked at %u by %s", req->bw_fixed,
                             g_active_producer->name);
                else
                    snprintf(msg, sizeof(msg),
                             "bw %u outside range [%u, %u]",
                             bw_hz, req->bw_min, req->bw_max);
                return proto_build_error(resp, type, msg);
            }

            /* Settle the rate the mode wants after a bw change.  For
             * independent modes this is a no-op: bw is just the analog
             * filter, so the producer keeps running.  Derived modes cycle
             * so no worker sees the DMA restart. */
            radio_txn_req_t txn = {
                .kind = RADIO_TXN_BW,
                .prev = g_active_producer,
                .next = g_active_producer,
                .prev_running = producer_is_active(g_active_producer),
                .value = bw_hz,
                .cur_bw = g_radio.bw_hz,
                .cur_sr = g_radio.sample_rate_hz,
            };
            radio_txn_result_t res;
            radio_txn_status_t st = radio_txn_run(&txn, &g_radio_txn_io,
                                                  NULL, &res);
            if (st == RADIO_TXN_BW_FAILED)
                return proto_build_error(resp, type, "bw rejected by hardware");
            if (st == RADIO_TXN_SR_FAILED)
                return proto_build_error(resp, type, "sample_rate rejected");
            if (st != RADIO_TXN_OK)
                return proto_build_error(resp, type, "bw change failed");
            return proto_build_ok_u32_pair(resp, type, res.bw, res.sr);
        }

    case CMD_SET_AGC:
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        {
            if (payload[0] > 2)
                return proto_build_error(resp, type, "agc must be 0-2");
            const mode_radio_req_t *req =
                producer_active_radio(g_active_producer);
            if (req && !mode_radio_agc_valid(req, payload[0])) {
                char msg[96];
                snprintf(msg, sizeof(msg),
                         "agc locked at fast_attack by %s",
                         g_active_producer->name);
                return proto_build_error(resp, type, msg);
            }
            if (producer_is_fabric_running(g_active_producer)) {
                /* The fabric policy fixes AGC to fast_attack.  A redundant
                 * request is already satisfied; the policy check above
                 * rejects every other mode. */
                if (payload[0] == g_radio.agc_mode)
                    return proto_build_ok(resp, type);
                return proto_build_error(resp, type, "agc rejected");
            }
            int rc = radio_set_agc(&g_radio, payload[0]);
            if (rc != 0)
                return proto_build_error(resp, type, "agc rejected");
        }
        return proto_build_ok(resp, type);

    case CMD_SET_SAMPLE_RATE:
        if (len < 4) return proto_build_error(resp, type, "need 4 bytes");
        {
            uint32_t rate;
            memcpy(&rate, payload, 4);

            /* Any active TX shares the sample clock, even one that does not
             * displace the RX producer.  Changing it would tear the clock out
             * from under the TX thread. */
            if (tx_session_active(&g_tx_session)
                && rate != g_radio.sample_rate_hz)
                return proto_build_error(resp, type, "locked by active tx");

            if (tx_holds_clock())
                return proto_build_error(resp, type, "locked by active tx");

            const mode_radio_req_t *req = &g_active_producer->radio;
            if (!mode_radio_sr_valid(req, g_radio.bw_hz, rate)) {
                char msg[96];
                if (req->sr_independent)
                    snprintf(msg, sizeof(msg),
                             "rate %u outside [%u, %u] for %s",
                             rate, req->sr_min, req->sr_max,
                             g_active_producer->name);
                else
                    snprintf(msg, sizeof(msg),
                             "rate %u invalid for %s (bw=%u requires sr=%u)",
                             rate, g_active_producer->name, g_radio.bw_hz,
                             mode_radio_derive_sr(req, g_radio.bw_hz));
                return proto_build_error(resp, type, msg);
            }

            /* Cycle the producer around the change: the worker count is
             * clamped against fs_max on enter(), and dma_rx_stop()/start()
             * inside the rate setter must not run under a live worker. */
            radio_txn_req_t txn = {
                .kind = RADIO_TXN_SR,
                .prev = g_active_producer,
                .next = g_active_producer,
                .prev_running = producer_is_active(g_active_producer),
                .value = rate,
                .cur_bw = g_radio.bw_hz,
                .cur_sr = g_radio.sample_rate_hz,
            };
            radio_txn_result_t res;
            radio_txn_status_t st = radio_txn_run(&txn, &g_radio_txn_io,
                                                  NULL, &res);
            if (st == RADIO_TXN_SR_FAILED)
                return proto_build_error(resp, type, "rate not supported");
            if (st != RADIO_TXN_OK)
                return proto_build_error(resp, type, "rate change failed");
            return proto_build_ok_u32(resp, type, res.sr);
        }

    case CMD_SET_IQ_BITS:
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        if (payload[0] != 8 && payload[0] != 12)
            return proto_build_error(resp, type, "bits must be 8 or 12");
        if (iq_tx_stream_active()
            && !iq_tx_duplex_fits(g_radio.sample_rate_hz, payload[0],
                                  iq_rx_subscriber_count(),
                                  iq_tx_stream_bits()))
            return proto_build_error(resp, type,
                "bits change exceeds IQ duplex budget");
        atomic_store(&g_iq_bits, payload[0]);
        return proto_build_ok(resp, type);

    case CMD_SET_TX_ATTEN:
        if (len < 4) return proto_build_error(resp, type, "need 4 bytes");
        {
            float atten;
            memcpy(&atten, payload, 4);
            if (!isfinite(atten))
                return proto_build_error(resp, type,
                                         "tx_atten must be finite");
            if (radio_set_tx_atten(&g_radio, atten) != 0)
                return proto_build_error(resp, type, "tx_atten rejected");
        }
        return proto_build_ok(resp, type);

    default:
        return 0;  /* not handled */
    }
}

/* --- Waterfall / display configuration --- */

/*
 * Is a waterfall producer thread currently reading g_waterfall?
 *
 * Every setter that mutates g_waterfall must stop the producer first,
 * because none of them are safe against a concurrent read: they realloc
 * buffers, swap the FFT plan, and rewrite the window coefficients in place.
 *
 * A TX that holds the sample clock has already suspended the producer, so
 * there is nothing to stop in that case — and restarting it would defeat
 * the suspension.  Hence the two conditions.
 */
static bool waterfall_producer_live(void)
{
    return g_mode == DAEMON_MODE_WATERFALL && !tx_holds_clock();
}

/*
 * Configuration changes that require the producer to be stopped are one
 * transaction: snapshot, stop, apply, re-enter, and roll back if re-entry
 * fails.  Without the rollback a transient thread failure leaves the
 * requested setting half-installed with no producer at all.
 */
typedef enum {
    WF_CONFIG_FFT,
    WF_CONFIG_WINDOW,
    WF_CONFIG_WORKERS,
} wf_config_kind_t;

typedef struct {
    size_t        fft_size;
    window_type_t window;
    int           workers;
    spsc_ring_t  *ring;     /* old PSD ring, kept until the change commits */
} wf_config_t;

typedef enum {
    WF_CONFIG_OK,
    WF_CONFIG_APPLY_FAILED,
    WF_CONFIG_ROLLED_BACK,
    WF_CONFIG_STOPPED,
} wf_config_result_t;

static wf_config_t wf_config_snapshot(void)
{
    wf_config_t cfg = {
        .fft_size = g_waterfall
            ? waterfall_get_fft_size(g_waterfall) : WATERFALL_DEFAULT_FFT_SIZE,
        .window = g_waterfall
            ? waterfall_get_window(g_waterfall) : WINDOW_HANN,
        .workers = waterfall_mode_worker_requested(),
        .ring = g_ringbuf,
    };
    return cfg;
}

/*
 * Apply one change with the producer stopped.  The FFT case allocates the
 * replacement ring before touching anything the old state still owns, and
 * only resets the shared claim after the new frame length is installed.
 *
 * Success leaves the old ring in `old->ring` and the new ring live; the
 * caller destroys the old ring once re-entry has succeeded.  Failure leaves
 * the old configuration completely intact.
 */
static int wf_config_apply(wf_config_kind_t kind, uint32_t value,
                           wf_config_t *old)
{
    switch (kind) {
    case WF_CONFIG_FFT: {
        if (!g_waterfall) return -1;
        spsc_ring_t *new_ring = spsc_ring_create(psd_slot_size(value),
                                                 RING_SLOTS_PSD);
        if (!new_ring)
            return -1;
        if (waterfall_set_fft_size(g_waterfall, value) != 0) {
            spsc_ring_destroy(new_ring);
            return -1;
        }
        old->ring = g_ringbuf;
        g_ringbuf = new_ring;
        wf_claim_reset(&g_wf_claim, value);
        return 0;
    }

    case WF_CONFIG_WINDOW:
        if (!g_waterfall) return -1;
        return waterfall_set_window(g_waterfall, (window_type_t)value);

    case WF_CONFIG_WORKERS:
        waterfall_mode_set_worker_count((int)value);
        return 0;
    }
    return -1;
}

/*
 * Restore a snapshot after a failed re-entry.  For an FFT change the
 * buffers, ring, and claim are restored together; a failed restore reports
 * false so the caller does not claim a rollback that did not happen.
 */
static bool wf_config_restore(wf_config_kind_t kind, wf_config_t *old)
{
    switch (kind) {
    case WF_CONFIG_FFT: {
        if (!g_waterfall) return false;
        spsc_ring_t *failed_ring = g_ringbuf;
        if (waterfall_set_fft_size(g_waterfall, old->fft_size) != 0) {
            /* The applied state is still intact; the old ring is now
             * unreachable, so free it rather than leak it. */
            spsc_ring_destroy(old->ring);
            old->ring = NULL;
            return false;
        }
        g_ringbuf = old->ring;
        old->ring = NULL;
        wf_claim_reset(&g_wf_claim, old->fft_size);
        spsc_ring_destroy(failed_ring);
        return true;
    }

    case WF_CONFIG_WINDOW:
        if (!g_waterfall) return false;
        waterfall_set_window(g_waterfall, old->window);
        return true;

    case WF_CONFIG_WORKERS:
        waterfall_mode_set_worker_count(old->workers);
        return true;
    }
    return false;
}

static wf_config_result_t wf_config_change(wf_config_kind_t kind,
                                           uint32_t value)
{
    wf_config_t old = wf_config_snapshot();
    bool live = waterfall_producer_live() && g_active_producer != NULL;

    if (live)
        g_active_producer->exit();

    if (wf_config_apply(kind, value, &old) != 0) {
        /* The apply is atomic, so the snapshot is still the live state.
         * Restart it when this was a live cycle. */
        if (live && g_active_producer->enter() != 0)
            return WF_CONFIG_STOPPED;
        return WF_CONFIG_APPLY_FAILED;
    }

    /* A TX that owns the clock has already suspended the producer.  Apply
     * without entering: starting it here would defeat the suspension. */
    if (!live) {
        if (kind == WF_CONFIG_FFT)
            spsc_ring_destroy(old.ring);
        return WF_CONFIG_OK;
    }

    if (g_active_producer->enter() == 0) {
        if (kind == WF_CONFIG_FFT)
            spsc_ring_destroy(old.ring);
        return WF_CONFIG_OK;
    }

    if (wf_config_restore(kind, &old) && g_active_producer->enter() == 0)
        return WF_CONFIG_ROLLED_BACK;
    return WF_CONFIG_STOPPED;
}

static size_t wf_config_response(uint8_t *resp, uint8_t type,
                                 const char *what,
                                 wf_config_result_t result)
{
    char msg[96];

    switch (result) {
    case WF_CONFIG_OK:
        return proto_build_ok(resp, type);
    case WF_CONFIG_APPLY_FAILED:
        snprintf(msg, sizeof(msg), "%s change failed", what);
        return proto_build_error(resp, type, msg);
    case WF_CONFIG_ROLLED_BACK:
        snprintf(msg, sizeof(msg), "%s change rolled back", what);
        return proto_build_error(resp, type, msg);
    case WF_CONFIG_STOPPED:
        snprintf(msg, sizeof(msg), "%s change failed; producer stopped", what);
        return proto_build_error(resp, type, msg);
    }
    return proto_build_error(resp, type, "waterfall config failed");
}

static size_t cmd_waterfall_config(client_t *c, uint8_t *resp, uint8_t type,
                                   const uint8_t *payload, uint32_t len)
{
    (void)c;
    switch (type) {
    case CMD_SET_FFT_SIZE:
        if (len < 4) return proto_build_error(resp, type, "need 4 bytes");
        {
            uint32_t fft_size;
            memcpy(&fft_size, payload, 4);
            if (fft_size < PSD_FFT_SIZE_MIN || fft_size > PSD_FFT_SIZE_MAX
                || (fft_size & (fft_size - 1)) != 0)
                return proto_build_error(resp, type, "invalid fft size");

            return wf_config_response(resp, type, "fft size",
                wf_config_change(WF_CONFIG_FFT, fft_size));
        }

    case CMD_SET_WINDOW:
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        {
            if (payload[0] > WINDOW_FLAT_TOP)
                return proto_build_error(resp, type, "invalid window (0-3)");

            return wf_config_response(resp, type, "window",
                wf_config_change(WF_CONFIG_WINDOW, payload[0]));
        }

    case CMD_SET_FPS:
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        g_target_fps = payload[0];
        if (g_target_fps < 1) g_target_fps = 1;
        if (g_target_fps > 100) g_target_fps = 100;
        return proto_build_ok(resp, type);

    case CMD_SET_ACCUM:
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        {
            uint8_t mode = payload[0];
            if (mode > 2)
                return proto_build_error(resp, type, "invalid mode (0-2)");
            g_accum_mode = (accum_mode_t)mode;
        }
        return proto_build_ok(resp, type);

    case CMD_SET_WORKERS:
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        if (payload[0] < 1 || payload[0] > WF_MAX_WORKERS)
            return proto_build_error(resp, type, "invalid worker count (1-2)");
        /* The count is the number of producer threads, so it cannot change
         * under a running producer: cycle it.  enter() re-applies the
         * fs_max clamp, so a request made below fs_max runs one worker
         * while still being remembered for a later rate change. */
        return wf_config_response(resp, type, "worker count",
            wf_config_change(WF_CONFIG_WORKERS, payload[0]));

    default:
        return 0;
    }
}

/* --- Mode / producer switching --- */

static size_t cmd_mode(client_t *c, uint8_t *resp, uint8_t type,
                       const uint8_t *payload, uint32_t len)
{
    (void)c;
    switch (type) {
    case CMD_SET_MODE: {
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        uint8_t m = payload[0];

        /* Validate before touching the TX or the producer.  An invalid mode
         * used to tear down an active TX first and reject the mode after,
         * leaving no producer and a cleartext carrier stopped for nothing. */
        const producer_ops_t *target = producer_for_mode((int)m);
        if (!target)
            return proto_build_error(resp, type, "invalid mode");

        /* Last mode change wins, TX or RX.  A TX holding the sample clock
         * is cancelled rather than the request being refused — otherwise
         * the mode change is a LOST WRITE: the mode switch would set
         * g_mode while the exclusivity state still held the pre-TX value,
         * and tx_resume_producer() would later restore that and silently
         * discard what the client asked for.
         *
         * Cancelling is the honest option: the client gets the mode it
         * asked for, and TX telemetry shows it stopped. */
        bool was_suspended = tx_holds_clock();
        bool tx_was_cancelled = false;
        if (was_suspended) {
            fprintf(stderr, "tx: cancelled by mode change to %u\n", m);
            fm_tx_stream_end();
            iq_tx_stream_end();
            tx_scheduler_stop();
            dma_tx_stop();
            fm_tx_stream_release();
            iq_tx_stream_release();
            /* The mode change replaces the suspension; do not resume the
             * old producer behind the new mode's back. */
            tx_session_cancel_clock(&g_tx_session);
            tx_was_cancelled = true;
        } else if (tx_session_active(&g_tx_session)) {
            fprintf(stderr, "tx: cancelled by mode change to %u (concurrent)\n", m);
            fm_tx_stream_end();
            iq_tx_stream_end();
            tx_scheduler_stop();
            dma_tx_stop();
            fm_tx_stream_release();
            iq_tx_stream_release();
            tx_was_cancelled = true;
        }

        if (tx_was_cancelled) {
            tx_session_complete(&g_tx_session);
            tx_flush_zero();
        }

        /* Redundant mode set: no-op when a producer for this mode is
         * already running — whichever variant.  Switching here is how a
         * reconnecting client silently tears down wifi_fpga in favour of
         * wifi_sw; variant changes belong to CMD_SET_FPGA.  Skipped on
         * forced re-entry (tx_was_cancelled): the producer named by
         * g_active_producer was torn down by the TX. */
        if (!tx_was_cancelled && producer_mode_active(g_active_producer, m)
            && producer_is_running(g_active_producer))
            return proto_build_ok_u32_pair(resp, type,
                                           g_radio.bw_hz,
                                           g_radio.sample_rate_hz);

        /* Force re-entry after a cancellation: g_active_producer still
         * names the suspended producer, but the TX tore it down.  A
         * suspended producer is not running; a concurrent TX cancellation
         * leaves it running, so the transaction must cycle it.  A producer
         * that failed to start is also not running, so a redundant mode set
         * retries it instead of returning a false success. */
        bool prev_running = g_active_producer != NULL && !was_suspended
                            && producer_is_active(g_active_producer);
        if (switch_producer(target, tx_was_cancelled, prev_running) == 0)
            return proto_build_ok_u32_pair(resp, type, g_radio.bw_hz, g_radio.sample_rate_hz);
        return proto_build_error(resp, type, "mode switch failed");
    }

    case CMD_SET_FPGA: {
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        uint8_t enable = payload[0];

        /* The FPGA variant forces a different bandwidth and can restart the
         * sample clock, so it must not run under an FM TX that owns it. */
        if (tx_holds_clock())
            return proto_build_error(resp, type, "locked by active tx");

        /* Entering or leaving the fabric reinitializes the radio and can
         * stop the RX producer.  Do not do that under any active TX. */
        if (tx_session_active(&g_tx_session))
            return proto_build_error(resp, type, "locked by active tx");

        bool prev_running = producer_is_active(g_active_producer);
        if (enable) {
            const producer_ops_t *fpga = producer_fpga_for_mode(g_mode);
            if (!fpga)
                return proto_build_error(resp, type, "no fpga variant for this mode");
            if (hal_reg_read(REG_BUILD_ID_PROJECT) != PROJECT_ID_WIFI)
                return proto_build_error(resp, type, "no compatible bitstream");
            if (g_active_producer == fpga && producer_is_running(fpga))
                return proto_build_ok(resp, type);
            if (switch_producer(fpga, false, prev_running) != 0)
                return proto_build_error(resp, type, "fpga start failed");
        } else {
            if (!g_active_producer->requires_fpga
                && producer_is_running(g_active_producer))
                return proto_build_ok(resp, type);
            const producer_ops_t *sw = producer_for_mode(g_mode);
            if (switch_producer(sw, false, prev_running) != 0)
                return proto_build_error(resp, type, "sw fallback failed");
        }
        return proto_build_ok(resp, type);
    }

    default:
        return 0;
    }
}

/* --- TX commands --- */

/*
 * Open-ended TX runs until CMD_TX_STOP (or forever): streaming, cyclic,
 * or repeat-forever.  Only these need an owner to clean them up when the
 * starting client disappears.
 */
static bool tx_is_open_ended(uint8_t flags, uint16_t repeat)
{
    return (flags & (TX_FLAG_CYCLIC | TX_FLAG_STREAM)) != 0
        || repeat == TX_REPEAT_FOREVER;
}

/*
 * When a TX stops, the drain FSM holds its last output word and the
 * AD9361 keeps converting it — a static carrier the receiver hears as
 * silence ("blank").  The fabric's DAC data registers are write-only
 * from software, so the only way to clear them is to play a short burst
 * of zeros through the same DMA: the last sample it emits is zero, and
 * that is what stays on the wire.
 */
#define TX_ZERO_FLUSH_SAMPLES 64

static void tx_flush_zero(void)
{
    static float zeros[TX_ZERO_FLUSH_SAMPLES];

    if (dma_tx_load(zeros, zeros, TX_ZERO_FLUSH_SAMPLES, false) != 0)
        return;
    if (dma_tx_trigger() != 0)
        return;
    for (int i = 0; i < 200 && !dma_tx_done(); i++)
        usleep(100);
}

/*
 * Reap a TX whose thread has already finished and release everything it
 * owned.  Shared by the blocking teardown and the poll-loop completion so
 * both stop the scheduler and the DMA in exactly the same order.
 */
static void finish_tx_teardown(void)
{
    tx_scheduler_join();
    dma_tx_stop();
    tx_flush_zero();
    fm_tx_stream_release();
    iq_tx_stream_release();
    tx_resume_producer();
    tx_session_complete(&g_tx_session);
}

/*
 * Begin a stop without waiting for the drain.  The TX thread finishes on its
 * own and the poll loop calls finish_tx_teardown() when it does.  The session
 * stays active until then, so status still reports tx_busy and a late
 * observer cannot mistake a draining transmitter for an idle one.
 */
static void request_tx_stop(void)
{
    fm_tx_stream_end();
    iq_tx_stream_end();
    /* Stop owning the session now, so a client disconnect cannot re-enter
     * this path while the drain finishes. */
    tx_session_stream_end(&g_tx_session);
    tx_scheduler_request_stop();
}

static size_t cmd_tx(client_t *c, uint8_t *resp, uint8_t type,
                     const uint8_t *payload, uint32_t len)
{
    switch (type) {
    case CMD_TX: {
        if (len < 8) return proto_build_error(resp, type, "need at least 8 bytes");
        uint8_t protocol = payload[0];
        uint8_t flags = payload[1];
        uint16_t repeat;
        uint32_t interval_us;
        memcpy(&repeat, payload + 2, 2);
        memcpy(&interval_us, payload + 4, 4);

        float *real = NULL, *imag = NULL;
        size_t n_samples = 0;
        int rc = -1;

        switch (protocol) {
        case TX_PROTO_WIFI: {
            if (flags & ~TX_FLAG_CYCLIC)
                return proto_build_error(resp, type,
                    "wifi: unsupported tx flags");
            /* The waveform is built at 20 MSPS.  Transmitting it at any
             * other active rate stretches it in time, so require the clock
             * to already be right instead of silently radiating garbage. */
            if (g_mode != DAEMON_MODE_WIFI ||
                g_radio.sample_rate_hz != 20000000)
                return proto_build_error(resp, type,
                    "wifi tx requires wifi mode at 20 MSPS");
            if (len < 11)
                return proto_build_error(resp, type, "wifi: need rate+psdu_len+psdu");
            int rate = payload[8];
            uint16_t psdu_n;
            memcpy(&psdu_n, payload + 9, 2);
            if (psdu_n == 0 || (uint32_t)(11 + psdu_n) > len)
                return proto_build_error(resp, type, "wifi: invalid psdu length");
            if (rate < 6 || rate > 54)
                return proto_build_error(resp, type, "wifi: invalid rate");
            rc = wifi_tx_build(rate, payload + 11, psdu_n,
                              &real, &imag, &n_samples);
            break;
        }
        case TX_PROTO_FM: {
            if (flags & ~TX_FLAG_STREAM)
                return proto_build_error(resp, type,
                    "fm: unsupported tx flags");
            if (repeat != 1)
                return proto_build_error(resp, type, "fm: repeat must be 1");
            if (interval_us != 0)
                return proto_build_error(resp, type, "fm: interval must be 0");
            if (len < 24 + PROTO_RDS_TX_SIZE)
                return proto_build_error(resp, type,
                    "fm: need header+rds block");
            uint32_t audio_rate, deviation_hz, dac_rate, n_audio;
            memcpy(&audio_rate,   payload + 8,  4);
            memcpy(&deviation_hz, payload + 12, 4);
            memcpy(&dac_rate,     payload + 16, 4);
            memcpy(&n_audio,      payload + 20, 4);

            if (audio_rate == 0 || dac_rate == 0)
                return proto_build_error(resp, type, "fm: invalid rate");
            if (!fm_mod_deviation_valid(deviation_hz, dac_rate))
                return proto_build_error(resp, type,
                    "fm: deviation must be below half the sample rate");
            if (n_audio > (len - 24u - PROTO_RDS_TX_SIZE) / 4u)
                return proto_build_error(resp, type, "fm: payload truncated");

            rds_tx_config_t rds;
            proto_unpack_rds_tx(payload + 24, &rds);

            const float *audio_data = NULL;
            if (n_audio > 0)
                audio_data = (const float *)(payload + 24
                                             + PROTO_RDS_TX_SIZE);

            /* Claim the core before touching the radio: the rate change
             * below stops and restarts RX DMA, which corrupts a producer
             * that is still holding a read pointer into the old stream. */
            if (tx_session_rate_needs_suspend(dac_rate,
                                              g_radio.sample_rate_hz))
                tx_suspend_producer();

            if (dac_rate != g_radio.sample_rate_hz) {
                if (radio_set_sample_rate(&g_radio, dac_rate, &(uint32_t){0}) != 0) {
                    tx_resume_producer();
                    return proto_build_error(resp, type, "fm: unsupported dac rate");
                }
            }

            if (flags & TX_FLAG_STREAM) {
                rc = fm_tx_stream_start(audio_data, n_audio,
                                        audio_rate, deviation_hz,
                                        dac_rate, &rds);
            } else {
                rc = fm_tx_start(audio_data, audio_rate, deviation_hz,
                                dac_rate, n_audio, &rds);
            }
            if (rc == 0) {
                tx_session_start(&g_tx_session, protocol, flags, repeat,
                                 interval_us, c->id,
                                 tx_is_open_ended(flags, repeat));
                return proto_build_ok(resp, type);
            }
            tx_resume_producer();
            return proto_build_error(resp, type, "fm: start failed");
        }
        default:
            return proto_build_error(resp, type, "unknown tx protocol");
        }

        if (rc != 0)
            return proto_build_error(resp, type, "tx build failed");

        rc = tx_scheduler_start(real, imag, n_samples, flags, repeat, interval_us);
        /* The scheduler copied both arrays before returning, so ownership
         * stays here whether it started or not. */
        free(real);
        free(imag);
        if (rc == 0) {
            tx_session_start(&g_tx_session, protocol, flags, repeat,
                             interval_us, c->id,
                             tx_is_open_ended(flags, repeat));
            return proto_build_ok(resp, type);
        }
        return proto_build_error(resp, type, "tx schedule failed");
    }

    case CMD_TX_STOP: {
        if (len < 1) return proto_build_error(resp, type, "need target byte");
        uint8_t target = payload[0];
        if (target == TX_STOP_ALL ||
            (tx_session_active(&g_tx_session) &&
             target == g_tx_session.proto))
            request_tx_stop();
        return proto_build_ok(resp, type);
    }

    case CMD_SET_RDS: {
        if (len < PROTO_RDS_TX_SIZE)
            return proto_build_error(resp, type, "rds: need 76 bytes");
        rds_tx_config_t rds;
        proto_unpack_rds_tx(payload, &rds);
        if (fm_tx_set_rds(&rds) != 0)
            return proto_build_error(resp, type, "rds: no active fm stream");
        return proto_build_ok(resp, type);
    }

    case CMD_TX_STREAM_DATA: {
        if (len < 4) return proto_build_error(resp, type, "need 4 bytes");
        uint32_t n;
        memcpy(&n, payload, 4);
        if (n > (len - 4u) / 4u)
            return proto_build_error(resp, type, "payload truncated");
        const float *audio = (const float *)(payload + 4);
        switch (fm_tx_stream_push(audio, n)) {
        case FM_TX_PUSH_OK:
            return proto_build_ok(resp, type);
        case FM_TX_PUSH_FULL:
            return proto_build_frame(resp, RESP_RETRY, &type, 1);
        default:
            return proto_build_error(resp, type, "no active fm stream");
        }
    }

    case CMD_IQ_TX_STREAM_DATA: {
        if (len < 4) return proto_build_error(resp, type, "need 4 bytes");
        uint32_t n;
        memcpy(&n, payload, 4);
        if (n > len - 4u)
            return proto_build_error(resp, type, "payload truncated");
        const uint8_t *data = payload + 4;

        /* Implicit start: the first DATA creates the stream at the mode's
         * shared clock.  No START command exists because, unlike FM, there
         * are no modulator parameters. */
        if (!iq_tx_stream_active()) {
            if (g_mode != DAEMON_MODE_IQ)
                return proto_build_error(resp, type,
                                         "iq tx requires iq mode");
            uint8_t bits = (uint8_t)atomic_load(&g_iq_bits);
            unsigned rx = iq_rx_subscriber_count();
            if (!iq_tx_duplex_fits(g_radio.sample_rate_hz, bits, rx, bits)) {
                uint64_t per = iq_bits_bytes(bits) * (rx + 1);
                uint32_t max_rate = (uint32_t)(IQ_DUPLEX_BUDGET_BPS / per);
                char msg[128];
                snprintf(msg, sizeof(msg),
                         "IQ tx at %u Hz with %u rx stream(s) exceeds duplex "
                         "budget; max %u Hz",
                         g_radio.sample_rate_hz, rx, max_rate);
                return proto_build_error(resp, type, msg);
            }
            if (iq_tx_stream_start(bits, g_radio.sample_rate_hz) != 0)
                return proto_build_error(resp, type, "iq stream start failed");
            tx_session_start(&g_tx_session, TX_PROTO_IQ, 0, 0, 0,
                             c->id, true);
        }

        switch (iq_tx_stream_push(data, n)) {
        case IQ_TX_PUSH_OK:
            return proto_build_ok(resp, type);
        case IQ_TX_PUSH_FULL:
            return proto_build_frame(resp, RESP_RETRY, &type, 1);
        default:
            return proto_build_error(resp, type, "iq push rejected");
        }
    }

    case CMD_IQ_TX_STREAM_END:
        if (g_tx_session.proto == TX_PROTO_IQ) {
            iq_tx_stream_end();
            /* Drain without an owner: the client may disconnect now. */
            tx_session_stream_end(&g_tx_session);
        }
        return proto_build_ok(resp, type);

    case CMD_TX_STREAM_END:
        if (g_tx_session.proto == TX_PROTO_FM) {
            fm_tx_stream_end();
            tx_session_stream_end(&g_tx_session);
        }
        return proto_build_ok(resp, type);

    case CMD_TX_STREAM_FLUSH:
        if (fm_tx_stream_flush() != 0)
            return proto_build_error(resp, type, "no active fm stream");
        return proto_build_ok(resp, type);

    default:
        return 0;
    }
}

/* --- Query / diagnostics --- */

static size_t cmd_query(client_t *c, uint8_t *resp, uint8_t type,
                        const uint8_t *payload, uint32_t len)
{
    switch (type) {
    case CMD_STATUS: {
        uint8_t sp[STATUS_PAYLOAD_BYTES];
        size_t n = build_status_payload(sp);
        return proto_build_frame(resp, RESP_STATUS, sp, (uint32_t)n);
    }

    case CMD_CAPS: {
        uint8_t cp[24];
        uint32_t fpga_id = radio_get_fpga_id();
        uint32_t features = 0x03 | (1u << 3);  /* bit3=iq */
        if (hal_reg_read(REG_BUILD_ID_PROJECT) == PROJECT_ID_WIFI)
            features |= (1 << 2);  /* bit2=fabric_wifi */

        uint32_t max_rate = 61440000;
        uint32_t max_fft = 8192;
        uint16_t min_freq = (uint16_t)(RADIO_FREQ_MIN_HZ / 1000000ULL);
        uint16_t max_freq = (uint16_t)(RADIO_FREQ_MAX_HZ / 1000000ULL);
        uint32_t fw_build_id = SHESHA_FW_BUILD_ID;
        memcpy(cp + 0, &fpga_id, 4);
        memcpy(cp + 4, &features, 4);
        memcpy(cp + 8, &max_rate, 4);
        memcpy(cp + 12, &max_fft, 4);
        memcpy(cp + 16, &min_freq, 2);
        memcpy(cp + 18, &max_freq, 2);
        memcpy(cp + 20, &fw_build_id, 4);
        return proto_build_frame(resp, RESP_CAPS, cp, 24);
    }

    case CMD_TX_STATS: {
        tx_stream_stats_t ts;
        fm_tx_stream_stats_t fs;
        iq_tx_stream_stats_t is;
        tx_scheduler_stream_stats(&ts);
        fm_tx_stream_stats(&fs);
        iq_tx_stream_stats(&is);

        uint8_t sp[96];
        size_t off = 0;
        memcpy(sp + off, &ts.fed, 8);              off += 8;
        memcpy(sp + off, &ts.emitted, 8);          off += 8;
        memcpy(sp + off, &ts.elapsed_us, 8);       off += 8;
        memcpy(sp + off, &fs.audio_consumed, 8);   off += 8;
        memcpy(sp + off, &fs.starved_samples, 8);  off += 8;
        memcpy(sp + off, &ts.dac_rate, 4);         off += 4;
        memcpy(sp + off, &ts.depth, 4);            off += 4;
        memcpy(sp + off, &ts.max_depth, 4);        off += 4;
        memcpy(sp + off, &ts.high_water, 4);       off += 4;
        memcpy(sp + off, &ts.underruns, 4);        off += 4;
        memcpy(sp + off, &fs.ring_fill, 4);        off += 4;
        memcpy(sp + off, &fs.ring_size, 4);        off += 4;
        memcpy(sp + off, &fs.audio_rate, 4);       off += 4;
        memcpy(sp + off, &fs.push_rejects, 4);     off += 4;
        sp[off++] = (uint8_t)tx_session_active(&g_tx_session);
        sp[off++] = (uint8_t)fs.active;
        sp[off++] = (uint8_t)fs.end_of_stream;
        sp[off++] = 0;
        memcpy(sp + off, &is.ring_fill, 4);        off += 4;
        memcpy(sp + off, &is.push_rejects, 4);     off += 4;
        memcpy(sp + off, &is.starved_chunks, 4);   off += 4;
        sp[off++] = (uint8_t)is.active;
        sp[off++] = (uint8_t)tx_scheduler_start_failed();
        sp[off++] = 0;
        sp[off++] = 0;
        return proto_build_frame(resp, RESP_TX_STATS, sp, (uint32_t)off);
    }

    case CMD_THREAD_STATS: {
        thread_stats_t st;
        if (thread_stats_sample(&st) != 0)
            return proto_build_error(resp, type, "procfs read failed");
        uint8_t sp[THREAD_STATS_MAX_BYTES];
        size_t n = thread_stats_serialize(sp, &st);
        return proto_build_frame(resp, RESP_THREAD_STATS, sp, (uint32_t)n);
    }

    case CMD_WIFI_DIAG: {
        uint8_t dp[24];
        size_t off = 0;
        if (producer_is_fabric_running(g_active_producer)) {
            uint32_t delivered = 0, fcs_good = 0;
            wifi_fpga_get_diag(&delivered, &fcs_good);
            uint32_t captures = deimos_rx_frame_count();
            uint32_t fcs_bad = delivered > fcs_good ? delivered - fcs_good : 0;
            float zero = 0.0f;
            memcpy(dp + off, &captures, 4); off += 4;
            memcpy(dp + off, &delivered, 4); off += 4;
            memcpy(dp + off, &fcs_good, 4); off += 4;
            memcpy(dp + off, &fcs_bad, 4); off += 4;
            memcpy(dp + off, &zero, 4); off += 4;
            memcpy(dp + off, &zero, 4); off += 4;
        } else {
            wifi_rx_diag_t d;
            wifi_rx_get_diag(&d);
            memcpy(dp + off, &d.captures, 4); off += 4;
            memcpy(dp + off, &d.decode_ok, 4); off += 4;
            memcpy(dp + off, &d.fcs_ok, 4); off += 4;
            memcpy(dp + off, &d.fcs_bad, 4); off += 4;
            memcpy(dp + off, &d.iq_rms, 4); off += 4;
            memcpy(dp + off, &d.iq_peak, 4); off += 4;
        }
        return proto_build_frame(resp, RESP_WIFI_DIAG, dp, (uint32_t)off);
    }

    case CMD_SUBSCRIBE:
        if (len < 1) return proto_build_error(resp, type, "need 1 byte");
        {
            uint32_t mask = (uint32_t)payload[0];
            bool wants_iq = (mask & SESSION_DATA_IQ) != 0;
            if (wants_iq && iq_tx_stream_active()) {
                unsigned rx = iq_rx_subscriber_count();
                if (!session_client_wants(&c->session, DATA_IQ))
                    rx++;
                if (!iq_tx_duplex_fits(g_radio.sample_rate_hz,
                                       (uint8_t)atomic_load(&g_iq_bits),
                                       rx, iq_tx_stream_bits()))
                    return proto_build_error(resp, type,
                        "IQ tx is active at this rate; lower it to receive");
            }
            session_client_subscribe(&c->session, mask);
        }
        return proto_build_ok(resp, type);

    default:
        return 0;
    }
}

/* --- Top-level command dispatch --- */

typedef size_t (*cmd_handler_t)(client_t *c, uint8_t *resp, uint8_t type,
                                const uint8_t *payload, uint32_t len);

typedef struct {
    uint8_t       type;
    cmd_handler_t handler;
    bool          mutates;
} cmd_entry_t;

/* The single source of command -> handler and mutation policy.  `mutates`
 * says a successful command changed daemon state and every observer should
 * receive an unsolicited RESP_STATUS.  Queries and ordinary stream feeds do
 * not; an IQ feed that starts a session is detected in handle_command. */
static const cmd_entry_t g_commands[] = {
    { CMD_SET_FREQ,         cmd_radio_config,     true  },
    { CMD_SET_GAIN,         cmd_radio_config,     true  },
    { CMD_SET_BW,           cmd_radio_config,     true  },
    { CMD_SET_FFT_SIZE,     cmd_waterfall_config, true  },
    { CMD_SET_WINDOW,       cmd_waterfall_config, true  },
    { CMD_SET_FPS,          cmd_waterfall_config, true  },
    { CMD_SET_AGC,          cmd_radio_config,     true  },
    { CMD_SET_SAMPLE_RATE,  cmd_radio_config,     true  },
    { CMD_SET_ACCUM,        cmd_waterfall_config, true  },
    { CMD_SET_MODE,         cmd_mode,             true  },
    { CMD_SET_TX_ATTEN,     cmd_radio_config,     true  },
    { CMD_WIFI_DIAG,        cmd_query,            false },
    { CMD_TX,               cmd_tx,               true  },
    { CMD_TX_STOP,          cmd_tx,               true  },
    { CMD_SET_FPGA,         cmd_mode,             true  },
    { CMD_TX_STREAM_DATA,   cmd_tx,               false },
    { CMD_TX_STREAM_END,    cmd_tx,               false },
    { CMD_TX_STATS,         cmd_query,            false },
    { CMD_SET_IQ_BITS,      cmd_radio_config,     true  },
    { CMD_IQ_TX_STREAM_DATA, cmd_tx,              false },
    { CMD_IQ_TX_STREAM_END, cmd_tx,               false },
    { CMD_SET_WORKERS,      cmd_waterfall_config, true  },
    { CMD_SET_RDS,          cmd_tx,               true  },
    { CMD_TX_STREAM_FLUSH,  cmd_tx,               false },
    { CMD_STATUS,           cmd_query,            false },
    { CMD_CAPS,             cmd_query,            false },
    { CMD_THREAD_STATS,     cmd_query,            false },
    { CMD_SUBSCRIBE,        cmd_query,            false },
};

static const cmd_entry_t *find_command(uint8_t type)
{
    size_t n = sizeof(g_commands) / sizeof(g_commands[0]);
    for (size_t i = 0; i < n; i++) {
        if (g_commands[i].type == type)
            return &g_commands[i];
    }
    return NULL;
}

/* Push the post-command truth to every observer, including the issuer.  The
 * issuer must not reconstruct state from its own intent: a stale bw or a
 * silently torn-down fpga variant is otherwise displayed forever. */
static void notify_all_clients(void)
{
    for (int i = 0; i < g_num_clients; i++)
        g_clients[i].session.notify_pending = true;
}

/* Returns false when the response could not be queued and the caller must
 * close the client.  A command response is never dropped silently. */
static bool handle_command(client_t *c, uint8_t type,
                           const uint8_t *payload, uint32_t len)
{
    uint8_t resp[RESP_BUF_BYTES];
    const cmd_entry_t *cmd = find_command(type);
    bool was_tx_active = tx_session_active(&g_tx_session);
    size_t resp_len;

    if (cmd)
        resp_len = cmd->handler(c, resp, type, payload, len);
    else
        resp_len = proto_build_error(resp, type, "unknown command");

    if (resp_len == 0)
        resp_len = proto_build_error(resp, type,
                                     "command produced no response");

    /* The first IQ feed starts a TX session even though CMD_IQ_TX_STREAM_DATA
     * is not itself a mutating command.  Notify on the idle -> active edge
     * only, so the remaining 4 KiB feeds stay silent.  A RETRY can still
     * carry that edge: the stream started even though this chunk did not
     * fit. */
    bool tx_edge = !was_tx_active && tx_session_active(&g_tx_session);
    bool mutated = cmd && (cmd->mutates ? resp[0] == RESP_OK : tx_edge);

    if (!sendq_push_control(c->sendq, resp, resp_len)) {
        if (mutated)
            notify_all_clients();
        fprintf(stderr, "client %u: control queue full, closing\n", c->id);
        return false;
    }

    if (mutated)
        notify_all_clients();

    return true;
}

/* --- Networking --- */
static int setup_server(void)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { perror("socket"); return -1; }
    int opt = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(g_port),
        .sin_addr.s_addr = INADDR_ANY,
    };
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind"); close(fd); return -1;
    }
    if (listen(fd, 2) < 0) {
        perror("listen"); close(fd); return -1;
    }
    return fd;
}

static void accept_client(int server_fd)
{
    int fd = accept(server_fd, NULL, NULL);
    if (fd < 0) return;
    if (g_num_clients >= MAX_CLIENTS) {
        close(fd);
        return;
    }
    int opt = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));

    /* Let the kernel hold more in flight.  The default autotuned window
     * is tuned for a LAN, not a 6 MB/s IQ stream over USB2, and a full
     * window is what stalls the userspace ring. */
    int sndbuf = SEND_SOCKBUF;
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

    /*
     * Non-blocking for both recv and send.  The outbound queues + POLLOUT
     * flush replaces the old blocking send_all() retry loop.
     */
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0)
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);

    sendq_t *sendq = sendq_create(CONTROL_RING_SIZE, SEND_RING_SIZE);
    if (!sendq) {
        close(fd);
        return;
    }

    client_t *c = &g_clients[g_num_clients];
    c->fd = fd;
    c->id = g_next_client_id++;
    c->recv_len = 0;
    c->ovf_buf = NULL;
    c->sendq = sendq;
    session_client_init(&c->session);
    g_num_clients++;
    printf("client connected (total: %d)\n", g_num_clients);
}

static void remove_client(int idx)
{
    close(g_clients[idx].fd);
    free(g_clients[idx].ovf_buf);
    sendq_destroy(g_clients[idx].sendq);
    g_clients[idx] = g_clients[g_num_clients - 1];
    g_num_clients--;
    printf("client disconnected (total: %d)\n", g_num_clients);
}

static void process_client_data(int idx)
{
    client_t *c = &g_clients[idx];

    if (c->ovf_buf) {
        ssize_t n = recv(c->fd, c->ovf_buf + c->ovf_offset,
                         c->ovf_total - c->ovf_offset, 0);
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            return;                       /* nothing more yet, keep waiting */
        if (n <= 0) {
            free(c->ovf_buf); c->ovf_buf = NULL; remove_client(idx); return;
        }
        c->ovf_offset += (size_t)n;
        if (c->ovf_offset < c->ovf_total) return;

        uint8_t type;
        uint32_t payload_len = proto_parse_header(c->ovf_buf, &type, NULL);
        size_t frame_size;
        if (!proto_frame_size(payload_len, &frame_size) ||
            frame_size != c->ovf_total) {
            fprintf(stderr, "client %u: reassembled header is invalid, "
                    "closing\n", c->id);
            remove_client(idx);
            return;
        }
        if (!handle_command(c, type, c->ovf_buf + FRAME_HEADER_SIZE,
                            payload_len)) {
            remove_client(idx);
            return;
        }
        free(c->ovf_buf);
        c->ovf_buf = NULL;
        return;
    }

    ssize_t n = recv(c->fd, c->recv_buf + c->recv_len,
                     RECV_BUF_SIZE - c->recv_len, 0);
    /* EAGAIN is "no data", not a closed peer.  Conflating them evicted
     * healthy clients, and because remove_client() compacts the array an
     * eviction can shift indices under the caller. */
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;
    if (n <= 0) { remove_client(idx); return; }
    c->recv_len += (size_t)n;

    while (c->recv_len >= FRAME_HEADER_SIZE) {
        uint8_t type;
        uint32_t payload_len = proto_parse_header(c->recv_buf, &type, NULL);
        size_t frame_size;

        if (!proto_frame_size(payload_len, &frame_size)) {
            fprintf(stderr, "client %u: oversized frame (%u bytes), "
                    "closing\n", c->id, payload_len);
            remove_client(idx);
            return;
        }

        if (frame_size > RECV_BUF_SIZE) {
            c->ovf_buf = malloc(frame_size);
            if (!c->ovf_buf) { remove_client(idx); return; }
            c->ovf_total = frame_size;
            memcpy(c->ovf_buf, c->recv_buf, c->recv_len);
            c->ovf_offset = c->recv_len;
            c->recv_len = 0;
            return;
        }

        if (c->recv_len < frame_size) break;

        if (!handle_command(c, type, c->recv_buf + FRAME_HEADER_SIZE,
                            payload_len)) {
            remove_client(idx);
            return;
        }

        c->recv_len -= frame_size;
        if (c->recv_len > 0)
            memmove(c->recv_buf, c->recv_buf + frame_size, c->recv_len);
    }
}

static void broadcast_frame(const uint8_t *frame, size_t len, void *user)
{
    (void)user;

    /* Data frames are gated by subscription.  The frame type is the first
     * byte of the TLV envelope. */
    uint8_t frame_type = frame[0];

    for (int i = g_num_clients - 1; i >= 0; i--) {
        if (!session_client_wants(&g_clients[i].session, frame_type))
            continue;
        /* Queue into the data ring.  If it is full the frame is silently
         * dropped — the client will see a gap via the sequence number in
         * the frame header. */
        sendq_push_data(g_clients[i].sendq, frame, len);
    }
}

/* Build and send an unsolicited RESP_STATUS to one client.  This is the
 * notification mechanism: when the session changes (mode, freq, etc.),
 * observers learn about it without polling.  Same format as CMD_STATUS
 * response — the client can parse it identically.  Returns whether the
 * frame was queued. */
static bool send_status_notification(client_t *c)
{
    uint8_t buf[STATUS_PAYLOAD_BYTES + FRAME_HEADER_SIZE];
    uint8_t sp[STATUS_PAYLOAD_BYTES];
    size_t n = build_status_payload(sp);
    size_t frame_len = proto_build_frame(buf, RESP_STATUS, sp, (uint32_t)n);
    return sendq_push_control(c->sendq, buf, frame_len);
}

/* --- Main drain dispatcher ---
 *
 * Each producer owns its drain; this loop supplies the shared scratch
 * buffer, the broadcast sink, the pacing target, and the accumulation mode.
 */

static void drain_and_broadcast(uint8_t *sendbuf, double *last_broadcast)
{
    if (!g_active_producer || !g_active_producer->drain) return;
    drain_ctx_t ctx = {
        .sendbuf        = sendbuf,
        .sink           = broadcast_frame,
        .sink_user      = NULL,
        .last_broadcast = last_broadcast,
        .target_fps     = g_target_fps,
        .accum          = (uint8_t)g_accum_mode,
    };
    g_active_producer->drain(&ctx);
}

/* --- Main --- */
int main(int argc, char *argv[])
{
    setbuf(stdout, NULL);
    signal(SIGINT, sighandler);
    signal(SIGTERM, sighandler);
    signal(SIGPIPE, SIG_IGN);

    if (argc > 1) {
        int p = atoi(argv[1]);
        if (p > 0 && p <= 65535)
            g_port = (uint16_t)p;
    }

    clock_gettime(CLOCK_MONOTONIC, &g_start_time);

    printf("shesha-daemon starting...\n");

    /* Require both Cortex-A9 cores.  FM TX + RX needs ~103% CPU, and the
     * daemon poll loop / PSD broadcast adds to that.  Run
     * firmware/platforms/styx/bin/enable_dual_core.sh once to persist. */
    {
        FILE *f = fopen("/sys/devices/system/cpu/online", "r");
        char buf[32] = {0};
        if (!f) {
            fprintf(stderr, "FATAL: cannot open "
                    "/sys/devices/system/cpu/online\n");
            return 1;
        }
        if (fgets(buf, sizeof(buf), f) == NULL) {
            fclose(f);
            fprintf(stderr, "FATAL: cannot read "
                    "/sys/devices/system/cpu/online\n");
            return 1;
        }
        fclose(f);
        if (buf[0] != '0' || buf[1] != '-' || buf[2] < '1') {
            fprintf(stderr, "FATAL: CPU1 is offline (online=%s).\n"
                    "  Run: firmware/platforms/styx/bin/enable_dual_core.sh\n",
                    buf);
            return 1;
        }
        printf("  cpus online: %s", buf);
    }

    if (radio_init(&g_radio) != 0) {
        fprintf(stderr, "radio init failed\n");
        return 1;
    }
    printf("  FPGA ID: 0x%08x\n", radio_get_fpga_id());
    printf("  freq: %llu Hz, bw: %u Hz, rate: %u Hz\n",
           (unsigned long long)g_radio.freq_hz, g_radio.bw_hz, g_radio.sample_rate_hz);

    wifi_tx_init();
    tx_scheduler_init();
    tx_session_init(&g_tx_session);

    wf_claim_init(&g_wf_claim, WATERFALL_DEFAULT_FFT_SIZE);
    g_waterfall = waterfall_create(WATERFALL_DEFAULT_FFT_SIZE, WINDOW_HANN, &g_wf_claim);
    if (!g_waterfall) {
        fprintf(stderr, "FATAL: waterfall allocation failed\n");
        return 1;
    }
    g_ringbuf = spsc_ring_create(psd_slot_size(WATERFALL_DEFAULT_FFT_SIZE), RING_SLOTS_PSD);
    if (!g_ringbuf) {
        fprintf(stderr, "FATAL: PSD ring allocation failed\n");
        return 1;
    }
    g_active_producer = producer_for_mode(DAEMON_MODE_WATERFALL);
    if (!g_active_producer) {
        fprintf(stderr, "FATAL: waterfall producer is not registered\n");
        return 1;
    }
    if (g_active_producer->enter() != 0) {
        fprintf(stderr, "FATAL: waterfall producer failed to start\n");
        return 1;
    }

    int server_fd = setup_server();
    if (server_fd < 0) return 1;
    printf("  listening on port %d\n", g_port);

    size_t sendbuf_size = FRAME_HEADER_SIZE + PROTO_FM_HEADER_SIZE
                        + PROTO_RDS_WIRE_SIZE + 8192 * sizeof(float);
    size_t iq_size = FRAME_HEADER_SIZE + 20 + IQ_CHUNK_SAMPLES * 3;
    if (iq_size > sendbuf_size)
        sendbuf_size = iq_size;
    uint8_t *sendbuf = malloc(sendbuf_size);
    if (!sendbuf) {
        fprintf(stderr, "FATAL: send buffer allocation failed (%zu bytes)\n",
                sendbuf_size);
        close(server_fd);
        return 1;
    }

    double last_broadcast = 0;
    bool iq_rx_was_wanted = false;

    while (atomic_load(&g_running)) {
        /* Subscription masks are the only source of truth for whether the
         * IQ producer's packing work can reach anyone. */
        bool iq_rx_wanted = iq_rx_subscriber_count() > 0;
        if (iq_rx_was_wanted && !iq_rx_wanted)
            iq_mode_flush();
        iq_rx_was_wanted = iq_rx_wanted;
        iq_mode_set_rx_wanted(iq_rx_wanted);

        int timeout_ms;

        /* The client that started an open-ended TX is gone: stop it.
         * Without this, an FM/IQ stream starved of data keeps the DAC
         * fed with pad samples (carrier/zeros) forever.  Checked before
         * the natural-completion cleanup so it also runs when the last
         * client has already disconnected. */
        if (g_tx_session.owner_id != 0 &&
            !client_alive_id(g_tx_session.owner_id)) {
            fprintf(stderr, "tx: owner client %u disconnected, stopping TX\n",
                    g_tx_session.owner_id);
            request_tx_stop();
        }

        /* TX finished on its own (end of stream, finite repeat, or a
         * requested stop whose drain completed): release the stream
         * objects, resume a suspended producer, and tell every observer
         * tx_busy is clear.  Every completion notifies, not only the ones
         * that held the clock; a one-shot WiFi burst must clear the
         * indicator too. */
        if (tx_session_active(&g_tx_session) && !tx_scheduler_busy()) {
            finish_tx_teardown();
            notify_others(-1);
        }

        if (tx_session_clock_held(&g_tx_session)) {
            /* No producer, so nothing to drain or broadcast — and no
             * reason to wake at frame rate and take cycles from TX. */
            timeout_ms = 50;
        } else if (g_mode == DAEMON_MODE_WATERFALL) {
            /* Waterfall's own drain paces broadcasts to target FPS. */
            timeout_ms = (g_target_fps > 0) ? (1000 / g_target_fps) : 20;
        } else {
            /* IQ, FM, and WiFi produce at their own rates.  Tying the
             * poll interval to g_target_fps lets those rings overrun at
             * fps=1 and drops most frames, so wake at a fixed cadence. */
            timeout_ms = 20;
        }

        struct pollfd fds[MAX_CLIENTS + 1];
        fds[0].fd = server_fd;
        fds[0].events = POLLIN;

        /*
         * Snapshot the count the array is built for.  accept_client() below
         * increments g_num_clients, and indexing fds[i + 1] by the *new*
         * count reaches a slot that was never initialised — stack garbage
         * whose revents bits decide whether a brand-new client gets read
         * from.  That was observable: a client that connected and sent
         * nothing intermittently stalled broadcasts to every other client
         * and could get an unrelated client evicted.
         */
        int n_polled = g_num_clients;
        for (int i = 0; i < n_polled; i++) {
            fds[i + 1].fd = g_clients[i].fd;
            fds[i + 1].events = POLLIN;
            if (sendq_pending(g_clients[i].sendq))
                fds[i + 1].events |= POLLOUT;
        }

        int ready = poll(fds, 1 + n_polled, timeout_ms);

        if (ready > 0) {
            /* Existing clients first, top down so remove_client()'s
             * compaction cannot invalidate an index still to be visited.
             * Accept afterwards, so a client added now is never indexed
             * against this iteration's fds[]. */
            for (int i = n_polled - 1; i >= 0; i--) {
                int fd = fds[i + 1].fd;

                /* Read before declaring the client gone.  A STOP sent
                 * just before close (or an abrupt close whose RST
                 * carries POLLERR|POLLHUP) arrives with POLLIN set;
                 * removing first discards the pending command and the
                 * transmitter runs on. */
                if (fds[i + 1].revents & POLLIN) {
                    process_client_data(i);
                    if (i >= g_num_clients || g_clients[i].fd != fd)
                        continue;   /* removed + compacted by the read */
                }
                if (fds[i + 1].revents & POLLOUT) {
                    if (!client_flush(&g_clients[i])) {
                        remove_client(i);
                        continue;
                    }
                }
                if (fds[i + 1].revents & (POLLERR | POLLHUP)) {
                    remove_client(i);
                    continue;
                }
            }
            if (fds[0].revents & POLLIN)
                accept_client(server_fd);
        }

        if (g_num_clients == 0)
            continue;

        /* --- Deliver pending notifications ---
         * A mode change (or TX completion that restored a mode) flagged
         * observers as needing an unsolicited RESP_STATUS.  Deliver before
         * the data broadcast so the client learns about the reconfiguration
         * before it sees data from the new mode. */
        for (int i = g_num_clients - 1; i >= 0; i--) {
            if (!g_clients[i].session.notify_pending)
                continue;
            if (send_status_notification(&g_clients[i]))
                session_client_clear_notify(&g_clients[i].session);
        }

        /* --- Drain ring buffers and broadcast to subscribed clients ---
         * Skip when TX has suspended the producer: nothing to drain. */
        if (!tx_session_clock_held(&g_tx_session))
            drain_and_broadcast(sendbuf, &last_broadcast);

        /* --- Flush outbound queues ---
         * Best-effort immediate flush after queueing data.  Any remainder
         * will be picked up via POLLOUT on the next iteration. */
        for (int i = g_num_clients - 1; i >= 0; i--) {
            if (sendq_pending(g_clients[i].sendq)) {
                if (!client_flush(&g_clients[i]))
                    remove_client(i);
            }
        }
    }

    printf("shutting down...\n");
    atomic_store(&g_running, false);

    /* Stop TX before the RX producer goes away.  Joining the scheduler
     * stops the engine but leaves its last word in the DAC, so blank the
     * DAC explicitly; otherwise the carrier outlives the process.  Unlike
     * stop_all_tx(), do not restore the suspended producer: we are
     * shutting down, not transitioning. */
    fm_tx_stream_end();
    iq_tx_stream_end();
    tx_scheduler_cleanup();
    dma_tx_stop();
    tx_flush_zero();
    fm_tx_stream_release();
    iq_tx_stream_release();
    tx_session_complete(&g_tx_session);

    if (g_active_producer)
        g_active_producer->exit();

    wifi_tx_cleanup();

    for (int i = 0; i < g_num_clients; i++) {
        close(g_clients[i].fd);
        sendq_destroy(g_clients[i].sendq);
    }
    close(server_fd);

    waterfall_destroy(g_waterfall);
    spsc_ring_destroy(g_ringbuf);
    free(sendbuf);

    return 0;
}
