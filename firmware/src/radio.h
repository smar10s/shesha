#pragma once
#include <stdint.h>

#define RADIO_FREQ_MIN_HZ 70000000ULL
#define RADIO_FREQ_MAX_HZ 6000000000ULL

typedef struct {
    uint64_t freq_hz;
    uint32_t sample_rate_hz;
    uint32_t bw_hz;
    float    gain_db;
    uint8_t  agc_mode;  // 0=manual, 1=fast_attack, 2=slow_attack
    float    tx_atten_db;
} radio_state_t;

int radio_init(radio_state_t *state);
int radio_set_freq(radio_state_t *state, uint64_t freq_hz, uint64_t *actual_hz);
int radio_set_gain(radio_state_t *state, float gain_db);
int radio_set_bw(radio_state_t *state, uint32_t bw_hz, uint32_t *actual_hz);
int radio_set_agc(radio_state_t *state, uint8_t mode);
int radio_set_sample_rate(radio_state_t *state, uint32_t rate_hz, uint32_t *actual_hz);
int radio_set_tx_atten(radio_state_t *state, float atten_db);
uint32_t radio_get_fpga_id(void);

/* Capture-time tuning snapshot.  Producers read these atomics instead of
 * racing the main thread's writes to the state struct. */
void radio_capture_publish(const radio_state_t *state);
void radio_capture_snapshot(uint64_t *freq_hz, uint32_t *sample_rate_hz);
