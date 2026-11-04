/* Internal protocol constants and parsers.  Mirrors firmware/src/protocol.h.
 * Not part of the public API, but exposed with external linkage so the
 * offline test suite can exercise the parsers directly. */
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#include "shesha/client.h"

/* Command types. */
#define SHE_CMD_SET_FREQ          0x01
#define SHE_CMD_SET_GAIN          0x02
#define SHE_CMD_SET_BW            0x03
#define SHE_CMD_SET_FFT_SIZE      0x04
#define SHE_CMD_SET_WINDOW        0x05
#define SHE_CMD_SET_FPS           0x06
#define SHE_CMD_SET_AGC           0x07
#define SHE_CMD_SET_SAMPLE_RATE   0x08
#define SHE_CMD_SET_ACCUM         0x09
#define SHE_CMD_SET_MODE          0x0A
#define SHE_CMD_SET_TX_ATTEN      0x0B
#define SHE_CMD_TX                0x0D
#define SHE_CMD_TX_STOP           0x0E
#define SHE_CMD_SET_FPGA          0x0F
#define SHE_CMD_TX_STREAM_DATA    0x15
#define SHE_CMD_TX_STREAM_END     0x16
#define SHE_CMD_TX_STATS          0x17
#define SHE_CMD_SET_IQ_BITS       0x1C
#define SHE_CMD_IQ_TX_STREAM_DATA 0x1D
#define SHE_CMD_IQ_TX_STREAM_END  0x1E

#define SHE_RESP_OK          0x10
#define SHE_RESP_ERROR       0x11
#define SHE_RESP_STATUS      0x12
#define SHE_RESP_CAPS        0x13
#define SHE_RESP_TX_STATS    0x18
#define SHE_RESP_RETRY       0x19

#define SHE_DATA_PSD   0x20
#define SHE_DATA_WIFI  0x21
#define SHE_DATA_FM    0x22
#define SHE_DATA_IQ    0x23

#define SHE_CMD_STATUS       0x30
#define SHE_CMD_CAPS         0x31
#define SHE_CMD_THREAD_STATS 0x32
#define SHE_CMD_SUBSCRIBE    0x33

/* Frame envelope: type(u8) + payload_len(u32 le) + seq(u32 le). */
#define SHE_FRAME_HEADER_SIZE 9

/* Payload sizes, pinned on both ends. */
/* The C client parses only this 44-byte prefix of the 91-byte payload. */
#define SHE_STATUS_WIRE_SIZE   44
#define SHE_TX_STATS_WIRE_SIZE 96
#define SHE_IQ_HEADER_SIZE     20

static inline uint16_t she_le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline uint32_t she_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline uint64_t she_le64(const uint8_t *p)
{
    return (uint64_t)she_le32(p) | ((uint64_t)she_le32(p + 4) << 32);
}

static inline float she_lef32(const uint8_t *p)
{
    uint32_t u = she_le32(p);
    float f;
    memcpy(&f, &u, sizeof f);
    return f;
}

static inline void she_put_le16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
}

static inline void she_put_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}

static inline void she_put_le64(uint8_t *p, uint64_t v)
{
    she_put_le32(p, (uint32_t)(v & 0xFFFFFFFFu));
    she_put_le32(p + 4, (uint32_t)(v >> 32));
}

static inline void she_put_lef32(uint8_t *p, float f)
{
    uint32_t u;
    memcpy(&u, &f, sizeof u);
    she_put_le32(p, u);
}

/* Parsers.  Return SHESHA_OK on success, or SHESHA_ERR_PROTOCOL if the
 * payload is the wrong size.  shesha__parse_iq allocates `out->samples`. */
int shesha__parse_status(const uint8_t *payload, uint32_t len,
                         shesha_status_t *out);
int shesha__parse_tx_stats(const uint8_t *payload, uint32_t len,
                           shesha_tx_stats_t *out);
int shesha__parse_iq(const uint8_t *payload, uint32_t len,
                     shesha_iq_frame_t *out);
