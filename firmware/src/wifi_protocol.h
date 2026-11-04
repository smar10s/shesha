/* wifi_protocol.h — WiFi receive wire format.
 *
 * Kept apart from protocol.h so protocol-wide code does not drag in the
 * scanner's metadata types.  The wire bytes are unchanged. */
#pragma once

#include "protocol.h"
#include "wifi_scan.h"

#define WIFI_MAX_PSDU 1500

typedef struct {
    uint8_t  rate_code;
    int      rate_mbps;
    uint16_t length;
    bool     fcs_ok;
    float    rx_gain;
    uint64_t freq_hz;      /* capture-time RX centre frequency */
    uint8_t  psdu[WIFI_MAX_PSDU];
    uint16_t psdu_len;
} wifi_rx_frame_t;

size_t proto_build_wifi(uint8_t *buf, uint32_t seq, double timestamp,
                        const wifi_rx_frame_t *frame, float rx_gain,
                        wifi_frame_class_t frame_class,
                        const wifi_meta_t *meta, const wifi_ie_summary_t *ies);
