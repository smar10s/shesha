#include "wifi_rx.h"
#include "dma_rx.h"
#include "radio.h"
#include "hal.h"
#include <lib80211/fft.h>
#include <lib80211/rx.h>
#include <lib80211/constants.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <math.h>
#include <pthread.h>

#define CAPTURE_SAMPLES  400000
#define CAPTURE_TIMEOUT_MS 50

static lib80211_fft_plan *g_rx_plan = NULL;
static float *g_iq_real = NULL;
static float *g_iq_imag = NULL;

static pthread_mutex_t g_diag_mutex = PTHREAD_MUTEX_INITIALIZER;
static wifi_rx_diag_t g_diag;

int wifi_rx_init(void)
{
    g_rx_plan = lib80211_fft_plan_create();
    if (!g_rx_plan) return -1;

    g_iq_real = malloc(CAPTURE_SAMPLES * sizeof(float));
    g_iq_imag = malloc(CAPTURE_SAMPLES * sizeof(float));
    if (!g_iq_real || !g_iq_imag) {
        wifi_rx_cleanup();
        return -1;
    }
    memset(&g_diag, 0, sizeof(g_diag));
    return 0;
}

void wifi_rx_cleanup(void)
{
    if (g_rx_plan) { lib80211_fft_plan_destroy(g_rx_plan); g_rx_plan = NULL; }
    free(g_iq_real); g_iq_real = NULL;
    free(g_iq_imag); g_iq_imag = NULL;
}

static uint8_t rate_mbps_to_code(int rate_mbps)
{
    switch (rate_mbps) {
    case 6:  return 0x0B;
    case 9:  return 0x0F;
    case 12: return 0x0A;
    case 18: return 0x0E;
    case 24: return 0x09;
    case 36: return 0x0D;
    case 48: return 0x08;
    case 54: return 0x0C;
    default: return 0;
    }
}

int wifi_rx_decode_one(wifi_rx_frame_t *frame)
{
    uint64_t capture_freq_hz = 0;
    radio_capture_snapshot(&capture_freq_hz, NULL);

    int captured = dma_rx_capture(CAPTURE_SAMPLES, g_iq_real, g_iq_imag,
                                  CAPTURE_TIMEOUT_MS);
    pthread_mutex_lock(&g_diag_mutex);
    g_diag.captures++;
    pthread_mutex_unlock(&g_diag_mutex);
    if (captured <= 0) return -1;

    /* Remove DC offset */
    {
        double sum_re = 0.0, sum_im = 0.0;
        for (int i = 0; i < captured; i++) {
            sum_re += (double)g_iq_real[i];
            sum_im += (double)g_iq_imag[i];
        }
        float dc_re = (float)(sum_re / (double)captured);
        float dc_im = (float)(sum_im / (double)captured);
        for (int i = 0; i < captured; i++) {
            g_iq_real[i] -= dc_re;
            g_iq_imag[i] -= dc_im;
        }
    }

    /* Compute IQ RMS and peak for diagnostics */
    {
        double sum_sq = 0.0;
        float peak = 0.0f;
        for (int i = 0; i < captured; i++) {
            float r = g_iq_real[i];
            float im = g_iq_imag[i];
            float p = r * r + im * im;
            sum_sq += (double)p;
            if (p > peak) peak = p;
        }
        pthread_mutex_lock(&g_diag_mutex);
        g_diag.iq_rms = sqrtf((float)(sum_sq / (double)captured));
        g_diag.iq_peak = sqrtf(peak);
        pthread_mutex_unlock(&g_diag_mutex);
    }

    lib80211_rx_result result;
    memset(&result, 0, sizeof(result));

    int rc = lib80211_rx_decode(g_rx_plan, g_iq_real, g_iq_imag,
                                (size_t)captured, &result);
    if (rc != 0) return -1;

    pthread_mutex_lock(&g_diag_mutex);
    g_diag.decode_ok++;
    if (result.fcs_valid)
        g_diag.fcs_ok++;
    else
        g_diag.fcs_bad++;
    pthread_mutex_unlock(&g_diag_mutex);

    memset(frame, 0, sizeof(*frame));
    frame->freq_hz = capture_freq_hz;
    frame->fcs_ok = result.fcs_valid;
    frame->rate_mbps = result.rate_mbps;
    frame->length = (uint16_t)(result.psdu_len + 4);

    if (result.type == LIB80211_FRAME_LEGACY) {
        frame->rate_code = rate_mbps_to_code(result.rate_mbps);
    } else {
        frame->rate_code = 0;
    }

    if (result.psdu_len > 0 && result.psdu_len <= WIFI_MAX_PSDU) {
        memcpy(frame->psdu, result.psdu, result.psdu_len);
        frame->psdu_len = (uint16_t)result.psdu_len;
    }

    frame->rx_gain = (float)hal_ad9361_get_rx_gain();

    return 0;
}

void wifi_rx_get_diag(wifi_rx_diag_t *out)
{
    if (out) {
        pthread_mutex_lock(&g_diag_mutex);
        *out = g_diag;
        memset(&g_diag, 0, sizeof(g_diag));
        pthread_mutex_unlock(&g_diag_mutex);
    }
}
