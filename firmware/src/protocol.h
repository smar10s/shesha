#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "tx_flags.h"

#define CMD_SET_FREQ        0x01
#define CMD_SET_GAIN        0x02
#define CMD_SET_BW          0x03
#define CMD_SET_FFT_SIZE    0x04
#define CMD_SET_WINDOW      0x05
#define CMD_SET_FPS         0x06
#define CMD_SET_AGC         0x07
#define CMD_SET_SAMPLE_RATE 0x08
#define CMD_SET_ACCUM       0x09
#define CMD_SET_MODE        0x0A
#define CMD_SET_TX_ATTEN    0x0B
#define CMD_WIFI_DIAG       0x0C
#define CMD_TX              0x0D
#define CMD_TX_STOP         0x0E
#define CMD_SET_FPGA        0x0F
#define CMD_TX_STREAM_DATA  0x15
#define CMD_TX_STREAM_END   0x16
#define CMD_TX_STATS        0x17
#define CMD_SET_IQ_BITS     0x1C
#define CMD_IQ_TX_STREAM_DATA 0x1D
#define CMD_IQ_TX_STREAM_END  0x1E
#define CMD_SET_WORKERS     0x1F
#define CMD_SET_RDS         0x24
#define CMD_TX_STREAM_FLUSH 0x25

#define TX_PROTO_WIFI       0
#define TX_PROTO_LORA       1
#define TX_PROTO_IQ         2
#define TX_PROTO_FM         3
/* No TX is active.  Distinct from every real protocol and from the stop
 * target that means "every path". */
#define TX_PROTO_NONE       0xFE

/* CMD_TX_STOP target: stop every TX path. */
#define TX_STOP_ALL         0xFF

#define RESP_OK             0x10
#define RESP_ERROR          0x11
#define RESP_STATUS         0x12
#define RESP_CAPS           0x13
#define RESP_WIFI_DIAG      0x14
#define RESP_TX_STATS       0x18

/*
 * Backpressure, not failure.  The daemon buffers TX audio in a bounded
 * ring; when a client outruns realtime the correct answer is "retry
 * shortly", not an error that makes it abandon a healthy stream.  Kept a
 * distinct frame type so a client cannot confuse it with a real fault.
 */
#define RESP_RETRY          0x19
#define RESP_THREAD_STATS   0x1A

#define DATA_PSD            0x20
#define DATA_WIFI           0x21
#define DATA_FM             0x22
#define DATA_IQ             0x23

#define CMD_STATUS          0x30
#define CMD_CAPS            0x31
#define CMD_THREAD_STATS    0x32
#define CMD_SUBSCRIBE       0x33

#define DAEMON_MODE_WATERFALL 0
#define DAEMON_MODE_WIFI      1
#define DAEMON_MODE_IQ        2
#define DAEMON_MODE_FM        3

#define FRAME_HEADER_SIZE   9

/* Largest payload a client may send.  The daemon reassembles oversized
 * frames in memory, so an unbounded length is a remote OOM. */
#define PROTO_MAX_PAYLOAD (1024u * 1024u)

size_t proto_build_frame(uint8_t *buf, uint8_t type, const void *payload, uint32_t payload_len);
uint32_t proto_parse_header(const uint8_t *buf, uint8_t *type_out, uint32_t *seq_out);

/* Compute the full wire size for a payload.  Returns false when the
 * payload exceeds PROTO_MAX_PAYLOAD; callers must reject the frame
 * before doing any length arithmetic or allocation. */
bool proto_frame_size(uint32_t payload_len, size_t *frame_size);
size_t proto_build_ok(uint8_t *buf, uint8_t cmd_type);
size_t proto_build_ok_u32(uint8_t *buf, uint8_t cmd_type, uint32_t actual);
size_t proto_build_ok_u32_pair(uint8_t *buf, uint8_t cmd_type, uint32_t a, uint32_t b);
size_t proto_build_ok_u64(uint8_t *buf, uint8_t cmd_type, uint64_t actual);
size_t proto_build_error(uint8_t *buf, uint8_t cmd_type, const char *msg);

size_t proto_build_psd(uint8_t *buf, uint32_t seq, double timestamp,
                       float center_freq_mhz,
                       uint32_t n_bins, const float *bins);

size_t proto_build_iq(uint8_t *buf, uint32_t seq, double timestamp,
                      float freq_mhz, uint32_t sample_rate_hz,
                      uint8_t bits, uint16_t n_samples,
                      const uint8_t *packed);
