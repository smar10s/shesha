/*
 * Host tests for wifi_scan classification: the management subtypes the
 * TUI colour-codes (deauth, disassoc), and the EAPOL promotion applied
 * after the header parse.
 */
#include "../src/wifi_scan.h"
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

static wifi_frame_class_t classify_fc(uint8_t fc0)
{
    const uint8_t psdu[2] = { fc0, 0x00 };
    return wifi_classify_frame(psdu, sizeof(psdu), true, 6, 100);
}

static void test_management_subtypes(void)
{
    CHECK(classify_fc(0x80) == WIFI_FCLASS_BEACON);
    CHECK(classify_fc(0x40) == WIFI_FCLASS_PROBE_REQ);
    CHECK(classify_fc(0x50) == WIFI_FCLASS_PROBE_RESP);
    CHECK(classify_fc(0xB0) == WIFI_FCLASS_MGMT);        /* auth */
    CHECK(classify_fc(0xC0) == WIFI_FCLASS_DEAUTH);
    CHECK(classify_fc(0xA0) == WIFI_FCLASS_DISASSOC);
}

static void test_control_and_data(void)
{
    CHECK(classify_fc(0xD4) == WIFI_FCLASS_ACK);
    CHECK(classify_fc(0x94) == WIFI_FCLASS_BLOCK_ACK);
    CHECK(classify_fc(0x84) == WIFI_FCLASS_BLOCK_ACK);
    CHECK(classify_fc(0x08) == WIFI_FCLASS_DATA);
}

static void test_fcs_fail_fallback(void)
{
    CHECK(wifi_classify_frame(NULL, 0, false, 6, 23) == WIFI_FCLASS_HT_VHT);
    CHECK(wifi_classify_frame(NULL, 0, false, 48, 200) == WIFI_FCLASS_HT_VHT);
    CHECK(wifi_classify_frame(NULL, 0, false, 12, 200) == WIFI_FCLASS_UNKNOWN);
}

/* WDS has four addresses: addr4 is the source and starts after sequence
 * control, at offset 24.  Reading offset 22 picks up the sequence bytes. */
static void test_wds_source_address(void)
{
    uint8_t psdu[30] = {0};
    wifi_meta_t meta;
    static const uint8_t sa[6] = {0x02, 0x11, 0x22, 0x33, 0x44, 0x55};

    psdu[0] = 0x08;             /* data */
    psdu[1] = 0x03;             /* ToDS + FromDS: WDS */
    psdu[22] = 0xAA;            /* sequence control */
    psdu[23] = 0xBB;
    memcpy(psdu + 24, sa, sizeof(sa));

    wifi_parse_header(psdu, sizeof(psdu), &meta);

    CHECK(meta.meta_flags & WIFI_META_HAS_SA);
    CHECK(memcmp(meta.sa, sa, sizeof(sa)) == 0);
}

static void test_eapol_refine(void)
{
    wifi_meta_t meta;
    memset(&meta, 0, sizeof(meta));

    meta.meta_flags = WIFI_META_IS_EAPOL;
    CHECK(wifi_refine_class(WIFI_FCLASS_DATA, &meta) == WIFI_FCLASS_EAPOL);
    CHECK(wifi_refine_class(WIFI_FCLASS_BEACON, &meta) == WIFI_FCLASS_BEACON);

    meta.meta_flags = 0;
    CHECK(wifi_refine_class(WIFI_FCLASS_DATA, &meta) == WIFI_FCLASS_DATA);
    CHECK(wifi_refine_class(WIFI_FCLASS_DATA, NULL) == WIFI_FCLASS_DATA);
}

static void fill_mgmt(uint8_t *psdu, uint8_t fc0,
                      const uint8_t da[6], const uint8_t sa[6],
                      const uint8_t bssid[6])
{
    memset(psdu, 0, 36);
    psdu[0] = fc0;
    memcpy(psdu + 4, da, 6);
    memcpy(psdu + 10, sa, 6);
    memcpy(psdu + 16, bssid, 6);
}

static size_t add_ie(uint8_t *psdu, size_t pos, uint8_t id,
                     const uint8_t *data, uint8_t len)
{
    psdu[pos] = id;
    psdu[pos + 1] = len;
    if (len)
        memcpy(psdu + pos + 2, data, len);
    return pos + 2 + len;
}

static void make_data(uint8_t *psdu, uint8_t fc0, uint8_t fc1,
                      const uint8_t addr1[6], const uint8_t addr2[6],
                      const uint8_t addr3[6], const uint8_t addr4[6])
{
    memset(psdu, 0, 64);
    psdu[0] = fc0;
    psdu[1] = fc1;
    memcpy(psdu + 4, addr1, 6);
    memcpy(psdu + 10, addr2, 6);
    memcpy(psdu + 16, addr3, 6);
    if (addr4)
        memcpy(psdu + 24, addr4, 6);
}

