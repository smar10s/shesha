#include "../src/adc_clip.h"

#include <stdio.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

static void test_raw12_boundaries(void)
{
    CHECK(adc_clip_is_raw12(0x800) == 1);   /* -2048 */
    CHECK(adc_clip_is_raw12(0x7FF) == 1);   /* +2047 */
    CHECK(adc_clip_is_raw12(0x7FE) == 0);   /* +2046 */
    CHECK(adc_clip_is_raw12(0x801) == 0);   /* -2047 */
}

static void test_float_boundaries(void)
{
    CHECK(adc_clip_is_float(2047.0f / 2048.0f) == 1);
    CHECK(adc_clip_is_float(-1.0f) == 1);
    CHECK(adc_clip_is_float(2046.0f / 2048.0f) == 0);
    CHECK(adc_clip_is_float(-2047.0f / 2048.0f) == 0);
}

static void test_accumulator(void)
{
    adc_clip_add(3, 100);
    adc_clip_add(5, 200);

    uint64_t clips = 0, samples = 0;
    adc_clip_get(&clips, &samples);
    CHECK(clips == 8);
    CHECK(samples == 300);
}

int main(void)
{
    TEST(test_raw12_boundaries);
    TEST(test_float_boundaries);
    TEST(test_accumulator);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
