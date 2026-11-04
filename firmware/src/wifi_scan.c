/*
 * wifi_scan.c — Frame classification, header parsing, IE extraction.
 *
 * Provides the intelligence layer between raw lib80211 decode output and
 * the wire protocol. Classifies frames by true FC bytes when available,
 * extracts addressing (BSSID/SA/DA) with proper ToDS/FromDS handling,
 * and parses beacon/probe IEs for security, PHY capabilities, PMF, etc.
 */

#include "wifi_scan.h"
#include <string.h>

/* --------------------------------------------------------------------------
 * Frame classification
 * -------------------------------------------------------------------------- */

wifi_frame_class_t wifi_classify_frame(const uint8_t *psdu, uint16_t psdu_len,
                                       bool fcs_ok, int rate_mbps, uint16_t length)
{
    /* If we have a valid PSDU, use true FC-based classification */
    if (fcs_ok && psdu && psdu_len >= 2) {
        uint8_t fc0 = psdu[0];
        uint8_t type = (fc0 >> 2) & 0x03;
        uint8_t subtype = (fc0 >> 4) & 0x0F;

        switch (type) {
        case 0: /* Management */
            switch (subtype) {
            case 0x8: return WIFI_FCLASS_BEACON;
            case 0x4: return WIFI_FCLASS_PROBE_REQ;
            case 0x5: return WIFI_FCLASS_PROBE_RESP;
            case 0xC: return WIFI_FCLASS_DEAUTH;
            case 0xA: return WIFI_FCLASS_DISASSOC;
            default:  return WIFI_FCLASS_MGMT;
            }
        case 1: /* Control */
            switch (subtype) {
            case 0xD: return WIFI_FCLASS_ACK;       /* ACK */
            case 0x9: return WIFI_FCLASS_BLOCK_ACK; /* Block ACK */
            case 0x8: return WIFI_FCLASS_BLOCK_ACK; /* Block ACK Req */
            default:  return WIFI_FCLASS_DATA;      /* other control → generic */
            }
        case 2: /* Data */
            return WIFI_FCLASS_DATA;
        default:
            return WIFI_FCLASS_UNKNOWN;
        }
    }

    /* Fallback: heuristic from rate + length (FCS-fail or stripped PSDU) */
    if (!fcs_ok) {
        /* Rate 6 Mbps FCS fail with specific lengths = HT/VHT */
        if (rate_mbps == 6) {
            if (length == 23)
                return WIFI_FCLASS_HT_VHT;
            if (length >= 18 && length <= 66 && length % 3 == 0)
                return WIFI_FCLASS_HT_VHT;
        }
        /* Rates never used for legacy control = HT/VHT */
        if (rate_mbps == 9 || rate_mbps == 18 || rate_mbps == 36 ||
            rate_mbps == 48 || rate_mbps == 54)
            return WIFI_FCLASS_HT_VHT;
        return WIFI_FCLASS_UNKNOWN;
    }

    /* FCS OK but no PSDU (stripped in wifi_fast mode) */
    if (length <= 14)
        return WIFI_FCLASS_ACK;
    if (rate_mbps == 6 && length >= 300 && length <= 500)
        return WIFI_FCLASS_BEACON;
    if (rate_mbps == 6 && length >= 50 && length < 300)
        return WIFI_FCLASS_MGMT;
    if (rate_mbps == 6 && length > 500)
        return WIFI_FCLASS_PROBE_RESP;

    return WIFI_FCLASS_DATA;
}

wifi_frame_class_t wifi_refine_class(wifi_frame_class_t fclass,
                                     const wifi_meta_t *meta)
{
    if (fclass == WIFI_FCLASS_DATA && meta &&
        (meta->meta_flags & WIFI_META_IS_EAPOL))
        return WIFI_FCLASS_EAPOL;
    return fclass;
}

/* --------------------------------------------------------------------------
 * 802.11 header parser
 * -------------------------------------------------------------------------- */

