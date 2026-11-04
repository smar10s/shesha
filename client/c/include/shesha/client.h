/* shesha/client.h — C TCP client for the shesha daemon TLV protocol.
 *
 * A dependency-free (POSIX sockets) equivalent of the Python
 * `shesha.client.DaemonClient`, scoped to what the IQ RX/TX path needs:
 * connection management, synchronous config/query commands, DATA_IQ frame
 * reception, and the IQ TX byte stream.  Wire layouts mirror
 * `firmware/src/protocol.h`.
 *
 * The daemon's outbound per-client ring stamps every data frame with a
 * monotonic sequence number.  A jump > 1 means this client could not keep
 * up and frames were dropped; the gap/dropped counters expose that.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "shesha/iq.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SHESHA_DEFAULT_HOST "192.168.2.1"
#define SHESHA_DEFAULT_PORT 5555

/* Return codes.  Functions return SHESHA_OK (0) or a positive count on
 * success, and one of these negative codes on failure. */
enum {
    SHESHA_OK           = 0,
    SHESHA_ERR_PARAM    = -1,
    SHESHA_ERR_SOCKET   = -2,
    SHESHA_ERR_CONNECT  = -3,
    SHESHA_ERR_SEND     = -4,
    SHESHA_ERR_RECV     = -5,
    SHESHA_ERR_TIMEOUT  = -6,
    SHESHA_ERR_PROTOCOL = -7,
    SHESHA_ERR_DAEMON   = -8,   /* daemon replied RESP_ERROR */
    SHESHA_ERR_CLOSED   = -9,
    SHESHA_ERR_NOMEM    = -10,
    SHESHA_ERR_STATE    = -11,  /* not connected, or invalid local state */
};

/* DaemonMode (firmware protocol.h). */
typedef enum {
    SHESHA_MODE_WATERFALL = 0,
    SHESHA_MODE_WIFI      = 1,
    SHESHA_MODE_IQ        = 2,
    SHESHA_MODE_FM        = 3,
} shesha_mode_t;

/* IQ wire sample width, both directions (SHESHA_IQ_BITS_*, shesha/iq.h). */

/* Subscription bitmask (CMD_SUBSCRIBE).  Default after connect is 0. */
enum {
    SHESHA_SUBSCRIBE_PSD  = 1u << 0,
    SHESHA_SUBSCRIBE_WIFI = 1u << 1,
    SHESHA_SUBSCRIBE_FM   = 1u << 2,
    SHESHA_SUBSCRIBE_IQ   = 1u << 3,
    SHESHA_SUBSCRIBE_ALL  = 0x0Fu,
};

/* Outcome of an IQ TX stream push.  RETRY is backpressure, not failure:
 * the daemon's DDR ring is bounded, so a client running ahead of realtime
 * should wait and re-send the same chunk. */
typedef enum {
    SHESHA_PUSH_OK    = 0,
    SHESHA_PUSH_RETRY = 1,
    SHESHA_PUSH_ERROR = 2,
} shesha_push_result_t;

/* Device status (RESP_STATUS), mirrors Python `shesha.protocol.Status`. */
typedef struct {
    uint64_t freq_hz;
    uint32_t sample_rate_hz;
    uint32_t bw_hz;
    uint32_t fft_size;
    float    gain_db;
    uint8_t  agc_mode;
    uint8_t  window_type;
    uint8_t  target_fps;
    uint8_t  daemon_mode;
    float    tx_atten_db;
    uint8_t  tx_busy;
    uint8_t  tx_proto;
    uint8_t  tx_flags;
    uint16_t tx_repeat;
    uint32_t tx_interval_us;
    bool     fpga_active;
    bool     rx_active;
    uint8_t  iq_bits;
} shesha_status_t;

/* Streaming-TX telemetry (RESP_TX_STATS), straight off the FPGA counters.
 * `emitted` is the only trustworthy output-rate measurement. */
typedef struct {
    bool     tx_active;
    bool     fm_active;
    bool     end_of_stream;
    uint32_t dac_rate;
    uint64_t fed;
    uint64_t emitted;
    uint64_t elapsed_us;
    int32_t  depth;
    uint32_t max_depth;
    uint32_t high_water;
    uint32_t underruns;
    uint32_t ring_fill;
    uint32_t ring_size;
    uint32_t audio_rate;
    uint64_t audio_consumed;
    uint64_t starved_samples;
    uint32_t push_rejects;
    bool     iq_active;
    uint32_t iq_ring_fill;
    uint32_t iq_push_rejects;
    uint32_t iq_starved;
    bool     dma_start_failed;
} shesha_tx_stats_t;

/* One DATA_IQ frame.  `samples` is malloc'd by the library and must be
 * released with shesha_iq_frame_free(). */
typedef struct {
    double   timestamp;
    float    freq_mhz;
    uint32_t sample_rate_hz;
    uint8_t  bits;
    uint16_t n_samples;
    uint8_t *samples;   /* packed wire bytes: n_samples * 2 (8-bit) or * 3 */
} shesha_iq_frame_t;

typedef struct shesha_client shesha_client_t;

/* -- lifecycle -- */

shesha_client_t *shesha_client_new(void);
void shesha_client_free(shesha_client_t *c);

