#include "../src/iq_pack.h"
#include <stdio.h>
#include <string.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

static void test_pack_bytes(void)
{
    CHECK(iq_pack_bytes(IQ_BITS_12, 4) == 12);
    CHECK(iq_pack_bytes(IQ_BITS_8, 4) == 8);
}

static void test_pack_12_layout(void)
{
    /* DDR word: imag=0xABC, real=0x123 */
    uint32_t w = 0xABC123u;
    uint8_t out[3];
    iq_pack_12(&w, 1, out, NULL);
    CHECK(out[0] == 0x23);
    CHECK(out[1] == 0xC1);  /* I[11:8]=0x1 | Q[3:0]=0xC << 4 */
    CHECK(out[2] == 0xAB);
}

static void test_pack_12_negative_values(void)
{
    /* real=-1 (0xFFF), imag=-2048 (0x800) */
    uint32_t w = (0x800u << 12) | 0xFFFu;
    uint8_t out[3];
    iq_pack_12(&w, 1, out, NULL);
    CHECK(out[0] == 0xFF);
    CHECK(out[1] == 0x0F);
    CHECK(out[2] == 0x80);
}

static void test_pack_8_layout(void)
{
    uint32_t w = 0xABC123u;
    uint8_t out[2];
    iq_pack_8(&w, 1, out, NULL);
    CHECK(out[0] == 0x12);  /* real[11:4] */
    CHECK(out[1] == 0xAB);  /* imag[11:4] */
}

static void test_unpack_12_roundtrip(void)
{
    int32_t pairs[4][2] = {
        {0, 0}, {2047, -2048}, {100, -100}, {-1, 1},
    };
    uint32_t words[4];
    for (int i = 0; i < 4; i++)
        words[i] = ((uint32_t)(pairs[i][1] & 0xFFF) << 12)
                   | (uint32_t)(pairs[i][0] & 0xFFF);

    uint8_t packed[12];
    float re[4], im[4];
    iq_pack_12(words, 4, packed, NULL);
    iq_unpack_12(packed, 4, re, im);

    for (int i = 0; i < 4; i++) {
        CHECK(re[i] == pairs[i][0] / 2048.0f);
        CHECK(im[i] == pairs[i][1] / 2048.0f);
    }
}

static void test_unpack_8_expands_to_12bit(void)
{
    /* 0x7F = +127 -> +2032; 0x80 = -128 -> -2048 */
    uint8_t in[2] = {0x7F, 0x80};
    float re, im;
    iq_unpack_8(in, 1, &re, &im);
    CHECK(re == 2032.0f / 2048.0f);
    CHECK(im == -2048.0f / 2048.0f);
}

static void test_pack_8_truncates_low_bits(void)
{
    /* real=0x123 -> 0x12; low 4 bits dropped */
    uint32_t w = 0x123u;
    uint8_t out[2];
    iq_pack_8(&w, 1, out, NULL);
    CHECK(out[0] == 0x12);
}

static void test_pack_counts_rails(void)
{
    /* real/imag rails: +2047 and -2048.  Clean neighbours do not count. */
    uint32_t words[4] = {
        0x0007FFu,                    /* real +2047, imag 0     -> 1 */
        0x800000u,                    /* real 0, imag -2048     -> 1 */
        0x7FE7FEu,                    /* real/imag +2046        -> 0 */
        0x801801u,                    /* real/imag -2047        -> 0 */
    };
    uint8_t packed[12];
    uint64_t clips = 0;

    iq_pack_12(words, 4, packed, &clips);
    CHECK(clips == 2);

    clips = 0;
    iq_pack_8(words, 4, packed, &clips);
    CHECK(clips == 2);
}

static void test_multi_sample_packing(void)
{
    uint32_t words[3] = {0x000000u, 0xABC123u, 0x800FFFu};
    uint8_t packed[9];
    uint8_t expect[9] = {
        0x00, 0x00, 0x00,
        0x23, 0xC1, 0xAB,
        0xFF, 0x0F, 0x80,
    };
    iq_pack_12(words, 3, packed, NULL);
    CHECK(memcmp(packed, expect, 9) == 0);
}

int main(void)
{
    TEST(test_pack_bytes);
    TEST(test_pack_12_layout);
    TEST(test_pack_12_negative_values);
    TEST(test_pack_8_layout);
    TEST(test_unpack_12_roundtrip);
    TEST(test_unpack_8_expands_to_12bit);
    TEST(test_pack_8_truncates_low_bits);
    TEST(test_pack_counts_rails);
    TEST(test_multi_sample_packing);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
