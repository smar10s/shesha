#pragma once
#include <stdint.h>
#include <stddef.h>

#define WIFI_TX_SILENCE_PAD 2000

int wifi_tx_build(int rate_mbps, const uint8_t *psdu, size_t psdu_len,
                  float **real, float **imag, size_t *n_samples);

int wifi_tx_init(void);
void wifi_tx_cleanup(void);
