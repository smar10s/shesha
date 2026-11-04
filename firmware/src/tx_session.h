/* tx_session.h — the one record of what the transmitter is doing.
 *
 * TX lifecycle used to be spread across a busy flag, a protocol byte, an
 * owner id, and a separate exclusivity struct.  That split produced two
 * bugs: a stream END left the owner set, so a disconnect truncated the
 * drain; and a stale owner from a previous stream could stop a new
 * one-shot TX.  One state machine makes both transitions explicit.
 *
 * The sample-clock claim is a field, not a state, because it is orthogonal
 * to the TX lifetime: a short FM burst and an open-ended FM stream can
 * both displace the RX producer.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    TX_STATE_IDLE = 0,
    /* Finite TX with no owner: one-shot or finite repeat. */
    TX_STATE_SHORT,
    /* Open-ended TX: stream, cyclic, or repeat-forever.  Owner is set. */
    TX_STATE_STREAM_OPEN,
    /* Client sent END; the queued data is draining.  Owner is cleared so a
     * disconnect cannot truncate the drain. */
    TX_STATE_STREAM_DRAINING,
} tx_state_t;

typedef struct {
    tx_state_t state;
    uint8_t  proto;        /* TX_PROTO_* or TX_PROTO_NONE */
    uint8_t  flags;
    uint16_t repeat;
    uint32_t interval_us;
    uint32_t owner_id;     /* client to stop on disconnect, 0 = none */

    /* RX producer displaced because the TX owns the sample clock. */
    bool     clock_held;
    int      saved_mode;
    uint32_t saved_rate;
    bool     saved_fpga;
} tx_session_t;

typedef struct {
    int      mode;   /* mode to restore, or -1 if nothing was suspended */
    uint32_t rate;
    bool     fpga;
} tx_clock_resume_t;

void tx_session_init(tx_session_t *s);

bool tx_session_active(const tx_session_t *s);
bool tx_session_clock_held(const tx_session_t *s);

/* Does a TX at `tx_rate` conflict with a producer at `current_rate`?
 * Only the shared sample clock matters; TX and RX run concurrently. */
bool tx_session_rate_needs_suspend(uint32_t tx_rate, uint32_t current_rate);

/* Begin a TX.  `open_ended` selects the state and decides whether the
 * owner is recorded; any previous owner is discarded either way. */
void tx_session_start(tx_session_t *s, uint8_t proto, uint8_t flags,
                      uint16_t repeat, uint32_t interval_us,
                      uint32_t owner_id, bool open_ended);

/* The client closed an open-ended stream.  The session keeps draining,
 * but stops being owned, so disconnect cleanup must leave it alone. */
void tx_session_stream_end(tx_session_t *s);

/* Reached idle: clear every TX field.  The caller must have released or
 * cancelled any clock claim first. */
void tx_session_complete(tx_session_t *s);

/* Record that the RX producer was displaced; idempotent. */
void tx_session_hold_clock(tx_session_t *s, int mode, uint32_t rate,
                           bool fpga);

/* Consume the saved state for the caller to restore.  Returns mode == -1
 * when no clock was held. */
tx_clock_resume_t tx_session_release_clock(tx_session_t *s);

/* Drop the saved state without restoring (a mode change replaces it). */
void tx_session_cancel_clock(tx_session_t *s);