void wifi_parse_header(const uint8_t *psdu, uint16_t psdu_len,
                       wifi_meta_t *meta)
{
    memset(meta, 0, sizeof(*meta));

    if (!psdu || psdu_len < 24)
        return;

    uint8_t fc0 = psdu[0];
    uint8_t fc1 = psdu[1];
    uint8_t type = (fc0 >> 2) & 0x03;
    uint8_t subtype = (fc0 >> 4) & 0x0F;

    if (type == 0) {
        /* Management frame: DA=addr1, SA=addr2, BSSID=addr3 */
        memcpy(meta->da, psdu + 4, 6);
        memcpy(meta->sa, psdu + 10, 6);
        memcpy(meta->bssid, psdu + 16, 6);
        meta->meta_flags |= WIFI_META_HAS_DA | WIFI_META_HAS_SA | WIFI_META_HAS_BSSID;

        /* Extract SSID from beacons, probe responses, and probe requests */
        if ((subtype == 8 || subtype == 5) && psdu_len > 36) {
            size_t pos = 36;  /* skip fixed fields (timestamp+interval+capability) */
            while (pos + 2 <= psdu_len) {
                uint8_t tag_id = psdu[pos];
                uint8_t tag_len = psdu[pos + 1];
                if (pos + 2 + tag_len > psdu_len)
                    break;
                if (tag_id == 0) { /* SSID */
                    size_t copy_len = tag_len > 32 ? 32 : tag_len;
                    memcpy(meta->ssid, psdu + pos + 2, copy_len);
                    meta->ssid[copy_len] = '\0';
                    if (copy_len > 0)
                        meta->meta_flags |= WIFI_META_HAS_SSID;
                    break;
                }
                pos += 2 + tag_len;
            }
        } else if (subtype == 4 && psdu_len > 24) {
            /* Probe request: IEs start right after 24-byte MAC header */
            size_t pos = 24;
            while (pos + 2 <= psdu_len) {
                uint8_t tag_id = psdu[pos];
                uint8_t tag_len = psdu[pos + 1];
                if (pos + 2 + tag_len > psdu_len)
                    break;
                if (tag_id == 0) { /* SSID */
                    size_t copy_len = tag_len > 32 ? 32 : tag_len;
                    memcpy(meta->ssid, psdu + pos + 2, copy_len);
                    meta->ssid[copy_len] = '\0';
                    if (copy_len > 0)
                        meta->meta_flags |= WIFI_META_HAS_SSID;
                    break;
                }
                pos += 2 + tag_len;
            }
        }
    } else if (type == 2) {
        /* Data frame: address mapping depends on ToDS/FromDS */
        uint8_t to_ds = fc1 & 0x01;
        uint8_t from_ds = (fc1 >> 1) & 0x01;

        if (!to_ds && !from_ds) {
            /* IBSS: DA=addr1, SA=addr2, BSSID=addr3 */
            memcpy(meta->da, psdu + 4, 6);
            memcpy(meta->sa, psdu + 10, 6);
            memcpy(meta->bssid, psdu + 16, 6);
        } else if (to_ds && !from_ds) {
            /* STA→AP: BSSID=addr1, SA=addr2, DA=addr3 */
            memcpy(meta->bssid, psdu + 4, 6);
            memcpy(meta->sa, psdu + 10, 6);
            memcpy(meta->da, psdu + 16, 6);
        } else if (!to_ds && from_ds) {
            /* AP→STA: DA=addr1, BSSID=addr2, SA=addr3 */
            memcpy(meta->da, psdu + 4, 6);
            memcpy(meta->bssid, psdu + 10, 6);
            memcpy(meta->sa, psdu + 16, 6);
        } else {
            /* WDS: RA=addr1, TA=addr2, DA=addr3, SA=addr4.
             * addr4 follows sequence control, so it starts at 24. */
            memcpy(meta->bssid, psdu + 10, 6);  /* TA as BSSID proxy */
            memcpy(meta->da, psdu + 16, 6);
            if (psdu_len >= 30) {
                memcpy(meta->sa, psdu + 24, 6);
                meta->meta_flags |= WIFI_META_HAS_SA;
            }
        }
        meta->meta_flags |= WIFI_META_HAS_DA | WIFI_META_HAS_BSSID;
        if (!(to_ds && from_ds))
            meta->meta_flags |= WIFI_META_HAS_SA;

        /* EAPOL detection via LLC/SNAP */
        size_t hdr_len = (to_ds && from_ds) ? 30 : 24;
        if (subtype & 0x08) /* QoS data */
            hdr_len += 2;
        if (psdu_len >= hdr_len + 8) {
            const uint8_t *llc = psdu + hdr_len;
            if (llc[0] == 0xAA && llc[1] == 0xAA && llc[2] == 0x03 &&
                llc[3] == 0x00 && llc[4] == 0x00 && llc[5] == 0x00) {
                uint16_t ethertype = ((uint16_t)llc[6] << 8) | llc[7];
                if (ethertype == 0x888E)
                    meta->meta_flags |= WIFI_META_IS_EAPOL;
            }
        }
    }
}

/* --------------------------------------------------------------------------
 * IE summary extraction
 * -------------------------------------------------------------------------- */

