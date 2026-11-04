/*
 * fir_init argument validation.  A zero tap count, a count past the fixed
 * history buffer, or a zero decimation would all produce a stage that either
 * reads out of bounds or divides by zero on the first push.
 */
#include "../src/fir.h"

#include <stdio.h>
#include <string.h>

static int fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

static void test_rejects_zero_taps(void)
{
    fir_t f;
    const float taps[1] = { 1.0f };

    CHECK(fir_init(&f, taps, 0, 1) != 0);
}

static void test_rejects_too_many_taps(void)
{
    fir_t f;
    static float taps[FIR_MAX_TAPS + 1];

    for (size_t i = 0; i < sizeof(taps) / sizeof(taps[0]); i++)
        taps[i] = 1.0f;

    CHECK(fir_init(&f, taps, FIR_MAX_TAPS + 1, 1) != 0);
}

static void test_rejects_zero_decimation(void)
{
    fir_t f;
    const float taps[1] = { 1.0f };

    CHECK(fir_init(&f, taps, 1, 0) != 0);
}

static void test_accepts_a_symmetric_filter(void)
{
    fir_t f;
    const float taps[5] = { 0.1f, 0.2f, 0.4f, 0.2f, 0.1f };

    CHECK(fir_init(&f, taps, 5, 2) == 0);
    CHECK(f.n_taps == 5);
    CHECK(f.decim == 2);
}

static void test_rejects_asymmetric_taps(void)
{
    fir_t f;
    const float taps[5] = { 0.1f, 0.2f, 0.4f, 0.3f, 0.1f };

    CHECK(fir_init(&f, taps, 5, 1) != 0);
}

int main(void)
{
    TEST(test_rejects_zero_taps);
    TEST(test_rejects_too_many_taps);
    TEST(test_rejects_zero_decimation);
    TEST(test_accepts_a_symmetric_filter);
    TEST(test_rejects_asymmetric_taps);

    if (fails) {
        printf("%d check(s) failed\n", fails);
        return 1;
    }
    printf("all checks passed\n");
    return 0;
}
