/* Host tests for the fabric WiFi frequency -> channel derivation.
 *
 * The daemon stores the AD9361 LO readback, which is often a few Hz below
 * the requested channel centre (e.g. 2436.999998 MHz for 2437 MHz).  The
 * derivation must round to the nearest centre, not truncate to whole MHz,
 * or the previous 5 MHz channel is selected.
 */
#include "../src/wifi_fpga.h"

#include <stdio.h>
#include <stdint.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

static void test_24ghz_centres(void)
{
    CHECK(wifi_fpga_freq_to_channel(2412000000ULL) == 1);
    CHECK(wifi_fpga_freq_to_channel(2432000000ULL) == 5);
    CHECK(wifi_fpga_freq_to_channel(2437000000ULL) == 6);
    CHECK(wifi_fpga_freq_to_channel(2452000000ULL) == 9);
    CHECK(wifi_fpga_freq_to_channel(2457000000ULL) == 10);
    CHECK(wifi_fpga_freq_to_channel(2472000000ULL) == 13);
    CHECK(wifi_fpga_freq_to_channel(2484000000ULL) == 14);
}

static void test_24ghz_readback_below_centre(void)
{
    /* What the AD9361 actually reports for these requests. */
    CHECK(wifi_fpga_freq_to_channel(2421999998ULL) == 3);
    CHECK(wifi_fpga_freq_to_channel(2436999998ULL) == 6);
    CHECK(wifi_fpga_freq_to_channel(2441999998ULL) == 7);
    CHECK(wifi_fpga_freq_to_channel(2456999998ULL) == 10);
    CHECK(wifi_fpga_freq_to_channel(2461999998ULL) == 11);
}

static void test_24ghz_ch14_gap(void)
{
    /* ch13 is 2472 and ch14 is 2484; the midpoint is 2478. */
    CHECK(wifi_fpga_freq_to_channel(2477000000ULL) == 13);
    CHECK(wifi_fpga_freq_to_channel(2478000000ULL) == 14);
}

static void test_5ghz_centres(void)
{
    CHECK(wifi_fpga_freq_to_channel(5180000000ULL) == 36);
    CHECK(wifi_fpga_freq_to_channel(5745000000ULL) == 149);
    CHECK(wifi_fpga_freq_to_channel(5744999998ULL) == 149);
    CHECK(wifi_fpga_freq_to_channel(5825000000ULL) == 165);
}

static void test_outside_bands_falls_back(void)
{
    CHECK(wifi_fpga_freq_to_channel(2402000000ULL) == 1);
    CHECK(wifi_fpga_freq_to_channel(100000000ULL) == 1);
}

int main(void)
{
    TEST(test_24ghz_centres);
    TEST(test_24ghz_readback_below_centre);
    TEST(test_24ghz_ch14_gap);
    TEST(test_5ghz_centres);
    TEST(test_outside_bands_falls_back);

    if (g_fails) {
        printf("%d check(s) failed\n", g_fails);
        return 1;
    }
    printf("all checks passed\n");
    return 0;
}
