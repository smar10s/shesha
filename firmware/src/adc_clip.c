#include "adc_clip.h"

#include <stdatomic.h>

static _Atomic uint64_t g_clips;
static _Atomic uint64_t g_samples;

void adc_clip_add(uint64_t clips, uint64_t samples)
{
    atomic_fetch_add_explicit(&g_clips, clips, memory_order_relaxed);
    atomic_fetch_add_explicit(&g_samples, samples, memory_order_relaxed);
}

void adc_clip_get(uint64_t *clips, uint64_t *samples)
{
    if (clips)
        *clips = atomic_load_explicit(&g_clips, memory_order_relaxed);
    if (samples)
        *samples = atomic_load_explicit(&g_samples, memory_order_relaxed);
}