static void test_header_beacon_and_probe_response(void)
{
    static const uint8_t da[6] = { 0x11, 0x22, 0x33, 0x44, 0x55, 0x66 };
    static const uint8_t sa[6] = { 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF };
    static const uint8_t bssid[6] = { 0x02, 0x00, 0x00, 0x00, 0x00, 0x01 };
    static const uint8_t ssid[] = "home";
    uint8_t psdu[128];
    wifi_meta_t meta;
    size_t pos;

    fill_mgmt(psdu, 0x80, da, sa, bssid);
    pos = add_ie(psdu, 36, 0, ssid, sizeof(ssid) - 1);
    wifi_parse_header(psdu, (uint16_t)pos, &meta);

    CHECK(meta.meta_flags & WIFI_META_HAS_DA);
    CHECK(meta.meta_flags & WIFI_META_HAS_SA);
    CHECK(meta.meta_flags & WIFI_META_HAS_BSSID);
    CHECK(meta.meta_flags & WIFI_META_HAS_SSID);
    CHECK(memcmp(meta.da, da, 6) == 0);
    CHECK(memcmp(meta.sa, sa, 6) == 0);
    CHECK(memcmp(meta.bssid, bssid, 6) == 0);
    CHECK(strcmp(meta.ssid, "home") == 0);

    fill_mgmt(psdu, 0x50, da, sa, bssid);       /* probe response */
    pos = add_ie(psdu, 36, 0, ssid, sizeof(ssid) - 1);
    wifi_parse_header(psdu, (uint16_t)pos, &meta);

    CHECK(meta.meta_flags & WIFI_META_HAS_SSID);
    CHECK(strcmp(meta.ssid, "home") == 0);
}

static void test_data_address_mapping(void)
{
    static const uint8_t a1[6] = { 0x01, 0x01, 0x01, 0x01, 0x01, 0x01 };
    static const uint8_t a2[6] = { 0x02, 0x02, 0x02, 0x02, 0x02, 0x02 };
    static const uint8_t a3[6] = { 0x03, 0x03, 0x03, 0x03, 0x03, 0x03 };
    static const uint8_t a4[6] = { 0x04, 0x04, 0x04, 0x04, 0x04, 0x04 };
    uint8_t psdu[64];
    wifi_meta_t meta;

    make_data(psdu, 0x08, 0x00, a1, a2, a3, NULL);
    wifi_parse_header(psdu, 24, &meta);
    CHECK(memcmp(meta.da, a1, 6) == 0);
    CHECK(memcmp(meta.sa, a2, 6) == 0);
    CHECK(memcmp(meta.bssid, a3, 6) == 0);

    make_data(psdu, 0x08, 0x01, a1, a2, a3, NULL);  /* ToDS */
    wifi_parse_header(psdu, 24, &meta);
    CHECK(memcmp(meta.bssid, a1, 6) == 0);
    CHECK(memcmp(meta.sa, a2, 6) == 0);
    CHECK(memcmp(meta.da, a3, 6) == 0);

    make_data(psdu, 0x08, 0x02, a1, a2, a3, NULL);  /* FromDS */
    wifi_parse_header(psdu, 24, &meta);
    CHECK(memcmp(meta.da, a1, 6) == 0);
    CHECK(memcmp(meta.bssid, a2, 6) == 0);
    CHECK(memcmp(meta.sa, a3, 6) == 0);

    make_data(psdu, 0x08, 0x03, a1, a2, a3, a4);    /* WDS */
    wifi_parse_header(psdu, 30, &meta);
    CHECK(memcmp(meta.bssid, a2, 6) == 0);
    CHECK(memcmp(meta.da, a3, 6) == 0);
    CHECK(memcmp(meta.sa, a4, 6) == 0);
}

static void put_eapol(uint8_t *psdu, size_t offset)
{
    static const uint8_t llc[8] = {
        0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x88, 0x8E,
    };
    memcpy(psdu + offset, llc, sizeof(llc));
}

