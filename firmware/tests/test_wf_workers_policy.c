/* The worker-count policy: a second PSD producer core is worth its duty only
 * above fs_max.  At or below it one core already transforms every sample, so
 * the extra worker only waits -- and waiting polls the DMA write pointer,
 * which costs 14-17 points of duty for no coverage (docs/psd-waterfall.md,
 * "The second core").
 *
 * The decision is pure arithmetic over a measured table, so it is tested on
 * the host.  A boundary mistake here would silently spend a core for nothing,
 * which no on-target number would make obvious.
 */
#include "../src/wf_workers_policy.h"

#include <stdio.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* The tabulated fs_max values, as Hz, so the boundary cases can name them. */
#define MSPS(x) ((uint64_t)((x) * 1000000.0))

static void test_table_matches_the_measured_curves(void)
{
    /* docs/psd-waterfall.md "Per-frame compute, C(N)": fs_max = N/C. */
    CHECK(wf_workers_fs_max_hz(256)  == 7300000);
    CHECK(wf_workers_fs_max_hz(512)  == 7270000);
    CHECK(wf_workers_fs_max_hz(1024) == 6950000);
    CHECK(wf_workers_fs_max_hz(2048) == 6720000);
    CHECK(wf_workers_fs_max_hz(4096) == 5340000);
    CHECK(wf_workers_fs_max_hz(8192) == 4850000);
}

static void test_below_fs_max_clamps_to_one(void)
{
    CHECK(wf_workers_effective(2, MSPS(2.1), 4096) == 1);
    CHECK(wf_workers_effective(2, MSPS(5.0), 4096) == 1);
}

static void test_the_boundary_is_inclusive(void)
{
    /* At exactly fs_max the second core is measured 1.00x gain, so it is
     * clamped; one hertz above, it is allowed. */
    CHECK(wf_workers_effective(2, MSPS(5.34), 4096) == 1);
    CHECK(wf_workers_effective(2, MSPS(5.34) + 1, 4096) == 2);
}

static void test_above_fs_max_allows_the_second(void)
{
    CHECK(wf_workers_effective(2, MSPS(10), 4096) == 2);
    CHECK(wf_workers_effective(2, MSPS(6.0), 8192) == 2);
}

static void test_never_overrides_the_user_upward(void)
{
    /* Asking for one worker at any rate stays one. */
    CHECK(wf_workers_effective(1, MSPS(20), 4096) == 1);
}

static void test_request_is_clamped_to_the_core_count(void)
{
    CHECK(wf_workers_effective(0, MSPS(20), 4096) == 1);
    CHECK(wf_workers_effective(-3, MSPS(20), 4096) == 1);
    CHECK(wf_workers_effective(3, MSPS(20), 4096) == WF_MAX_WORKERS);
    CHECK(wf_workers_effective(100, MSPS(20), 4096) == WF_MAX_WORKERS);
}

static void test_unknown_fft_size_falls_back_to_the_nearest(void)
{
    /* The daemon only ever passes a power of two in [256, 8192], so this is
     * robustness, not a real path.  Nearest tabulated N is the least
     * surprising choice. */
    /* 3000 is nearer 2048 (fs_max 6.72) than 4096 (5.34). */
    CHECK(wf_workers_effective(2, MSPS(6.5), 3000) == 1);
    CHECK(wf_workers_effective(2, MSPS(7.0), 3000) == 2);
    /* 9000 is nearer 8192 (4.85) than 4096. */
    CHECK(wf_workers_effective(2, MSPS(4.0), 9000) == 1);
    CHECK(wf_workers_effective(2, MSPS(5.0), 9000) == 2);
}

int main(void)
{
    TEST(test_table_matches_the_measured_curves);
    TEST(test_below_fs_max_clamps_to_one);
    TEST(test_the_boundary_is_inclusive);
    TEST(test_above_fs_max_allows_the_second);
    TEST(test_never_overrides_the_user_upward);
    TEST(test_request_is_clamped_to_the_core_count);
    TEST(test_unknown_fft_size_falls_back_to_the_nearest);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
