#include "iq_ring.h"
#include <stdlib.h>
#include <string.h>

int iq_byte_ring_init(iq_byte_ring_t *r, uint32_t size_bytes)
{
    if (size_bytes == 0 || (size_bytes & (size_bytes - 1)) != 0)
        return -1;  /* must be a power of two */
    r->buf = malloc(size_bytes);
    if (!r->buf)
        return -1;
    r->size = size_bytes;
    r->mask = size_bytes - 1;
    atomic_init(&r->head, 0);
    atomic_init(&r->tail, 0);
    atomic_init(&r->end_of_stream, false);
    atomic_init(&r->push_rejects, 0);
    return 0;
}

void iq_byte_ring_destroy(iq_byte_ring_t *r)
{
    free(r->buf);
    r->buf = NULL;
}

int iq_byte_ring_push(iq_byte_ring_t *r, const uint8_t *data, uint32_t n)
{
    if (!r->buf || !data)
        return -1;
    if (n == 0)
        return 0;
    if (n > r->size)
        return -1;

    uint64_t head = atomic_load_explicit(&r->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&r->tail, memory_order_acquire);
    uint32_t space = r->size - (uint32_t)(head - tail);

    if (n > space) {
        atomic_fetch_add_explicit(&r->push_rejects, 1, memory_order_relaxed);
        return 1;
    }

    uint32_t start = (uint32_t)(head & r->mask);
    uint32_t first = r->size - start;
    if (first > n)
        first = n;
    memcpy(&r->buf[start], data, first);
    if (n > first)
        memcpy(&r->buf[0], data + first, n - first);

    /* Publish the data before the cursor that advertises it. */
    atomic_store_explicit(&r->head, head + n, memory_order_release);
    return 0;
}

uint32_t iq_byte_ring_peek(iq_byte_ring_t *r, uint8_t *out, uint32_t want)
{
    if (!out || want == 0)
        return 0;

    uint64_t tail = atomic_load_explicit(&r->tail, memory_order_relaxed);
    uint64_t head = atomic_load_explicit(&r->head, memory_order_acquire);
    uint32_t avail = (uint32_t)(head - tail);
    if (avail == 0)
        return 0;
    if (want > avail)
        want = avail;

    uint32_t start = (uint32_t)(tail & r->mask);
    uint32_t first = r->size - start;
    if (first > want)
        first = want;
    memcpy(out, &r->buf[start], first);
    if (want > first)
        memcpy(out + first, &r->buf[0], want - first);
    return want;
}

void iq_byte_ring_consume(iq_byte_ring_t *r, uint32_t n)
{
    if (n == 0)
        return;
    uint64_t tail = atomic_load_explicit(&r->tail, memory_order_relaxed);
    atomic_store_explicit(&r->tail, tail + n, memory_order_release);
}
