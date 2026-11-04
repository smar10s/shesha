/* firmware/src/spsc_ring.c — type-erased SPSC ring implementation */

#include "spsc_ring.h"
#include <stdlib.h>
#include <string.h>

spsc_ring_t *spsc_ring_create(size_t slot_size, size_t capacity)
{
    spsc_ring_t *r = calloc(1, sizeof(*r));
    if (!r) return NULL;
    r->slot_size = slot_size;
    r->capacity = capacity;
    r->slots = calloc(capacity, slot_size);
    if (!r->slots) { free(r); return NULL; }
    atomic_store(&r->write_seq, 0);
    r->read_seq = 0;
    return r;
}

void spsc_ring_destroy(spsc_ring_t *r)
{
    if (!r) return;
    free(r->slots);
    free(r);
}

size_t spsc_ring_push(spsc_ring_t *r, const void *item)
{
    size_t seq = atomic_load(&r->write_seq);
    size_t slot = seq % r->capacity;
    memcpy((char *)r->slots + slot * r->slot_size, item, r->slot_size);
    atomic_store(&r->write_seq, seq + 1);
    return seq;
}

int spsc_ring_pop_all(spsc_ring_t *r, void *out, int max_items,
                      size_t *start_seq_out)
{
    size_t wseq = atomic_load(&r->write_seq);
    if (wseq == r->read_seq)
        return 0;  /* empty */

    size_t start = r->read_seq;

    /* If lapped, clamp to oldest available */
    if (wseq - start > r->capacity)
        start = wseq - r->capacity;

    size_t count = wseq - start;
    if (count > (size_t)max_items) {
        /* Can't fit all — take the most recent max_items */
        start = wseq - (size_t)max_items;
        count = (size_t)max_items;
    }

    if (start_seq_out)
        *start_seq_out = start;

    for (size_t i = 0; i < count; i++) {
        size_t slot = (start + i) % r->capacity;
        memcpy((char *)out + i * r->slot_size,
               (char *)r->slots + slot * r->slot_size,
               r->slot_size);
    }

    r->read_seq = wseq;
    return (int)count;
}
