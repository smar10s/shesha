/* session.c — per-client subscription and notification logic.
 *
 * Pure computation — no sockets, no threads, no hardware.  Testable on the
 * host.  The daemon embeds session_client_t alongside its networking state
 * and calls these functions to decide what to send where.
 */
#include "session.h"

void session_client_init(session_client_t *c)
{
    c->data_interest = 0;
    c->notify_pending = false;
}

void session_client_subscribe(session_client_t *c, uint32_t mask)
{
    c->data_interest = mask & SESSION_DATA_ALL;
}

uint32_t session_data_bit(uint8_t frame_type)
{
    switch (frame_type) {
    case DATA_PSD:  return SESSION_DATA_PSD;
    case DATA_WIFI: return SESSION_DATA_WIFI;
    case DATA_FM:   return SESSION_DATA_FM;
    case DATA_IQ:   return SESSION_DATA_IQ;
    default:        return 0;
    }
}

bool session_client_wants(const session_client_t *c, uint8_t frame_type)
{
    return (c->data_interest & session_data_bit(frame_type)) != 0;
}

void session_client_clear_notify(session_client_t *c)
{
    c->notify_pending = false;
}
