/* Producer registry: lookup by mode, FPGA vs SW disambiguation.
 *
 * Stubs for enter/exit so we can link without mode modules.
 */
#include "../src/producer.h"
#include "../src/protocol.h"
#include "../src/mode_radio.h"

#include <stdio.h>
#include <string.h>

/* --- stubs -------------------------------------------------------------- */

int  waterfall_mode_enter(void) { return 0; }
void waterfall_mode_exit(void) {}
bool waterfall_mode_running(void) { return false; }
bool waterfall_mode_active(void) { return false; }
int  wifi_mode_enter(void) { return 0; }
void wifi_mode_exit(void) {}
bool wifi_mode_running(void) { return false; }
bool wifi_mode_active(void) { return false; }
int  wifi_fpga_enter(void) { return 0; }
void wifi_fpga_exit(void) {}
bool wifi_fpga_running(void) { return false; }
bool wifi_fpga_active(void) { return false; }
int  fm_mode_enter(void) { return 0; }
void fm_mode_exit(void) {}
bool fm_mode_running(void) { return false; }
bool fm_mode_active(void) { return false; }
int  iq_mode_enter(void) { return 0; }
void iq_mode_exit(void) {}
bool iq_mode_running(void) { return false; }
bool iq_mode_active(void) { return false; }

void drain_waterfall(const drain_ctx_t *ctx) { (void)ctx; }
void drain_wifi(const drain_ctx_t *ctx) { (void)ctx; }
void drain_fm(const drain_ctx_t *ctx) { (void)ctx; }
void drain_iq(const drain_ctx_t *ctx) { (void)ctx; }

/* --- test harness ------------------------------------------------------- */

static int g_fails;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        g_fails++; \
    } \
} while (0)

#define TEST(name) do { printf("%s\n", #name); name(); } while (0)

/* --- tests -------------------------------------------------------------- */

static void test_lookup_waterfall_sw(void)
{
    const producer_ops_t *p = producer_for_mode(DAEMON_MODE_WATERFALL);
    CHECK(p != NULL);
    CHECK(p->mode == DAEMON_MODE_WATERFALL);
    CHECK(!p->requires_fpga);
    CHECK(strcmp(p->name, "waterfall_sw") == 0);
    CHECK(p->radio.bw_min == 200000);
    CHECK(p->radio.bw_max == 56000000);
    CHECK(p->radio.sr_fixed == 0);  /* sr = bw */
}

static void test_lookup_wifi_sw(void)
{
    const producer_ops_t *p = producer_for_mode(DAEMON_MODE_WIFI);
    CHECK(p != NULL);
    CHECK(p->mode == DAEMON_MODE_WIFI);
    CHECK(!p->requires_fpga);
    CHECK(strcmp(p->name, "wifi_sw") == 0);
    CHECK(p->radio.bw_fixed == 20000000);
    CHECK(p->radio.sr_fixed == 20000000);
}

static void test_lookup_wifi_fpga(void)
{
    const producer_ops_t *p = producer_fpga_for_mode(DAEMON_MODE_WIFI);
    CHECK(p != NULL);
    CHECK(p->mode == DAEMON_MODE_WIFI);
    CHECK(p->requires_fpga);
    CHECK(strcmp(p->name, "wifi_fpga") == 0);
    CHECK(p->radio.bw_fixed == 28000000);
    CHECK(p->radio.sr_fixed == 20000000);
    CHECK(p->radio.agc_fixed == MODE_RADIO_AGC_FAST_ATTACK);
}

static void test_lookup_fm_sw(void)
{
    const producer_ops_t *p = producer_for_mode(DAEMON_MODE_FM);
    CHECK(p != NULL);
    CHECK(p->mode == DAEMON_MODE_FM);
    CHECK(!p->requires_fpga);
    CHECK(strcmp(p->name, "fm_sw") == 0);
    CHECK(p->radio.bw_fixed == 200000);
    CHECK(p->radio.sr_fixed == 2100000);
}

static void test_lookup_iq_sw(void)
{
    const producer_ops_t *p = producer_for_mode(DAEMON_MODE_IQ);
    CHECK(p != NULL);
    CHECK(p->mode == DAEMON_MODE_IQ);
    CHECK(!p->requires_fpga);
    CHECK(strcmp(p->name, "iq_sw") == 0);
    CHECK(p->radio.bw_min == 200000);
    CHECK(p->radio.bw_max == 8000000);
    CHECK(p->radio.sr_independent);
    CHECK(p->radio.sr_min == 2100000);
    CHECK(p->radio.sr_max == 6000000);
}

static void test_no_fpga_for_waterfall(void)
{
    /* No FPGA producer registered for waterfall mode. */
    const producer_ops_t *p = producer_fpga_for_mode(DAEMON_MODE_WATERFALL);
    CHECK(p == NULL);
}

static void test_invalid_mode_returns_null(void)
{
    CHECK(producer_for_mode(99) == NULL);
    CHECK(producer_fpga_for_mode(99) == NULL);
}

static void test_enter_exit_pointers_non_null(void)
{
    for (int i = 0; i < g_producer_count; i++) {
        CHECK(g_producers[i].enter != NULL);
        CHECK(g_producers[i].exit != NULL);
        CHECK(g_producers[i].running != NULL);
        CHECK(g_producers[i].active != NULL);
        CHECK(g_producers[i].drain != NULL);
    }
}

