/* firmware/src/iq_ring.h — lock-free byte ring for IQ TX streaming.
 *
 * Same cursor discipline as fm_tx.c's audio ring: absolute head/tail
 * counts (never wrapped, so full/empty can't be confused), head written
 * only by the producer, tail only by the consumer, acquire/release on
 * those two is the whole synchronization protocol.
 */
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdatomic.h>

typedef struct {
    uint8_t *buf;
    uint32_t size;             /* power of two */
    uint32_t mask;

    _Atomic uint64_t head;
    _Atomic uint64_t tail;

    _Atomic bool end_of_stream;
    _Atomic uint32_t push_rejects;
} iq_byte_ring_t;

int iq_byte_ring_init(iq_byte_ring_t *r, uint32_t size_bytes);
void iq_byte_ring_destroy(iq_byte_ring_t *r);

/* 0 = ok, 1 = ring full (backpressure, not failure), -1 = invalid. */
int iq_byte_ring_push(iq_byte_ring_t *r, const uint8_t *data, uint32_t n);

/* Linearize up to `want` bytes at the tail into `out`.  Returns the
 * number of bytes copied.  Does not advance the tail. */
uint32_t iq_byte_ring_peek(iq_byte_ring_t *r, uint8_t *out, uint32_t want);

void iq_byte_ring_consume(iq_byte_ring_t *r, uint32_t n);

static inline uint32_t iq_byte_ring_avail(const iq_byte_ring_t *r)
{
    return (uint32_t)(atomic_load(&((iq_byte_ring_t *)r)->head)
                      - atomic_load(&((iq_byte_ring_t *)r)->tail));
}
