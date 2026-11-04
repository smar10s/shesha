/* Wire-format IQ packing.  See include/shesha/iq.h and firmware/src/iq_pack.h. */

#include "shesha/iq.h"

#include <math.h>

static int32_t sign12(uint32_t v)
{
    v &= 0xFFFu;
    return (v & 0x800u) ? (int32_t)v - 0x1000 : (int32_t)v;
}

size_t shesha_iq_bytes(int bits, size_t n_samples)
{
    return bits == SHESHA_IQ_BITS_8 ? n_samples * 2 : n_samples * 3;
}

void shesha_iq_pack(const int16_t *iq, size_t n, int bits, uint8_t *out)
{
    for (size_t k = 0; k < n; k++) {
        uint32_t i = (uint16_t)iq[2 * k] & 0xFFFu;
        uint32_t q = (uint16_t)iq[2 * k + 1] & 0xFFFu;
        if (bits == SHESHA_IQ_BITS_8) {
            out[0] = (uint8_t)((i >> 4) & 0xFF);   /* I[11:4] */
            out[1] = (uint8_t)((q >> 4) & 0xFF);   /* Q[11:4] */
            out += 2;
        } else {
            out[0] = (uint8_t)(i & 0xFF);
            out[1] = (uint8_t)(((i >> 8) & 0x0F) | ((q & 0x0F) << 4));
            out[2] = (uint8_t)((q >> 4) & 0xFF);
            out += 3;
        }
    }
}

void shesha_iq_unpack(const uint8_t *in, size_t n, int bits, int16_t *out)
{
    for (size_t k = 0; k < n; k++) {
        if (bits == SHESHA_IQ_BITS_8) {
            out[2 * k]     = (int16_t)((int8_t)in[0] * 16);
            out[2 * k + 1] = (int16_t)((int8_t)in[1] * 16);
            in += 2;
        } else {
            uint32_t v = (uint32_t)in[0] | ((uint32_t)in[1] << 8)
                       | ((uint32_t)in[2] << 16);
            out[2 * k]     = (int16_t)sign12(v & 0xFFFu);
            out[2 * k + 1] = (int16_t)sign12((v >> 12) & 0xFFFu);
            in += 3;
        }
    }
}

static int32_t clamp12(long v)
{
    if (v > 2047) return 2047;
    if (v < -2048) return -2048;
    return (int32_t)v;
}

void shesha_iq_pack_scaled(const float *re, const float *im, size_t n,
                           int bits, uint8_t *out)
{
    float peak = 1e-9f;
    for (size_t i = 0; i < n; i++) {
        float a = fabsf(re[i]);
        float b = fabsf(im[i]);
        if (a > peak) peak = a;
        if (b > peak) peak = b;
    }
    const float scale = 2047.0f / peak;
    for (size_t i = 0; i < n; i++) {
        uint32_t iu = (uint32_t)clamp12(lroundf(re[i] * scale)) & 0xFFFu;
        uint32_t qu = (uint32_t)clamp12(lroundf(im[i] * scale)) & 0xFFFu;
        if (bits == SHESHA_IQ_BITS_8) {
            out[0] = (uint8_t)((iu >> 4) & 0xFF);
            out[1] = (uint8_t)((qu >> 4) & 0xFF);
            out += 2;
        } else {
            out[0] = (uint8_t)(iu & 0xFF);
            out[1] = (uint8_t)(((iu >> 8) & 0x0F) | ((qu & 0x0F) << 4));
            out[2] = (uint8_t)((qu >> 4) & 0xFF);
            out += 3;
        }
    }
}

void shesha_iq_unpack_scaled(const uint8_t *in, size_t n, int bits,
                             float *re, float *im)
{
    for (size_t k = 0; k < n; k++) {
        if (bits == SHESHA_IQ_BITS_8) {
            re[k] = (float)((int8_t)in[0] * 16) / 2048.0f;
            im[k] = (float)((int8_t)in[1] * 16) / 2048.0f;
            in += 2;
        } else {
            uint32_t v = (uint32_t)in[0] | ((uint32_t)in[1] << 8)
                       | ((uint32_t)in[2] << 16);
            re[k] = (float)sign12(v & 0xFFFu) / 2048.0f;
            im[k] = (float)sign12((v >> 12) & 0xFFFu) / 2048.0f;
            in += 3;
        }
    }
}