static bool g_ctl_running = false;
static bool ctl_running(void) { return g_ctl_running; }
static bool g_ctl_active = false;
static bool ctl_active(void) { return g_ctl_active; }

static void test_producer_is_running_composes_callback(void)
{
    const producer_ops_t probe = { .running = ctl_running };

    g_ctl_running = false;
    CHECK(!producer_is_running(&probe));

    g_ctl_running = true;
    CHECK(producer_is_running(&probe));

    CHECK(!producer_is_running(NULL));

    const producer_ops_t no_callback = { 0 };
    CHECK(!producer_is_running(&no_callback));
}

/* Configuration is not liveness.  A fabric producer stays configured while
 * an exclusive TX has suspended it; radio commands must use the normal path
 * then, or they talk to a stopped fabric. */
static void test_producer_is_fabric_running_requires_both(void)
{
    const producer_ops_t fpga = {
        .requires_fpga = true,
        .running = ctl_running,
    };
    const producer_ops_t sw = {
        .requires_fpga = false,
        .running = ctl_running,
    };
    const producer_ops_t fpga_no_running = { .requires_fpga = true };

    g_ctl_running = false;
    CHECK(!producer_is_fabric_running(&fpga));

    g_ctl_running = true;
    CHECK(producer_is_fabric_running(&fpga));

    CHECK(!producer_is_fabric_running(&sw));
    CHECK(!producer_is_fabric_running(&fpga_no_running));
    CHECK(!producer_is_fabric_running(NULL));
}

/* A suspended producer keeps its configuration but no longer owns the
 * radio, so its AGC/gain policy must not block normal commands. */
static void test_producer_active_radio_drops_suspended_policy(void)
{
    const producer_ops_t probe = {
        .active = ctl_active,
        .radio = { .agc_fixed = MODE_RADIO_AGC_FAST_ATTACK },
    };

    g_ctl_active = false;
    CHECK(producer_active_radio(&probe) == NULL);

    g_ctl_active = true;
    CHECK(producer_active_radio(&probe) == &probe.radio);

    CHECK(producer_active_radio(NULL) == NULL);
}

static void test_producer_is_active_composes_callback(void)
{
    const producer_ops_t probe = { .active = ctl_active };

    g_ctl_active = false;
    CHECK(!producer_is_active(&probe));

    g_ctl_active = true;
    CHECK(producer_is_active(&probe));

    CHECK(!producer_is_active(NULL));

    const producer_ops_t no_callback = { 0 };
    CHECK(!producer_is_active(&no_callback));
}

static void test_registry_producers_report_not_running_by_default(void)
{
    for (int i = 0; i < g_producer_count; i++)
        CHECK(!producer_is_running(&g_producers[i]));
}

static void test_registry_producers_report_not_active_by_default(void)
{
    for (int i = 0; i < g_producer_count; i++)
        CHECK(!producer_is_active(&g_producers[i]));
}

static void test_producer_count(void)
{
    CHECK(g_producer_count == 5);
}

static void test_mode_active_fpga_variant(void)
{
    /* The CMD_SET_MODE no-op guard: a redundant mode set must not tear
     * down the running producer.  wifi_fpga is "in" WIFI mode even though
     * it is not the SW variant. */
    const producer_ops_t *fpga = producer_fpga_for_mode(DAEMON_MODE_WIFI);
    CHECK(fpga != NULL);
    CHECK(producer_mode_active(fpga, DAEMON_MODE_WIFI));
}

static void test_mode_active_sw_variant(void)
{
    const producer_ops_t *sw = producer_for_mode(DAEMON_MODE_WIFI);
    CHECK(sw != NULL);
    CHECK(producer_mode_active(sw, DAEMON_MODE_WIFI));
}

static void test_mode_active_different_mode(void)
{
    const producer_ops_t *fpga = producer_fpga_for_mode(DAEMON_MODE_WIFI);
    CHECK(fpga != NULL);
    CHECK(!producer_mode_active(fpga, DAEMON_MODE_WATERFALL));
    CHECK(!producer_mode_active(fpga, DAEMON_MODE_FM));

    const producer_ops_t *waterfall = producer_for_mode(DAEMON_MODE_WATERFALL);
    CHECK(waterfall != NULL);
    CHECK(!producer_mode_active(waterfall, DAEMON_MODE_WIFI));
}

static void test_mode_active_no_producer(void)
{
    CHECK(!producer_mode_active(NULL, DAEMON_MODE_WIFI));
}

int main(void)
{
    TEST(test_lookup_waterfall_sw);
    TEST(test_lookup_wifi_sw);
    TEST(test_lookup_wifi_fpga);
    TEST(test_lookup_fm_sw);
    TEST(test_lookup_iq_sw);
    TEST(test_no_fpga_for_waterfall);
    TEST(test_invalid_mode_returns_null);
    TEST(test_enter_exit_pointers_non_null);
    TEST(test_producer_is_running_composes_callback);
    TEST(test_producer_is_fabric_running_requires_both);
    TEST(test_producer_active_radio_drops_suspended_policy);
    TEST(test_producer_is_active_composes_callback);
    TEST(test_registry_producers_report_not_running_by_default);
    TEST(test_registry_producers_report_not_active_by_default);
    TEST(test_producer_count);
    TEST(test_mode_active_fpga_variant);
    TEST(test_mode_active_sw_variant);
    TEST(test_mode_active_different_mode);
    TEST(test_mode_active_no_producer);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
