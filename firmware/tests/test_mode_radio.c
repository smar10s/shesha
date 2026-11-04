#include "../src/mode_radio.h"
#include "../src/iq_tx.h"
#include <stdio.h>

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* Mode constraint definitions (mirrors what producer.c will declare) */
static const mode_radio_req_t WATERFALL = { .bw_min = 200000, .bw_max = 56000000 };
static const mode_radio_req_t WIFI_SW   = { .bw_fixed = 20000000, .sr_fixed = 20000000 };
static const mode_radio_req_t WIFI_FPGA = { .bw_fixed = 28000000, .sr_fixed = 20000000,
                                            .agc_fixed = MODE_RADIO_AGC_FAST_ATTACK };
static const mode_radio_req_t FM_RX     = { .bw_fixed = 200000, .sr_fixed = 2100000 };

/* IQ mode: bw and sr are independent (prototyping mode).  sr floor is
 * 2.1 MHz: the AD9361 driver's minimum sampling rate is 25 MHz / 12 =
 * 2.0833 MHz, and 2.1 MHz is the rate FM already proves. */
static const mode_radio_req_t IQ = {
    .bw_min = 200000, .bw_max = 8000000,
    .sr_independent = true, .sr_min = 2100000, .sr_max = 6000000,
};

/* --- bw validation --- */

static void test_waterfall_accepts_any_bw_in_range(void)
{
    CHECK(mode_radio_bw_valid(&WATERFALL, 200000));
    CHECK(mode_radio_bw_valid(&WATERFALL, 20000000));
    CHECK(mode_radio_bw_valid(&WATERFALL, 56000000));
    CHECK(!mode_radio_bw_valid(&WATERFALL, 100000));   /* below min */
    CHECK(!mode_radio_bw_valid(&WATERFALL, 60000000)); /* above max */
}

static void test_wifi_sw_only_accepts_20mhz(void)
{
    CHECK(mode_radio_bw_valid(&WIFI_SW, 20000000));
    CHECK(!mode_radio_bw_valid(&WIFI_SW, 28000000));
    CHECK(!mode_radio_bw_valid(&WIFI_SW, 56000000));
}

static void test_wifi_fpga_only_accepts_28mhz(void)
{
    CHECK(mode_radio_bw_valid(&WIFI_FPGA, 28000000));
    CHECK(!mode_radio_bw_valid(&WIFI_FPGA, 20000000));
}

static void test_fm_only_accepts_200khz(void)
{
    CHECK(mode_radio_bw_valid(&FM_RX, 200000));
    CHECK(!mode_radio_bw_valid(&FM_RX, 20000000));
}

/* --- sr derivation --- */

static void test_waterfall_sr_equals_bw(void)
{
    CHECK(mode_radio_derive_sr(&WATERFALL, 56000000) == 56000000);
    CHECK(mode_radio_derive_sr(&WATERFALL, 5000000) == 5000000);
}

static void test_waterfall_sr_clamps_to_hw_floor(void)
{
    /* BW below the AD9361 minimum sample rate: derive_sr must clamp up. */
    CHECK(mode_radio_derive_sr(&WATERFALL, 1000000) == SR_HW_MIN);
    CHECK(mode_radio_derive_sr(&WATERFALL, 200000) == SR_HW_MIN);
    /* At the floor: no clamping. */
    CHECK(mode_radio_derive_sr(&WATERFALL, SR_HW_MIN) == SR_HW_MIN);
    /* Above the floor: sr = bw as before. */
    CHECK(mode_radio_derive_sr(&WATERFALL, 3000000) == 3000000);
}

static void test_waterfall_bw_sr_clamps_to_hw_floor(void)
{
    /* CMD_SET_BW with a low bw should still produce a valid sr. */
    CHECK(mode_radio_bw_sr(&WATERFALL, 20000000, 1000000) == SR_HW_MIN);
    CHECK(mode_radio_bw_sr(&WATERFALL, 20000000, 200000) == SR_HW_MIN);
    CHECK(mode_radio_bw_sr(&WATERFALL, 20000000, 5000000) == 5000000);
}

static void test_waterfall_entry_sr_clamps_to_hw_floor(void)
{
    /* Entering waterfall with a low bw should settle at the floor. */
    CHECK(mode_radio_entry_sr(&WATERFALL, 20000000, 1000000) == SR_HW_MIN);
    CHECK(mode_radio_entry_sr(&WATERFALL, 20000000, 200000) == SR_HW_MIN);
}