static void test_eapol_detection_and_qos_header_length(void)
{
    static const uint8_t a1[6] = { 1, 0, 0, 0, 0, 0 };
    static const uint8_t a2[6] = { 2, 0, 0, 0, 0, 0 };
    static const uint8_t a3[6] = { 3, 0, 0, 0, 0, 0 };
    uint8_t psdu[64];
    wifi_meta_t meta;

    make_data(psdu, 0x08, 0x01, a1, a2, a3, NULL);
    put_eapol(psdu, 24);
    wifi_parse_header(psdu, 32, &meta);
    CHECK(meta.meta_flags & WIFI_META_IS_EAPOL);
    CHECK(wifi_refine_class(WIFI_FCLASS_DATA, &meta) == WIFI_FCLASS_EAPOL);

    /* QoS data has a two-byte QoS control field before the LLC. */
    make_data(psdu, 0x88, 0x01, a1, a2, a3, NULL);
    put_eapol(psdu, 26);
    wifi_parse_header(psdu, 34, &meta);
    CHECK(meta.meta_flags & WIFI_META_IS_EAPOL);

    /* The same bytes at the non-QoS offset must not be mistaken for EAPOL. */
    make_data(psdu, 0x08, 0x01, a1, a2, a3, NULL);
    put_eapol(psdu, 26);
    wifi_parse_header(psdu, 34, &meta);
    CHECK(!(meta.meta_flags & WIFI_META_IS_EAPOL));

    /* WDS adds addr4, so the LLC starts at 30 (32 with QoS). */
    make_data(psdu, 0x88, 0x03, a1, a2, a3, a1);
    put_eapol(psdu, 32);
    wifi_parse_header(psdu, 40, &meta);
    CHECK(meta.meta_flags & WIFI_META_IS_EAPOL);
}

static wifi_ie_summary_t parse_beacon_with_ie(const uint8_t *data,
                                              uint8_t len)
{
    uint8_t psdu[192];
    wifi_ie_summary_t ies;
    size_t pos;

    fill_mgmt(psdu, 0x80, (const uint8_t[6]){0}, (const uint8_t[6]){0},
              (const uint8_t[6]){0});
    pos = add_ie(psdu, 36, 48, data, len);
    wifi_parse_ies(psdu, (uint16_t)pos, &ies);
    return ies;
}

/* version, group CCMP, pairwise count 1 + CCMP, AKM count 1 + PSK, caps */
static const uint8_t RSN_BASE[20] = {
    0x01, 0x00,
    0x00, 0x0F, 0xAC, 0x04,
    0x01, 0x00,
    0x00, 0x0F, 0xAC, 0x04,
    0x01, 0x00,
    0x00, 0x0F, 0xAC, 0x02,
    0x00, 0x00,
};

static void test_rsn_akm_cipher_and_pmf_bits(void)
{
    uint8_t rsn[24];
    wifi_ie_summary_t ies;

    memcpy(rsn, RSN_BASE, sizeof(RSN_BASE));

    /* MFPR is bit 6: set means required. */
    rsn[18] = 0x40;
    ies = parse_beacon_with_ie(rsn, sizeof(RSN_BASE));
    CHECK(ies.security == WIFI_SEC_WPA2_PSK);
    CHECK(ies.cipher == WIFI_CIPHER_CCMP);
    CHECK(ies.pmf == WIFI_PMF_REQUIRED);

    /* MFPC is bit 7: set means capable. */
    rsn[18] = 0x80;
    ies = parse_beacon_with_ie(rsn, sizeof(RSN_BASE));
    CHECK(ies.pmf == WIFI_PMF_CAPABLE);

    rsn[18] = 0x00;
    ies = parse_beacon_with_ie(rsn, sizeof(RSN_BASE));
    CHECK(ies.pmf == WIFI_PMF_NONE);

    /* AKM type 8 is SAE, type 1 is 802.1X. */
    rsn[17] = 0x08;
    ies = parse_beacon_with_ie(rsn, sizeof(RSN_BASE));
    CHECK(ies.security == WIFI_SEC_WPA3_SAE);

    rsn[17] = 0x01;
    ies = parse_beacon_with_ie(rsn, sizeof(RSN_BASE));
    CHECK(ies.security == WIFI_SEC_WPA2_EAP);

    /* TKIP in the pairwise list overrides the group cipher. */
    memcpy(rsn, RSN_BASE, sizeof(RSN_BASE));
    rsn[11] = 0x02;
    ies = parse_beacon_with_ie(rsn, sizeof(RSN_BASE));
    CHECK(ies.cipher == WIFI_CIPHER_TKIP);

    /* Two AKM suites are reported as mixed mode. */
    uint8_t mixed[24] = {
        0x01, 0x00,
        0x00, 0x0F, 0xAC, 0x04,
        0x01, 0x00,
        0x00, 0x0F, 0xAC, 0x04,
        0x02, 0x00,
        0x00, 0x0F, 0xAC, 0x02,
        0x00, 0x0F, 0xAC, 0x08,
        0x00, 0x00,
    };
    ies = parse_beacon_with_ie(mixed, sizeof(mixed));
    CHECK(ies.security == WIFI_SEC_MIXED);
}

