/* firmware/src/sendq.h — per-client control/data outbound queues
 *
 * Two independent byte rings.  Every frame carries an internal four-byte
 * little-endian length prefix; peek returns payload bytes only.
 *
 * The ring chooser only switches at frame boundaries.  A frame already
 * being sent keeps its ring until fully consumed, so control bytes can
 * never appear inside a partial data frame.  At a boundary control wins,
 * so a backlog of data can never delay a command response or an
 * unsolicited status frame.  Data frames are dropped when the data ring
 * is full; the daemon treats that as the slow-client path.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct sendq sendq_t;

/* Create a queue with power-of-two byte capacities.  Returns NULL on
 * invalid capacity or allocation failure. */
sendq_t *sendq_create(size_t control_bytes, size_t data_bytes);

void sendq_destroy(sendq_t *q);

/* Push a whole frame.  The four-byte length prefix is reserved inside the
 * destination ring.  Returns false when that ring lacks room; a control
 * push never falls back to the data ring.  A NULL frame is accepted only
 * when len is zero. */
bool sendq_push_control(sendq_t *q, const uint8_t *frame, size_t len);
bool sendq_push_data(sendq_t *q, const uint8_t *frame, size_t len);

/* True while unread payload bytes remain in either ring. */
bool sendq_pending(const sendq_t *q);

/* Return the next contiguous payload run, capped at the ring wrap and at
 * the current frame's remaining bytes.  A mid-frame ring keeps priority;
 * at a frame boundary the control ring wins.  *is_control names the ring
 * the run came from.  On an empty queue *ptr is set to NULL and
 * *is_control to false.  Returns 0 when no payload remains. */
size_t sendq_peek(sendq_t *q, bool *is_control, const uint8_t **ptr);

/* Drop n payload bytes from the ring named by is_control.  Clamps to the
 * payload bytes actually available.  Reaching a frame end lets the next
 * peek switch rings. */
void sendq_consume(sendq_t *q, bool is_control, size_t n);
