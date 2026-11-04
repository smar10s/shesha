/* tx_session.c — TX lifecycle transitions, with no hardware side effects. */
#include "tx_session.h"
#include "protocol.h"

static void clear_tx_fields(tx_session_t *s)
{
    s->state = TX_STATE_IDLE;
    s->proto = TX_PROTO_NONE;
    s->flags = 0;
    s->repeat = 0;
    s->interval_us = 0;
    s->owner_id = 0;
}

void tx_session_init(tx_session_t *s)
{
    clear_tx_fields(s);
    s->clock_held = false;
    s->saved_mode = -1;
    s->saved_rate = 0;
    s->saved_fpga = false;
}

bool tx_session_active(const tx_session_t *s)
{
    return s->state != TX_STATE_IDLE;
}

bool tx_session_clock_held(const tx_session_t *s)
{
    return s->clock_held;
}

bool tx_session_rate_needs_suspend(uint32_t tx_rate, uint32_t current_rate)
{
    /* Matching rates let TX and RX share the clock: the FM TX+RX path. */
    return tx_rate != current_rate;
}

void tx_session_start(tx_session_t *s, uint8_t proto, uint8_t flags,
                      uint16_t repeat, uint32_t interval_us,
                      uint32_t owner_id, bool open_ended)
{
    s->state = open_ended ? TX_STATE_STREAM_OPEN : TX_STATE_SHORT;
    s->proto = proto;
    s->flags = flags;
    s->repeat = repeat;
    s->interval_us = interval_us;
    s->owner_id = open_ended ? owner_id : 0;
}

void tx_session_stream_end(tx_session_t *s)
{
    if (s->state != TX_STATE_STREAM_OPEN)
        return;
    s->state = TX_STATE_STREAM_DRAINING;
    s->owner_id = 0;
}

void tx_session_complete(tx_session_t *s)
{
    clear_tx_fields(s);
}

void tx_session_hold_clock(tx_session_t *s, int mode, uint32_t rate,
                           bool fpga)
{
    if (s->clock_held)
        return;  /* already held: keep the original state to restore */
    s->clock_held = true;
    s->saved_mode = mode;
    s->saved_rate = rate;
    s->saved_fpga = fpga;
}

tx_clock_resume_t tx_session_release_clock(tx_session_t *s)
{
    tx_clock_resume_t r = {
        .mode = s->saved_mode,
        .rate = s->saved_rate,
        .fpga = s->saved_fpga,
    };
    s->clock_held = false;
    s->saved_mode = -1;
    s->saved_rate = 0;
    s->saved_fpga = false;
    return r;
}

void tx_session_cancel_clock(tx_session_t *s)
{
    s->clock_held = false;
    s->saved_mode = -1;
    s->saved_rate = 0;
    s->saved_fpga = false;
}
