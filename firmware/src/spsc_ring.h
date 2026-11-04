/* firmware/src/spsc_ring.h — type-erased single-producer single-consumer ring
 *
 * Lock-free SPSC queue parameterized on slot size.  Monotonic write index
 * doubles as a sequence number: consumers can detect lap tears by comparing
 * the returned seq against their last-seen value.
 *
 * Overwrite policy: producer always succeeds.  If the consumer falls behind
 * by more than `capacity` frames, the oldest are silently lost (but the
 * consumer sees the gap via the sequence number).
 */
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdatomic.h>

typedef struct {
    void          *slots;       /* capacity * slot_size bytes */
    size_t         slot_size;
    size_t         capacity;
    _Atomic size_t write_seq;   /* monotonic, never wraps in practice */
    size_t         read_seq;    /* consumer-private */
} spsc_ring_t;

/* Create a ring with `capacity` slots of `slot_size` bytes each.
 * Returns NULL on allocation failure. */
spsc_ring_t *spsc_ring_create(size_t slot_size, size_t capacity);

void spsc_ring_destroy(spsc_ring_t *r);

/* --- Producer (single thread) --- */

/* Push one slot.  Always succeeds (overwrites oldest if full).
 * Returns the sequence number assigned to this slot. */
size_t spsc_ring_push(spsc_ring_t *r, const void *item);

/* --- Consumer (single thread) --- */

/* Pop all unread slots into `out` (must hold at least `max_items * slot_size`
 * bytes).  Returns number of items copied.  `start_seq_out` receives the
 * sequence number of the first item returned (for gap detection).
 * If the consumer was lapped, items are lost and *start_seq_out > last read. */
int spsc_ring_pop_all(spsc_ring_t *r, void *out, int max_items,
                      size_t *start_seq_out);

/* Return number of unread items (approximate — racy if producer is active). */
static inline size_t spsc_ring_available(const spsc_ring_t *r)
{
    size_t w = atomic_load(&((spsc_ring_t *)r)->write_seq);
    return w - r->read_seq;
}
