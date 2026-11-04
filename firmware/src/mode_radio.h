/* firmware/src/mode_radio.h — per-mode bandwidth + sample rate constraints.
 *
 * Each producer mode declares what bandwidths it accepts and how sample_rate
 * is derived from bandwidth.  Replaces rate_owner.h.
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>

/* AGC modes for the agc_fixed constraint (mirrors CMD_SET_AGC payload). */
/* AD9361 minimum sample rate.  The IIO driver rejects anything below
 * 25 MHz / 12 ≈ 2.083 MHz; 2.1 MHz is the proven floor (FM mode). */
#define SR_HW_MIN  2100000

#define MODE_RADIO_AGC_MANUAL       0
#define MODE_RADIO_AGC_FAST_ATTACK  1
#define MODE_RADIO_AGC_SLOW_ATTACK  2

/* Constraint descriptor for one producer. */
typedef struct {
    uint32_t bw_fixed;     /* non-zero = mode forces exactly this bw */
    uint32_t bw_min;       /* ignored if bw_fixed != 0 */
    uint32_t bw_max;       /* ignored if bw_fixed != 0 */
    uint32_t sr_fixed;     /* non-zero = sr is always this (ignores bw) */
    /* If sr_fixed == 0 and !sr_independent, sample_rate = bw
     * (waterfall rule).  sr_independent modes keep their own sr. */
    bool     sr_independent;
    uint32_t sr_min;       /* valid sr range when sr_independent */
    uint32_t sr_max;
    /* Non-zero = the mode owns the AGC mode and rejects any other
     * (MODE_RADIO_AGC_*).  wifi_fpga's fabric pipeline is designed for
     * fast_attack only. */
    uint8_t  agc_fixed;
} mode_radio_req_t;

/* Query: is this bw valid for the given mode constraints? */
bool mode_radio_bw_valid(const mode_radio_req_t *req, uint32_t bw_hz);

/* Query: what sample_rate does this mode need for a given bw? */
uint32_t mode_radio_derive_sr(const mode_radio_req_t *req, uint32_t bw_hz);

/* Query: what bw should be forced on mode entry?
 *
 * Returns the bw to set, or 0 if current_bw is already valid.
 * If the mode has bw_fixed, returns bw_fixed (unless already at it).
 * If the mode has a range and current_bw is outside it, returns bw_max.
 */
uint32_t mode_radio_entry_bw(const mode_radio_req_t *req, uint32_t current_bw);

/* What sr does mode entry settle on, given the current one? */
uint32_t mode_radio_entry_sr(const mode_radio_req_t *req, uint32_t current_sr,
                             uint32_t bw_hz);

/* What sr results from a bw change (CMD_SET_BW)? */
uint32_t mode_radio_bw_sr(const mode_radio_req_t *req, uint32_t current_sr,
                          uint32_t bw_hz);

/* Query: can an explicit CMD_SET_SAMPLE_RATE(rate) proceed?
 * Valid only if rate == derive_sr(current_bw) or mode allows independent sr.
 */
bool mode_radio_sr_valid(const mode_radio_req_t *req, uint32_t bw_hz, uint32_t sr_hz);

/* Query: is this AGC mode allowed for the mode constraints?
 * 0/1/2 = manual/fast_attack/slow_attack. */
bool mode_radio_agc_valid(const mode_radio_req_t *req, uint8_t agc);
