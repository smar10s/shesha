/* Radio transaction: ordering and rollback.
 *
 * The module is deliberately side-effect free: producer enter/exit and
 * radio setters are callbacks.  These tests assert the exact sequence,
 * including the rollback order, against fakes.
 */
#include "../src/radio_txn.h"
#include "../src/mode_radio.h"

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

/* --- fake hardware ------------------------------------------------------ */

typedef struct {
    char kind;        /* 'X' exit, 'E' enter, 'B' set_bw, 'R' set_sr */
    uint32_t value;   /* producer id, bw, or sr */
} event_t;

static event_t g_events[32];
static int g_nevents;

static int g_fail_bw;
static int g_fail_sr;
static int g_fail_enter_id;   /* producer id whose enter() fails, 0 = none */
static uint32_t g_bw_actual;  /* non-zero: report this instead of the request */
static uint32_t g_sr_actual;

static void record(char kind, uint32_t value)
{
    if (g_nevents < (int)(sizeof(g_events) / sizeof(g_events[0]))) {
        g_events[g_nevents].kind = kind;
        g_events[g_nevents].value = value;
        g_nevents++;
    }
}

static void reset_fakes(void)
{
    g_nevents = 0;
    g_fail_bw = 0;
    g_fail_sr = 0;
    g_fail_enter_id = 0;
    g_bw_actual = 0;
    g_sr_actual = 0;
}

static int enter_producer(int id)
{
    record('E', (uint32_t)id);
    return id == g_fail_enter_id ? -1 : 0;
}

static void exit_producer(int id)
{
    record('X', (uint32_t)id);
}

static int enter_a(void) { return enter_producer(1); }
static void exit_a(void) { exit_producer(1); }
static int enter_b(void) { return enter_producer(2); }
static void exit_b(void) { exit_producer(2); }

static int fake_set_bw(uint32_t bw_hz, uint32_t *actual_hz, void *ctx)
{
    (void)ctx;
    record('B', bw_hz);
    if (g_fail_bw)
        return -1;
    *actual_hz = g_bw_actual ? g_bw_actual : bw_hz;
    return 0;
}

static int fake_set_sr(uint32_t sr_hz, uint32_t *actual_hz, void *ctx)
{
    (void)ctx;
    record('R', sr_hz);
    if (g_fail_sr)
        return -1;
    *actual_hz = g_sr_actual ? g_sr_actual : sr_hz;
    return 0;
}

static const radio_txn_io_t IO = {
    .set_bw = fake_set_bw,
    .set_sr = fake_set_sr,
};

/* Producer A is waterfall-like: rate follows bandwidth. */
static const producer_ops_t PROD_A = {
    .name = "wf",
    .mode = DAEMON_MODE_WATERFALL,
    .enter = enter_a,
    .exit = exit_a,
    .radio = { .bw_min = 200000, .bw_max = 56000000 },
};

/* Producer B is FM-like: both settings are fixed. */
static const producer_ops_t PROD_B = {
    .name = "fm",
    .mode = DAEMON_MODE_FM,
    .enter = enter_b,
    .exit = exit_b,
    .radio = { .bw_fixed = 200000, .sr_fixed = 2100000 },
};

/* Producer C is IQ-like: bandwidth and rate are independent. */
static const producer_ops_t PROD_C = {
    .name = "iq",
    .mode = DAEMON_MODE_IQ,
    .enter = enter_a,
    .exit = exit_a,
    .radio = { .bw_min = 200000, .bw_max = 8000000,
               .sr_independent = true,
               .sr_min = 2100000, .sr_max = 4000000 },
};

static radio_txn_req_t req_entry(const producer_ops_t *prev, bool running,
                                 const producer_ops_t *next, bool force)
{
    radio_txn_req_t r;
    memset(&r, 0, sizeof(r));
    r.kind = RADIO_TXN_ENTRY;
    r.prev = prev;
    r.next = next;
    r.prev_running = running;
    r.force = force;
    r.cur_bw = 20000000;
    r.cur_sr = 20000000;
    return r;
}

static radio_txn_req_t req_bw(const producer_ops_t *p, bool running,
                              uint32_t bw, uint32_t cur_bw, uint32_t cur_sr)
{
    radio_txn_req_t r;
    memset(&r, 0, sizeof(r));
    r.kind = RADIO_TXN_BW;
    r.prev = p;
    r.next = p;
    r.prev_running = running;
    r.value = bw;
    r.cur_bw = cur_bw;
    r.cur_sr = cur_sr;
    return r;
}

