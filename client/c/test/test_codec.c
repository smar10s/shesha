/* Offline tests for the wire-format IQ codec. */

#include "shesha/iq.h"
#include "test_util.h"

#include <math.h>

static void test_bytes(void)
{
    TEST_BEGIN("iq byte counts");
    TEST_EQ_I64(shesha_iq_bytes(12, 0), 0);
    TEST_EQ_I64(shesha_iq_bytes(12, 4), 12);
    TEST_EQ_I64(shesha_iq_bytes(8, 4), 8);
}

static void test_pack12_layout(void)
{
    TEST_BEGIN("12-bit pack layout");
    int16_t iq[4] = { 0x123, 0x456, -1, -2048 };
    uint8_t out[6];
    shesha_iq_pack(iq, 2, 12, out);
    TEST_EQ_I64(out[0], 0x23);
    TEST_EQ_I64(out[1], 0x61);
    TEST_EQ_I64(out[2], 0x45);
    TEST_EQ_I64(out[3], 0xFF);
    TEST_EQ_I64(out[4], 0x0F);
    TEST_EQ_I64(out[5], 0x80);
}

static void test_unpack12_layout(void)
{
    TEST_BEGIN("12-bit unpack layout");
    uint8_t in[6] = { 0x23, 0x61, 0x45, 0xFF, 0x0F, 0x80 };
    int16_t out[4];
    shesha_iq_unpack(in, 2, 12, out);
    TEST_EQ_I64(out[0], 0x123);
    TEST_EQ_I64(out[1], 0x456);
    TEST_EQ_I64(out[2], -1);
    TEST_EQ_I64(out[3], -2048);
}

static void test_roundtrip12(void)
{
    TEST_BEGIN("12-bit round trip (lossless)");
    int16_t iq[2 * 512];
    for (int i = 0; i < 512; i++) {
        int32_t re = ((i * 37) % 4096) - 2048;
        int32_t im = ((i * 91 + 7) % 4096) - 2048;
        iq[2 * i] = (int16_t)re;
        iq[2 * i + 1] = (int16_t)im;
    }
    uint8_t packed[3 * 512];
    shesha_iq_pack(iq, 512, 12, packed);
    int16_t back[2 * 512];
    shesha_iq_unpack(packed, 512, 12, back);
    TEST_CHECK(memcmp(iq, back, sizeof iq) == 0);
}

static void test_pack8_layout(void)
{
    TEST_BEGIN("8-bit pack layout (top 8 bits)");
    int16_t iq[4] = { 0x123, 0x456, -1, -2048 };
    uint8_t out[4];
    shesha_iq_pack(iq, 2, 8, out);
    TEST_EQ_I64(out[0], 0x12);
    TEST_EQ_I64(out[1], 0x45);
    TEST_EQ_I64(out[2], 0xFF);
    TEST_EQ_I64(out[3], 0x80);
}

static void test_unpack8_scale(void)
{
    TEST_BEGIN("8-bit unpack left-justifies into 12-bit");
    uint8_t in[4] = { 0x12, 0x45, 0xFF, 0x80 };
    int16_t out[4];
    shesha_iq_unpack(in, 2, 8, out);
    TEST_EQ_I64(out[0], 0x12 * 16);
    TEST_EQ_I64(out[1], 0x45 * 16);
    TEST_EQ_I64(out[2], -16);
    TEST_EQ_I64(out[3], -2048);
}

static void test_pack_scaled(void)
{
    TEST_BEGIN("pack_scaled scales to full 12-bit");
    float re[3] = { 1.0f, -1.0f, 0.0f };
    float im[3] = { 0.5f, 0.0f, 0.0f };
    uint8_t out[9];
    shesha_iq_pack_scaled(re, im, 3, 12, out);
    int16_t want[6] = { 2047, 1024, -2047, 0, 0, 0 };
    uint8_t ref[9];
    shesha_iq_pack(want, 3, 12, ref);
    TEST_CHECK(memcmp(out, ref, sizeof out) == 0);
}

static void test_pack_scaled8(void)
{
    TEST_BEGIN("pack_scaled 8-bit keeps the top 8 bits");
    float re[2] = { 1.0f, 0.0f };
    float im[2] = { 0.0f, -1.0f };
    uint8_t out[4];
    shesha_iq_pack_scaled(re, im, 2, 8, out);
    int16_t want[4] = { 2047, 0, 0, -2048 };
    uint8_t ref[4];
    shesha_iq_pack(want, 2, 8, ref);
    TEST_CHECK(memcmp(out, ref, sizeof out) == 0);
}

static void test_scaled_roundtrip(void)
{
    TEST_BEGIN("pack_scaled/unpack_scaled round trip");
    enum { N = 64 };
    float re[N], im[N], r2[N], i2[N];
    for (int i = 0; i < N; i++) {
        re[i] = (float)((i % 17) - 8) / 16.0f;
        im[i] = (float)((i % 13) - 6) / 16.0f;
    }
    uint8_t packed[3 * N];
    shesha_iq_pack_scaled(re, im, N, 12, packed);

    float peak = 1e-9f;
    for (int i = 0; i < N; i++) {
        float a = fabsf(re[i]);
        float b = fabsf(im[i]);
        if (a > peak) peak = a;
        if (b > peak) peak = b;
    }

    shesha_iq_unpack_scaled(packed, N, 12, r2, i2);
    float maxerr = 0.0f;
    for (int i = 0; i < N; i++) {
        float e = fabsf(r2[i] - re[i] / peak);
        if (e > maxerr) maxerr = e;
        e = fabsf(i2[i] - im[i] / peak);
        if (e > maxerr) maxerr = e;
    }
    TEST_CHECK_MSG(maxerr < 0.002f, "maxerr=%g", maxerr);
}

int main(void)
{
    test_bytes();
    test_pack12_layout();
    test_unpack12_layout();
    test_roundtrip12();
    test_pack8_layout();
    test_unpack8_scale();
    test_pack_scaled();
    test_pack_scaled8();
    test_scaled_roundtrip();
    TEST_SUMMARY();
    return TEST_EXIT();
}
