/* TX session: lifecycle, ownership, drain, and clock claim.
 *
 * The hardware side effects stay in main.c; these tests pin the decisions
 * that used to be scattered across flags.
 */
#include "../src/tx_session.h"
#include "../src/protocol.h"

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

static void test_initial_state_is_idle(void)
{
    tx_session_t st;
    tx_session_init(&st);

    CHECK(!tx_session_active(&st));
    CHECK(!tx_session_clock_held(&st));
    CHECK(st.proto == TX_PROTO_NONE);
    CHECK(st.owner_id == 0);
    CHECK(st.saved_mode == -1);
}

static void test_short_tx_has_no_owner(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_start(&st, TX_PROTO_WIFI, 0, 1, 0, 42, false);

    CHECK(tx_session_active(&st));
    CHECK(st.state == TX_STATE_SHORT);
    CHECK(st.owner_id == 0);
    CHECK(st.proto == TX_PROTO_WIFI);
    CHECK(st.repeat == 1);
}

static void test_open_ended_tx_records_owner(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_start(&st, TX_PROTO_FM, TX_FLAG_STREAM, 0, 0, 42, true);

    CHECK(st.state == TX_STATE_STREAM_OPEN);
    CHECK(st.owner_id == 42);
}

static void test_new_short_tx_clears_stale_owner(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_start(&st, TX_PROTO_FM, TX_FLAG_STREAM, 0, 0, 42, true);

    tx_session_start(&st, TX_PROTO_WIFI, 0, 1, 0, 7, false);
    CHECK(st.state == TX_STATE_SHORT);
    CHECK(st.owner_id == 0);

    tx_session_start(&st, TX_PROTO_FM, TX_FLAG_STREAM, 0, 0, 7, true);
    CHECK(st.owner_id == 7);
}

static void test_stream_end_clears_owner_and_drains(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_start(&st, TX_PROTO_IQ, 0, 0, 0, 42, true);

    tx_session_stream_end(&st);
    CHECK(st.state == TX_STATE_STREAM_DRAINING);
    CHECK(st.owner_id == 0);
    CHECK(tx_session_active(&st));   /* still draining */
}

static void test_stream_end_ignored_for_short_tx(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_start(&st, TX_PROTO_WIFI, 0, 1, 0, 0, false);

    tx_session_stream_end(&st);
    CHECK(st.state == TX_STATE_SHORT);
}

static void test_complete_clears_every_tx_field(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_start(&st, TX_PROTO_FM, TX_FLAG_STREAM | TX_FLAG_CYCLIC,
                     5, 1000, 42, true);
    tx_session_complete(&st);

    CHECK(!tx_session_active(&st));
    CHECK(st.state == TX_STATE_IDLE);
    CHECK(st.proto == TX_PROTO_NONE);
    CHECK(st.flags == 0);
    CHECK(st.repeat == 0);
    CHECK(st.interval_us == 0);
    CHECK(st.owner_id == 0);
}

/* --- sample clock claim ------------------------------------------------ */

static void test_rate_conflict_only_when_rates_differ(void)
{
    CHECK(!tx_session_rate_needs_suspend(2100000, 2100000));
    CHECK(tx_session_rate_needs_suspend(2100000, 20000000));
    CHECK(tx_session_rate_needs_suspend(20000000, 2100000));
}

static void test_hold_clock_saves_state(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_hold_clock(&st, DAEMON_MODE_WIFI, 20000000, true);

    CHECK(tx_session_clock_held(&st));
    CHECK(st.saved_mode == DAEMON_MODE_WIFI);
    CHECK(st.saved_rate == 20000000);
    CHECK(st.saved_fpga);
}

static void test_hold_clock_is_idempotent(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_hold_clock(&st, DAEMON_MODE_WIFI, 20000000, true);
    tx_session_hold_clock(&st, DAEMON_MODE_FM, 2100000, false);

    CHECK(st.saved_mode == DAEMON_MODE_WIFI);
    CHECK(st.saved_rate == 20000000);
    CHECK(st.saved_fpga);
}

static void test_release_clock_returns_and_clears(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_hold_clock(&st, DAEMON_MODE_WIFI, 20000000, true);

    tx_clock_resume_t r = tx_session_release_clock(&st);
    CHECK(r.mode == DAEMON_MODE_WIFI);
    CHECK(r.rate == 20000000);
    CHECK(r.fpga);
    CHECK(!tx_session_clock_held(&st));
    CHECK(st.saved_mode == -1);

    r = tx_session_release_clock(&st);
    CHECK(r.mode == -1);
}

static void test_cancel_clock_drops_state_without_restoring(void)
{
    tx_session_t st;
    tx_session_init(&st);
    tx_session_hold_clock(&st, DAEMON_MODE_WIFI, 20000000, true);

    tx_session_cancel_clock(&st);
    CHECK(!tx_session_clock_held(&st));
    CHECK(st.saved_mode == -1);
    CHECK(tx_session_release_clock(&st).mode == -1);
}

int main(void)
{
    TEST(test_initial_state_is_idle);
    TEST(test_short_tx_has_no_owner);
    TEST(test_open_ended_tx_records_owner);
    TEST(test_new_short_tx_clears_stale_owner);
    TEST(test_stream_end_clears_owner_and_drains);
    TEST(test_stream_end_ignored_for_short_tx);
    TEST(test_complete_clears_every_tx_field);
    TEST(test_rate_conflict_only_when_rates_differ);
    TEST(test_hold_clock_saves_state);
    TEST(test_hold_clock_is_idempotent);
    TEST(test_release_clock_returns_and_clears);
    TEST(test_cancel_clock_drops_state_without_restoring);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