static radio_txn_req_t req_sr(const producer_ops_t *p, bool running,
                              uint32_t sr, uint32_t cur_bw, uint32_t cur_sr)
{
    radio_txn_req_t r;
    memset(&r, 0, sizeof(r));
    r.kind = RADIO_TXN_SR;
    r.prev = p;
    r.next = p;
    r.prev_running = running;
    r.value = sr;
    r.cur_bw = cur_bw;
    r.cur_sr = cur_sr;
    return r;
}

static bool events_are(const char *kinds, const uint32_t *values, int n)
{
    if (g_nevents != n)
        return false;
    for (int i = 0; i < n; i++) {
        if (g_events[i].kind != kinds[i])
            return false;
        if (values && g_events[i].value != values[i])
            return false;
    }
    return true;
}

/* --- mode entry --------------------------------------------------------- */

static void test_entry_switches_producer_in_order(void)
{
    reset_fakes();
    radio_txn_req_t r = req_entry(&PROD_A, true, &PROD_B, false);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);

    const uint32_t want[] = { 1, 200000, 2100000, 2 };
    CHECK(events_are("XBRE", want, 4));
    CHECK(out.bw == 200000);
    CHECK(out.sr == 2100000);
}

static void test_entry_noop_for_running_same_producer(void)
{
    reset_fakes();
    radio_txn_req_t r = req_entry(&PROD_A, true, &PROD_A, false);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);
    CHECK(g_nevents == 0);
    CHECK(out.bw == 20000000);
    CHECK(out.sr == 20000000);
}

static void test_entry_force_cycles_same_producer(void)
{
    reset_fakes();
    radio_txn_req_t r = req_entry(&PROD_A, true, &PROD_A, true);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);

    const uint32_t want[] = { 1, 1 };
    CHECK(events_are("XE", want, 2));
}

static void test_entry_enters_when_producer_is_not_running(void)
{
    reset_fakes();
    radio_txn_req_t r = req_entry(&PROD_A, false, &PROD_A, false);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);

    const uint32_t want[] = { 1 };
    CHECK(events_are("E", want, 1));
    CHECK(g_nevents == 1);
}

/* --- sample rate -------------------------------------------------------- */

static void test_sr_change_cycles_then_sets_rate(void)
{
    reset_fakes();
    radio_txn_req_t r = req_sr(&PROD_C, true, 3000000, 5000000, 2500000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);

    const uint32_t want[] = { 1, 3000000, 1 };
    CHECK(events_are("XRE", want, 3));
    CHECK(out.bw == 5000000);
    CHECK(out.sr == 3000000);
}

static void test_sr_same_is_noop(void)
{
    reset_fakes();
    radio_txn_req_t r = req_sr(&PROD_A, true, 20000000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);
    CHECK(g_nevents == 0);
    CHECK(out.sr == 20000000);
}

static void test_resume_sets_rate_and_enters_without_prev(void)
{
    reset_fakes();
    radio_txn_req_t r = req_sr(&PROD_B, false, 2100000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);

    const uint32_t want[] = { 2100000, 2 };
    CHECK(events_are("RE", want, 2));
    CHECK(out.sr == 2100000);
}

/* --- bandwidth ---------------------------------------------------------- */

static void test_bw_derived_mode_cycles_before_rate_change(void)
{
    reset_fakes();
    radio_txn_req_t r = req_bw(&PROD_A, true, 5000000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);

    const uint32_t want[] = { 1, 5000000, 5000000, 1 };
    CHECK(events_are("XBRE", want, 4));
    CHECK(out.bw == 5000000);
    CHECK(out.sr == 5000000);
}

static void test_bw_independent_mode_keeps_producer_running(void)
{
    reset_fakes();
    radio_txn_req_t r = req_bw(&PROD_C, true, 3000000, 5000000, 2500000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);

    const uint32_t want[] = { 3000000 };
    CHECK(events_are("B", want, 1));
    CHECK(out.bw == 3000000);
    CHECK(out.sr == 2500000);
}

static void test_bw_uses_hardware_actual_to_derive_rate(void)
{
    reset_fakes();
    g_bw_actual = 4800000;
    radio_txn_req_t r = req_bw(&PROD_A, true, 5000000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_OK);

    const uint32_t want[] = { 1, 5000000, 4800000, 1 };
    CHECK(events_are("XBRE", want, 4));
    CHECK(out.bw == 4800000);
    CHECK(out.sr == 4800000);
}