void wifi_parse_ies(const uint8_t *psdu, uint16_t psdu_len,
                    wifi_ie_summary_t *ies)
{
    memset(ies, 0, sizeof(*ies));

    if (!psdu || psdu_len < 38)
        return;

    /* Verify beacon (subtype 8) or probe response (subtype 5) */
    uint8_t fc0 = psdu[0];
    uint8_t type = (fc0 >> 2) & 0x03;
    uint8_t subtype = (fc0 >> 4) & 0x0F;
    if (type != 0 || (subtype != 8 && subtype != 5))
        return;

    size_t pos = 36;
    bool found_rsn = false;

    while (pos + 2 <= psdu_len) {
        uint8_t tag_id  = psdu[pos];
        uint8_t tag_len = psdu[pos + 1];
        size_t  tag_end = pos + 2 + tag_len;
        if (tag_end > psdu_len)
            break;

        const uint8_t *data = psdu + pos + 2;

        switch (tag_id) {
        case 1:  /* Supported Rates */
            for (int i = 0; i < tag_len; i++) {
                uint8_t rate = data[i] & 0x7F;
                if (rate > ies->legacy_rate)
                    ies->legacy_rate = rate;
            }
            break;

        case 3:  /* DS Parameter Set */
            if (tag_len >= 1)
                ies->channel = data[0];
            break;

        case 7:  /* Country */
            if (tag_len >= 2) {
                ies->country[0] = data[0];
                ies->country[1] = data[1];
            }
            break;

        case 45: /* HT Capabilities */
            if (tag_len >= 2) {
                uint16_t ht_cap = (uint16_t)data[0] | ((uint16_t)data[1] << 8);
                ies->phy_flags |= WIFI_PHY_HT;
                if (ht_cap & 0x0002)  /* Supported Channel Width Set */
                    ies->phy_flags |= WIFI_PHY_HT_40MHZ;
            }
            /* MCS set at offset 3: bitmask of supported MCS indices */
            if (tag_len >= 7) {
                /* Count spatial streams from MCS bitmask bytes 3-6 */
                uint8_t n_ss = 0;
                for (int s = 0; s < 4; s++) {
                    if (data[3 + s] != 0)
                        n_ss = s + 1;
                }
                if (n_ss > ies->n_ss)
                    ies->n_ss = n_ss;
            }
            break;

        case 48: /* RSN */
            if (!found_rsn && tag_len >= 8) {
                found_rsn = true;
                /* version(2) + group_oui(3) + group_type(1) = type at offset 5 */
                uint8_t group_type = data[5];
                if (group_type == 4)      ies->cipher = WIFI_CIPHER_CCMP;
                else if (group_type == 2) ies->cipher = WIFI_CIPHER_TKIP;
                else if (group_type == 8) ies->cipher = WIFI_CIPHER_GCMP256;
                else if (group_type == 1) ies->cipher = WIFI_CIPHER_WEP;

                /* Pairwise cipher (overrides group) */
                if (tag_len >= 10) {
                    uint16_t pw_count = (uint16_t)data[6] | ((uint16_t)data[7] << 8);
                    if (pw_count >= 1 && tag_len >= 12) {
                        uint8_t pw_type = data[11];
                        if (pw_type == 4)      ies->cipher = WIFI_CIPHER_CCMP;
                        else if (pw_type == 2) ies->cipher = WIFI_CIPHER_TKIP;
                        else if (pw_type == 8) ies->cipher = WIFI_CIPHER_GCMP256;
                    }

                    /* AKM */
                    size_t akm_offset = 8 + (size_t)pw_count * 4;
                    if (akm_offset + 2 <= (size_t)tag_len) {
                        uint16_t akm_count = (uint16_t)data[akm_offset]
                            | ((uint16_t)data[akm_offset + 1] << 8);
                        if (akm_count >= 1 && akm_offset + 6 <= (size_t)tag_len) {
                            uint8_t akm_type = data[akm_offset + 5];
                            if (akm_type == 2)      ies->security = WIFI_SEC_WPA2_PSK;
                            else if (akm_type == 1) ies->security = WIFI_SEC_WPA2_EAP;
                            else if (akm_type == 8) ies->security = WIFI_SEC_WPA3_SAE;
                            else if (akm_type == 6) ies->security = WIFI_SEC_WPA2_PSK;
                            if (akm_count > 1)
                                ies->security = WIFI_SEC_MIXED;
                        }

                        /* RSN Capabilities — after AKM list */
                        size_t rsn_cap_offset = akm_offset + 2 + (size_t)akm_count * 4;
                        if (rsn_cap_offset + 2 <= (size_t)tag_len) {
                            uint16_t rsn_caps = (uint16_t)data[rsn_cap_offset]
                                | ((uint16_t)data[rsn_cap_offset + 1] << 8);
                            bool mfpc = (rsn_caps >> 7) & 0x01;
                            bool mfpr = (rsn_caps >> 6) & 0x01;
                            if (mfpr)
                                ies->pmf = WIFI_PMF_REQUIRED;
                            else if (mfpc)
                                ies->pmf = WIFI_PMF_CAPABLE;
                        }
                    }
                }
            }
            break;

        case 50: /* Extended Supported Rates */
            for (int i = 0; i < tag_len; i++) {
                uint8_t rate = data[i] & 0x7F;
                if (rate > ies->legacy_rate)
                    ies->legacy_rate = rate;
            }
            break;

        case 61: /* HT Operation */
            if (tag_len >= 2) {
                /* Byte 0: primary channel */
                if (ies->channel == 0)
                    ies->channel = data[0];
                /* Byte 1 bits 0-1: secondary channel offset */
                uint8_t sco = data[1] & 0x03;
                ies->secondary_offset = sco;  /* 0=none, 1=above, 3=below */
                if (sco != 0)
                    ies->chan_width = 1;  /* at least 40 MHz */
            }
            break;

        case 191: /* VHT Capabilities */
            if (tag_len >= 8) {
                uint32_t vht_cap = (uint32_t)data[0] | ((uint32_t)data[1] << 8)
                    | ((uint32_t)data[2] << 16) | ((uint32_t)data[3] << 24);
                ies->phy_flags |= WIFI_PHY_VHT;
                ies->phy_flags |= WIFI_PHY_VHT_80MHZ;  /* mandatory for VHT */
                uint8_t chan_width_cap = (vht_cap >> 2) & 0x03;
                if (chan_width_cap >= 1)
                    ies->phy_flags |= WIFI_PHY_VHT_160;

                /* VHT Supported MCS/NSS at offset 4: 2-bit fields per SS */
                uint16_t rx_mcs_map = (uint16_t)data[4] | ((uint16_t)data[5] << 8);
                uint8_t n_ss = 0;
                for (int s = 0; s < 8; s++) {
                    uint8_t mcs = (rx_mcs_map >> (s * 2)) & 0x03;
                    if (mcs != 3)  /* 3 = not supported */
                        n_ss = s + 1;
                }
                if (n_ss > ies->n_ss)
                    ies->n_ss = n_ss;
            }
            break;

        case 192: /* VHT Operation */
            if (tag_len >= 3) {
                uint8_t vht_width = data[0];
                uint8_t center_seg0 = data[1];
                ies->center_chan = center_seg0;
                if (vht_width == 1)
                    ies->chan_width = 2;       /* 80 MHz */
                else if (vht_width == 2 || vht_width == 3)
                    ies->chan_width = 3;       /* 160 MHz */
            }
            break;

        case 221: /* Vendor Specific */
            if (tag_len >= 4) {
                /* Check for WPA IE: OUI 00:50:f2 type 1 */
                if (data[0] == 0x00 && data[1] == 0x50 && data[2] == 0xf2
                    && data[3] == 0x01 && !found_rsn) {
                    /* WPA1 — only set if no RSN IE was found */
                    ies->security = WIFI_SEC_WPA1;
                    /* Parse cipher from WPA IE if available */
                    if (tag_len >= 12) {
                        uint8_t pw_type = data[11];
                        if (pw_type == 4)      ies->cipher = WIFI_CIPHER_CCMP;
                        else if (pw_type == 2) ies->cipher = WIFI_CIPHER_TKIP;
                    } else if (ies->cipher == 0) {
                        ies->cipher = WIFI_CIPHER_TKIP;  /* WPA1 default */
                    }
                }
            }
            break;

        case 255: /* Extension element */
            if (tag_len >= 1) {
                uint8_t ext_id = data[0];
                if (ext_id == 35 && tag_len >= 22) {
                    /* HE Capabilities */
                    ies->phy_flags |= WIFI_PHY_HE;
                }
                if (ext_id == 36 && tag_len >= 7) {
                    /* HE Operation */
                    ies->phy_flags |= WIFI_PHY_HE;
                }
            }
            break;
        }

        pos = tag_end;
    }

    /* If no RSN IE but Privacy bit set in Capability → WEP */
    if (!found_rsn && ies->security == WIFI_SEC_OPEN && psdu_len >= 36) {
        uint16_t capability = (uint16_t)psdu[34] | ((uint16_t)psdu[35] << 8);
        if (capability & 0x0010) {
            ies->security = WIFI_SEC_WEP;
            ies->cipher = WIFI_CIPHER_WEP;
        }
    }

    /* Compute MCS-derived max rate */
    if (ies->n_ss > 0) {
        /* Base rate per stream for highest MCS at each width:
         * 20 MHz MCS9: 86.7 Mbps/ss, 40: 200, 80: 433.3, 160: 866.7 */
        static const uint16_t rate_per_ss[] = {87, 200, 433, 867};
        uint8_t w = ies->chan_width;
        if (w > 3) w = 3;
        ies->max_rate = rate_per_ss[w] * ies->n_ss;
    }
}
