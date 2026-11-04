#include "iq_pack.h"
#include "adc_clip.h"

size_t iq_pack_bytes(int bits, size_t n)
{
    return bits == IQ_BITS_8 ? n * 2 : n * 3;
}

void iq_pack_12(const uint32_t *ddr, size_t n, uint8_t *out,
                uint64_t *clip_count)
{
    uint64_t clips = clip_count ? *clip_count : 0;
    for (size_t i = 0; i < n; i++) {
        uint32_t w = ddr[i];
        uint32_t re = w & 0xFFFu;
        uint32_t im = (w >> 12) & 0xFFFu;
        clips += adc_clip_is_raw12(re);
        clips += adc_clip_is_raw12(im);
        out[0] = (uint8_t)(re & 0xFF);
        out[1] = (uint8_t)((re >> 8) | ((im & 0xF) << 4));
        out[2] = (uint8_t)(im >> 4);
        out += 3;
    }
    if (clip_count)
        *clip_count = clips;
}

void iq_pack_8(const uint32_t *ddr, size_t n, uint8_t *out,
               uint64_t *clip_count)
{
    uint64_t clips = clip_count ? *clip_count : 0;
    for (size_t i = 0; i < n; i++) {
        uint32_t w = ddr[i];
        clips += adc_clip_is_raw12(w);
        clips += adc_clip_is_raw12(w >> 12);
        out[0] = (uint8_t)((w >> 4) & 0xFF);    /* I[11:4] */
        out[1] = (uint8_t)((w >> 16) & 0xFF);   /* Q[11:4] */
        out += 2;
    }
    if (clip_count)
        *clip_count = clips;
}

static int32_t sign12(int32_t v)
{
    v &= 0xFFF;
    return (v & 0x800) ? v - 0x1000 : v;
}

void iq_unpack_12(const uint8_t *in, size_t n, float *real, float *imag)
{
    for (size_t i = 0; i < n; i++) {
        uint32_t v = (uint32_t)in[0] | ((uint32_t)in[1] << 8)
                     | ((uint32_t)in[2] << 16);
        real[i] = sign12((int32_t)(v & 0xFFF)) / 2048.0f;
        imag[i] = sign12((int32_t)(v >> 12)) / 2048.0f;
        in += 3;
    }
}

void iq_unpack_8(const uint8_t *in, size_t n, float *real, float *imag)
{
    for (size_t i = 0; i < n; i++) {
        real[i] = (int8_t)in[0] * 16.0f / 2048.0f;
        imag[i] = (int8_t)in[1] * 16.0f / 2048.0f;
        in += 2;
    }
}
