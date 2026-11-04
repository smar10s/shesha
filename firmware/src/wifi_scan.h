#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/* ============================================================================
 * Frame classification (from PSDU frame control, with fallbacks)
 * ============================================================================ */

typedef enum {
    WIFI_FCLASS_ACK = 0,
    WIFI_FCLASS_BLOCK_ACK,
    WIFI_FCLASS_HT_VHT,
    WIFI_FCLASS_BEACON,
    WIFI_FCLASS_MGMT,
    WIFI_FCLASS_PROBE_REQ,
    WIFI_FCLASS_PROBE_RESP,
    WIFI_FCLASS_DATA,
    WIFI_FCLASS_EAPOL,
    WIFI_FCLASS_DEAUTH,
    WIFI_FCLASS_DISASSOC,
    WIFI_FCLASS_UNKNOWN,
} wifi_frame_class_t;

/*
 * Classify a decoded frame. Uses PSDU frame control bytes when available
 * (fcs_ok && psdu_len >= 2). Falls back to rate+length heuristic when
 * PSDU is unavailable (FCS fail, stripped frames).
 */
wifi_frame_class_t wifi_classify_frame(const uint8_t *psdu, uint16_t psdu_len,
                                       bool fcs_ok, int rate_mbps, uint16_t length);

/* ============================================================================
 * 802.11 header parser (BSSID, SA, DA with ToDS/FromDS)
 * ============================================================================ */

#define WIFI_META_HAS_BSSID      0x01
#define WIFI_META_HAS_SA         0x02
#define WIFI_META_HAS_DA         0x04
#define WIFI_META_HAS_SSID       0x08
#define WIFI_META_IS_EAPOL       0x10
#define WIFI_META_HAS_IE_SUMMARY 0x20

typedef struct {
    uint8_t  bssid[6];
    uint8_t  sa[6];
    uint8_t  da[6];
    char     ssid[33];      /* null-terminated, max 32 chars */
    uint8_t  meta_flags;    /* WIFI_META_* bitfield */
} wifi_meta_t;

/*
 * Parse 802.11 MAC header from PSDU. Extracts BSSID, SA, DA based on
 * frame type and ToDS/FromDS bits. For beacons/probe responses, also
 * extracts SSID. For data frames, detects EAPOL via LLC/SNAP.
 *
 * Requires fcs_ok && psdu_len >= 24.
 */
void wifi_parse_header(const uint8_t *psdu, uint16_t psdu_len,
                       wifi_meta_t *meta);

/*
 * Promote a classified frame once the header parser has seen it. Data
 * frames carrying the LLC/SNAP EAPOL signature become WIFI_FCLASS_EAPOL.
 */
wifi_frame_class_t wifi_refine_class(wifi_frame_class_t fclass,
                                     const wifi_meta_t *meta);

/* ============================================================================
 * IE summary extraction (beacons and probe responses)
 * ============================================================================ */

typedef enum {
    WIFI_SEC_OPEN = 0,
    WIFI_SEC_WEP,
    WIFI_SEC_WPA2_PSK,
    WIFI_SEC_WPA2_EAP,
    WIFI_SEC_WPA3_SAE,
    WIFI_SEC_MIXED,
    WIFI_SEC_WPA1,
} wifi_security_t;

typedef enum {
    WIFI_CIPHER_NONE = 0,
    WIFI_CIPHER_WEP,
    WIFI_CIPHER_TKIP,
    WIFI_CIPHER_CCMP,
    WIFI_CIPHER_GCMP256,
} wifi_cipher_t;

typedef enum {
    WIFI_PMF_NONE = 0,
    WIFI_PMF_CAPABLE,
    WIFI_PMF_REQUIRED,
} wifi_pmf_t;

/* PHY capability flags */
#define WIFI_PHY_HT        0x01
#define WIFI_PHY_HT_40MHZ  0x02
#define WIFI_PHY_VHT       0x04
#define WIFI_PHY_VHT_80MHZ 0x08
#define WIFI_PHY_VHT_160   0x10
#define WIFI_PHY_HE        0x20

typedef struct {
    uint8_t  channel;           /* Primary channel (from DS Param or HT Op) */
    uint8_t  security;          /* wifi_security_t */
    uint8_t  cipher;            /* wifi_cipher_t */
    uint8_t  phy_flags;         /* WIFI_PHY_* bitfield */
    uint16_t max_rate;          /* MCS-derived max rate in Mbps (LE); 0 = use legacy */
    uint8_t  n_ss;              /* spatial streams (1-8, 0=unknown) */
    uint8_t  chan_width;        /* 0=20MHz, 1=40, 2=80, 3=160 */
    uint8_t  center_chan;       /* VHT/HE center freq segment 0 channel */
    uint8_t  secondary_offset;  /* 0=none, 1=above, 3=below */
    uint8_t  country[2];        /* Country IE (e.g. 'U','S') */
    uint8_t  pmf;               /* wifi_pmf_t */
    uint8_t  legacy_rate;       /* from Supported Rates (0.5 Mbps units) — kept for ref */
} wifi_ie_summary_t;

/*
 * Extract IE summary from a beacon or probe response PSDU.
 * Only processes management frames with subtype 8 (beacon) or 5 (probe resp).
 * Sets all fields to 0 if not a beacon/probe or too short.
 */
void wifi_parse_ies(const uint8_t *psdu, uint16_t psdu_len,
                    wifi_ie_summary_t *ies);
