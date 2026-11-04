#include "wf_workers_policy.h"

/*
 * Measured fs_max = N / C(N) in MSPS, from docs/psd-waterfall.md
 * ("Per-frame compute").  Held in hundredths of an MSPS -- one unit is 10 kHz
 * -- so the comparison against an integer sample rate is exact rather than a
 * float rounding question at the boundary.
 */
static const struct {
    size_t   n;
    uint32_t fs_max_centi_msps;
} k_fs_max[] = {
    {  256, 730 },
    {  512, 727 },
    { 1024, 695 },
    { 2048, 672 },
    { 4096, 534 },
    { 8192, 485 },
};

#define FS_MAX_TABLE_LEN (sizeof(k_fs_max) / sizeof(k_fs_max[0]))

static uint32_t fs_max_centi(size_t n)
{
    size_t best = 0;
    uint64_t best_dist = UINT64_MAX;
    for (size_t i = 0; i < FS_MAX_TABLE_LEN; i++) {
        uint64_t dist = (n > k_fs_max[i].n)
            ? (uint64_t)(n - k_fs_max[i].n)
            : (uint64_t)(k_fs_max[i].n - n);
        if (dist < best_dist) {
            best_dist = dist;
            best = i;
        }
    }
    return k_fs_max[best].fs_max_centi_msps;
}

uint64_t wf_workers_fs_max_hz(size_t n)
{
    return (uint64_t)fs_max_centi(n) * 10000ULL;
}

int wf_workers_effective(int requested, uint64_t fs_hz, size_t n)
{
    if (requested < 1) requested = 1;
    if (requested > WF_MAX_WORKERS) requested = WF_MAX_WORKERS;
    if (requested == 1)
        return 1;
    return (fs_hz <= wf_workers_fs_max_hz(n)) ? 1 : requested;
}