static void test_ht_and_vht_ie_summary(void)
{
    static const uint8_t rates[] = { 0x82, 0x0C };
    static const uint8_t ds[] = { 6 };
    static const uint8_t country[] = { 'U', 'S' };
    static const uint8_t htcap[] = { 0x02, 0x00, 0x00, 0xFF, 0, 0, 0 };
    static const uint8_t htop[] = { 11, 0x01 };
    static const uint8_t vhtcap[] = { 0x04, 0, 0, 0, 0xFC, 0xFF, 0, 0 };
    static const uint8_t vhtop[] = { 1, 42, 0 };
    uint8_t psdu[192];
    wifi_ie_summary_t ies;
    size_t pos;

    fill_mgmt(psdu, 0x80, (const uint8_t[6]){0}, (const uint8_t[6]){0},
              (const uint8_t[6]){0});
    pos = add_ie(psdu, 36, 1, rates, sizeof(rates));
    pos = add_ie(psdu, pos, 3, ds, sizeof(ds));
    pos = add_ie(psdu, pos, 7, country, sizeof(country));
    pos = add_ie(psdu, pos, 45, htcap, sizeof(htcap));
    pos = add_ie(psdu, pos, 61, htop, sizeof(htop));
    pos = add_ie(psdu, pos, 191, vhtcap, sizeof(vhtcap));
    pos = add_ie(psdu, pos, 192, vhtop, sizeof(vhtop));
    wifi_parse_ies(psdu, (uint16_t)pos, &ies);

    CHECK(ies.channel == 6);
    CHECK(ies.legacy_rate == 12);
    CHECK(ies.country[0] == 'U' && ies.country[1] == 'S');
    CHECK(ies.phy_flags & WIFI_PHY_HT);
    CHECK(ies.phy_flags & WIFI_PHY_HT_40MHZ);
    CHECK(ies.phy_flags & WIFI_PHY_VHT);
    CHECK(ies.phy_flags & WIFI_PHY_VHT_80MHZ);
    CHECK(ies.phy_flags & WIFI_PHY_VHT_160);
    CHECK(ies.secondary_offset == 1);
    CHECK(ies.chan_width == 2);
    CHECK(ies.center_chan == 42);
    CHECK(ies.n_ss == 1);
    CHECK(ies.max_rate == 433);
}

static void test_truncated_ies_are_not_read_past_the_end(void)
{
    uint8_t psdu[64];
    wifi_meta_t meta;
    wifi_ie_summary_t ies;

    /* RSN header claims 20 bytes of data but only 4 are present. */
    memset(psdu, 0, sizeof(psdu));
    psdu[0] = 0x80;
    psdu[36] = 48;
    psdu[37] = 20;
    psdu[38] = 0x01;
    psdu[39] = 0x00;
    psdu[40] = 0x00;
    psdu[41] = 0x0F;
    wifi_parse_ies(psdu, 42, &ies);
    CHECK(ies.security == WIFI_SEC_OPEN);

    /* SSID IE claims 20 bytes but the frame ends after two. */
    fill_mgmt(psdu, 0x80, (const uint8_t[6]){0}, (const uint8_t[6]){0},
              (const uint8_t[6]){0});
    psdu[36] = 0;
    psdu[37] = 20;
    psdu[38] = 'a';
    psdu[39] = 'b';
    wifi_parse_header(psdu, 40, &meta);
    CHECK(!(meta.meta_flags & WIFI_META_HAS_SSID));

    /* A one-byte HT Capabilities IE cannot set the HT flag. */
    memset(psdu, 0, sizeof(psdu));
    psdu[0] = 0x80;
    psdu[36] = 45;
    psdu[37] = 1;
    psdu[38] = 0x02;
    wifi_parse_ies(psdu, 39, &ies);
    CHECK(!(ies.phy_flags & WIFI_PHY_HT));
}

int main(void)
{
    TEST(test_management_subtypes);
    TEST(test_control_and_data);
    TEST(test_fcs_fail_fallback);
    TEST(test_wds_source_address);
    TEST(test_eapol_refine);
    TEST(test_header_beacon_and_probe_response);
    TEST(test_data_address_mapping);
    TEST(test_eapol_detection_and_qos_header_length);
    TEST(test_rsn_akm_cipher_and_pmf_bits);
    TEST(test_ht_and_vht_ie_summary);
    TEST(test_truncated_ies_are_not_read_past_the_end);

    if (g_fails) {
        printf("%d check(s) failed\n", g_fails);
        return 1;
    }
    printf("all checks passed\n");
    return 0;
}
