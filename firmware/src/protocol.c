#include "protocol.h"
#include "wifi_protocol.h"
#include "fm_protocol.h"
#include <string.h>

size_t proto_build_frame(uint8_t *buf, uint8_t type, const void *payload, uint32_t payload_len)
{
    buf[0] = type;
    memcpy(buf + 1, &payload_len, 4);
    uint32_t seq = 0;
    memcpy(buf + 5, &seq, 4);
    if (payload_len > 0 && payload)
        memcpy(buf + FRAME_HEADER_SIZE, payload, payload_len);
    return FRAME_HEADER_SIZE + payload_len;
}

uint32_t proto_parse_header(const uint8_t *buf, uint8_t *type_out, uint32_t *seq_out)
{
    *type_out = buf[0];
    uint32_t len;
    memcpy(&len, buf + 1, 4);
    if (seq_out) {
        memcpy(seq_out, buf + 5, 4);
    }
    return len;
}

bool proto_frame_size(uint32_t payload_len, size_t *frame_size)
{
    if (payload_len > PROTO_MAX_PAYLOAD)
        return false;
    *frame_size = FRAME_HEADER_SIZE + (size_t)payload_len;
    return true;
}

size_t proto_build_ok(uint8_t *buf, uint8_t cmd_type)
{
    return proto_build_frame(buf, RESP_OK, &cmd_type, 1);
}

size_t proto_build_ok_u32(uint8_t *buf, uint8_t cmd_type, uint32_t actual)
{
    uint8_t payload[5];
    payload[0] = cmd_type;
    memcpy(payload + 1, &actual, 4);
    return proto_build_frame(buf, RESP_OK, payload, 5);
}

size_t proto_build_ok_u32_pair(uint8_t *buf, uint8_t cmd_type, uint32_t a, uint32_t b)
{
    uint8_t payload[9];
    payload[0] = cmd_type;
    memcpy(payload + 1, &a, 4);
    memcpy(payload + 5, &b, 4);
    return proto_build_frame(buf, RESP_OK, payload, 9);
}

size_t proto_build_ok_u64(uint8_t *buf, uint8_t cmd_type, uint64_t actual)
{
    uint8_t payload[9];
    payload[0] = cmd_type;
    memcpy(payload + 1, &actual, 8);
    return proto_build_frame(buf, RESP_OK, payload, 9);
}

size_t proto_build_error(uint8_t *buf, uint8_t cmd_type, const char *msg)
{
    uint8_t payload[256];
    payload[0] = cmd_type;
    size_t msg_len = strlen(msg);
    if (msg_len > 253) msg_len = 253;
    memcpy(payload + 1, msg, msg_len);
    payload[1 + msg_len] = '\0';
    return proto_build_frame(buf, RESP_ERROR, payload, 2 + (uint32_t)msg_len);
}

void proto_pack_rds_tx(uint8_t *p, const rds_tx_config_t *cfg)
{
    memset(p, 0, PROTO_RDS_TX_SIZE);
    p[0] = (uint8_t)((cfg->enabled ? 1u : 0u) | (cfg->tp ? 2u : 0u));
    memcpy(p + 1, &cfg->pi, 2);
    p[3] = (uint8_t)(cfg->pty & 0x1Fu);
    memcpy(p + 4, cfg->ps, 8);
    memcpy(p + 12, cfg->rt, 64);
}

void proto_unpack_rds_tx(const uint8_t *p, rds_tx_config_t *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->enabled = (p[0] & 1u) != 0;
    cfg->tp      = (p[0] & 2u) != 0;
    memcpy(&cfg->pi, p + 1, 2);
    cfg->pty = (uint8_t)(p[3] & 0x1Fu);
    memcpy(cfg->ps, p + 4, 8);
    memcpy(cfg->rt, p + 12, 64);
}

size_t proto_build_psd(uint8_t *buf, uint32_t seq, double timestamp,
                       float center_freq_mhz,
                       uint32_t n_bins, const float *bins)
{
    uint32_t payload_len = 24 + n_bins * 4;
    buf[0] = DATA_PSD;
    memcpy(buf + 1, &payload_len, 4);
    memcpy(buf + 5, &seq, 4);

    uint8_t *p = buf + FRAME_HEADER_SIZE;
    memcpy(p + 0, &timestamp, 8);
    memcpy(p + 8, &center_freq_mhz, 4);

    // Compute floor/ceil
    float floor_db = bins[0], ceil_db = bins[0];
    for (uint32_t i = 1; i < n_bins; i++) {
        if (bins[i] < floor_db) floor_db = bins[i];
        if (bins[i] > ceil_db) ceil_db = bins[i];
    }
    memcpy(p + 12, &floor_db, 4);
    memcpy(p + 16, &ceil_db, 4);
    memcpy(p + 20, &n_bins, 4);
    memcpy(p + 24, bins, n_bins * sizeof(float));

    return FRAME_HEADER_SIZE + payload_len;
}

