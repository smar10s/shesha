/* Session model: subscription filtering and mode-change notification.
 *
 * The daemon is a single-tenant radio session.  Multiple clients can attach,
 * but the hardware has one LO, one clock, one ADC pipeline — so mode is
 * session-scoped, not per-client.  Clients opt in to data streams via a
 * subscription bitmask and get notified when the session changes.
 *
 * Policy: last writer wins for mode/config changes.  Notifications make the
 * consequence observable to other clients ("readers can go fuck themselves"
 * — they won't crash, just get data they don't need or silence, and the
 * notification tells them why).
 */
#include "../src/session.h"

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

/* --- subscription filtering --------------------------------------------- */

static void test_default_subscription_receives_nothing(void)
{
    /* A fresh client gets no data until it subscribes.  This prevents the
     * old behaviour: connect, get blasted with PSD/WiFi/FM immediately. */
    session_client_t c;
    session_client_init(&c);
    CHECK(c.data_interest == 0);
    CHECK(!session_client_wants(&c, DATA_PSD));
    CHECK(!session_client_wants(&c, DATA_WIFI));
    CHECK(!session_client_wants(&c, DATA_FM));
}

static void test_subscribe_to_psd_only(void)
{
    session_client_t c;
    session_client_init(&c);
    session_client_subscribe(&c, SESSION_DATA_PSD);
    CHECK(session_client_wants(&c, DATA_PSD));
    CHECK(!session_client_wants(&c, DATA_WIFI));
    CHECK(!session_client_wants(&c, DATA_FM));
}

static void test_subscribe_to_multiple_types(void)
{
    session_client_t c;
    session_client_init(&c);
    session_client_subscribe(&c, SESSION_DATA_PSD | SESSION_DATA_WIFI);
    CHECK(session_client_wants(&c, DATA_PSD));
    CHECK(session_client_wants(&c, DATA_WIFI));
    CHECK(!session_client_wants(&c, DATA_FM));
}

static void test_subscribe_replaces_previous(void)
{
    /* CMD_SUBSCRIBE is a full replacement, not additive.  Simple to reason
     * about: you always know what you'll get. */
    session_client_t c;
    session_client_init(&c);
    session_client_subscribe(&c, SESSION_DATA_PSD | SESSION_DATA_WIFI);
    session_client_subscribe(&c, SESSION_DATA_FM);
    CHECK(!session_client_wants(&c, DATA_PSD));
    CHECK(!session_client_wants(&c, DATA_WIFI));
    CHECK(session_client_wants(&c, DATA_FM));
}

static void test_subscribe_all(void)
{
    session_client_t c;
    session_client_init(&c);
    session_client_subscribe(&c, SESSION_DATA_ALL);
    CHECK(session_client_wants(&c, DATA_PSD));
    CHECK(session_client_wants(&c, DATA_WIFI));
    CHECK(session_client_wants(&c, DATA_FM));
}

/* --- notification tracking ---------------------------------------------- */

static void test_notify_cleared_after_delivery(void)
{
    session_client_t c;
    session_client_init(&c);
    c.notify_pending = true;

    session_client_clear_notify(&c);
    CHECK(c.notify_pending == false);
}

/* --- frame type to subscription bit mapping ----------------------------- */

static void test_frame_type_mapping(void)
{
    /* The mapping from wire DATA_* type bytes to subscription bits must be
     * stable — it's the protocol contract. */
    CHECK(session_data_bit(DATA_PSD) == SESSION_DATA_PSD);
    CHECK(session_data_bit(DATA_WIFI) == SESSION_DATA_WIFI);
    CHECK(session_data_bit(DATA_FM) == SESSION_DATA_FM);
    /* Unknown types map to 0 (no bit set → never delivered). */
    CHECK(session_data_bit(0xFF) == 0);
}

int main(void)
{
    TEST(test_default_subscription_receives_nothing);
    TEST(test_subscribe_to_psd_only);
    TEST(test_subscribe_to_multiple_types);
    TEST(test_subscribe_replaces_previous);
    TEST(test_subscribe_all);
    TEST(test_notify_cleared_after_delivery);
    TEST(test_frame_type_mapping);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
