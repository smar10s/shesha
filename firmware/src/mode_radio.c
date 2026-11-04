#include "mode_radio.h"

bool mode_radio_bw_valid(const mode_radio_req_t *req, uint32_t bw_hz)
{
    if (req->bw_fixed)
        return bw_hz == req->bw_fixed;
    return bw_hz >= req->bw_min && bw_hz <= req->bw_max;
}

uint32_t mode_radio_derive_sr(const mode_radio_req_t *req, uint32_t bw_hz)
{
    if (req->sr_fixed)
        return req->sr_fixed;
    return bw_hz < SR_HW_MIN ? SR_HW_MIN : bw_hz;
}

uint32_t mode_radio_entry_bw(const mode_radio_req_t *req, uint32_t current_bw)
{
    if (mode_radio_bw_valid(req, current_bw))
        return 0;  /* no change needed */
    if (req->bw_fixed)
        return req->bw_fixed;
    /* Out of range — clamp to max (widest useful span). */
    return req->bw_max;
}

uint32_t mode_radio_entry_sr(const mode_radio_req_t *req, uint32_t current_sr,
                             uint32_t bw_hz)
{
    if (req->sr_independent) {
        if (current_sr < req->sr_min) return req->sr_min;
        if (current_sr > req->sr_max) return req->sr_max;
        return current_sr;
    }
    return mode_radio_derive_sr(req, bw_hz);
}

uint32_t mode_radio_bw_sr(const mode_radio_req_t *req, uint32_t current_sr,
                          uint32_t bw_hz)
{
    if (req->sr_independent)
        return current_sr;
    return mode_radio_derive_sr(req, bw_hz);
}

bool mode_radio_sr_valid(const mode_radio_req_t *req, uint32_t bw_hz, uint32_t sr_hz)
{
    if (req->sr_independent)
        return sr_hz >= req->sr_min && sr_hz <= req->sr_max;
    return sr_hz == mode_radio_derive_sr(req, bw_hz);
}

bool mode_radio_agc_valid(const mode_radio_req_t *req, uint8_t agc)
{
    if (req->agc_fixed)
        return agc == req->agc_fixed;
    return true;
}