size_t proto_build_wifi(uint8_t *buf, uint32_t seq, double timestamp,
                        const wifi_rx_frame_t *frame, float rx_gain,
                        wifi_frame_class_t frame_class,
                        const wifi_meta_t *meta, const wifi_ie_summary_t *ies)
{
    uint16_t psdu_len = (frame->fcs_ok && frame->psdu_len > 0) ? frame->psdu_len : 0;

    /* Compute SSID length from meta */
    uint8_t ssid_len = 0;
    if (meta && (meta->meta_flags & WIFI_META_HAS_SSID)) {
        /* strlen but bounded to 32 */
        while (ssid_len < 32 && meta->ssid[ssid_len] != '\0')
            ssid_len++;
    }

    /* Meta block: 1 (flags) + 6 (bssid) + 6 (sa) + 6 (da) + 1 (ssid_len) + ssid_len = 20 + ssid_len */
    uint32_t meta_size = 20 + ssid_len;
    /* IE block: 14 bytes if present */
    uint32_t ie_size = (meta && (meta->meta_flags & WIFI_META_HAS_IE_SUMMARY) && ies) ? 14 : 0;

    uint32_t payload_len = 28 + psdu_len + meta_size + ie_size;

    buf[0] = DATA_WIFI;
    memcpy(buf + 1, &payload_len, 4);
    memcpy(buf + 5, &seq, 4);

    uint8_t *p = buf + FRAME_HEADER_SIZE;
    memcpy(p + 0, &timestamp, 8);
    p[8] = frame->rate_code;
    p[9] = (uint8_t)frame->rate_mbps;
    p[10] = frame->fcs_ok ? 1 : 0;
    memcpy(p + 11, &frame->length, 2);
    p[13] = (uint8_t)frame_class;
    memcpy(p + 14, &rx_gain, 4);
    memcpy(p + 18, &psdu_len, 2);
    memcpy(p + 20, &frame->freq_hz, 8);

    if (psdu_len > 0)
        memcpy(p + 28, frame->psdu, psdu_len);

    /* Meta block */
    uint8_t *m = p + 28 + psdu_len;
    if (meta) {
        m[0] = meta->meta_flags;
        memcpy(m + 1, meta->bssid, 6);
        memcpy(m + 7, meta->sa, 6);
        memcpy(m + 13, meta->da, 6);
        m[19] = ssid_len;
        if (ssid_len > 0)
            memcpy(m + 20, meta->ssid, ssid_len);
    } else {
        memset(m, 0, 20);
    }

    /* IE summary block */
    if (ie_size > 0) {
        uint8_t *ie = m + meta_size;
        ie[0] = ies->channel;
        ie[1] = ies->security;
        ie[2] = ies->cipher;
        ie[3] = ies->phy_flags;
        ie[4] = (uint8_t)(ies->max_rate & 0xFF);
        ie[5] = (uint8_t)((ies->max_rate >> 8) & 0xFF);
        ie[6] = ies->n_ss;
        ie[7] = ies->chan_width;
        ie[8] = ies->center_chan;
        ie[9] = ies->secondary_offset;
        memcpy(ie + 10, ies->country, 2);
        ie[12] = ies->pmf;
        ie[13] = ies->legacy_rate;
    }

    return FRAME_HEADER_SIZE + payload_len;
}

/*
 * Serialize the RDS state. Explicit field copies at explicit offsets: the
 * client parses this byte for byte, and a struct memcpy would silently
 * change the wire whenever a field is added or reordered. The padding
 * bytes, if any appear later, must be written as zeros.
 */
