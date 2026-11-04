/* firmware/src/iq_tx.h — streaming raw-IQ TX (DAEMON_MODE_IQ).
 *
 * The host pushes packed wire-format bytes (CMD_IQ_TX_STREAM_DATA); the
 * TX thread unpacks to float and feeds the DAC via the tx_scheduler
 * stream.  Runs concurrent with the IQ RX producer — same shared clock,
 * no displacement (unlike FM TX, which claims a different rate).
 */
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#define IQ_TX_PUSH_OK    0
#define IQ_TX_PUSH_FULL  1   /* backpressure: ring full, resend later */
#define IQ_TX_PUSH_ERR  -1

/* Both directions share the USB2 link.  16 MB/s is the measured zero-drop
 * point for 8-bit 4+4; one-way RX reaches more.  With no RX subscriber the
 * policy rate cap is the only limit. */
#define IQ_DUPLEX_BUDGET_BPS (16u * 1000u * 1000u)

static inline uint64_t iq_bits_bytes(uint8_t bits)
{
    return bits == 8 ? 2u : 3u;
}

/* Would one TX stream plus rx_streams RX streams fit at this shared rate?
 * RX and TX bits can differ: the width is a runtime knob. */
static inline bool iq_tx_duplex_fits(uint32_t rate_hz, uint8_t rx_bits,
                                     unsigned rx_streams, uint8_t tx_bits)
{
    if (rx_streams == 0)
        return true;
    uint64_t total = (uint64_t)rate_hz *
        ((uint64_t)rx_streams * iq_bits_bytes(rx_bits)
         + iq_bits_bytes(tx_bits));
    return total <= IQ_DUPLEX_BUDGET_BPS;
}

typedef struct {
    bool     active;
    uint32_t ring_fill;
    uint32_t ring_size;
    uint32_t push_rejects;
    uint32_t starved_chunks;   /* producer polls that found the ring empty */
} iq_tx_stream_stats_t;

int  iq_tx_stream_start(uint8_t bits, uint32_t dac_rate);
int  iq_tx_stream_push(const uint8_t *data, uint32_t n);
void iq_tx_stream_end(void);
void iq_tx_stream_release(void);
bool iq_tx_stream_active(void);
uint8_t iq_tx_stream_bits(void);
void iq_tx_stream_stats(iq_tx_stream_stats_t *out);
