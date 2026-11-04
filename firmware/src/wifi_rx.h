#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "wifi_protocol.h"

int wifi_rx_init(void);
void wifi_rx_cleanup(void);

// Decode a single WiFi frame from DMA (blocking, for producer thread).
// Returns 0 on success, -1 on capture timeout or decode error.
int wifi_rx_decode_one(wifi_rx_frame_t *frame_out);

// --- Diagnostics ---
typedef struct {
    uint32_t captures;
    uint32_t decode_ok;
    uint32_t fcs_ok;
    uint32_t fcs_bad;
    float    iq_rms;
    float    iq_peak;
} wifi_rx_diag_t;

void wifi_rx_get_diag(wifi_rx_diag_t *out);
