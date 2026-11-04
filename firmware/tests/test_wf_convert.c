#include "../src/wf_convert.h"
#include "hal.h"

#include <math.h>
#include <stdio.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

static void test_converts_and_windows(void)
{
    uint32_t words[2] = {IQ_PACK(100, -100), IQ_PACK(2047, -2048)};
    float win[2] = {1.0f, 0.5f};
    float re[2], im[2];
    uint64_t clips = 0;

    wf_convert_windowed(words, 2, win, re, im, &clips);

    CHECK(re[0] == 100.0f / 2048.0f);
    CHECK(im[0] == -100.0f / 2048.0f);
    CHECK(re[1] == 2047.0f / 2048.0f * 0.5f);
    CHECK(im[1] == -1.0f * 0.5f);
}

static void test_counts_rails_only(void)
{
    uint32_t words[3] = {
        IQ_PACK(2047, 0),
        IQ_PACK(0, -2048),
        IQ_PACK(100, -100),
    };
    float win[3] = {1.0f, 1.0f, 1.0f};
    float re[3], im[3];
    uint64_t clips = 0;

    wf_convert_windowed(words, 3, win, re, im, &clips);

    CHECK(clips == 2);
}

static void test_clip_count_accumulates_across_a_wrap(void)
{
    uint32_t words[2] = {IQ_PACK(2047, 0), IQ_PACK(0, 0)};
    float win[2] = {1.0f, 1.0f};
    float re[2], im[2];
    uint64_t clips = 0;

    wf_convert_windowed(words, 1, win, re, im, &clips);
    wf_convert_windowed(words + 1, 1, win + 1, re + 1, im + 1, &clips);

    CHECK(clips == 1);
}

static void test_counting_is_optional(void)
{
    uint32_t words[1] = {IQ_PACK(2047, -2048)};
    float win[1] = {1.0f};
    float re[1], im[1];

    wf_convert_windowed(words, 1, win, re, im, NULL);
    CHECK(re[0] == 2047.0f / 2048.0f);
}

int main(void)
{
    TEST(test_converts_and_windows);
    TEST(test_counts_rails_only);
    TEST(test_clip_count_accumulates_across_a_wrap);
    TEST(test_counting_is_optional);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
