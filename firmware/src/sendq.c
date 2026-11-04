/* firmware/src/sendq.c — per-client control/data outbound queues */

#include "sendq.h"

#include <stdlib.h>
#include <string.h>

typedef struct {
    uint8_t *buf;
    size_t   capacity;  /* power of two */
    size_t   mask;
    size_t   head;      /* monotonic, producer */
    size_t   tail;      /* monotonic, consumer */
    size_t   frame_remaining;
} byte_ring_t;

struct sendq {
    byte_ring_t control;
    byte_ring_t data;
};

static bool is_power_of_two(size_t n)
{
    return n != 0 && (n & (n - 1)) == 0;
}

static bool ring_init(byte_ring_t *r, size_t capacity)
{
    if (!is_power_of_two(capacity))
        return false;
    r->buf = calloc(capacity, 1);
    if (!r->buf)
        return false;
    r->capacity = capacity;
    r->mask = capacity - 1;
    r->head = 0;
    r->tail = 0;
    r->frame_remaining = 0;
    return true;
}

sendq_t *sendq_create(size_t control_bytes, size_t data_bytes)
{
    sendq_t *q = calloc(1, sizeof(*q));
    if (!q)
        return NULL;

    if (!ring_init(&q->control, control_bytes) ||
        !ring_init(&q->data, data_bytes)) {
        sendq_destroy(q);
        return NULL;
    }
    return q;
}

void sendq_destroy(sendq_t *q)
{
    if (!q)
        return;
    free(q->control.buf);
    free(q->data.buf);
    free(q);
}

static size_t ring_used(const byte_ring_t *r)
{
    return r->head - r->tail;
}

static void ring_write(byte_ring_t *r, const uint8_t *src, size_t len)
{
    if (len == 0)
        return;

    size_t off = r->head & r->mask;
    size_t first = r->capacity - off;
    if (first > len)
        first = len;

    memcpy(r->buf + off, src, first);
    memcpy(r->buf, src + first, len - first);
    r->head += len;
}

static bool ring_push_frame(byte_ring_t *r, const uint8_t *src, size_t len)
{
    if (len > UINT32_MAX)
        return false;

    size_t used = ring_used(r);
    if (used > r->capacity || r->capacity - used < 4)
        return false;
    if (len > r->capacity - used - 4)
        return false;

    uint8_t prefix[4] = {
        (uint8_t)(len & 0xffu),
        (uint8_t)((len >> 8) & 0xffu),
        (uint8_t)((len >> 16) & 0xffu),
        (uint8_t)((len >> 24) & 0xffu),
    };

    ring_write(r, prefix, sizeof(prefix));
    ring_write(r, src, len);
    return true;
}

static uint32_t ring_read_u32(const byte_ring_t *r)
{
    size_t off = r->tail & r->mask;
    uint32_t v = 0;

    for (size_t i = 0; i < 4; i++)
        v |= (uint32_t)r->buf[(off + i) & r->mask] << (8 * i);
    return v;
}

static bool ring_begin_frame(byte_ring_t *r)
{
    for (;;) {
        if (ring_used(r) < 4)
            return false;

        uint32_t len = ring_read_u32(r);
        r->tail += 4;
        if (len == 0)
            continue;

        r->frame_remaining = len;
        return true;
    }
}

static size_t ring_peek_payload(const byte_ring_t *r, const uint8_t **ptr)
{
    if (r->frame_remaining == 0)
        return 0;

    size_t off = r->tail & r->mask;
    size_t run = r->capacity - off;
    if (run > r->frame_remaining)
        run = r->frame_remaining;

    *ptr = r->buf + off;
    return run;
}

bool sendq_push_control(sendq_t *q, const uint8_t *frame, size_t len)
{
    if (!q || (!frame && len > 0))
        return false;
    return ring_push_frame(&q->control, frame, len);
}

bool sendq_push_data(sendq_t *q, const uint8_t *frame, size_t len)
{
    if (!q || (!frame && len > 0))
        return false;
    return ring_push_frame(&q->data, frame, len);
}

bool sendq_pending(const sendq_t *q)
{
    if (!q)
        return false;
    return ring_used(&q->control) != 0 || ring_used(&q->data) != 0;
}

size_t sendq_peek(sendq_t *q, bool *is_control, const uint8_t **ptr)
{
    if (is_control)
        *is_control = false;
    if (ptr)
        *ptr = NULL;
    if (!q || !ptr)
        return 0;

    if (q->control.frame_remaining > 0) {
        if (is_control)
            *is_control = true;
        return ring_peek_payload(&q->control, ptr);
    }

    if (q->data.frame_remaining > 0)
        return ring_peek_payload(&q->data, ptr);

    if (ring_begin_frame(&q->control)) {
        if (is_control)
            *is_control = true;
        return ring_peek_payload(&q->control, ptr);
    }

    if (ring_begin_frame(&q->data))
        return ring_peek_payload(&q->data, ptr);

    return 0;
}

static void ring_consume(byte_ring_t *r, size_t n)
{
    while (n > 0) {
        if (r->frame_remaining == 0 && !ring_begin_frame(r))
            break;

        size_t take = r->frame_remaining;
        if (take > n)
            take = n;

        r->tail += take;
        r->frame_remaining -= take;
        n -= take;
    }
}

void sendq_consume(sendq_t *q, bool is_control, size_t n)
{
    if (!q)
        return;
    ring_consume(is_control ? &q->control : &q->data, n);
}
