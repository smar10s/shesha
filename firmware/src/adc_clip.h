#pragma once
#include <stdint.h>

/*
 * ADC rail counters.
 *
 * The AD9361 delivers signed 12-bit I/Q.  A component sitting on +2047 or
 * -2048 has run out of headroom: the next sample cannot be represented
 * without clipping.  Producers count those rails before any 8-bit
 * truncation, so the number means the same thing in every mode.
 *
 * Counters are lifetime totals.  Consumers take deltas between status
 * samples; a reset (daemon restart) shows up as a negative delta and is
 * treated as zero by the client.
 */

/* True when an unsigned 12-bit field (0..4095) sits on a rail. */
static inline uint32_t adc_clip_is_raw12(uint32_t v)
{
    return (uint32_t)((v & 0xFFFu) - 0x7FFu) < 2u;
}

/* True when a 12-bit-scaled float sits on a rail. */
static inline uint32_t adc_clip_is_float(float v)
{
    return (uint32_t)(v >= 2047.0f / 2048.0f)
         | (uint32_t)(v <= -1.0f);
}

/* Add one buffer's worth of counts to the shared totals. */
void adc_clip_add(uint64_t clips, uint64_t samples);

/* Read the shared totals. */
void adc_clip_get(uint64_t *clips, uint64_t *samples);
