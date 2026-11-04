#include "wifi_tx.h"
#include "dma_tx.h"
#include <lib80211/tx.h>
#include <lib80211/fft.h>
#include <lib80211/mac.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

static lib80211_fft_plan *g_tx_plan = NULL;

int wifi_tx_init(void)
{
    g_tx_plan = lib80211_fft_plan_create();
    return g_tx_plan ? 0 : -1;
}

void wifi_tx_cleanup(void)
{
    if (g_tx_plan) {
        lib80211_fft_plan_destroy(g_tx_plan);
        g_tx_plan = NULL;
    }
}

int wifi_tx_build(int rate_mbps, const uint8_t *psdu, size_t psdu_len,
                  float **real, float **imag, size_t *n_samples)
{
    if (!g_tx_plan) return -1;
    if (rate_mbps < 6 || rate_mbps > 54) return -1;
    if (psdu_len == 0 || psdu_len > 4096) return -1;
    if (!real || !imag || !n_samples) return -1;

    size_t tx_psdu_len = psdu_len + 4;
    uint8_t *tx_psdu = malloc(tx_psdu_len);
    if (!tx_psdu) return -1;
    memcpy(tx_psdu, psdu, psdu_len);
    lib80211_append_fcs(tx_psdu, psdu_len);

    lib80211_tx_legacy_params txp = {
        .rate_mbps = rate_mbps,
        .psdu = tx_psdu,
        .psdu_len = tx_psdu_len,
        .scrambler_seed = 0x5D,
    };

    size_t wave_samps = lib80211_tx_legacy_samples(&txp);
    if (wave_samps == 0) {
        fprintf(stderr, "wifi_tx: rate %d Mbps produced no samples\n",
                rate_mbps);
        free(tx_psdu);
        return -1;
    }
    size_t total = WIFI_TX_SILENCE_PAD + wave_samps + WIFI_TX_SILENCE_PAD;

    if (total > DMA_TX_MAX_SAMPLES) {
        fprintf(stderr, "wifi_tx: waveform too large (%zu > %u)\n",
                total, (unsigned)DMA_TX_MAX_SAMPLES);
        free(tx_psdu);
        return -1;
    }

    *real = calloc(total, sizeof(float));
    *imag = calloc(total, sizeof(float));
    if (!*real || !*imag) {
        free(*real); *real = NULL;
        free(*imag); *imag = NULL;
        free(tx_psdu);
        return -1;
    }

    size_t written = lib80211_tx_legacy(g_tx_plan, &txp,
                                         *real + WIFI_TX_SILENCE_PAD,
                                         *imag + WIFI_TX_SILENCE_PAD);
    free(tx_psdu);

    if (written != wave_samps) {
        fprintf(stderr, "wifi_tx: lib80211_tx_legacy wrote %zu, expected %zu\n",
                written, wave_samps);
        free(*real); *real = NULL;
        free(*imag); *imag = NULL;
        return -1;
    }

    *n_samples = total;
    fprintf(stderr, "wifi_tx: rate=%d psdu=%zu samples=%zu\n",
            rate_mbps, psdu_len, total);
    return 0;
}
