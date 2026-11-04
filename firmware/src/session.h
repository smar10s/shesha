/* session.h — per-client subscription and notification state.
 *
 * Pure logic, no I/O.  The daemon is a single radio session; clients are
 * observers that subscribe to data streams and get notified when the
 * session changes.  Mode is session-scoped (one LO, one clock), not
 * per-client.
 *
 * Policy: last writer wins.  A mode change always succeeds.  Observers get
 * an unsolicited RESP_STATUS so they can react (reconnect, update display,
 * etc.) rather than silently receiving the wrong data.
 */
#ifndef SESSION_H
#define SESSION_H

#include <stdbool.h>
#include <stdint.h>
#include "protocol.h"

/* Subscription bits — one per DATA_* frame type.  A client sets these via
 * CMD_SUBSCRIBE to declare which streams it wants.  Default is 0 (nothing),
 * so a fresh connection doesn't get flooded. */
#define SESSION_DATA_PSD   (1u << 0)
#define SESSION_DATA_WIFI  (1u << 1)
#define SESSION_DATA_FM    (1u << 2)
#define SESSION_DATA_IQ    (1u << 3)
#define SESSION_DATA_ALL   (SESSION_DATA_PSD | SESSION_DATA_WIFI | SESSION_DATA_FM | SESSION_DATA_IQ)

/* Per-client session state.  Lives alongside the networking fields in
 * client_t but is pure data — no fd, no buffer, no I/O. */
typedef struct {
    uint32_t data_interest;    /* bitmask of SESSION_DATA_* */
    bool     notify_pending;   /* unsolicited status push queued */
} session_client_t;

/* Initialize a client's session state (zero subscription, no pending). */
void session_client_init(session_client_t *c);

/* Set subscription (full replacement, not additive). */
void session_client_subscribe(session_client_t *c, uint32_t mask);

/* Does this client want a frame of the given wire type?
 * `frame_type` is the protocol DATA_* constant (0x20, 0x21, 0x22, 0x23). */
bool session_client_wants(const session_client_t *c, uint8_t frame_type);

/* Map a wire DATA_* type byte to a SESSION_DATA_* bit.  Returns 0 for
 * unknown types (which means "never deliver"). */
uint32_t session_data_bit(uint8_t frame_type);

/* Clear the notification flag after delivery. */
void session_client_clear_notify(session_client_t *c);

#endif /* SESSION_H */
