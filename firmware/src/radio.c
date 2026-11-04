#include "radio.h"
#include "hal.h"
#include "dma_rx.h"

#include <stdatomic.h>
#include <stdio.h>

static _Atomic uint64_t g_capture_freq_hz;
static _Atomic uint32_t g_capture_sample_rate_hz;

void radio_capture_publish(const radio_state_t *state)
{
    atomic_store_explicit(&g_capture_freq_hz, state->freq_hz,
                          memory_order_seq_cst);
    atomic_store_explicit(&g_capture_sample_rate_hz, state->sample_rate_hz,
                          memory_order_seq_cst);
}

void radio_capture_snapshot(uint64_t *freq_hz, uint32_t *sample_rate_hz)
{
    if (freq_hz)
        *freq_hz = atomic_load_explicit(&g_capture_freq_hz,
                                        memory_order_seq_cst);
    if (sample_rate_hz)
        *sample_rate_hz = atomic_load_explicit(&g_capture_sample_rate_hz,
                                               memory_order_seq_cst);
}

int radio_init(radio_state_t *state)
{
    if (hal_init() != 0) return -1;

    // Defaults: 2457 MHz (ch10), 20 MHz BW, 20 MSPS, fast AGC
    state->freq_hz = 2457000000ULL;
    state->sample_rate_hz = 20000000;
    state->bw_hz = 20000000;
    state->gain_db = 30.0f;
    state->agc_mode = 1;
    state->tx_atten_db = 10.0f;
    radio_capture_publish(state);

    if (hal_ad9361_set_rx_lo(state->freq_hz) != 0) return -1;
    if (hal_ad9361_set_tx_lo(state->freq_hz) != 0) return -1;
    if (hal_ad9361_set_sample_rate((uint64_t)state->sample_rate_hz) != 0) return -1;
    if (hal_ad9361_set_rx_bandwidth((uint64_t)state->bw_hz) != 0) return -1;
    if (hal_ad9361_set_tx_bandwidth((uint64_t)state->bw_hz) != 0) return -1;
    if (hal_ad9361_set_rx_gain((double)state->gain_db) != 0) return -1;
    if (hal_ad9361_set_rx_gain_mode("fast_attack") != 0) return -1;
    if (hal_ad9361_set_tx_attenuation((double)state->tx_atten_db) != 0) return -1;

    if (dma_rx_start() != 0) {
        fprintf(stderr, "radio: RX DMA start failed\n");
        return -1;
    }
    return 0;
}

int radio_set_freq(radio_state_t *state, uint64_t freq_hz, uint64_t *actual_hz)
{
    uint64_t old_freq = state->freq_hz;

    if (hal_ad9361_set_rx_lo(freq_hz) != 0) return -1;
    if (hal_ad9361_set_tx_lo(freq_hz) != 0) goto rollback;

    long long readback = hal_ad9361_get_rx_lo();
    if (readback < 0) goto rollback;

    *actual_hz = (uint64_t)readback;
    state->freq_hz = *actual_hz;
    radio_capture_publish(state);
    return 0;

rollback:
    /* RX may already have moved, and a TX write can land before it
     * reports failure.  Restore both so hardware matches the state that
     * a failed command promises to leave behind. */
    if (hal_ad9361_set_rx_lo(old_freq) != 0)
        fprintf(stderr, "radio: failed to restore RX LO %llu Hz\n",
                (unsigned long long)old_freq);
    if (hal_ad9361_set_tx_lo(old_freq) != 0)
        fprintf(stderr, "radio: failed to restore TX LO %llu Hz\n",
                (unsigned long long)old_freq);
    return -1;
}

int radio_set_gain(radio_state_t *state, float gain_db)
{
    if (state->agc_mode == 0) {
        if (hal_ad9361_set_rx_gain((double)gain_db) != 0) return -1;
    }
    state->gain_db = gain_db;
    return 0;
}

int radio_set_bw(radio_state_t *state, uint32_t bw_hz, uint32_t *actual_hz)
{
    uint32_t old_bw = state->bw_hz;

    if (hal_ad9361_set_rx_bandwidth((uint64_t)bw_hz) != 0) return -1;
    long long readback = hal_ad9361_get_rx_bandwidth();
    if (readback < 0) {
        if (hal_ad9361_set_rx_bandwidth((uint64_t)old_bw) != 0)
            fprintf(stderr, "radio: failed to restore RX bandwidth %u Hz\n",
                    old_bw);
        return -1;
    }
    *actual_hz = (uint32_t)readback;
    state->bw_hz = *actual_hz;
    return 0;
}

int radio_set_agc(radio_state_t *state, uint8_t mode)
{
    const char *s = "manual";
    if (mode == 1) s = "fast_attack";
    else if (mode == 2) s = "slow_attack";
    if (hal_ad9361_set_rx_gain_mode(s) != 0) return -1;
    state->agc_mode = mode;
    return 0;
}

int radio_set_sample_rate(radio_state_t *state, uint32_t rate_hz, uint32_t *actual_hz)
{
    uint32_t old_rate = state->sample_rate_hz;

    /* A redundant request must not restart DMA: callers may be restoring
     * state, and a needless stop/start tears the producer's read stream. */
    if (rate_hz == old_rate) {
        if (actual_hz)
            *actual_hz = rate_hz;
        return 0;
    }

    dma_rx_stop();
    if (hal_ad9361_set_sample_rate((uint64_t)rate_hz) != 0)
        goto rollback;
    if (hal_ad9361_set_tx_bandwidth((uint64_t)rate_hz) != 0)
        goto rollback;

    long long readback = hal_ad9361_get_sample_rate();
    if (readback < 0)
        goto rollback;

    uint32_t new_rate = (uint32_t)readback;
    if (dma_rx_start() != 0)
        goto rollback;

    if (actual_hz)
        *actual_hz = new_rate;
    state->sample_rate_hz = new_rate;
    radio_capture_publish(state);
    return 0;

rollback:
    /* A failed set may have written the new rate before failing, and DMA
     * is stopped.  Callers treat a failed set as "the old rate is still
     * active" and will not roll back, so restore the old rate and TX
     * bandwidth here and try to bring DMA back.  The state stays at the
     * old rate so a retry of the same request applies it again. */
    dma_rx_stop();
    if (hal_ad9361_set_sample_rate((uint64_t)old_rate) != 0)
        fprintf(stderr, "radio: failed to restore sample rate %u Hz\n",
                old_rate);
    if (hal_ad9361_set_tx_bandwidth((uint64_t)old_rate) != 0)
        fprintf(stderr, "radio: failed to restore tx bandwidth %u Hz\n",
                old_rate);
    if (dma_rx_start() != 0)
        fprintf(stderr, "radio: RX DMA restart failed after restoring"
                " %u Hz\n", old_rate);
    return -1;
}

int radio_set_tx_atten(radio_state_t *state, float atten_db)
{
    if (hal_ad9361_set_tx_attenuation((double)atten_db) != 0) return -1;
    state->tx_atten_db = atten_db;
    return 0;
}

uint32_t radio_get_fpga_id(void)
{
    return hal_reg_read(REG_BUILD_ID);
}