static void test_wifi_sw_sr_always_20msps(void)
{
    CHECK(mode_radio_derive_sr(&WIFI_SW, 20000000) == 20000000);
}

static void test_wifi_fpga_sr_always_20msps(void)
{
    CHECK(mode_radio_derive_sr(&WIFI_FPGA, 28000000) == 20000000);
}

static void test_fm_sr_always_2_1msps(void)
{
    CHECK(mode_radio_derive_sr(&FM_RX, 200000) == 2100000);
}

/* --- entry bw --- */

static void test_waterfall_keeps_valid_bw(void)
{
    CHECK(mode_radio_entry_bw(&WATERFALL, 56000000) == 0);
    CHECK(mode_radio_entry_bw(&WATERFALL, 20000000) == 0);
}

static void test_waterfall_clamps_invalid_bw(void)
{
    CHECK(mode_radio_entry_bw(&WATERFALL, 100000) == 56000000);
    CHECK(mode_radio_entry_bw(&WATERFALL, 60000000) == 56000000);
}

static void test_wifi_sw_forces_20mhz(void)
{
    CHECK(mode_radio_entry_bw(&WIFI_SW, 56000000) == 20000000);
    CHECK(mode_radio_entry_bw(&WIFI_SW, 20000000) == 0);  /* already valid */
}

static void test_wifi_fpga_forces_28mhz(void)
{
    CHECK(mode_radio_entry_bw(&WIFI_FPGA, 20000000) == 28000000);
    CHECK(mode_radio_entry_bw(&WIFI_FPGA, 28000000) == 0);  /* already valid */
}

static void test_fm_forces_200khz(void)
{
    CHECK(mode_radio_entry_bw(&FM_RX, 20000000) == 200000);
    CHECK(mode_radio_entry_bw(&FM_RX, 200000) == 0);  /* already valid */
}

/* --- sr validation (for explicit CMD_SET_SAMPLE_RATE) --- */

static void test_waterfall_rejects_sr_not_equal_bw(void)
{
    CHECK(mode_radio_sr_valid(&WATERFALL, 56000000, 56000000));
    CHECK(!mode_radio_sr_valid(&WATERFALL, 56000000, 20000000));
}

static void test_wifi_rejects_sr_not_20msps(void)
{
    CHECK(mode_radio_sr_valid(&WIFI_SW, 20000000, 20000000));
    CHECK(!mode_radio_sr_valid(&WIFI_SW, 20000000, 56000000));
}

/* --- IQ: independent bw/sr policy --- */

static void test_iq_accepts_bw_range(void)
{
    CHECK(mode_radio_bw_valid(&IQ, 200000));
    CHECK(mode_radio_bw_valid(&IQ, 8000000));
    CHECK(!mode_radio_bw_valid(&IQ, 100000));
    CHECK(!mode_radio_bw_valid(&IQ, 10000000));
}

static void test_iq_entry_keeps_in_range_sr(void)
{
    CHECK(mode_radio_entry_sr(&IQ, 2100000, 500000) == 2100000);
    CHECK(mode_radio_entry_sr(&IQ, 4000000, 500000) == 4000000);
    CHECK(mode_radio_entry_sr(&IQ, 2500000, 500000) == 2500000);
}

static void test_iq_entry_clamps_out_of_range_sr(void)
{
    CHECK(mode_radio_entry_sr(&IQ, 56000000, 500000) == 6000000);
    CHECK(mode_radio_entry_sr(&IQ, 1000000, 500000) == 2100000);
}

static void test_iq_bw_change_leaves_sr_alone(void)
{
    CHECK(mode_radio_bw_sr(&IQ, 2500000, 200000) == 2500000);
    CHECK(mode_radio_bw_sr(&IQ, 4000000, 8000000) == 4000000);
}

static void test_iq_sr_valid_in_range_only(void)
{
    CHECK(mode_radio_sr_valid(&IQ, 500000, 2100000));
    CHECK(mode_radio_sr_valid(&IQ, 500000, 4000000));
    CHECK(!mode_radio_sr_valid(&IQ, 500000, 2000000));
    CHECK(!mode_radio_sr_valid(&IQ, 500000, 8000000));
}

static void test_derived_modes_unchanged_by_new_queries(void)
{
    CHECK(mode_radio_entry_sr(&WATERFALL, 56000000, 56000000) == 56000000);
    CHECK(mode_radio_entry_sr(&WIFI_SW, 30000000, 20000000) == 20000000);
    CHECK(mode_radio_entry_sr(&FM_RX, 3000000, 200000) == 2100000);
    CHECK(mode_radio_bw_sr(&WATERFALL, 56000000, 2000000) == SR_HW_MIN);
}