static void proto_build_rds(uint8_t *m, const rds_state_t *rds)
{
    uint8_t f0 = 0, f1 = 0;

    if (rds->synced)     f0 |= 0x01;
    if (rds->pi_valid)   f0 |= 0x02;
    if (rds_ps_valid(rds))   f0 |= 0x04;
    if (rds->rt_valid)   f0 |= 0x08;
    if (rds_ptyn_valid(rds)) f0 |= 0x10;
    if (rds->ct_valid)   f0 |= 0x20;
    if (rds->tp)         f0 |= 0x40;
    if (rds->ta)         f0 |= 0x80;

    if (rds->ms)      f1 |= 0x01;
    if (rds->rt_ab)   f1 |= 0x02;
    if (rds->ptyn_ab) f1 |= 0x04;
    f1 |= (uint8_t)((rds->di & 0x7u) << 3);

    memset(m, 0, PROTO_RDS_WIRE_SIZE);
    m[0] = f0;
    m[1] = f1;
    memcpy(m + 2, &rds->pi, 2);
    m[4] = rds->pty;
    memcpy(m + 5, rds->ps, RDS_PS_LEN);
    m[13] = rds->ps_mask;
    m[14] = rds->rt_len;
    memcpy(m + 15, rds->rt, RDS_RT_LEN);
    memcpy(m + 79, &rds->rt_mask, 8);
    m[87] = rds->ptyn_mask;
    memcpy(m + 88, rds->ptyn, RDS_PS_LEN);
    m[96] = rds->af_count;
    memcpy(m + 97, rds->af, RDS_MAX_AF);
    memcpy(m + 122, &rds->ct_mjd, 4);
    memcpy(m + 126, &rds->ct_minutes, 2);
    m[128] = (uint8_t)rds->ct_offset_half_hours;
    m[129] = rds->oda_count;
    for (int i = 0; i < RDS_MAX_ODA; i++) {
        uint8_t *o = m + 130 + i * 11;
        memcpy(o + 0, &rds->oda[i].aid, 2);
        o[2] = rds->oda[i].len;
        memcpy(o + 3, rds->oda[i].data, RDS_ODA_DATA);
    }
    memcpy(m + 174, &rds->groups, 4);
    memcpy(m + 178, &rds->blocks, 4);
    memcpy(m + 182, &rds->block_errors, 4);
    memcpy(m + 186, &rds->sync_losses, 4);
}

size_t proto_build_fm(uint8_t *buf, uint32_t seq, double timestamp,
                      uint32_t audio_rate_hz,
                      uint32_t n_samples, const float *audio,
                      const rds_state_t *rds)
{
    uint32_t payload_len = PROTO_FM_HEADER_SIZE + PROTO_RDS_WIRE_SIZE
                         + n_samples * 4;
    buf[0] = DATA_FM;
    memcpy(buf + 1, &payload_len, 4);
    memcpy(buf + 5, &seq, 4);

    uint8_t *p = buf + FRAME_HEADER_SIZE;
    memcpy(p + 0, &timestamp, 8);
    memcpy(p + 8, &audio_rate_hz, 4);
    memcpy(p + 12, &n_samples, 4);

    if (rds)
        proto_build_rds(p + PROTO_FM_HEADER_SIZE, rds);
    else
        memset(p + PROTO_FM_HEADER_SIZE, 0, PROTO_RDS_WIRE_SIZE);

    memcpy(p + PROTO_FM_HEADER_SIZE + PROTO_RDS_WIRE_SIZE,
           audio, n_samples * sizeof(float));

    return FRAME_HEADER_SIZE + payload_len;
}

size_t proto_build_iq(uint8_t *buf, uint32_t seq, double timestamp,
                      float freq_mhz, uint32_t sample_rate_hz,
                      uint8_t bits, uint16_t n_samples,
                      const uint8_t *packed)
{
    uint32_t bps = (bits == 8) ? 2u : 3u;
    uint32_t payload_len = 20 + (uint32_t)n_samples * bps;

    buf[0] = DATA_IQ;
    memcpy(buf + 1, &payload_len, 4);
    memcpy(buf + 5, &seq, 4);

    uint8_t *p = buf + FRAME_HEADER_SIZE;
    memcpy(p + 0, &timestamp, 8);
    memcpy(p + 8, &freq_mhz, 4);
    memcpy(p + 12, &sample_rate_hz, 4);
    p[16] = bits;
    p[17] = 0;
    memcpy(p + 18, &n_samples, 2);
    memcpy(p + 20, packed, (size_t)n_samples * bps);

    return FRAME_HEADER_SIZE + payload_len;
}
