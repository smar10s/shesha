/* shesha/iq.h — wire-format IQ packing.
 *
 * Mirrors firmware/src/iq_pack.h and liblora/pluto.py.  Samples are
 * interleaved I,Q as signed 12-bit integers (range -2048..2047) carried in
 * int16_t.
 *
 *   12-bit: 3 B/sample  b0 = I[7:0], b1 = I[11:8] | Q[3:0]<<4, b2 = Q[11:4]
 *   8-bit:  2 B/sample  b0 = I[11:4], b1 = Q[11:4]   (top 8 bits of 12)
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* IQ wire sample width, both directions. */
enum {
    SHESHA_IQ_BITS_8  = 8,
    SHESHA_IQ_BITS_12 = 12,
};

/* Bytes per sample for a given wire width (2 for 8-bit, 3 for 12-bit). */
size_t shesha_iq_bytes(int bits, size_t n_samples);

/* Pack n interleaved (I,Q) signed 12-bit samples into `out`, which must
 * hold shesha_iq_bytes(bits, n). */
void shesha_iq_pack(const int16_t *iq, size_t n, int bits, uint8_t *out);

/* Unpack n samples from wire bytes into `out` (2*n int16, interleaved).
 * Values are sign-extended into the 12-bit domain. */
void shesha_iq_unpack(const uint8_t *in, size_t n, int bits, int16_t *out);

/* Split-complex float codec — the full-scale convention used by
 * liblora.pluto.vectors_to_shesha / liblora.tx._to_int12 and by the
 * firmware's iq_unpack_* (scale 1/2048).
 *
 * shesha_iq_pack_scaled() scales {re,im} to the full 12-bit range
 * (scale = 2047 / peak), rounds half away from zero and clamps to
 * [-2048, 2047], then packs exactly as shesha_iq_pack() would. */
void shesha_iq_pack_scaled(const float *re, const float *im, size_t n,
                           int bits, uint8_t *out);

/* shesha_iq_unpack_scaled() is the inverse: signed 12-bit / 2048. */
void shesha_iq_unpack_scaled(const uint8_t *in, size_t n, int bits,
                             float *re, float *im);

#ifdef __cplusplus
}
#endif
