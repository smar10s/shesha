/* firmware/src/iq_pack.h — wire-format IQ packing.
 *
 * One packing, both directions (RX frames and TX stream):
 *   12-bit: 3 B/sample  b0 = I[7:0], b1 = I[11:8] | Q[3:0]<<4, b2 = Q[11:4]
 *   8-bit:  2 B/sample  b0 = I[11:4], b1 = Q[11:4]  (top 8 bits of 12)
 *
 * Unpacking expands to signed 12-bit scaled by 1/2048 to float, matching
 * convert.h's scale convention, so unpacked samples feed
 * dma_tx_stream_feed_fixed(scale=2047) directly.
 */
#pragma once

#include <stdint.h>
#include <stddef.h>

#define IQ_BITS_12 12
#define IQ_BITS_8  8

size_t iq_pack_bytes(int bits, size_t n_samples);

/* Pack DDR words ({8'b0, imag[11:0], real[11:0]}) to wire bytes.
 * When clip_count is non-NULL, add the number of rail components seen. */
void iq_pack_12(const uint32_t *ddr_words, size_t n, uint8_t *out,
                uint64_t *clip_count);
void iq_pack_8(const uint32_t *ddr_words, size_t n, uint8_t *out,
               uint64_t *clip_count);

/* Unpack wire bytes to split-complex float (scale 1/2048). */
void iq_unpack_12(const uint8_t *in, size_t n, float *real, float *imag);
void iq_unpack_8(const uint8_t *in, size_t n, float *real, float *imag);