/* --- agc constraint --- */

static void test_wifi_fpga_locks_agc_to_fast_attack(void)
{
    CHECK(mode_radio_agc_valid(&WIFI_FPGA, MODE_RADIO_AGC_FAST_ATTACK));
    CHECK(!mode_radio_agc_valid(&WIFI_FPGA, MODE_RADIO_AGC_MANUAL));
    CHECK(!mode_radio_agc_valid(&WIFI_FPGA, MODE_RADIO_AGC_SLOW_ATTACK));
}

static void test_unconstrained_modes_accept_any_agc(void)
{
    CHECK(mode_radio_agc_valid(&WATERFALL, MODE_RADIO_AGC_MANUAL));
    CHECK(mode_radio_agc_valid(&WATERFALL, MODE_RADIO_AGC_FAST_ATTACK));
    CHECK(mode_radio_agc_valid(&WATERFALL, MODE_RADIO_AGC_SLOW_ATTACK));
    CHECK(mode_radio_agc_valid(&WIFI_SW, MODE_RADIO_AGC_MANUAL));
    CHECK(mode_radio_agc_valid(&FM_RX, MODE_RADIO_AGC_SLOW_ATTACK));
    CHECK(mode_radio_agc_valid(&IQ, MODE_RADIO_AGC_FAST_ATTACK));
}

/* --- IQ duplex budget --- */

static void test_iq_duplex_budget(void)
{
    /* TX alone is bounded only by the policy rate cap. */
    CHECK(iq_tx_duplex_fits(6000000, 8, 0, 8));
    CHECK(iq_tx_duplex_fits(6000000, 12, 0, 12));

    /* 8-bit 4+4 is the measured duplex point. */
    CHECK(iq_tx_duplex_fits(4000000, 8, 1, 8));
    CHECK(!iq_tx_duplex_fits(4250000, 8, 1, 8));

    /* 12-bit has less headroom at the same rate. */
    CHECK(!iq_tx_duplex_fits(4000000, 12, 1, 12));

    /* The rate is shared; 6 MSPS with RX subscribed leaves no TX room. */
    CHECK(!iq_tx_duplex_fits(6000000, 8, 1, 8));

    /* Every RX subscriber multiplies the link load. */
    CHECK(iq_tx_duplex_fits(2600000, 8, 2, 8));
    CHECK(!iq_tx_duplex_fits(2700000, 8, 2, 8));

    /* RX and TX widths can differ; the budget uses both. */
    CHECK(iq_tx_duplex_fits(3000000, 8, 1, 12));
    CHECK(!iq_tx_duplex_fits(3500000, 8, 1, 12));
}

int main(void)
{
    TEST(test_waterfall_accepts_any_bw_in_range);
    TEST(test_wifi_sw_only_accepts_20mhz);
    TEST(test_wifi_fpga_only_accepts_28mhz);
    TEST(test_fm_only_accepts_200khz);
    TEST(test_waterfall_sr_equals_bw);
    TEST(test_waterfall_sr_clamps_to_hw_floor);
    TEST(test_waterfall_bw_sr_clamps_to_hw_floor);
    TEST(test_waterfall_entry_sr_clamps_to_hw_floor);
    TEST(test_wifi_sw_sr_always_20msps);
    TEST(test_wifi_fpga_sr_always_20msps);
    TEST(test_fm_sr_always_2_1msps);
    TEST(test_waterfall_keeps_valid_bw);
    TEST(test_waterfall_clamps_invalid_bw);
    TEST(test_wifi_sw_forces_20mhz);
    TEST(test_wifi_fpga_forces_28mhz);
    TEST(test_fm_forces_200khz);
    TEST(test_waterfall_rejects_sr_not_equal_bw);
    TEST(test_wifi_rejects_sr_not_20msps);
    TEST(test_iq_accepts_bw_range);
    TEST(test_iq_entry_keeps_in_range_sr);
    TEST(test_iq_entry_clamps_out_of_range_sr);
    TEST(test_iq_bw_change_leaves_sr_alone);
    TEST(test_iq_sr_valid_in_range_only);
    TEST(test_derived_modes_unchanged_by_new_queries);
    TEST(test_wifi_fpga_locks_agc_to_fast_attack);
    TEST(test_unconstrained_modes_accept_any_agc);
    TEST(test_iq_duplex_budget);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
