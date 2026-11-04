/* radio_txn.c — ordered producer/radio changes with rollback. */
#include "radio_txn.h"
#include "mode_radio.h"

static uint32_t derive_sr(const radio_txn_req_t *req, uint32_t bw_hz)
{
    if (bw_hz == req->cur_bw)
        return req->cur_sr;
    return mode_radio_bw_sr(&req->next->radio, req->cur_sr, bw_hz);
}

radio_txn_status_t radio_txn_run(const radio_txn_req_t *req,
                                 const radio_txn_io_t *io, void *io_ctx,
                                 radio_txn_result_t *out)
{
    if (!req || !req->next || !io || !io->set_bw || !io->set_sr || !out)
        return RADIO_TXN_INVALID;
    if (req->prev_running && !req->prev)
        return RADIO_TXN_INVALID;

    uint32_t new_bw = req->cur_bw;
    uint32_t new_sr = req->cur_sr;

    switch (req->kind) {
    case RADIO_TXN_ENTRY:
        new_bw = mode_radio_entry_bw(&req->next->radio, req->cur_bw);
        if (new_bw == 0)
            new_bw = req->cur_bw;
        new_sr = mode_radio_entry_sr(&req->next->radio, req->cur_sr, new_bw);
        break;
    case RADIO_TXN_BW:
        if (!mode_radio_bw_valid(&req->next->radio, req->value))
            return RADIO_TXN_INVALID;
        new_bw = req->value;
        new_sr = derive_sr(req, new_bw);
        break;
    case RADIO_TXN_SR:
        if (!mode_radio_sr_valid(&req->next->radio, req->cur_bw, req->value))
            return RADIO_TXN_INVALID;
        new_sr = req->value;
        break;
    default:
        return RADIO_TXN_INVALID;
    }

    bool bw_change = new_bw != req->cur_bw;
    bool sr_change = new_sr != req->cur_sr;
    /*
     * A derived mode may land on a different rate when the hardware rounds
     * the requested bandwidth, so cycle it for any bandwidth change.  An
     * independent mode's rate cannot move with bandwidth, so it can keep
     * running.
     */
    bool radio_cycle = bw_change && !req->next->radio.sr_independent;
    bool cycle = req->prev_running &&
                 (req->force || req->next != req->prev || radio_cycle || sr_change);

    out->bw = req->cur_bw;
    out->sr = req->cur_sr;

    if (cycle)
        req->prev->exit();

    radio_txn_status_t status = RADIO_TXN_OK;
    bool bw_applied = false;
    bool sr_applied = false;

    if (bw_change) {
        uint32_t actual = req->cur_bw;
        if (io->set_bw(new_bw, &actual, io_ctx) != 0) {
            status = RADIO_TXN_BW_FAILED;
            goto rollback;
        }
        out->bw = actual;
        bw_applied = true;

        if (req->kind == RADIO_TXN_BW) {
            new_sr = derive_sr(req, actual);
            sr_change = new_sr != req->cur_sr;
        }
    }

    if (sr_change) {
        uint32_t actual = req->cur_sr;
        if (io->set_sr(new_sr, &actual, io_ctx) != 0) {
            status = RADIO_TXN_SR_FAILED;
            goto rollback;
        }
        out->sr = actual;
        sr_applied = true;
    }

    if (cycle || !req->prev_running) {
        if (req->next->enter() != 0) {
            status = RADIO_TXN_ENTER_FAILED;
            goto rollback;
        }
    }

    return RADIO_TXN_OK;

rollback:
    if (sr_applied) {
        uint32_t ignored;
        io->set_sr(req->cur_sr, &ignored, io_ctx);
    }
    if (bw_applied) {
        uint32_t ignored;
        io->set_bw(req->cur_bw, &ignored, io_ctx);
    }
    if (req->prev && (cycle || req->force))
        req->prev->enter();

    out->bw = req->cur_bw;
    out->sr = req->cur_sr;
    return status;
}
