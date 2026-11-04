/* DATA_WIFI wire layout.
 *
 * The receive frequency is stamped when the frame is captured, not when the
 * drain builds the wire frame.  It sits in the fixed header so the client can
 * label frames received while scanning without relying on its own tuning
 * state, which lags a retune.  These checks pin the offsets the client
 * parser uses.
 */
#include "../src/protocol.h"
#include "../src/wifi_protocol.h"

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

static uint16_t u16_at(const uint8_t *buf, size_t off)
{
    uint16_t v;
    memcpy(&v, buf + FRAME_HEADER_SIZE + off, sizeof(v));
    return v;
}

static double double_at(const uint8_t *buf, size_t off)
{
    double v;
    memcpy(&v, buf + FRAME_HEADER_SIZE + off, sizeof(v));
    return v;
}

static float float_at(const uint8_t *buf, size_t off)
{
    float v;
    memcpy(&v, buf + FRAME_HEADER_SIZE + off, sizeof(v));
    return v;
}

static uint64_t u64_at(const uint8_t *buf, size_t off)
{
    uint64_t v;
    memcpy(&v, buf + FRAME_HEADER_SIZE + off, sizeof(v));
    return v;
}

static uint32_t payload_len(const uint8_t *buf)
{
    uint32_t v;
    memcpy(&v, buf + 1, sizeof(v));
    return v;
}

static void test_header_layout_with_meta_and_ie(void)
{
    uint8_t buf[256];
    uint8_t psdu[24];
    for (size_t i = 0; i < sizeof(psdu); i++)
        psdu[i] = (uint8_t)(0x80 + i);

    wifi_rx_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    frame.rate_code = 0x0B;
    frame.rate_mbps = 6;
    frame.fcs_ok = true;
    frame.length = 0x0123;
    frame.freq_hz = 5180000000ULL;
    frame.psdu_len = sizeof(psdu);
    memcpy(frame.psdu, psdu, sizeof(psdu));

    wifi_meta_t meta;
    memset(&meta, 0, sizeof(meta));
    meta.meta_flags = WIFI_META_HAS_BSSID | WIFI_META_HAS_SA |
                      WIFI_META_HAS_DA | WIFI_META_HAS_SSID |
                      WIFI_META_HAS_IE_SUMMARY;
    for (int i = 0; i < 6; i++) {
        meta.bssid[i] = (uint8_t)(0xA0 + i);
        meta.sa[i] = (uint8_t)(0xB0 + i);
        meta.da[i] = (uint8_t)(0xC0 + i);
    }
    memcpy(meta.ssid, "NET", 3);

    wifi_ie_summary_t ies;
    memset(&ies, 0, sizeof(ies));
    ies.channel = 36;
    ies.security = WIFI_SEC_OPEN;
    ies.cipher = WIFI_CIPHER_CCMP;
    ies.phy_flags = WIFI_PHY_VHT;
    ies.max_rate = 866;
    ies.n_ss = 2;
    ies.chan_width = 2;
    ies.center_chan = 42;
    ies.secondary_offset = 1;
    ies.country[0] = 'C';
    ies.country[1] = 'A';
    ies.pmf = WIFI_PMF_CAPABLE;
    ies.legacy_rate = 108;

    size_t len = proto_build_wifi(buf, 7, 123.5, &frame, 42.5f,
                                  WIFI_FCLASS_BEACON, &meta, &ies);

    const size_t meta_size = 20 + 3;
    const size_t expect_payload = 28 + sizeof(psdu) + meta_size + 14;
    CHECK(len == FRAME_HEADER_SIZE + expect_payload);
    CHECK(payload_len(buf) == expect_payload);
    CHECK(buf[0] == DATA_WIFI);

    CHECK(double_at(buf, 0) == 123.5);
    CHECK(buf[FRAME_HEADER_SIZE + 8] == 0x0B);
    CHECK(buf[FRAME_HEADER_SIZE + 9] == 6);
    CHECK(buf[FRAME_HEADER_SIZE + 10] == 1);
    CHECK(u16_at(buf, 11) == 0x0123);
    CHECK(buf[FRAME_HEADER_SIZE + 13] == WIFI_FCLASS_BEACON);
    CHECK(float_at(buf, 14) == 42.5f);
    CHECK(u16_at(buf, 18) == sizeof(psdu));

    /* The capture frequency sits at the end of the fixed header. */
    CHECK(u64_at(buf, 20) == 5180000000ULL);

    const uint8_t *p = buf + FRAME_HEADER_SIZE;
    CHECK(memcmp(p + 28, psdu, sizeof(psdu)) == 0);

    const uint8_t *m = p + 28 + sizeof(psdu);
    CHECK(memcmp(m + 1, meta.bssid, 6) == 0);
    CHECK(memcmp(m + 7, meta.sa, 6) == 0);
    CHECK(memcmp(m + 13, meta.da, 6) == 0);
    CHECK(m[19] == 3);
    CHECK(memcmp(m + 20, "NET", 3) == 0);

    const uint8_t *ie = m + meta_size;
    CHECK(ie[0] == 36);
    CHECK(ie[1] == WIFI_SEC_OPEN);
    CHECK(ie[2] == WIFI_CIPHER_CCMP);
    CHECK(ie[3] == WIFI_PHY_VHT);
    CHECK(memcmp(ie + 10, "CA", 2) == 0);
    CHECK(ie[12] == WIFI_PMF_CAPABLE);
    CHECK(ie[13] == 108);
}

static void test_header_layout_without_meta(void)
{
    uint8_t buf[128];
    wifi_rx_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    frame.freq_hz = 2437000000ULL;
    frame.fcs_ok = true;
    frame.psdu_len = 0;

    size_t len = proto_build_wifi(buf, 1, 1.0, &frame, 10.0f,
                                  WIFI_FCLASS_ACK, NULL, NULL);

    CHECK(len == FRAME_HEADER_SIZE + 28 + 20);
    CHECK(payload_len(buf) == 28 + 20);
    CHECK(u64_at(buf, 20) == 2437000000ULL);
    for (int i = 0; i < 20; i++)
        CHECK(buf[FRAME_HEADER_SIZE + 28 + i] == 0);
}

static void test_frequency_survives_fcs_failure(void)
{
    uint8_t buf[128];
    wifi_rx_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    frame.freq_hz = 2412000000ULL;
    frame.fcs_ok = false;
    frame.psdu_len = 24;
    memset(frame.psdu, 0xAA, sizeof(frame.psdu));

    size_t len = proto_build_wifi(buf, 1, 1.0, &frame, 10.0f,
                                  WIFI_FCLASS_UNKNOWN, NULL, NULL);

    CHECK(u16_at(buf, 18) == 0);
    CHECK(u64_at(buf, 20) == 2412000000ULL);
    CHECK(len == FRAME_HEADER_SIZE + 28 + 20);
}

int main(void)
{
    TEST(test_header_layout_with_meta_and_ie);
    TEST(test_header_layout_without_meta);
    TEST(test_frequency_survives_fcs_failure);

    if (g_fails) {
        printf("\n%d check(s) FAILED\n", g_fails);
        return 1;
    }
    printf("\nall checks passed\n");
    return 0;
}