/* Non-blocking connect with a wall-clock timeout.  Returns SHESHA_OK on
 * success; SHESHA_ERR_CONNECT / SHESHA_ERR_TIMEOUT on failure.  A client
 * must be connected before any command is issued. */
int shesha_client_connect(shesha_client_t *c, const char *host,
                          uint16_t port, double timeout_s);
void shesha_client_disconnect(shesha_client_t *c);
bool shesha_client_connected(const shesha_client_t *c);

/* Sequence-gap accounting since the last connect. */
uint64_t shesha_client_gaps(const shesha_client_t *c);
uint64_t shesha_client_dropped(const shesha_client_t *c);

/* -- queries -- */

int shesha_client_status(shesha_client_t *c, shesha_status_t *out);
int shesha_client_tx_stats(shesha_client_t *c, shesha_tx_stats_t *out);

/* -- config (synchronous; SHESHA_OK on success) -- */

int shesha_client_set_mode(shesha_client_t *c, shesha_mode_t mode);
int shesha_client_set_freq(shesha_client_t *c, uint64_t freq_hz);
int shesha_client_set_bw(shesha_client_t *c, uint32_t bw_hz);
int shesha_client_set_gain(shesha_client_t *c, float gain_db);
int shesha_client_set_agc(shesha_client_t *c, uint8_t mode);
int shesha_client_set_sample_rate(shesha_client_t *c, uint32_t rate_hz);
int shesha_client_set_iq_bits(shesha_client_t *c, int bits);
int shesha_client_set_tx_atten(shesha_client_t *c, float db);
int shesha_client_subscribe(shesha_client_t *c, uint8_t mask);

/* -- IQ receive -- */

/* Pull one DATA_IQ frame.  Returns 1 on a frame, 0 on timeout, <0 on
 * error.  Non-IQ frames are consumed and ignored.  Caller must subscribe
 * (SHESHA_SUBSCRIBE_IQ) first, or use shesha_client_iq_capture(). */
int shesha_client_iq_rx_next(shesha_client_t *c, shesha_iq_frame_t *out,
                             int timeout_ms);

/* Free a frame's sample buffer and zero the struct. */
void shesha_iq_frame_free(shesha_iq_frame_t *f);

/* -- IQ transmit -- */

/* Push raw packed wire bytes.  The daemon starts the stream implicitly on
 * the first call (daemon must already be in IQ mode).  RETRY means "ahead
 * of realtime". */
shesha_push_result_t shesha_client_iq_tx_stream_data(shesha_client_t *c,
                                                     const void *data,
                                                     size_t len,
                                                     int timeout_ms);
int shesha_client_iq_tx_stream_end(shesha_client_t *c);

/* Wait until the daemon has finished emitting the active stream.
 *
 * The stream commands only guarantee the bytes were accepted; the daemon
 * emits asynchronously from a bounded ring, and it stops TX when the owning
 * client disconnects — so a client that disconnects immediately after
 * iq_tx_stream_end truncates the packet tail.  Poll tx_stats until no TX/IQ
 * stream is active.  Returns SHESHA_OK once drained, SHESHA_ERR_TIMEOUT if
 * the stream is still active after `timeout_ms` (or another error). */
int shesha_client_tx_wait(shesha_client_t *c, int timeout_ms);

/* -- convenience: configure + capture / configure + transmit --
 *
 * Mirror liblora/pluto.py::capture() and liblora/tx.py::transmit() at the
 * protocol level and operate on packed wire bytes.  RX LO offset handling
 * and digital DC removal remain the caller's job.
 */

typedef struct {
    int      bits;            /* 8 or 12 */
    uint32_t sample_rate_hz;
    uint32_t bw_hz;
    uint64_t lo_hz;           /* value written to the RX LO */
    float    gain_db;
    uint8_t  agc_mode;        /* 0 = manual gain */
    double   duration_s;
} shesha_capture_opts_t;

typedef struct {
    uint32_t sample_rate_hz;  /* hardware readback */
    uint64_t lo_hz;           /* hardware readback */
    float    gain_db;         /* hardware readback */
    uint32_t n_samples;
    int      bits;
} shesha_capture_info_t;

/* Collect until duration_s elapses, writing concatenated packed samples
 * into `out` (*out_len set to the byte count).  Previous mode/rate/bw are
 * restored and the subscription cleared on the way out.  Returns SHESHA_OK
 * or an error; SHESHA_ERR_NOMEM if `out_capacity` is too small. */
int shesha_client_iq_capture(shesha_client_t *c,
                             const shesha_capture_opts_t *opts,
                             uint8_t *out, size_t out_capacity,
                             size_t *out_len,
                             shesha_capture_info_t *info);

typedef struct {
    int      bits;            /* 8 or 12 */
    uint32_t sample_rate_hz;
    uint32_t bw_hz;
    uint64_t freq_hz;
    float    atten_db;
    size_t   chunk_samples;   /* 0 -> 4096 */
} shesha_transmit_opts_t;

/* Stream packed wire bytes, retrying chunks the daemon reports RETRY, then
 * call iq_tx_stream_end and block until the daemon has finished emitting
 * (shesha_client_tx_wait).  Returns the number of samples pushed, or a
 * negative error code. */
int64_t shesha_client_iq_transmit(shesha_client_t *c,
                                  const uint8_t *packed, size_t packed_len,
                                  const shesha_transmit_opts_t *opts);

#ifdef __cplusplus
}
#endif