static void test_bw_invalid_rejected_before_side_effects(void)
{
    reset_fakes();
    radio_txn_req_t r = req_bw(&PROD_A, true, 100000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_INVALID);
    CHECK(g_nevents == 0);
}

static void test_sr_invalid_rejected_before_side_effects(void)
{
    reset_fakes();
    radio_txn_req_t r = req_sr(&PROD_A, true, 5000000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_INVALID);
    CHECK(g_nevents == 0);
}

/* --- rollback ----------------------------------------------------------- */

static void test_enter_failure_restores_radio_and_previous_producer(void)
{
    reset_fakes();
    g_fail_enter_id = 2;
    radio_txn_req_t r = req_entry(&PROD_A, true, &PROD_B, false);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_ENTER_FAILED);

    const uint32_t want[] = {
        1, 200000, 2100000, 2,          /* forward */
        20000000, 20000000, 1,          /* restore sr, bw, then prev */
    };
    CHECK(events_are("XBRERBE", want, 7));
    CHECK(out.bw == 20000000);
    CHECK(out.sr == 20000000);
}

static void test_sr_failure_restores_bw_and_previous_producer(void)
{
    reset_fakes();
    g_fail_sr = 1;
    radio_txn_req_t r = req_bw(&PROD_A, true, 5000000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_SR_FAILED);

    const uint32_t want[] = { 1, 5000000, 5000000, 20000000, 1 };
    CHECK(events_are("XBRBE", want, 5));
    CHECK(out.bw == 20000000);
    CHECK(out.sr == 20000000);
}

static void test_bw_failure_reenters_previous_producer(void)
{
    reset_fakes();
    g_fail_bw = 1;
    radio_txn_req_t r = req_bw(&PROD_A, true, 5000000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_BW_FAILED);

    const uint32_t want[] = { 1, 5000000, 1 };
    CHECK(events_are("XBE", want, 3));
}

static void test_resume_failure_does_not_invent_a_producer(void)
{
    reset_fakes();
    g_fail_enter_id = 2;
    radio_txn_req_t r = req_sr(&PROD_B, false, 2100000, 20000000, 20000000);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_ENTER_FAILED);

    const uint32_t want[] = { 2100000, 2, 20000000 };
    CHECK(events_are("RER", want, 3));
    CHECK(out.sr == 20000000);
}

static void test_force_failure_reenters_suspended_producer(void)
{
    reset_fakes();
    g_fail_enter_id = 2;
    radio_txn_req_t r = req_entry(&PROD_A, false, &PROD_B, true);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_ENTER_FAILED);

    const uint32_t want[] = {
        200000, 2100000, 2,             /* forward */
        20000000, 20000000, 1,          /* restore, then re-enter prev */
    };
    CHECK(events_are("BRERBE", want, 6));
}

static void test_missing_producer_rejected(void)
{
    reset_fakes();
    radio_txn_req_t r = req_entry(NULL, false, NULL, false);
    radio_txn_result_t out;

    CHECK(radio_txn_run(&r, &IO, NULL, &out) == RADIO_TXN_INVALID);
    CHECK(g_nevents == 0);
}

int main(void)
{
    TEST(test_entry_switches_producer_in_order);
    TEST(test_entry_noop_for_running_same_producer);
    TEST(test_entry_force_cycles_same_producer);
    TEST(test_entry_enters_when_producer_is_not_running);
    TEST(test_sr_change_cycles_then_sets_rate);
    TEST(test_sr_same_is_noop);
    TEST(test_resume_sets_rate_and_enters_without_prev);
    TEST(test_bw_derived_mode_cycles_before_rate_change);
    TEST(test_bw_independent_mode_keeps_producer_running);
    TEST(test_bw_uses_hardware_actual_to_derive_rate);
    TEST(test_bw_invalid_rejected_before_side_effects);
    TEST(test_sr_invalid_rejected_before_side_effects);
    TEST(test_enter_failure_restores_radio_and_previous_producer);
    TEST(test_sr_failure_restores_bw_and_previous_producer);
    TEST(test_bw_failure_reenters_previous_producer);
    TEST(test_resume_failure_does_not_invent_a_producer);
    TEST(test_force_failure_reenters_suspended_producer);
    TEST(test_missing_producer_rejected);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
